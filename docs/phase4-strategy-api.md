# Fase 4: interfaz y aislamiento lógico de estrategias

## API y ciclo de vida

La API administrada está versionada como `1.0.0` (`STRATEGY_API_VERSION`); su
configuración y los snapshots tienen esquemas JSON públicos en
`schemas/strategy_config.schema.json` y `schemas/strategy_state.schema.json`.
Una instancia combina una implementación nueva de estrategia, `StrategyConfig`,
una semilla y su estado inicial JSON:

Los cambios incompatibles del callback o del formato serializado requieren una
nueva versión de API o de estado. Esta fase no incluye migraciones automáticas.

```python
config = StrategyConfig(
    strategy_id="mean-reversion",
    instance_id="mean-reversion-eu-1",
    strategy_version="0.1.0",
    parameters={"threshold": "0.25", "window": 20},
)
runtime = StrategyInstance(MyStrategy(), config, seed=17, initial_state={"count": 0})
processor = EventProcessor(pipeline, catalog, runtime, session_id="replay-2026-10-12")
```

`StrategyConfig` valida identidad, versión de API y que los parámetros sean un
objeto JSON finito. Las claves de nivel superior desconocidas se rechazan al
cargar con `StrategyConfig.from_mapping`. La estrategia puede implementar
`validate_config(parameters)` para validar su propio contrato y rangos.
Representa dinero, precios y cantidades como cadenas decimales en parámetros y
estado; conviértelos a `Decimal` al usarlos. No guardes secretos ni valores
dependientes del entorno en la configuración reproducible.

El orquestador llama a `on_start(context, state)`, a `on_market(context,
market, portfolio, state)` por cada evento utilizable con L1 y a
`on_stop(context, state)` al cerrar la sesión. `on_market` devuelve
`StrategyDecision(state, intents)`: el nuevo estado debe ser un objeto JSON y
los intents pueden ser `OrderIntent` o `CancelOrderIntent`. Devolver `None`
conserva el estado y no emite órdenes. Los hooks de ciclo de vida son opcionales y reciben estado de solo
lectura; el estado explícito cambia únicamente mediante `StrategyDecision`.
Los IDs de orden se prefijan con `instance_id` antes de la validación central
para evitar colisiones entre instancias.

El callback es síncrono. El callback de una instancia se ejecuta en el único
worker de estrategia de esa sesión, preservando el orden. Las instancias
administradas ven la cartera hasta el commit del evento anterior, y el motor
espera ese commit antes de invocar la siguiente decisión. Así, una misma
reproducción no observa una cartera distinta por el planificador de threads.
El campo `arrival_ordinal` identifica el evento de sesión y el campo `version`
identifica la revisión de cada vista. La primera vista de cartera tiene
`as_of_ordinal = -1` (sin evento confirmado); después, el ordinal indica el
último evento ya contabilizado.

## Vistas, límites y contabilidad

`StrategyMarketView` contiene el evento canónico con payload profundamente
inmutable y un `TopOfBook` congelado. `StrategyPortfolioView` expone mapas
inmutables de efectivo, posiciones, P&L realizado/no realizado, comisiones,
dividendos y financiación, además de exposición, requerimientos de margen y
métricas convertidas si se configuraron. El motor actualiza la cartera, sus
marcas y su versión antes de publicar la vista para la siguiente decisión. Las
posiciones largas se marcan al bid y las cortas al ask para estimar una
liquidación conservadora con L1. El P&L se agrupa por moneda y permanece
separado de comisiones, efectivo y margen. El coste medio ponderado determina
P&L al reducir una posición. Las tasas y convenciones contables están descritas
en [phase6-portfolio-accounting.md](phase6-portfolio-accounting.md).

La estrategia recibe configuración, semilla y un generador pseudoaleatorio
propio de la instancia; no recibe el adaptador/feed, reloj, matcher ni cartera
mutable. El estado explícito devuelto por `StrategyDecision` es la fuente de
verdad que se guarda. El snapshot `StrategyState 1.0.0` también contiene la
identidad, versión, hash de configuración, semilla, contador y estado del PRNG.
El snapshot identifica el algoritmo PRNG usado para rechazar restauraciones con
un generador incompatible.
`save_state(path)` escribe el snapshot; `restore_state(snapshot)` solo funciona
antes de iniciar la instancia y exige coincidencia de API, IDs, versión,
configuración y semilla. Esto permite continuar de forma controlada y rechaza
restauraciones incompatibles. Al iniciar desde un snapshot, el estado del PRNG
restaurado se conserva aunque `on_start` vuelva a ejecutarse.

`SessionReport.strategy_runs` registra la configuración canónica, semilla,
versión de API/estrategia, contador, estado final y resultado del ciclo de vida.
`SessionReport.session_id` identifica la sesión usada por las vistas; fija un ID
estable y único al crear cada sesión/replay. No guarda automáticamente un
snapshot por evento: el caller puede pedir y persistir checkpoints entre
sesiones cuando su política de replay lo requiera.

## Aislamiento y efectos externos

El aislamiento actual es lógico y dentro del proceso. Cada `StrategyInstance`
requiere un objeto de estrategia distinto, parámetros y estado propios, y un
PRNG local. Los argumentos de evento y cartera son inmutables. No obstante,
Python no puede impedir que un callback escriba archivos, consulte servicios,
use el reloj del sistema o modifique variables de clase. Para reproducibilidad,
los callbacks deben evitar esos efectos y derivar sus decisiones de la entrada,
el estado explícito y el PRNG entregado.

Si se requiere aislamiento de fallos o recursos, evaluar workers por proceso con
un benchmark del contrato completo de `StrategyMarketView` y
`StrategyDecision`: tiempo de serialización, IPC, colas, variabilidad y coste de
reinicio. Esta fase no crea procesos ni afirma que IPC sea más rápido.

La API anterior `Strategy.on_event(event, book)` permanece como compatibilidad
para la Fase 3. No tiene estado serializable ni snapshots de cartera; los
desarrollos nuevos deben usar `StrategyInstance` y la API versionada.

## Ejemplo ejecutable

El ejemplo usa exclusivamente un instrumento y cotizaciones sintéticos. Envía
una limitada, consume liquidez L1 mostrada en más de una cotización, compara dos
replays y verifica que el estado se puede guardar/restaurar:

```console
python examples/strategy_replay.py
```

Los fills ilustran el matcher de referencia; no afirman prioridad de cola,
liquidez oculta, ejecución real ni especificaciones de una bolsa.
