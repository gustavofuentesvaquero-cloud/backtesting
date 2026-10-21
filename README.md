# Motor de backtesting

Proyecto Python para investigación histórica y simulación: Parquet/DuckDB, backtesting diario, motor de eventos L1, ledger, grabación/replay y exportación a Excel.

El objetivo actual es [verificar las simulaciones y comparar estrategias](docs/plan-avance-backtesting.md). La estrategia diaria y el motor histórico todavía tienen diferencias de reglas y supuestos de ejecución pendientes de corregir. Los resultados actuales sirven como referencia de desarrollo.

## Preparar el entorno en Windows

La raíz del paquete es esta carpeta, donde están `pyproject.toml`, `src` y `tests`. La [decisión de raíz y las referencias Git](docs/raiz-proyecto.md) identifican el punto de partida previo a reorganizar los directorios duplicados. El entorno fijado usa **Python 3.12 de 64 bits**; el paquete declara Python 3.11 o superior, pero las otras versiones aún no se han verificado.

Desde PowerShell:

```powershell
.\scripts\setup.ps1
```

Si Python no está en PATH, indicar su ejecutable:

```powershell
.\scripts\setup.ps1 -Python 'C:\ruta\python.exe'
```

El script crea `.venv`, instala las versiones fijadas y el proyecto en modo editable, verifica los datos congelados y ejecuta las pruebas. No es necesario activar el entorno. `-WithDownloads` añade las dependencias opcionales de descarga; `-SkipChecks` omite la suite, pero mantiene las verificaciones de dependencias e históricos.

La instalación necesita acceso al índice de paquetes. Las pruebas posteriores usan datos locales y bloquean conexiones de red.

## Instalación manual

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m pip check
```

En Linux, crear el entorno con `python3.12 -m venv .venv` y usar `.venv/bin/python`. `requirements.lock` fija las dependencias del núcleo y de construcción; `requirements-download.lock` añade las de descarga. Para actualizar los locks, resolver en un entorno limpio, revisar los cambios y repetir todas las verificaciones; no copiar el entorno global.

Los siguientes comandos suponen que `python` es el del entorno virtual; en Windows puede sustituirse por `.\.venv\Scripts\python.exe`.

## Pruebas y referencia histórica

```console
python -m scripts.check --suite unit
python -m scripts.data_baseline
python -m scripts.check --suite historical
python -m scripts.check --suite all --report artifacts/preparation/checks.json
```

- Las pruebas unitarias usan escenarios sintéticos y directorios temporales. El ejecutor impide abrir un almacén implícito.
- Las regresiones históricas usan copias congeladas en `tests/fixtures/historical`, con hashes, fechas únicas y ordenadas, precios finitos y positivos, coherencia OHLC, volumen e identificador comprobados.
- `data/historical` sigue siendo la caché operativa; actualizarla no cambia las regresiones.
- El manifiesto reconoce que faltan verificar proveedor, ajustes corporativos y completitud del calendario. Congelar datos garantiza una referencia estable, no su autenticidad financiera.
- Los errores y las pruebas omitidas hacen fallar el ejecutor. `python -m unittest discover -s tests -v` descubre solo el grupo unitario; `scripts.check --suite all` cubre ambos.

GitHub Actions está configurado en `.github/workflows/checks.yml`, para Python 3.12 en Windows y Linux. Su ejecución remota requiere guardar y subir el repositorio. Consultar [el estado de preparación](docs/preparacion.md) para la evidencia local.

## Verificar el paquete instalado

```console
python -m scripts.check_wheel
```

Construye el wheel y comprueba la importación y los comandos de catálogo, despliegue y alertas en un entorno limpio fuera del repositorio. Para instalar sus dependencias sin red, guardar previamente los wheels:

```console
python -m pip download --only-binary=:all: -r requirements.lock --dest artifacts/wheelhouse
python -m scripts.check_wheel --wheelhouse artifacts/wheelhouse
```

## Flujos disponibles

```console
python -m market_catalog validate data/market_catalog.json
python -m market_catalog validate data/gold_miners_stocks.json
python -m market_catalog validate-deployment config/deployment.profile.json
python -m market_catalog validate-alerts config/alerts.json
python examples/run_multi_cycle_backtest.py --symbol GOLD --direction long
```

El catálogo inicial `data/market_catalog.json` sigue vacío; los ejemplos usan catálogos específicos. El backtest usa la caché operativa y los supuestos actuales del motor. El adaptador IBKR tiene una cola de quotes y comprobación de socket; la recepción efectiva del protocolo sigue pendiente. El proyecto no envía órdenes a un mercado.

Actualizar el diario con los datos de caché:

```console
python actualizar_diario.py --no-download
```

Este comando **reescribe** `data/diario_trading_oro.xlsx`. Para descargar cotizaciones, instalar el entorno con `-WithDownloads` y ejecutar sin `--no-download`. `actualizar_diario.bat` prioriza `.venv`.

## Documentación técnica

- [Raíz del proyecto y referencia anterior a la reorganización](docs/raiz-proyecto.md)
- [Estado de preparación y siguiente trabajo](docs/preparacion.md)
- [Plan de avance](docs/plan-avance-backtesting.md)
- [Catálogo y convenciones](docs/phase1-market-conventions.md)
- [Adaptadores y calidad](docs/phase2-adapters-and-quality.md)
- [Motor de eventos](docs/phase3-event-engine.md)
- [API de estrategias](docs/phase4-strategy-api.md)
- [Simulación de órdenes](docs/phase5-order-simulation.md)
- [Contabilidad](docs/phase6-portfolio-accounting.md)
- [Grabación y replay](docs/phase7-recording-replay.md)
- [Calidad y rendimiento](docs/phase8-quality-performance.md)
- [Observabilidad y despliegue](docs/phase9-observability-deployment.md)
