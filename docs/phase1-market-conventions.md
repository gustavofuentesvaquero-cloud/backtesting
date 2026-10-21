# Fase 1: requisitos y convenciones de mercado

## Estado y alcance

El catálogo inicial está vacío: todavía no se han elegido proveedor, venue,
región ni símbolos. No se deben completar datos de mercado mediante suposiciones.
Los fixtures sintéticos de las pruebas no son instrumentos negociables ni datos
de una bolsa real.

Esta fase contiene el contrato versionado, carga y validación. No implementa
feeds, estrategias, matching, cartera, eventos corporativos ni envío de órdenes.

## Versiones y cambios

`schema_version` identifica la versión del contrato JSON soportada por el
validador. `catalog_version` identifica una instantánea semántica del catálogo
y usa `MAJOR.MINOR.PATCH`. Una modificación que cambie el significado de un
registro requiere incrementar `catalog_version`; no se debe reutilizar una
versión anterior con contenido distinto. El historial del repositorio conserva
las instantáneas anteriores.

`valid_from` es inclusivo y `valid_to` es exclusivo. Si `valid_to` es nulo, el
registro no tiene fecha final declarada. Los cambios históricos de contrato se
representan mediante nuevas versiones del catálogo y sus fechas de validez.

## Números exactos y unidades

Ticks, cantidades, pips y multiplicadores se escriben como cadenas decimales
(por ejemplo, `"0.25"`) y se convierten a `decimal.Decimal`. No se aceptan
números JSON con parte fraccionaria para evitar la conversión implícita a
`float`. El formato del catálogo requiere que las magnitudes de ticks y
cantidades sean positivas; la cantidad mínima debe ser múltiplo exacto del
incremento.

`price_precision` expresa el máximo de decimales representables por el precio y
debe admitir todos los decimales de `price_tick`. `quote_currency` es un código
de tres letras mayúsculas; la validación comprueba su forma, pero no afirma que
pertenezca a un registro monetario vigente.
`quantity_precision` aplica la misma regla a `minimum_quantity` y
`quantity_increment`.

Los modelos de valoración documentados son:

- `equity_shares`: nominal de referencia = cantidad de acciones × precio.
- `fx_base_units`: la cantidad está expresada en unidades de moneda base; el
  valor en moneda de cotización depende del tipo de cambio.
- `future_contract_multiplier`: nominal de referencia = contratos ×
  multiplicador × precio.

Estas fórmulas definen convenciones de unidades. No son todavía un cálculo de
P&L, margen ni contabilidad.

## Instrumentos y divisas

Cada instrumento tiene un identificador interno estable, venue, símbolos
externos por proveedor, clase de activo, calendario, intervalo de validez,
unidad de cantidad, tick, divisa y procedencia. La versión 1 admite `equity`,
`fx` y `future` y exige campos específicos por clase.

Para FX, `fx_convention.base_currency` y `quote_currency` fijan el orden del
par. `pip_size` fija el movimiento de precio llamado pip. La regla de valor del
pip en esta versión es cantidad en moneda base × tamaño del pip, denominada en
moneda cotizada. Convertirlo a otra moneda requiere un tipo de cambio y un
momento de conversión; por eso el valor del pip no es una constante universal.

### Datos completables y datos pendientes

El registro solo se considera completo cuando sus unidades y relaciones están
definidas. Los campos concretos de un instrumento real se rellenarán cuando se
seleccione el mercado y la fuente; no se deben sustituir por valores ficticios.

| Se valida como parte del contrato | Requiere seleccionar y verificar el mercado/fuente |
| --- | --- |
| Clase de activo, unidad de cantidad y modelo de valoración | Venue, símbolo negociado y alias del proveedor |
| Forma decimal exacta del tick, cantidades, pip y multiplicador | Valores reales de ticks, lotes, precisión y multiplicador |
| Referencia a calendario, fechas de validez y zona IANA | Horarios, cierres extraordinarios y semántica local de sesiones |
| Orden base/cotizada y divisa de valoración | Convención de símbolo y regla real de pip del instrumento |
| Forma del contrato listado y de la política de la serie continua | Fechas reales de vencimiento/aviso, liquidación y regla de roll |

Por eso `data/market_catalog.json` comienza sin instrumentos ni calendarios.
La estructura permite expresar convenciones sin afirmar que una especificación
de mercado todavía no seleccionada sea correcta.

## Calendarios y sesiones

Cada calendario contiene una zona horaria IANA, sesiones semanales y
excepciones fechadas. Los días de semana usan lunes = 0 hasta domingo = 6. El
día de inicio identifica la fecha local de apertura. `close_day_offset` vale 0
para cierre el mismo día y 1 para cierre al día siguiente. Si el cierre no es
posterior a la apertura en la sesión local, debe usarse el offset 1 para una
sesión nocturna.

Las excepciones son `closed` con una lista vacía de sesiones o `override` con
una o más sesiones que sustituyen el horario semanal de esa fecha. No se
permiten sesiones solapadas dentro de la semana ni dentro de una excepción.
La validez del nombre IANA depende de que la base de zonas horarias esté
disponible en el runtime de Python.

## Futuros y series continuas

Cada futuro representa un contrato listado: contiene mes, expiración, último
día de negociación, método/divisa de liquidación y multiplicador. La validación
comprueba que la última negociación no sea posterior a la expiración. La divisa
de liquidación se conserva como campo independiente de la divisa de cotización;
no se asume que coincidan. Las fechas y unidades concretas deben verificarse
para el mercado elegido.

Una serie continua referencia contratos futuros listados, define método de roll
y método de ajuste, y siempre declara `executable: false`. Sirve para análisis y
no es un instrumento que se pueda enviar al simulador como contrato negociable.
Las series no deben añadirse hasta conocer sus contratos y política de roll.

## Acciones y precios históricos

`historical_price_policy` distingue `raw`, `split_adjusted` y
`total_return_adjusted`. Una política ajustada debe declarar los tipos de
eventos corporativos considerados. `total_return_adjusted` requiere declarar
splits y dividendos. Esta fase solo registra la política: no
ingiere eventos, no aplica ajustes y no mezcla precios de análisis con precios
ejecutables.

## Procedencia y validación

La procedencia se marca como `verified`, `unverified` o `synthetic`. Un registro
`verified` requiere al menos una URI de fuente. La forma de los códigos,
referencias, decimales, calendarios y relaciones entre campos se valida en el
catálogo; los requisitos concretos de un proveedor o venue deberán documentarse
con fuentes oficiales al seleccionarlos.

El validador informa rutas JSON como `$.instruments[0].price_tick`, un código
de error y una explicación correctiva. También impide que el mismo símbolo de
una fuente identifique dos instrumentos con periodos de validez solapados;
puede reutilizarse tras cerrar el periodo anterior. Rechaza campos desconocidos
y versiones de esquema que no sean `1.0.0`.
