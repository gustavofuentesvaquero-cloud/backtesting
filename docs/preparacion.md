# Preparativos implementados

Fecha: 21 de octubre de 2026.

## Alcance

Esta entrega prepara la instalación, las pruebas y la referencia de datos necesarias para ejecutar el plan de avance. Las correcciones del modelo de ejecución y la comparación de estrategias siguen siendo el siguiente bloque de trabajo.

Se utiliza como raíz del paquete la carpeta superior a `docs`, que contiene `pyproject.toml`, `src`, `tests` y el repositorio Git interior. No se han reorganizado las carpetas del proyecto.

## Entregables

| Preparativo | Ubicación | Comprobación |
|---|---|---|
| Dependencias de ejecución completas y descarga opcional | `pyproject.toml` | Instalación editable, importación y `pip check` |
| Versiones fijadas para Python 3.12 | `requirements.lock`, `requirements-download.lock` | Núcleo y variante de descarga instalados en entornos limpios, con versiones exactas verificadas |
| Preparación de Windows | `scripts/setup.ps1` | Instalación y ejecución de ambos grupos de pruebas |
| Lanzador diario con prioridad para `.venv` | `actualizar_diario.bat` | Revisión del flujo de selección de Python; no se ejecutó el diario |
| Ejecutor sin red, con informe JSON | `scripts/check.py` | Pruebas unitarias e históricas; falla ante errores, grupos vacíos o casos omitidos |
| Casos sintéticos del motor | `tests/unit/test_backtest_engine.py` | Operación calculada a mano, costes, gap en stop, doble toque, calentamiento, repetibilidad e invariancia de un prefijo sin zonas |
| Históricos de regresión independientes de la caché | `tests/fixtures/historical` | Cinco Parquet copiados sin cambios, manifiesto y SHA-256 |
| Congelación y verificación de históricos | `scripts/data_baseline.py` | Casos de manipulación, archivos ausentes, datos inválidos y rechazo de sobrescritura |
| Conjunto inicial con identidad y procedencia verificadas | `data/frozen/nyse-equities-2024-v1`, `scripts/equity_dataset.py` | BE y NEM, 252 sesiones de 2024 por acción; originales Yahoo, calendario NYSE y comparación exacta con Parquet; véase [evidencia](datos-iniciales-acciones.md) |
| Regresiones históricas separadas | `tests/integration` | Mismos casos existentes, aplicados a copias congeladas |
| Verificación de instalación del wheel | `scripts/check_wheel.py` | Entorno limpio fuera del repositorio, importación y validaciones de catálogo, despliegue y alertas |
| Automatización de verificaciones | `.githooks/pre-commit`, `scripts/check_staged.py`, `.github/workflows/checks.yml` | Hook local activo y probado; cuatro trabajos CI configurados para Windows/Linux; remoto `origin` conectado a GitHub |
| Guía de arranque actualizada | `README.md` | Comandos de preparación, verificaciones y flujos disponibles |

## Evidencia local

- Antes de separar las pruebas, la suite original ejecutó **113 casos**, todos correctos.
- La referencia actual ejecuta **155 casos unitarios y 14 históricos**, **169 en total**, sin errores ni casos omitidos. Incluye cuatro casos de aislamiento, tres del control de commits preparados y veintiún casos del conjunto inicial identificado, sus metadatos y calidad diaria.
- La instalación del wheel se comprobó en otro entorno virtual sin acceso al código fuente por `PYTHONPATH`, usando dependencias locales y sin descargar cotizaciones. Esta comprobación motivó corregir la búsqueda del esquema de despliegue en una instalación normal.
- Los cinco históricos congelados pasan los controles estructurales de fechas, OHLC, volumen e identificador. Se registra una tolerancia numérica de `1e-10` absoluta y relativa para diferencias de coma flotante en los límites OHLC.
- `pip check` no detecta dependencias incompatibles. Se verificaron en entornos limpios los 18 paquetes del núcleo y los 35 de la variante de descarga, comprobando cada versión frente a su lock. Los paquetes compartidos tienen versiones idénticas en ambos archivos. yfinance se importa correctamente; no se ha comprobado su servicio remoto.
- `scripts/setup.ps1 -WithDownloads -SkipChecks` instala explícitamente el extra `download`, verifica las dependencias y los históricos congelados. Se comprobó utilizando solo los wheels locales.
- En la comprobación anterior a esta separación, `scripts/setup.ps1` pasó tanto sobre el entorno existente como en una copia independiente con un entorno nuevo, instalando el núcleo desde wheels locales y ejecutando los 126 casos de esa referencia.
- Las 118 pruebas unitarias también pasaron en una copia aislada del código que no contenía Parquet históricos ni el diario Excel, manteniendo bloqueadas las APIs de red y los métodos de descarga.
- Los archivos originales de `data`, incluido el diario Excel, conservan sus hashes iniciales.

Los informes y logs locales están en `artifacts/preparation/`: `original-tests.log`, `checks.json`, `checks.log`, `setup.log`, `setup-clean.log`, `wheel-check.json` y `wheel-check.log`. Son artefactos locales ignorados por Git y se regeneran al ejecutar las herramientas.

La verificación ampliada de dependencias añade `dependency-lock-audit.json`, `wheel-check-download.json`, `wheel-check-download.log` y `setup-download.log`. `scripts.check_wheel --with-downloads` repite la instalación limpia del extra; ambos modos comprueban todas las versiones fijadas y ejecutan `pip check`.

La separación explícita utiliza `tests/unit` y `tests/integration`. `scripts.check` descubre solo el grupo seleccionado; CI ejecuta cada grupo en un trabajo independiente para Windows y Linux. Los resultados de la entrega de separación están en `test-separation.json` y `unit-without-histories.json`, con sus logs. Las regresiones de integración siguen utilizando los cinco archivos congelados y comprobados de `tests/fixtures/historical`.

La [automatización](automatizacion-pruebas.md) añade un hook local verificado y los informes `automation-tests.json` y `workflow-audit.json`. Las pruebas de integración y las unitarias conservan trabajos independientes aunque falle otra combinación de la matriz. GitHub Actions está configurado y el remoto `origin` apunta a `https://github.com/gustavofuentesvaquero-cloud/backtesting.git`. Los resultados remotos se consultan en la pestaña Actions del repositorio.

Antes de editar se guardaron 86 archivos originales en `artifacts/preparation/before-preparation.zip`, acompañados por `before-preparation.sha256.json`. Esa copia verificada se registró como primer commit local del repositorio interior: `a94f6b6` (`chore: snapshot imported backtesting project before preparation`). El estado preparado se guarda en el commit identificado por la etiqueta `before-directory-reorganization`; la [decisión de raíz](raiz-proyecto.md) explica cómo comprobarlo. El historial y esa etiqueta se publicaron posteriormente al conectar el repositorio GitHub indicado por el usuario.

## Repetir la verificación

Desde la raíz del paquete, con el entorno preparado:

```powershell
.\scripts\setup.ps1
.\.venv\Scripts\python.exe -m scripts.check --suite all --report artifacts/preparation/checks.json
.\.venv\Scripts\python.exe -m scripts.check_wheel --wheelhouse artifacts/wheelhouse
```

El último comando requiere los wheels locales. Si no están disponibles, ejecutar `python -m scripts.check_wheel` para instalar sus dependencias desde el índice. El README explica cómo guardarlos previamente.

Para cambiar una referencia histórica, crear otra carpeta con `scripts.data_baseline --create --source ... --target ...`, revisar la procedencia y los resultados, y versionar datos y manifiesto juntos. La herramienta rechaza sobrescribir una carpeta de referencia existente.

## Próximo bloque: verificar el modelo histórico

1. Definir qué información se conoce al enviar la orden y desde qué instante se permite ejecutarla.
2. Crear un caso donde el máximo de una vela alcance el objetivo antes de que pueda tocarse el límite de entrada. Establecer una política conservadora o exigir datos de mayor frecuencia.
3. Comprobar y corregir la selección de zonas de control para que una decisión simulada en la apertura no dependa del cierre de ese día.
4. Hacer coincidir el tamaño de la posición con el patrimonio y la distancia real al stop; aplicar el capital disponible y los límites de exposición.
5. Compartir las reglas de señal entre motor histórico y seguimiento diario.
6. Después de cerrar esos puntos, ejecutar comparaciones con fechas y costes comunes y periodos reservados para evaluación.

Los tests de regresión no prueban estos requisitos completos. El caso de invariancia nuevo cubre un escenario concreto sin zonas de control. BE y NEM en 2024 disponen ya de identidad, procedencia y sesiones verificadas según el [contrato del conjunto inicial](datos-iniciales-acciones.md). La contabilidad de acciones corporativas en el motor, las demás series y el rollover de futuros siguen pendientes antes de la investigación comparativa.

La entrega de política de datos añade [acciones corporativas](acciones-corporativas.md), [protección de referencias congeladas](separacion-datos.md) y la [especificación para ampliar a MGC](ampliacion-mgc.md). El informe local es `artifacts/preparation/data-policy-tests.json`. Los datos de precios congelados y sus metadatos conservan sus bytes; se corrige la referencia operativa de MGC para describir su entrega física y la identidad todavía no verificada. Las fechas de primer aviso y los identificadores nativos siguen pendientes para experimentar con contratos individuales.
