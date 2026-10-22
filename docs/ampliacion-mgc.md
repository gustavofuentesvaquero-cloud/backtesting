# Identidad y rollover de Micro Gold

Revisión: 22 de octubre de 2026. La especificación para ampliar la investigación está en `data/futures/mgc-research.json`. Se han archivado históricos públicos de tres contratos para investigar su calidad; todavía no constituyen una cadena validada para ejecución. Se usan solo fuentes públicas, según la preferencia del usuario.

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

## Identificadores de proveedor

La ficha CME identifica MGC como código de producto para Globex, ClearPort y clearing; MGT se usa para TAS. La [tabla oficial de símbolos de proveedores CME](https://www.cmegroup.com/tools-information/vendorSymbol.html) muestra el root `MGC` para distribuidores como CQG, Fidessa/ION, FIS Global, Trading Technologies y Vela. Son códigos de producto, no el identificador completo de un vencimiento ni una prueba del histórico servido por esos proveedores. Para esta investigación se observaron los identificadores específicos Yahoo `MGCV26.CMX`, `MGCZ26.CMX` y `MGCG27.CMX` en los metadatos de sus respuestas. Por eso el catálogo conserva por separado el root CME, el símbolo visible CME y la clave del proveedor; esta última sigue siendo elegible solo para investigación mientras no se certifiquen su semántica y cobertura histórica.

Se consultó el [calendario público CME QuikStrike](https://www.cmegroup.com/tools-information/quikstrike/product-expiration-browser-metals.html), filtrando Micro Gold Futures, COMEX, producto 775. Mostró estos vencimientos, coincidentes con las derivaciones anteriores:

| Instrumento | Símbolo CME mostrado | Símbolo Yahoo consultado | Expiración CME observada |
|---|---|---|---|
| MGC-2026-10 | MGCV6 | MGCV26.CMX | 28/10/2026 |
| MGC-2026-12 | MGCZ6 | MGCZ26.CMX | 29/12/2026 |
| MGC-2027-02 | MGCG7 | MGCG27.CMX | 24/02/2027 |

La tabla mostraba 12:30 y la etiqueta «All Times CST». Se conserva esa presentación sin inferir un timestamp UTC. La evidencia local es una **transcripción manual** en `data/futures/cme-public-observations-2026-10-21.json`, no un CSV original ni una captura archivada. La exportación quedó interrumpida y la descarga directa posterior devolvió HTML de error, por lo que no se acepta ese archivo como calendario. La transcripción no confirma primeros avisos ni sesiones hábiles.

CME mantiene además un [calendario público de vencimientos](https://www.cmegroup.com/tools-information/calendars/expiration-calendar.html) que permite filtrar el tipo de evento. Su [descripción oficial de campos](https://www.cmegroup.com/trading/about-listings.html) incluye First Position Date, First Notice Date y First Delivery Date. Esto confirma dónde se deben verificar las fechas y por qué el primer intento de entrega debe separarse del primer aviso. No se pudieron extraer de ese calendario las filas individuales de los contratos.

La [guía oficial de entrega física de metales CME](https://www.cmegroup.com/trading/metals/files/precious-metals-delivery-process.pdf) indica que el primer día de intención/posición cae dos días hábiles antes del primer día hábil del mes de entrega; el aviso/invoice es el día hábil siguiente. El [aviso oficial CME 26-321](https://www.cmegroup.com/content/dam/cmegroup/notices/clearing/2026/09/chadv26-321.pdf), fechado el 24/09/2026, ya confirma para Gold/Micro Gold el primer intento 29/09, primer aviso 30/09, primera entrega 01/10, último trade 28/10 y últimos límites 29–30/10. Así que las fechas de MGCV26 son confirmadas por aviso individual; también aparecen su último aviso y último día de entrega.

Para los candidatos posteriores, la regla junto al [calendario de festivos y horarios CME de 2026](https://www.cmegroup.com/trading-hours.html) da fechas de investigación de MGCZ26: intento 27/11 y aviso 30/11/2026 (el 26/11 es festivo y el 27 hay sesión abreviada); MGCG27: 28/01 y 29/01/2027. Estas dos parejas siguen **derivadas**, a la espera del memorando de entrega CME correspondiente. El calendario anual tampoco es un archivo de sesiones completo y versionado para backtesting. Por eso `exchange_calendar_verified` permanece falso para los tres candidatos.

Para horarios, CME documenta el API de referencia Globex y el archivo semanal `TradingSessionList.dat`. La consulta anónima al API respondió HTTP 401 y la descarga directa del archivo semanal HTTP 403. La [documentación del API v3](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457215780) lo describe como acceso OAuth restringido a clientes. Se mantiene fuera del alcance acordado de fuentes públicas; no se usaron credenciales ni datos de broker. La [página CME de festivos y horarios](https://www.cmegroup.com/trading-hours.html) indica que horarios festivos suelen concretarse unas dos semanas antes y pueden cambiar. Por ello, la tabla de festivos no sustituye una lista por fecha de sesiones negociables. El detalle de fuentes y resultados de acceso está en `data/futures/cme-public-observations-2026-10-21.json`.

Se localizó también el [Daily Bulletin de CME](https://www.cmegroup.com/market-data/daily-bulletin.html): el PDF público más reciente incluye la tabla de futuros MGC con meses, precios de settlement, volumen y open interest. La propia página advierte que sus datos son solo de referencia y no validan ni complementan el feed MDP en tiempo real. El enlace «Historical Daily Bulletin» conduce a DataMine; su [descripción del conjunto de datos](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457420037) señala que los archivos disponibles allí son del día superior y se reemplazan al publicarse los del día siguiente. Por tanto, la página abierta no proporciona un archivo histórico público que cubra los vencimientos vencidos necesarios para reconstruir la cadena. El intento de descarga automatizada del PDF fue denegado por las condiciones de datos de CME; no se elude esa restricción ni se incorpora el documento a los datos locales.

Los símbolos de Yahoo se comprobaron contra los metadatos de cada respuesta: nombre con mes/año, símbolo, FUTURE, CMX, USD y America/New_York. Esa identidad declarada por el proveedor no prueba por sí sola la atribución de cada barra histórica a operaciones de ese vencimiento. Los primeros intentos/avisos y límites del broker siguen pendientes; `execution_eligible` permanece falso. No se sustituyen por el último día del mes ni por un calendario NYSE.

Se ha corregido `data/cme_micro_gold.json`: antes indicaba liquidación cash, un último día de negociación distinto del derivado y un alias `MGC-ACTIVE` presentado como contrato verificado. Ahora describe la entrega física y marca su identidad como no verificada. Conserva el ID MGC para las regresiones anteriores. Ese registro sigue siendo una referencia heredada, no una identidad suficiente para ejecutar un vencimiento concreto. `first_notice_date: null` es una carencia explícita que impide aceptar el contrato para un experimento con ejecución.

## Regla de rollover elegida

Se adopta `mgc-calendar-before-delivery-v2`, una decisión de investigación del proyecto, no una regla obligatoria de CME. El cambio se programa cinco sesiones CME verificadas antes de la más temprana entre primer intento/posición de entrega, primer aviso, último día de negociación y límite de cierre del broker. Los cuatro límites y el calendario deben estar confirmados. No se usa un cruce de volumen determinado con información futura.

La versión 2 incorpora explícitamente el primer intento: el [aviso CME 26-033 para febrero de 2026](https://www.cmegroup.com/content/dam/cmegroup/notices/clearing/2026/01/chadv26-033.pdf) distingue intento 29/01, aviso 30/01 y último trade 25/02; el [26-192 para junio](https://www.cmegroup.com/content/dam/cmegroup/notices/clearing/2026/05/chadv26-192.pdf) distingue 28/05, 29/05 y 26/06. Los PDF se revisaron mediante extracción web; su descarga directa devolvió 403 y no hay originales locales archivados. Para los contratos candidatos se registran las fechas derivadas de la regla de CME descritas arriba, a la espera de archivar avisos contractuales individuales.

El destino es el siguiente vencimiento listado que tenga identidad de proveedor y precios raw verificados con observaciones solapadas. En la apertura de la sesión programada se cierra el contrato anterior y se abre el nuevo; el calendario se decide antes de esa apertura. Cada ejecución lleva su precio, spread, comisión y deslizamiento. Si falta un límite, una sesión o el precio de cualquiera de las patas, se rechaza el experimento; no se fabrica el fill ni se aplaza silenciosamente el roll hasta la entrega.

`market_catalog.futures_research.require_individual_contract` rechaza aliases, series continuas, datos marcados no elegibles, fechas incompatibles e identidades o límites sin confirmar. `rollover_session` calcula la sesión sobre el calendario revisado suministrado y rechaza calendarios cortos, duplicados o desordenados. No certifica la completitud del calendario ni la autenticidad de los metadatos que aporte el llamador. Sus pruebas usan un calendario sintético; no presentan como verificado un calendario CME real.

## Adquisición pública y comprobación sin red

`data/research/mgc-public-2026-10-22/` conserva las respuestas originales Yahoo, recibos de petición/fecha UTC/checksum, un diagnóstico reconstruible y un manifiesto. Contiene 201 etiquetas observadas por contrato, de 02/01/2026 a 20/10/2026, con 201 fechas comunes. No se han redondeado, rellenado, ajustado ni convertido en barras de apertura de sesión. Los tres históricos se solapan, pero no se ha certificado ningún roll.

Las peticiones de los contratos vencidos MGCG26.CMX y MGCM26.CMX devolvieron HTTP 404; sus cuerpos y recibos también se conservan. Esto limita esta adquisición, sin demostrar que el histórico sea inexistente en cualquier fuente pública.

El diagnóstico comprueba identidad, timestamps únicos/ordenados, fechas dentro de la petición, valores finitos, coherencia OHLC y volumen entero no negativo. Conserva y señala el volumen cero. No deduce los cierres de mercado a partir de fechas ausentes: `expected_sessions_verified` es falso hasta disponer del calendario CME correspondiente. Un cierre diario Yahoo no se denomina settlement oficial ni precio ejecutable.

La carpeta es evidencia de investigación separada de `data/frozen/` y no alimenta los experimentos del motor. El comando de congelación rechaza guardarla bajo `data/frozen/`; las rutas con manifiesto tienen la protección de escritura compartida. Reproducir una descarga puede producir una respuesta revisada por el proveedor; verificar la copia archivada no usa red.

```powershell
.\.venv\Scripts\python.exe -m scripts.mgc_public_data verify
# Adquisición nueva: usar una carpeta que aún no exista.
.\.venv\Scripts\python.exe -m scripts.mgc_public_data download artifacts/downloads/mgc-nueva --start 2026-01-01 --end-exclusive 2026-10-21
.\.venv\Scripts\python.exe -m scripts.mgc_public_data freeze artifacts/downloads/mgc-nueva data/research/mgc-nueva
```

La suite histórica reconstruye el diagnóstico desde los originales y verifica sus hashes. Las pruebas unitarias usan precios sintéticos y detectan identidad incorrecta, corrupción, OHLCV inválido, fechas duplicadas, ventanas incompatibles y la alteración de la elegibilidad del informe. Superarlas no convierte esta muestra en datos admitidos para ejecución.

## Serie continua frente a precios negociables

`MGC=F` y el Parquet heredado `MGC_1d.parquet` no acreditan una cadena de contratos individuales, ni documentan qué vencimiento compone cada barra o cómo cambia el proveedor. El método de descarga ya no los llama datos oficiales de contratos CME. Los resultados anteriores del motor identifican MGC como referencia del proveedor con precios negociables y rollover no verificados.

Para la ampliación, cada barra de ejecución debe identificar contrato, mes, proveedor y procedencia. El P&L y la variación de margen se calculan con los precios raw del contrato efectivamente mantenido y multiplicador 10. El salto de precio entre dos vencimientos no es por sí solo una ganancia o pérdida de la posición: el cambio exige dos operaciones registradas.

La muestra pública ilustra la diferencia entre niveles de contratos: el 21/09/2026 Yahoo mostraba cierre 4350,20 para MGCV26.CMX y 4383,90 para MGCZ26.CMX; ambos tienen la misma fecha de etiqueta y metadatos de contrato distintos. La diferencia observada es 33,70 USD/onza, o 337 USD si se multiplica por las 10 onzas del contrato. Es una diferencia entre cierres diarios de contratos con meses distintos, no una cotización simultánea garantizada, precio de settlement, spread ejecutable ni P&L. Los valores originales, identificadores de petición y checksums están en `data/research/mgc-public-2026-10-22/`; la atribución por barra y los límites temporales del proveedor aún no se certifican.

La primera serie continua de análisis se concatenará **sin ajuste**, guardando contrato por barra y un manifiesto de roll. Solo será una vista de referencia. Si posteriormente se construye una serie ajustada por diferencias o ratios para indicadores, se guardará aparte, con factores y versión; sus niveles no se usarán para fills, stops, liquidación diaria ni atribución de P&L. Una reconstrucción retrospectiva tampoco prueba causalidad de indicadores históricos.

## Puerta de entrada a los experimentos

La identificación del producto, los símbolos públicos, las observaciones de vencimientos y la regla quedan documentadas. El aviso 26-321 confirma las fechas de MGCV26; las de MGCZ26 y MGCG27 continúan derivadas hasta que se publiquen los avisos CME respectivos. Faltan una lista de sesiones versionada con excepciones, verificar la atribución y los límites temporales de las barras/settlements y obtener los límites del broker antes de admitir una cadena para ejecución. Hay cero rolls históricos validados. Además, la fase contable deberá conciliar margen, liquidación diaria y ambas patas del roll. Este trabajo no demuestra todavía un backtest verificado de contratos negociables.
