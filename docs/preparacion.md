# Preparativos implementados

Fecha: 21 de octubre de 2026.

## Alcance

Esta entrega prepara la instalación, las pruebas y la referencia de datos necesarias para ejecutar el plan de avance. Las correcciones del modelo de ejecución y la comparación de estrategias siguen siendo el siguiente bloque de trabajo.

Se utiliza como raíz del paquete la carpeta superior a `docs`, que contiene `pyproject.toml`, `src`, `tests` y el repositorio Git interior. No se han reorganizado las carpetas del proyecto.

## Entregables

| Preparativo | Ubicación | Comprobación |
|---|---|---|
| Dependencias de ejecución completas y descarga opcional | `pyproject.toml` | Instalación editable, importación y `pip check` |
| Versiones fijadas para Python 3.12 | `requirements.lock`, `requirements-download.lock` | Núcleo instalado en entorno limpio; importación de yfinance opcional |
| Preparación de Windows | `scripts/setup.ps1` | Instalación y ejecución de ambos grupos de pruebas |
| Lanzador diario con prioridad para `.venv` | `actualizar_diario.bat` | Revisión del flujo de selección de Python; no se ejecutó el diario |
| Ejecutor sin red, con informe JSON | `scripts/check.py` | Pruebas unitarias e históricas; falla ante errores, grupos vacíos o casos omitidos |
| Casos sintéticos del motor | `tests/test_backtest_engine.py` | Operación calculada a mano, costes, gap en stop, doble toque, calentamiento, repetibilidad e invariancia de un prefijo sin zonas |
| Históricos de regresión independientes de la caché | `tests/fixtures/historical` | Cinco Parquet copiados sin cambios, manifiesto y SHA-256 |
| Congelación y verificación de históricos | `scripts/data_baseline.py` | Casos de manipulación, archivos ausentes, datos inválidos y rechazo de sobrescritura |
| Regresiones históricas separadas | `tests/integration` | Mismos casos existentes, aplicados a copias congeladas |
| Verificación de instalación del wheel | `scripts/check_wheel.py` | Entorno limpio fuera del repositorio, importación y validaciones de catálogo, despliegue y alertas |
| Automatización de verificaciones | `.github/workflows/checks.yml` | Configurada para Windows/Linux con Python 3.12; ejecución remota pendiente |
| Guía de arranque actualizada | `README.md` | Comandos de preparación, verificaciones y flujos disponibles |

## Evidencia local

- Antes de separar las pruebas, la suite original ejecutó **113 casos**, todos correctos.
- La referencia actual ejecuta **114 casos unitarios y 12 históricos**, **126 en total**, sin errores ni casos omitidos.
- La instalación del wheel se comprobó en otro entorno virtual sin acceso al código fuente por `PYTHONPATH`, usando dependencias locales y sin descargar cotizaciones. Esta comprobación motivó corregir la búsqueda del esquema de despliegue en una instalación normal.
- Los cinco históricos congelados pasan los controles estructurales de fechas, OHLC, volumen e identificador. Se registra una tolerancia numérica de `1e-10` absoluta y relativa para diferencias de coma flotante en los límites OHLC.
- `pip check` no detecta dependencias incompatibles y la dependencia opcional yfinance se importa correctamente. No se ha comprobado su servicio remoto.
- El comando `scripts/setup.ps1` pasa tanto sobre el entorno existente como en una copia independiente con un entorno nuevo, instalando el núcleo desde wheels locales y ejecutando los 126 casos.
- Los archivos originales de `data`, incluido el diario Excel, conservan sus hashes iniciales.

Los informes y logs locales están en `artifacts/preparation/`: `original-tests.log`, `checks.json`, `checks.log`, `setup.log`, `setup-clean.log`, `wheel-check.json` y `wheel-check.log`. Son artefactos locales ignorados por Git y se regeneran al ejecutar las herramientas.

Antes de editar se guardaron 86 archivos originales en `artifacts/preparation/before-preparation.zip`, acompañados por `before-preparation.sha256.json`. Esa copia verificada se registró como primer commit local del repositorio interior: `a94f6b6` (`chore: snapshot imported backtesting project before preparation`). El estado preparado se guarda en el commit identificado por la etiqueta local `before-directory-reorganization`; la [decisión de raíz](raiz-proyecto.md) explica cómo comprobarlo. El registro es local, sin publicación del repositorio ni de los datos.

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

Los tests de regresión no prueban estos requisitos completos. El caso de invariancia nuevo cubre un escenario concreto sin zonas de control. La procedencia financiera, las acciones corporativas, los calendarios y el rollover de futuros todavía requieren verificación antes de aceptar los datos para investigación comparativa.
