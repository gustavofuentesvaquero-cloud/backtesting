# Fase 8: pruebas, rendimiento y criterios de calidad

## Matriz de pruebas

| Área | Cobertura actual | Prueba o comando |
|---|---|---|
| Catálogo, esquema y unidades | Catálogo vacío y sintético; ticks, incrementos, monedas, sesiones, calendarios, zonas horarias, vencimientos y referencias inválidas | `tests/test_catalog.py` |
| Contrato del adaptador disponible | Esquema de eventos, normalización JSONL, payload incompleto, secuencias ausentes, duplicadas, tardías o con gaps, timestamps y recuperación L1/L2 | `tests/test_adapters.py` |
| Orden y calidad del motor | Orden común entre fuentes, timestamps iguales, ordinales preservados, bloqueo de decisiones inválidas, libro cruzado, saturación, excepciones y presupuestos | `tests/test_event_engine.py` |
| Órdenes y fills controlados | Compra market al ask, spread amplio, liquidez L1 parcial, límite revaluado con quotes posteriores y límite tocado en el bid sin ejecución garantizada; el remanente se cancela al cerrar sesión | `tests/test_event_engine.py` |
| Ledger y replay | Replay desde frames grabados con ordinales capturados; igualdad canónica de decisiones, fills, ledger, posiciones y efectivo, además de SHA-256 del resultado | `tests/test_recording.py` |
| Persistencia y fallos | Checksum de segmento alterado, fallo de escritura inyectado y rechazo de replay incompleto; apertura de captura cerrada y reproducción con un procesador nuevo | `tests/test_recording.py` |
| Benchmark | Camino sintético L1 con estrategia sin intents; histogramas, colas, CPU, memoria, asignaciones, GC y espera del grabador | `benchmarks/bench_event_engine.py` |

El adaptador real de proveedor no está elegido. Por eso sus contratos de protocolo, autenticación, límites, reconexión y semántica de secuencias siguen pendientes; las pruebas actuales no afirman compatibilidad con un proveedor concreto. Las pruebas de reconexión y resnapshot usan el monitor y el adaptador de fixtures. El cierre limpio y replay desde un procesador nuevo cubren la restauración de sesión; no se prueba la recuperación de una escritura interrumpida por un corte del proceso o del sistema operativo.

## Ejecución

Prepara el entorno fijado siguiendo el README, luego ejecuta:

```console
python -m scripts.check --suite all --report artifacts/preparation/checks.json
python benchmarks/bench_event_engine.py --events 10000
```

El benchmark imprime JSON. `--no-recorder` permite una pasada sin grabación; `--queue-capacity N` crea un burst que puede saturar las colas. Una ejecución que descarte eventos termina con estado distinto de cero y conserva los contadores en su JSON. El benchmark realiza una pasada sin `tracemalloc` para latencias y throughput y otra instrumentada para memoria y asignaciones, para que `tracemalloc` no distorsione los tiempos reportados.

## Informe sintético de referencia

Las pruebas unitarias y las regresiones con históricos congelados se ejecutan por separado. Consulta `docs/preparacion.md` para la referencia local y los límites pendientes de verificar en el motor diario.

Medición de una pasada de 10.000 quotes L1 sintéticas, estrategia no-op, grabador activado, capacidad de cada cola de motor 10.000 y capacidad del grabador 160.000. Runtime Python 3.12.14 sobre Windows 10, build 19045. No hubo warmup ni feed externo; el productor envió un burst. Es una observación de este entorno, no una prueba de carga representativa ni una SLA.

| Métrica | Resultado observado |
|---|---:|
| Eventos procesados / descartados | 10.000 / 0 |
| Tiempo de pared / CPU del proceso | 14,469 s / 13,172 s |
| Throughput en esta pasada | 691 eventos/s |
| Recepción a cartera p50 | 7.198,078 ms |
| Recepción a cartera p90 / p99 / p99.9 / máximo | 7.198,078 ms cada uno |
| Cola de normalización: capacidad / máximo ocupado | 10.000 / 7.178 |
| RSS actual / máximo working set | 140.251.136 / 159.318.016 bytes |
| Heap Python trazado: pico | 45.580.980 bytes |
| Asignaciones netas positivas por ubicación | 77.920 |
| Recolecciones GC en pasada de throughput | 290 gen0, 27 gen1, 2 gen2 |
| Grabador: registros escritos / descartados | 70.000 / 0 |
| Cola del grabador: capacidad / máximo ocupado | 160.000 / 61.431 |
| Espera máxima de cola / servicio máximo de escritura | 8.500 ms / 47 ms |

Distribuciones de etapa de esta pasada (p90/p99/p99.9 son límites superiores aproximados; máximo es exacto, en microsegundos):

| Etapa | p50 | p90 | p99 | p99.9 | máximo |
|---|---:|---:|---:|---:|---:|
| Normalización | 1.048,6 | 1.048,6 | 2.097,2 | 4.194,3 | 127.471,2 |
| Validación/secuencia | 1.048,6 | 2.097,2 | 4.194,3 | 4.194,3 | 35.340,5 |
| Libro | 1.048,6 | 2.097,2 | 4.194,3 | 4.194,3 | 35.668,6 |
| Estrategia | 1.048,6 | 2.097,2 | 4.194,3 | 4.194,3 | 7.675,3 |
| Validación del intent | 1.048,6 | 2.097,2 | 4.194,3 | 8.388,6 | 36.282,9 |
| Matching | 8,2 | 16,4 | 32,8 | 65,5 | 158,4 |
| Cartera | 1.048,6 | 2.097,2 | 4.194,3 | 8.388,6 | 35.236,7 |

Espera de colas (p99 aproximado y máximo exacto, milisegundos):

| Cola | p99 | Máximo | Ocupación máxima / capacidad |
|---|---:|---:|---:|
| Normalización | 6.105,697 | 6.105,697 | 7.178 / 10.000 |
| Validación/secuencia | 16,777 | 40,412 | 21 / 10.000 |
| Libro | 8,389 | 10,345 | 15 / 10.000 |
| Estrategia | 8,389 | 11,267 | 14 / 10.000 |
| Validación del intent | 8,389 | 37,189 | 17 / 10.000 |
| Matching | 1,049 | 5,282 | 10 / 10.000 |
| Cartera | 1.330,726 | 1.330,726 | 1.578 / 10.000 |

Las latencias por etapa y las esperas por cola se emiten por separado en el JSON del benchmark. Los percentiles son límites superiores de buckets log2 con memoria fija, acotados por el máximo observado; `max_ns` es el máximo exacto. La resolución de estos buckets es deliberadamente aproximada. En este burst, la cola de normalización llegó a su capacidad y la latencia de recepción a cartera estuvo dominada por espera; el promedio de throughput por sí solo ocultaría ese comportamiento.

La medición de RSS y el pico de working set se incluye cuando el runtime permite leerlos; `tracemalloc` informa el heap Python y no sustituye la medición de memoria nativa del proceso. El benchmark no estima latencia origen→recepción: los timestamps sintéticos no se consideran comparables con el reloj local. Para medirla hacen falta una semántica de timestamp conocida y relojes sincronizados o una calibración explícita.

## Criterios de aceptación por configuración

No se fijan objetivos numéricos universales antes de conocer feed, mercado, hardware y carga. Para cada configuración de despliegue, archiva estos datos junto al resultado:

| Identidad de configuración | Valor que se debe fijar |
|---|---|
| Feed y adaptador | Proveedor, protocolo, canales, frecuencia de actualización y semántica de secuencia/timestamp |
| Universo | Instrumentos, sesiones, mezcla L1/L2, trades y proporción de eventos por símbolo |
| Carga | Tasa sostenida, burst máximo, tamaño de payload y duración de la corrida |
| Hardware y SO | CPU/modelo y afinidad, RAM, almacenamiento, sistema operativo y configuración de energía |
| Software reproducible | Commit/hash del motor, versión Python, catálogo, políticas de matching/cartera, estrategias y semilla |
| Colas y grabación | Capacidades, presupuestos de espera, tamaño de segmentos y política de fallo |

Cada configuración pasa primero los controles de corrección: ningún evento descartado en una captura declarada completa; cualquier overflow, error de etapa o fallo del grabador invalida la sesión; los gaps y datos incompletos bloquean decisiones según la política; y el replay con los mismos inputs reproduce decisiones, órdenes, fills y ledger o reporta la primera divergencia. Los escenarios de fills deben declarar el modelo de liquidez, latencia, spread, comisión y deslizamiento.

Después de capturar una línea base con carga representativa, el responsable de despliegue fija límites por etapa y extremo a extremo para p50, p90, p99, p99.9 y máximo, además de throughput, ocupación/espera de colas, descartes, CPU, memoria, asignaciones, GC y retraso del grabador. Se repiten pasadas sostenidas y bursts, con grabación activada, y se conserva el JSON completo. Los umbrales se versionan junto a esa configuración y no se extrapolan a otro proveedor, mercado o máquina.

## Límites conocidos

- El único contrato de feed ejercitado es el fixture JSONL; no hay datos ni adaptadores de una bolsa reales.
- El benchmark es sintético, usa quotes L1 y una estrategia no-op. No representa bursts de proveedor, complejidad de estrategia, carga de símbolos ni comportamiento de L2 de producción.
- El runtime actual ejecuta el motor en Python. Estas cifras no demuestran HFT ni permiten decidir por sí solas qué mover a Rust.
- El matcher L1 no conoce prioridad de cola ni liquidez oculta. Un fill simulado no es una garantía de ejecución real.
- Los percentiles log2 son aproximaciones de memoria acotada. Para fijar un presupuesto p99.9 se recomienda una muestra mucho mayor, varias corridas y escenarios representativos.
