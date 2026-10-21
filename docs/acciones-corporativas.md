# Política de splits y dividendos

Decisión del 21 de octubre de 2026. El contrato para `nyse-equities-2024-v1` se guarda en `config/equity-corporate-actions.json`, ligado al SHA-256 de su manifiesto. Los archivos congelados y los metadatos originales permanecen idénticos.

## Convenciones admitidas

| Base de precios | Splits | Dividendos | Uso admitido |
|---|---|---|---|
| `raw` | Eventos que cambian cantidad y coste unitario | Eventos de efectivo en el ledger | Contabilidad explícita con precios de acciones sin ajustar |
| `split_adjusted` | Ya incorporados; no multiplicar otra vez las posiciones | Excluidos del rendimiento; conservar para auditoría | Muestra inicial BE/NEM y motores diarios |
| `total_return_adjusted` | Ya incorporados | Ya incorporados; nunca añadir pagos en efectivo | Análisis sobre la serie ajustada declarada; no son precios negociables |
| `unknown` | Sin afirmar ajuste; rechazar eventos observados | Excluidos; rechazar eventos observados | Regresiones heredadas, identificadas como base no verificada |

Se elige **rendimiento de precios** para la primera muestra. Los motores utilizan `close`, conservan `adj_close` y `dividend` como evidencia y no abonan dividendos ni alteran cantidades por splits. El resultado informa de esta decisión; no representa el rendimiento económico total de mantener las acciones. Esta elección permite conservar el conjunto actual sin inventar fechas de pago, factores de acciones o reinversión.

No se infiere que la cotización de una empresa sea ajustada porque su catálogo histórico lo indique: la base de los precios de una ejecución debe declararse. En particular, el ledger de eventos usa precios raw por defecto y declara `equity_price_basis` en `PortfolioPolicy`. El valor raw es el predeterminado documentado del esquema; se conserva la serialización anterior cuando se usa ese valor para no alterar hashes de grabaciones previas. Las bases ajustadas sí se guardan como campo explícito. Los catálogos históricos y los precios de cotizaciones en vivo son contratos distintos.

La [explicación de Yahoo sobre el cierre ajustado](https://in.help.yahoo.com/kb/adjusted-close-sln28256.html) confirma que incorpora splits y dividendos. Por ello no se sustituye silenciosamente `close` por `adj_close` ni se añade además un dividendo a esa serie. La [descripción de splits de Investor.gov](https://www.investor.gov/introduction-investing/investing-basics/glossary/stock-split) explica la relación inversa entre número de acciones y precio unitario.

## Uso del conjunto inicial

```console
python -m scripts.equity_dataset --check-actions config/equity-corporate-actions.json
```

El comando comprueba el contrato, el hash del manifiesto y ambas series. Rechaza cambiar la base a raw o rendimiento total sin crear una referencia correspondiente. La instalación y CI lo ejecutan.

Al usar los motores diarios se declara la política:

```python
from market_catalog.corporate_actions import INITIAL_EQUITY_POLICY

result = engine.run("NEM", corporate_action_policy=INITIAL_EQUITY_POLICY)
```

`run_stock_macro_backtest` acepta el mismo argumento. Sus salidas para el diario contienen campos escalares con la base del rendimiento y la exclusión de dividendos. `RollingBacktestEngine` incluye un informe en `kpis["corporate_actions"]` y en el resumen. El campo `price_basis_status: declared` describe la declaración del llamador, no una certificación de los precios: el contrato del conjunto debe verificarse antes de ejecutar. Ambos rechazan solicitar contabilidad raw con pagos y splits: esos hechos pertenecen al ledger, no a una opción silenciosamente ignorada por el motor diario.

Si aparecen columnas de eventos sin una base explícita, los motores rechazan la ejecución. Una regresión anterior sin eventos sigue disponible, pero el resultado declara `price_basis_status: unknown`: no se convierte por ello en un conjunto apto para comparación financiera.

## Contabilidad raw y prevención del doble conteo

En el ledger, un split 2:1 convierte 10 acciones en 20 y divide su coste unitario por dos; no crea efectivo ni beneficio. Un reverse split aplica la relación inversa. Un dividendo de 0,25 USD sobre 20 acciones genera 5 USD; una posición corta genera un débito. No hay reinversión automática. Las fracciones resultantes de splits se conservan como Decimal; no se inventa efectivo por fracciones ni un precio de liquidación.

El ledger rechaza eventos de split o dividendo cuando `PortfolioPolicy.equity_price_basis` es ajustada. También rechaza IDs repetidos y un segundo evento del mismo tipo/instrumento/instante efectivo, incluso con otro ID o al reconstruir el ledger. Los rechazos se producen antes de cambiar el estado. Si un emisor tiene varios componentes de dividendo para el mismo instante, deben reconciliarse y consolidarse antes de registrarlos; la clave temporal no sirve para identificar correcciones o cancelaciones, que requieren un proceso de revisión aparte.

`record_dividend` es un hecho contable explícito sobre la posición presente en ese instante. El llamador debe acreditar importe, moneda y derecho al cobro, y ordenar el hecho antes de operaciones que cambien esa posición si utiliza un instante ex-dividendo. No determina por sí solo la elegibilidad ni convierte una fecha ex en fecha de pago. La [distinción de fechas de Investor.gov](https://www.investor.gov/introduction-investing/investing-basics/glossary/ex-dividend-dates-when-are-you-entitled-stock-and) es la referencia conceptual. La muestra Yahoo aporta fechas ex; no se inyectan sus eventos como pagos. Para una comparación futura con rendimiento total y efectivo negociable habrá que aportar fechas de pago y modelar derechos/cuentas por cobrar, efectivo disponible y costes. Esta decisión no afirma que ese modelo ya esté implementado.

Las pruebas calculan a mano la conservación del valor en un split, el pago único de 5 USD, débitos a cortos y reverse splits; comprueban el rechazo de eventos repetidos, las reconstrucciones y las combinaciones incompatibles. Otras pruebas demuestran que cambiar `adj_close` o añadir un dividendo informativo no modifica las operaciones ni el NAV en el modo inicial, y que los splits ya incorporados no multiplican posiciones.
