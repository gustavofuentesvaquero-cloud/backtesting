# Fase 3: motor de eventos instrumentado

## Recorrido y orden

`EventProcessor` consume frames ya recibidos por los adaptadores. Todos los
productores concurrentes llaman a `submit(frame)`; el motor les asigna un
`arrival_ordinal` de sesión dentro de un secuenciador común antes de encolar el
frame. El modo de replay `preserve_arrival_ordinals=True` conserva los ordinales
capturados y rechaza los que no sean estrictamente crecientes. Para fusionar
varios archivos de replay, el lector debe presentarlos al motor ya ordenados por
ese ordinal.

```text
submit / recepción
  → normalización
  → validación y secuencia
  → estado L1
  → callback Python de estrategia
  → validación de intents
  → matching L1 simulado
  → saldos y posiciones
```

Cada flecha usa una FIFO acotada y cada etapa tiene un consumidor por sesión.
Esto conserva el orden incluso si dos frames comparten timestamp. Las etapas
pueden solaparse entre sí, pero no procesan en paralelo los eventos de una misma
etapa/sesión. Los adaptadores siguen siendo propiedad de la aplicación: esta
recibe frames y los envía a `submit`; el motor no crea conexiones externas.

## Interfaces

- `EventProcessor(pipeline, catalog, strategy, config)` prepara una sesión.
- `start()` inicia los consumidores; `submit(RawFrame)` acepta un frame y
  devuelve su ordinal o `None` si una cola se saturó; `finish()` cierra el
  ingreso, drena el trabajo aceptado y devuelve `SessionReport`.
- `EventProcessorConfig` permite fijar una capacidad común, sobrescribirla por
  cola, conservar ordinales de replay y definir límites opcionales de espera y
  duración. No hay objetivos de latencia por defecto.
- La interfaz de compatibilidad `Strategy.on_event(event, book)` es un callback
  síncrono de Python. La API administrada de Fase 4 se describe en
  `phase4-strategy-api.md`; toma vistas inmutables y estado explícito.
- `SessionReport` incluye estado (`completed`, `invalid` o `aborted`), conteos,
  eventos pendientes/en vuelo, diagnósticos, fills, órdenes, cancelaciones,
  cartera, métricas, políticas de matching/cartera serializadas y ledger de
  contabilidad. La persistencia y reproducción del ledger se describe en
  `phase6-portfolio-accounting.md`.

El pipeline divide la normalización y la evaluación de secuencia/calidad en
métodos públicos, manteniendo `MarketDataPipeline.ingest()` como API síncrona de
Fase 2. Las solicitudes de snapshot continúan disponibles y se enrutan por
proveedor si se proporciona `adapters_by_provider`.

## Saturación, calidad y errores

Si cualquier cola se llena, el motor registra `queue_overflow` fuera de la cola
saturada, rechaza el frame o la transferencia correspondiente, detiene la
recepción y aborta la sesión. El reporte conserva los conteos de cada cola, los
elementos pendientes, los que estaban en vuelo y los retirados de una cola justo
cuando se produjo el aborto. Una excepción de etapa también aborta. Una sesión con
rechazos de normalización, errores de calidad, intents inválidos o presupuestos
excedidos queda `invalid`; sigue procesando los frames aceptados para completar
el diagnóstico, pero no se presenta como resultado válido. Los fallos de etapa
y saturación impiden que se procesen más decisiones.

Los avisos repetidos se agregan por código/etapa y guardan hasta ocho ordinales
de ejemplo. Esto mantiene acotada la memoria de diagnóstico. `timestamp_unverified`
y `sequence_unavailable` informan ausencia de garantías, pero por sí solos no
invalidan el resultado mientras la política de calidad no los requiera.

## Métricas

Se usa el reloj monotónico de alta resolución `perf_counter_ns` para el ingreso
del motor, cada etapa, el callback, la espera en cada cola y el tiempo total
hasta cartera. Los histogramas log2
mantienen uso de memoria fijo y reportan conteo, suma, máximo, media, desviación
típica y percentiles aproximados (límite superior del bucket). Cada cola expone
capacidad, aceptados, rechazados, pendientes y ocupación máxima.

La medición de extremo a extremo empieza en `submit`, no incluye el tiempo que un
adaptador haya pasado esperando datos dentro de `receive()`. La generación de
diagnósticos y las métricas añaden coste al pipeline Python; cualquier benchmark
debe medir también el coste de instrumentación. No se hace logging ni
serialización síncrona por evento, y esta implementación no afirma latencia HFT
de producción.

## Matching y cartera de referencia

La política L1 de la Fase 3 fue extendida en la Fase 5 con latencia virtual,
comisiones, deslizamiento parametrizado, cancelación interna y campos de
auditoría. La descripción vigente de estados, fórmula de costes, precio,
supuestos de cola y límites está en
[phase5-order-simulation.md](phase5-order-simulation.md). El matcher sigue
rechazando series continuas; no modela profundidad L2, prioridad de cola ni
liquidez oculta. Los fills son resultados del modelo, no promesas de ejecución
real.

La contabilidad vigente usa el ledger append-only de la Fase 6. Para FX registra
las dos patas de efectivo y la exposición base del instrumento; no se deben
sumar ambas representaciones para calcular un NAV. El manejo de P&L por moneda,
conversiones fechadas, dividendos, financiación, liquidación de futuros,
exposición y margen se describe en
[phase6-portfolio-accounting.md](phase6-portfolio-accounting.md).

## Verificación y benchmark

Ejecuta la suite desde la raíz:

```console
python -m unittest discover -s tests -v
```

El benchmark sintético, sin objetivos numéricos ni requisitos de proveedor, se
ejecuta así:

```console
python benchmarks/bench_event_engine.py --events 10000
```

Informa el tiempo total de envío, throughput observado y métricas por etapa,
incluyendo percentiles y variabilidad. Los resultados dependen del intérprete,
el sistema, la carga y la configuración de colas. El script entrega el lote
completo con capacidad suficiente para aceptarlo; por eso sus esperas de cola
incluyen la ráfaga de replay y no equivalen a latencia estable de un feed en
directo. Rust queda fuera de esta fase;
solo se considerará una etapa si su coste o variabilidad domina en mediciones
repetibles y el contrato de esa etapa se mantiene estable.
