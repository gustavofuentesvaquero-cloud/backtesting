# Identidad y rollover de Micro Gold

Revisión: 21 de octubre de 2026. La especificación para ampliar la investigación está en `data/futures/mgc-research.json`. No equivale a una cadena histórica ya adquirida y validada.

## Producto y contratos

| Campo | Referencia |
|---|---|
| Producto y mercado | Micro Gold, raíz MGC, COMEX |
| Unidad / cotización | 10 onzas troy / USD por onza |
| Tick / valor por contrato | 0,10 USD por onza / 1 USD |
| Entrega | Física mediante ACE; no liquidación puramente en efectivo |
| Meses de referencia | Febrero G, abril J, junio M, agosto Q, octubre V, diciembre Z |
| Fin de negociación | Tercer último día hábil de bolsa del mes de entrega |

Fuentes primarias: [COMEX, capítulo 120](https://www.cmegroup.com/content/dam/cmegroup/rulebook/COMEX/1a/120.pdf), [guía de productos de metales CME](https://www.cmegroup.com/trading/metals/files/metals-prod-guide-2024.pdf) y [ficha de Micro Gold](https://www.cmegroup.com/markets/metals/precious/e-micro-gold.contractSpecs.html). Los meses cotizados en cada fecha deben contrastarse con la cadena de la bolsa y el proveedor; el código de mes no acredita por sí solo que exista una cotización disponible.

Los candidatos iniciales se identifican como `MGC-2026-12` (etiqueta de pantalla MGCZ6) y `MGC-2027-02` (MGCG7). Las fechas 29/12/2026 y 24/02/2027 son **derivaciones de la regla**, pendientes de confirmación contra el calendario específico de esos contratos. No son fechas de primer aviso ni fechas de roll. Los identificadores nativos del proveedor, primer aviso y límites del broker permanecen pendientes; por ello `execution_eligible` es falso. No se sustituyen esos datos por el último día del mes ni por un calendario NYSE.

Se ha corregido `data/cme_micro_gold.json`: antes indicaba liquidación cash, un último día de negociación distinto del derivado y un alias `MGC-ACTIVE` presentado como contrato verificado. Ahora describe la entrega física y marca su identidad como no verificada. Conserva el ID MGC para las regresiones anteriores. Ese registro sigue siendo una referencia heredada, no una identidad suficiente para ejecutar un vencimiento concreto. `first_notice_date: null` es una carencia explícita que impide aceptar el contrato para un experimento con ejecución.

## Regla de rollover elegida

Se adopta `mgc-calendar-before-delivery-v1`, una decisión de investigación del proyecto, no una regla obligatoria de CME. El cambio se programa cinco sesiones CME verificadas antes de la más temprana entre primer aviso, último día de negociación y límite de cierre del broker. Los tres límites y el calendario deben estar confirmados. No se usa un cruce de volumen determinado con información futura.

El destino es el siguiente vencimiento listado que tenga identidad de proveedor y precios raw verificados con observaciones solapadas. En la apertura de la sesión programada se cierra el contrato anterior y se abre el nuevo; el calendario se decide antes de esa apertura. Cada ejecución lleva su precio, spread, comisión y deslizamiento. Si falta un límite, una sesión o el precio de cualquiera de las patas, se rechaza el experimento; no se fabrica el fill ni se aplaza silenciosamente el roll hasta la entrega.

`market_catalog.futures_research.require_individual_contract` rechaza aliases, series continuas e identidades o fechas sin confirmar. `rollover_session` calcula la sesión sobre el calendario revisado suministrado y rechaza calendarios cortos, duplicados o desordenados. No certifica la completitud del calendario que aporte el llamador. Sus pruebas usan un calendario sintético; no presentan como verificado un calendario CME real.

## Serie continua frente a precios negociables

`MGC=F` y el Parquet heredado `MGC_1d.parquet` no acreditan una cadena de contratos individuales, ni documentan qué vencimiento compone cada barra o cómo cambia el proveedor. El método de descarga ya no los llama datos oficiales de contratos CME. Los resultados anteriores del motor identifican MGC como referencia del proveedor con precios negociables y rollover no verificados.

Para la ampliación, cada barra de ejecución debe identificar contrato, mes, proveedor y procedencia. El P&L y la variación de margen se calculan con los precios raw del contrato efectivamente mantenido y multiplicador 10. El salto de precio entre dos vencimientos no es por sí solo una ganancia o pérdida de la posición: el cambio exige dos operaciones registradas.

La primera serie continua de análisis se concatenará **sin ajuste**, guardando contrato por barra y un manifiesto de roll. Solo será una vista de referencia. Si posteriormente se construye una serie ajustada por diferencias o ratios para indicadores, se guardará aparte, con factores y versión; sus niveles no se usarán para fills, stops, liquidación diaria ni atribución de P&L. Una reconstrucción retrospectiva tampoco prueba causalidad de indicadores históricos.

## Puerta de entrada a los experimentos

La identificación del producto, las carencias del catálogo y la regla quedan documentadas. Sigue siendo necesario adquirir y congelar una cadena de contratos con avisos y vencimientos oficiales, calendario CME y excepciones, identificadores del proveedor, precios/settlements, volumen y límites del broker. Además, la fase contable deberá conciliar margen, liquidación diaria y ambas patas del roll. El proyecto todavía no admite esta referencia heredada como un backtest verificado de contratos negociables.
