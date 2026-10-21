# Conjunto inicial de acciones: BE y NEM

Congelado el 21 de octubre de 2026 con identificador `nyse-equities-2024-v1`, en `data/frozen/nyse-equities-2024-v1/`. El manifiesto identifica cada archivo mediante SHA-256. El creador rechaza sobrescribir un destino existente y el verificador rechaza archivos ausentes, cambios de contenido o diferencias respecto a los originales del proveedor. `.gitattributes` desactiva la conversión de finales de línea en esta carpeta para conservar los mismos bytes y hashes en Windows y Linux.

## Identidad y alcance

| ID del catálogo | Emisor e instrumento | Mercado | Moneda | Periodo | Barras |
|---|---|---|---|---|---|
| BE | Bloom Energy Corporation, Class A common stock, CIK 0001664703 | NYSE / XNYS | USD | 02/01/2024–31/12/2024 | 252 |
| NEM | Newmont Corporation, common stock, CIK 0001164727 | NYSE / XNYS | USD | 02/01/2024–31/12/2024 | 252 |

Ambos instrumentos ya pertenecen a `data/gold_miners_stocks.json`; BE es una empresa de tecnología energética, aunque figure en ese archivo. Se escoge un año cerrado común para tener una primera muestra completa y acotada, sin convertir esta selección en una recomendación de inversión ni una muestra representativa del mercado.

La identidad de BE se contrastó con la portada del [10-K de 2024 presentado a la SEC](https://www.sec.gov/Archives/edgar/data/1664703/000162828025008747/be-20241231.htm): clase A, símbolo BE y New York Stock Exchange. La identidad de NEM se contrastó con el [comunicado del emisor sobre 2024](https://investors.newmont.com/investors/news-release/news-details/2025/Newmont-Reports-Fourth-Quarter-and-Full-Year-2024-Results-Provides-Full-Year-2025-Guidance/default.aspx), que identifica Newmont y NYSE:NEM. No se confunde con la cotización NEM en ASX. El verificador exige además nombre, símbolo, tipo EQUITY, mercado NYQ, USD y zona America/New_York en los metadatos de Yahoo y comprueba la vigencia y el símbolo YFINANCE del catálogo.

## Procedencia e histórico

Se descargó una respuesta JSON por instrumento del endpoint chart v8 de Yahoo Finance, con intervalo `1d`, inicio inclusivo 2024-01-01, fin exclusivo 2025-01-01, eventos `div,splits` y cierre ajustado incluido. `source/*.receipt.json` conserva la URL exacta y el instante UTC de descarga. El manifiesto fija los hashes de los originales y de los Parquet; `catalog.json` conserva la configuración de catálogo utilizada.

Se comprobaron todas las fechas contra el [calendario oficial NYSE para 2024–2026](https://s2.q4cdn.com/154085107/files/doc_news/NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-Calendar-2023.pdf). `calendar.json` conserva los diez festivos y los cierres anticipados del 3 de julio, 29 de noviembre y 24 de diciembre. Hay 252 sesiones esperadas por acción, sin sesiones ausentes ni adicionales. Los festivos y fines de semana no se clasifican como huecos. No se interpolan precios.

Para ambas series se verifican fechas únicas y ordenadas, precios finitos y positivos, coherencia OHLC, volumen finito, entero y no negativo e identificador del instrumento. Cada ejecución del verificador vuelve a normalizar las respuestas originales y exige igualdad exacta con todas las columnas de los Parquet. Esto permite comprobar que no se sustituyó el cierre ni se cambió un precio al transformar el archivo; los hashes detectan alteraciones del original.

## Fechas, ajustes y eventos

- `session_date` es la fecha local de negociación. `source_timestamp` conserva el timestamp diario original de Yahoo y se exige que corresponda a las 09:30 de Nueva York.
- `date` etiqueta el cierre previsto de la sesión en UTC: 20:00 o 21:00 según el horario estacional, y 17:00 o 18:00 en sesiones reducidas. No indica disponibilidad de la barra en la apertura ni garantiza el instante de publicación del proveedor. Una señal calculada con esta barra debe ejecutarse como pronto en una sesión posterior.
- `open`, `high`, `low` y `close` conservan los valores de cotización suministrados por Yahoo, con su base de ajuste por splits y sin aplicar ajuste por dividendos en esta transformación. No se redondean los flotantes del proveedor.
- `adj_close` conserva por separado el cierre ajustado de Yahoo, únicamente para auditoría. Yahoo explica que su [cierre ajustado incorpora splits y dividendos](https://ca.help.yahoo.com/kb/SLN28256.html). No se sustituye `close` por `adj_close`, ni se reajustan OHLC mediante ese cociente.
- `dividend` conserva el importe por acción en USD y su fecha ex-dividendo comunicada por el proveedor. Para NEM hay cuatro eventos de 0,25 USD: 4 de marzo, 4 de junio, 5 de septiembre y 27 de noviembre. BE no presenta eventos; su 10-K también indica que no había pagado dividendos en efectivo. No son fechas de pago y esta herramienta no abona efectivo en una cartera.
- Yahoo no comunica splits dentro de esta muestra. Si una descarga futura los comunica, el normalizador falla y exige una política explícita antes de crear otro conjunto. Las acciones y dividendos no se deben volver a sumar a una serie de rendimiento total que ya los incorpore.

## Uso y reproducción

Verificación diaria o en un clon nuevo, sin red ni yfinance:

```console
python -m scripts.equity_dataset
python -m scripts.check --suite historical
```

Para inspeccionar el conjunto con el almacén existente, tras verificarlo:

```python
from market_catalog import MarketDataStore
from scripts.equity_dataset import DEFAULT_TARGET, verify

verify()
store = MarketDataStore(base_dir=DEFAULT_TARGET)
try:
    bars = store.load_bars("NEM")
finally:
    store._con.close()
```

No ejecutar métodos de descarga sobre ese almacén. La ruta se proporciona explícitamente: el conjunto no sustituye la caché ni cambia automáticamente los ejemplos o el diario Excel.

Para repetir la adquisición con conexión y conservar una nueva descarga de auditoría:

```console
python -m scripts.equity_dataset --download --downloads artifacts/downloads/nyse-2024-audit
```

El descargador utiliza la biblioteca estándar de Python; yfinance sigue siendo opcional para los flujos anteriores de descarga. No se llama a este comando desde las pruebas o CI. Yahoo puede revisar su histórico y los metadatos actuales; una descarga posterior no garantiza los mismos bytes. Los originales conservados permiten reconstruir los valores congelados sin consultar al proveedor.

El comando de creación usado fue `python -m scripts.equity_dataset --freeze`. Rechaza el destino ya existente. Para otra muestra aceptada se debe revisar el identificador/versionado, periodo y política, y crear otra carpeta; no regenerar silenciosamente el manifiesto de esta versión. Las descargas operativas permanecen en `artifacts/downloads`, ignoradas por Git; las respuestas aceptadas se copian a `source/` dentro del conjunto congelado para permitir su auditoría.

## Evidencia y límites

La suite local tiene **131 unitarias y 14 históricas: 145 casos correctos**. Diez unitarias nuevas generan respuestas sintéticas y prueban rechazo de identidad incorrecta, sesiones ausentes/duplicadas/desordenadas, OHLCV inválido, eventos no admitidos, recibos incorrectos y corrupción de archivos; también comprueban horario estacional, cierres anticipados, separación del cierre ajustado e imposibilidad de sobrescribir el destino. Una alteración del precio también se rechaza cuando se recalculan sus hashes, porque no coincide con la fuente. Dos integraciones verifican los originales, Parquet, identidad y eventos de la muestra real, con red bloqueada. Las 131 unitarias pasan además en una copia sin ningún Parquet ni Excel. Los informes están en `artifacts/preparation/equity-dataset-tests.json` y `equity-unit-without-histories.json`. La instalación y los trabajos históricos de CI ejecutan también el verificador específico.

La comprobación acredita procedencia declarada, identidad, integridad, transformación y completitud de sesiones de este proveedor. No compara todos los precios con un segundo proveedor ni certifica operaciones de bolsa. La muestra es una descarga realizada en 2026, no una captura disponible en tiempo real en 2024; el cierre ajustado puede incorporar eventos posteriores. Los dos instrumentos supervivientes y un año no permiten medir sesgo de supervivencia ni validar una estrategia fuera de muestra.

Los cinco Parquet de `tests/fixtures/historical` siguen siendo regresiones heredadas con procedencia y ajustes desconocidos. No se han renombrado como datos verificados. Tampoco se modificaron `data/historical`, el catálogo operativo o el diario Excel. Antes de comparar rendimientos totales queda por implementar y conciliar en el motor el tratamiento de efectivo por dividendos y la contabilidad prevista en el plan.
