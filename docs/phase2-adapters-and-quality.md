# Fase 2: adaptadores y calidad de datos

## Alcance actual

No se ha elegido proveedor, bolsa ni protocolo. El repositorio implementa un
contrato intercambiable y un adaptador JSONL determinista para fixtures; no
afirma compatibilidad con una API real. Un adaptador de producción y su
normalizador solo deben añadirse al seleccionar una fuente y verificar su
documentación oficial.

La fase no incluye órdenes reales, matching, fills, grabación histórica ni
replay persistente. El fixture demuestra el flujo de ingestión dentro de un
proceso.

## Contrato de adaptador

`FeedAdapter` en `src/market_catalog/adapters/base.py` define estas operaciones:

- `connect()`: abre la fuente y falla explícitamente si no puede conectarse.
- `subscribe(subscriptions)`: recibe instrumentos internos mapeados a símbolo
  de proveedor y canales.
- `receive(timeout)`: entrega un `RawFrame` o `None` al expirar el timeout.
- `health()`: devuelve estado local en caché; no debe esperar a una llamada de
  red.
- `close()`: libera recursos y puede llamarse más de una vez.

Cada `RawFrame` conserva los bytes exactos del frame, el identificador de
proveedor y canal, la hora UTC local de recepción, un reloj monotónico local y
un ordinal de llegada. El adaptador asigna las dos marcas locales al recibir
los datos; no debe reutilizar el timestamp de origen como timestamp de
recepción.

Si el protocolo soporta resincronizar un libro, puede implementar además la
capacidad opcional `SnapshotRecoveryAdapter.request_snapshot(instrument_id,
channel)`. `True` significa que la solicitud se puso en cola, no que el snapshot
ya haya llegado. El pipeline hace un intento por proveedor, instrumento y canal
mientras el libro siga bloqueado; no repite solicitudes rechazadas en cada
delta. Lo vuelve a habilitar al recibir un snapshot válido o al marcar una
reconexión. Si el adaptador no implementa esa capacidad, devuelve un diagnóstico
y mantiene el libro bloqueado hasta que llegue un snapshot.

## Evento canónico

`schemas/market_event.schema.json` define el sobre común en JSON Schema Draft
2020-12. `MarketEvent.to_mapping()` conserva precios y cantidades como cadenas
decimales. Incluye:

- `provider_id`, `channel`, `instrument_id` y `event_type`;
- timestamp de origen crudo, timestamp de origen en nanosegundos y su semántica;
- hora UTC y reloj monotónico de recepción, ordinal de llegada y secuencia;
- payload tipado para cotización L1, operación, snapshot/delta de libro o estado
  de negociación;
- flags de calidad, estado del libro, `decision_usable` y opcionalmente
  `raw_frame_b64` para retener el frame original.

Los valores admitidos para `timestamp_semantics` distinguen hora del evento,
hora de transacción, hora de publicación y significado desconocido. El
normalizador traduce timestamps de proveedor al instante Unix en nanosegundos y
mantiene el valor original por diagnóstico. No compara antigüedad entre relojes
si `source_clock_comparable` no está activado.

El alcance de secuencia forma parte del evento: `instrument_channel`, `channel`,
`provider` o `unknown`. Evita asumir que cada proveedor incrementa secuencias
por símbolo. Un adaptador debe declarar el alcance documentado; si trae una
secuencia pero no puede determinar su alcance, el monitor agrega
`sequence_scope_unverified` y marca el evento no utilizable. Un cambio de
alcance a mitad del flujo requiere reconfigurar o reiniciar el monitor.

## Calidad, decisiones y recuperación

`DataQualityMonitor` trabaja sobre cada flujo. Por defecto, los límites de edad
del timestamp de origen y silencio entre recepciones quedan sin configurar;
son umbrales propios del proveedor y del entorno, no valores universales. Se
pueden fijar mediante `QualityPolicy` cuando se conozcan la fuente, las
semánticas de reloj y el despliegue.

El monitor hace visibles secuencias duplicadas, repetición de payload cuando no
hay secuencia, huecos y retrocesos, timestamps de origen fuera de orden, cambio
de semántica, reloj de origen obsoleto, skew, recepción monotónica fuera de
orden, silencio excesivo y actualizaciones incompletas. Una actualización
incompleta no produce un evento canónico: devuelve un diagnóstico y degrada el
estado de calidad correspondiente.

Reglas de recuperación:

- Una cotización L1 completa contiene bid y ask; puede reconstruir el top of
  book aunque se haya perdido una secuencia previa. El hueco permanece visible
  como flag.
- Una secuencia inválida o un hueco en un delta L2 invalida el libro. Los deltas
  siguientes no se consideran utilizables hasta recibir un snapshot completo.
- Un snapshot válido reconstruye el libro y limpia una solicitud de snapshot
  pendiente. No se permite inferir el estado del libro aplicando deltas tras el
  hueco.
- Tras reconectar se reinicia el estado de secuencia y los libros conocidos
  quedan en `awaiting_snapshot`. Una cotización L1 completa restaura L1; el libro
  L2 requiere su snapshot.
- Un hueco en trades o estado de negociación marca el evento con el hueco y no
  permite que ese evento conduzca una decisión. La fuente tendría que ofrecer
  backfill para reconstruir historia perdida; esta fase no inventa una
  recuperación universal de trades.

`ProcessingResult` devuelve el evento, si pudo normalizarse, y diagnósticos.
Los consumidores deben comprobar `event.decision_usable` antes de pasar el
evento a estrategia o simulador y consultar `quality_health(now_monotonic_ns)`
para bloquear por estado de flujo. El pipeline nunca convierte un evento
defectuoso en una señal utilizable silenciosamente.

## L1 y L2

L1 representa la mejor cotización bid/ask y los tamaños disponibles en esos
precios si la fuente los suministra. Permite modelos de fills aproximados en el
top of book, pero no revela profundidad más allá de esos niveles.

L2 puede ser profundidad agregada por precio (`price_aggregated`) o datos
orden-por-orden (`order_by_order`). La segunda forma exige `order_id`; la primera
rechaza identificadores de órdenes. Incluso order-by-order no demuestra por sí
solo prioridad exacta en cola ni garantiza que una orden hipotética se hubiera
ejecutado. No se deben presentar fills simulados como ejecuciones reales.

## Fixture y pruebas

`tests/fixtures/market_data.jsonl` usa un sobre de prueba que conserva el frame
JSON como texto y lo acompaña con metadatos locales. Sus símbolos, precios,
cantidades y timestamps son sintéticos, no especificaciones de un mercado.
Incluye cotización, trade, snapshot agregado, gap de secuencia, delta posterior,
snapshot de recuperación y actualización L1.

Ejecutar desde la raíz del repositorio:

```console
python -m scripts.check --suite all
```

La suite comprueba el ciclo del adaptador, filtros de suscripción, preservación
opcional del frame, normalización, secuencias de varios alcances, calidad,
resincronización y bloqueo de decisiones. No mide latencia de producción ni
valida una API de proveedor.
