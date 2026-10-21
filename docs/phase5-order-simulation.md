# Fase 5: órdenes y simulación de fills

## Alcance y versiones

El simulador procesa exclusivamente órdenes internas de mercado y límite, y
solicitudes internas de cancelación. No hay adaptador de órdenes reales ni
ninguna ruta para enviar órdenes a una bolsa. Los resultados describen el
modelo configurado; nunca garantizan una ejecución real.

`MatchingPolicy` es el contrato versionado de las hipótesis de matching. Se
puede construir en Python o cargar desde un mapping estricto y su forma JSON
independiente del lenguaje está en
[`matching_policy.schema.json`](../schemas/matching_policy.schema.json). El
reporte conserva el mapping canónico en `SessionReport.matching_policy_json` y
cada fill incluye la versión y regla aplicada. Si cambia la semántica, publica
una versión nueva y conserva la anterior en el historial del repositorio.

```python
from decimal import Decimal
from market_catalog import (
    CommissionModel, EventProcessorConfig, LatencyModel,
    MatchingPolicy, SlippageModel,
)

config = EventProcessorConfig(matching_policy=MatchingPolicy(
    latency=LatencyModel(decision_to_arrival_ns=2_000_000),
    slippage=SlippageModel(ticks=1),
    commission=CommissionModel(
        per_unit=Decimal("0.01"),
        notional_rate=Decimal("0.0001"),
        currency="USD",
        currency_precision=2,
    ),
))
```

Los valores son parámetros de ejemplo, no recomendaciones de mercado. La
latencia usa nanosegundos virtuales del replay, no pausas del sistema. Cada
instante se toma del timestamp de evento capturado y, si falta, del timestamp
UTC de recepción capturado; si la semántica del timestamp de origen es
`unknown`, también se usa el de recepción. El ordinal de llegada sigue determinando el orden
estable de procesamiento; los timestamps iguales no reordenan los eventos.
Los defaults son latencia cero, comisión cero y deslizamiento cero; por tanto,
un reporte con esos defaults asume que una orden puede llegar y cruzar en el
mismo timestamp de la cotización que produjo la decisión.
Una orden solo puede cruzar contra una cotización válida observada cuyo tiempo
virtual sea igual o posterior a su hora de llegada calculada. Una orden que no
encuentre una cotización elegible queda pendiente hasta otro quote o el cierre
de sesión.

## Ciclo y estados

`OrderIntent` describe una orden. La validación central comprueba instrumento
negociable, clase, lado, tipo, cantidad mínima e incremento y tick de precio.
Las series continuas de futuros siguen siendo no ejecutables. El matcher
registra `rejected` con motivo si rechaza una orden bien formada. Las órdenes
aceptadas pasan por `accepted_pending_arrival`, `pending` o
`partially_filled_working`; terminan como `filled`, `cancelled` o
`partially_filled_cancelled`. `OrderOutcome` conserva cantidades llenadas y
restantes, ordinal de decisión, timestamps virtuales, comisiones acumuladas,
política y razón terminal.

Una `CancelOrderIntent(request_id, order_id)` cancela una orden límite aún
pendiente dentro de la sesión. La respuesta se expone en
`SessionReport.cancellations` con estado, razón, ordinal y tiempo de simulación.
Una orden desconocida o terminal produce cancelación rechazada. Las
cancelaciones se procesan al llegar a la etapa de matching del evento; la
latencia configurada modela decisión a llegada de órdenes, no latencia de
cancelación por separado. Primero se evalúa la cotización que disparó el
callback contra órdenes ya pendientes; después se aplican las cancelaciones
generadas por ese callback a cualquier remanente.

## Matching L1

La única política inicial es `conservative_l1_no_touch` con
`observed_l1_bid_ask`:

- una compra market consume el ask mostrado y una venta market consume el bid;
- una orden puede consumir como máximo el tamaño L1 mostrado y no más que su
  cantidad restante; el fill se redondea hacia abajo al incremento de cantidad
  del instrumento;
- una orden market cancela cualquier remanente al encontrar su primera quote
  elegible;
- una orden limit solo se ejecuta si se vuelve marketable contra el mejor lado
  opuesto observado y el precio final, después del deslizamiento, respeta su
  límite. Un simple toque del bid/ask propio no produce fill. No se simulan
  prioridad de cola, volumen negociado ni fills probabilísticos;
- cada nueva quote válida repone el tamaño visible para esa actualización. El
  matcher procesa primero las órdenes límite antiguas en FIFO y después las
  nuevas órdenes decididas sobre ese evento.

El spread observado no se añade como una tarifa artificial: ya está reflejado
en el precio de ejecución al usar ask para comprar y bid para vender. El fill
guarda bid, ask y spread del instante del matching para poder explicar el
precio. El deslizamiento agrega o resta un número entero y fijo de ticks,
configurado por `SlippageModel`; la regla se registra en cada fill. Si el precio
deslizado rebasa el límite, una limit queda trabajando sin fill.

L1 no revela la profundidad completa, la prioridad de cola, liquidez oculta,
cancelaciones competidoras ni latencia real. No extrapoles las estadísticas de
fill de esta política a ejecución real o a un modelo L2.

## Comisiones y cartera

`CommissionModel` calcula por fill:

`cantidad * per_unit + abs(cantidad * precio * multiplicador) * notional_rate`

Puede redondear a la precisión de la moneda elegida usando el modo Decimal
configurado. La moneda puede fijarse; si se omite, se usa la divisa de
cotización (o liquidación para futuros). El multiplicador es el del contrato;
para clases sin multiplicador explícito se usa uno. `Fill` conserva comisión,
moneda, slippage, spread, fuente de liquidez, regla, ordinal, identidad del
evento (`provider_id`, canal y secuencia), cantidades L1 visibles, liquidez
disponible antes del fill y tiempos de decisión, llegada y matching. La cartera
descuenta la comisión del saldo de su moneda y
expone `commissions_paid` en sus snapshots. El P&L realizado/no realizado
continúa siendo bruto de comisiones; interpreta ambos registros juntos. No se
hace conversión FX, cálculo de margen ni liquidación diaria de futuros.

Al terminar el replay, las órdenes límite aún abiertas se cierran en el
reporte: `session_closed_before_arrival` si nunca llegó su momento de entrada,
o `session_closed_unfilled` si llegó pero no se ejecutó. La salida muestra el
remanente, sin convertirlo en fill.

## Ejemplo

El ejemplo de la Fase 4 se actualizó para fijar comisión sintética y presentar
la configuración de matching y la auditoría del fill:

```console
python examples/strategy_replay.py
```

Usa exclusivamente cotizaciones e instrumento sintéticos. No afirma
especificaciones de un proveedor, latencia de producción, fills de mercado ni
rendimiento HFT.
