# Fase 6: cartera y contabilidad

La cartera deriva su estado de un ledger append-only. Cada entrada es un
`LedgerEntry` inmutable con identificador único; los fills permanecen como
hechos, y los saldos, posiciones, P&L y cargos se reconstruyen al reproducir
esas entradas. Los eventos de checkpoint conservan la versión y el límite de
tiempo/ordinal de cada evento procesado.

## Guardar y reproducir

`Portfolio.ledger` y `SessionReport.ledger` devuelven tuplas inmutables.
`ledger_to_jsonl(report.ledger)` produce JSONL canónico y
`Portfolio.from_ledger_jsonl(catalog, contenido, policy)`
reconstruye la cartera. Los importes financieros se escriben como cadenas
decimales. Cada línea declara `ledger_schema_version: "1.0.0"`; campos
desconocidos, versiones incompatibles, IDs repetidos y flujos de caja que no
reconcilian con su fill se rechazan.

La política se conserva aparte, en `SessionReport.portfolio_policy_json`.
`PortfolioPolicy.to_mapping()` y `PortfolioPolicy.from_mapping()` permiten
guardar y restaurar esa configuración junto al catálogo y el JSONL. La
configuración está definida por
[`portfolio_policy.schema.json`](../schemas/portfolio_policy.schema.json), y cada
entrada se describe en [`ledger_entry.schema.json`](../schemas/ledger_entry.schema.json).
Para añadir un dividendo, split, financiación, ajuste de futuros o tipo de
cambio entre quotes, se usa `EventProcessor.apply_ledger_entry`; el método
espera a que se confirmen los frames anteriores y bloquea el ingreso mientras
inserta el evento.

## Reglas contables

- **Acciones:** una compra/venta afecta efectivo por `-cantidad firmada ×
  precio`. La posición y el coste medio determinan P&L realizado al cerrar y
  P&L no realizado contra la marca. Los splits ajustan cantidad y coste medio;
  invalidan la marca antigua. Los dividendos se registran como flujo con
  cantidad, importe por unidad, moneda, fuente y momento. La política de
  precios históricos (raw, split-adjusted o total-return-adjusted) no ingiere
  eventos por sí sola: los eventos corporativos deben cargarse explícitamente.
- **FX:** cantidad significa unidades de la moneda base. Un fill registra la
  pata base (`+cantidad` al comprar) y la pata cotizada (`-cantidad × precio`)
  en sus monedas respectivas. P&L realizado se expresa en la divisa cotizada.
  Financiación/rollover requiere un evento `funding` con importe firmado,
  moneda, fuente y timestamp.
- **Futuros:** abrir un contrato no debita su nocional del efectivo. P&L se
  calcula con cantidad firmada × variación del precio × multiplicador, en la
  moneda de liquidación del contrato. Los cierres reconocen en efectivo la
  variación desde el coste vigente; `record_futures_settlement` registra la
  liquidación, añade la variación al efectivo y reinicia el coste al precio de
  liquidación. No se infiere calendario ni método de liquidación.
- **Comisiones:** cada cargo es una entrada contable aparte, con referencia al
  fill, cantidad, moneda y versión de la política de matching. P&L neto
  reportado es realizado + no realizado + dividendos + financiación −
  comisiones. El efectivo sigue mostrado por moneda y no se mezcla con P&L.
- **Capital:** aportes y retiros explícitos usan `cash_adjustment`; cambian el
  saldo de efectivo y no se cuentan como P&L. No se supone un saldo inicial.
- **Marcas:** el motor usa L1 conservador de liquidación: bid para posiciones
  largas y ask para cortas. Cada marca conserva bid/ask, fuente, proveedor,
  canal, secuencia, timestamp y `arrival_ordinal`. La liquidación de un futuro
  también pasa a ser marca hasta el siguiente quote.

## Conversión, exposición y margen

La cartera mantiene efectivo, P&L, dividendos, financiación y comisiones por
moneda original. `PortfolioPolicy.reporting_currency` habilita totales
convertidos. Se admiten tasas **directas** `from_currency → to_currency`; el
ledger conserva tasa, fuente, instrumento asociado si existe, timestamp y
ordinal. Se elige la última tasa que no supera el momento de valoración; una
tasa más vieja que `max_conversion_age_ns` se considera ausente. No se invierte
ni triangula tasas automáticamente. Si falta una tasa, la métrica afectada
queda como `null` y la moneda aparece en
`missing_conversion_currencies`.

Exposición y margen son cálculos aparte del saldo. La exposición agrega
nocionales firmados y exposición base FX; los límites configurados producen
incumplimientos visibles. Las reglas de margen pueden ser por unidad o tasa
sobre nocional. Para `notional_rate`, la moneda de la regla debe coincidir con
la del nocional: no se hace conversión implícita. No hay regla predeterminada;
futuros sin regla aparecen en `unconfigured_instruments`. El requerimiento de
margen no reserva ni resta efectivo.

## Límites

La Fase 6 no define reglas reales de bolsas, fiscalidad, netting, liquidación
física, cash-in-lieu, dividendos especiales, tasas de financiación, conversión
triangulada ni requisitos contractuales de margen. Esos datos se aportan como
eventos y política versionados. No se seleccionaron proveedor, mercado ni
moneda de reporte. Una cartera reconstruida requiere el mismo catálogo,
`PortfolioPolicy`, secuencia JSONL y convenciones de marca; el reporte de la
sesión conserva los dos últimos elementos de configuración y el ledger.
