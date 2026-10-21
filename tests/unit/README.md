# Pruebas unitarias sin red

Estos casos generan sus propias barras y eventos, usan fixtures sintéticos o crean archivos temporales. No utilizan los Parquet operativos ni los históricos congelados de integración.

Desde la raíz del proyecto:

```console
python -m scripts.check --suite unit --report artifacts/unit-checks.json
```

El ejecutor bloquea conexiones, DNS y envíos UDP mediante las APIs de Python, además de los métodos de descarga del almacén. También rechaza `MarketDataStore()` sin ruta y los almacenes que apuntan a `data` o `tests/fixtures/historical`. Las protecciones cubren el descubrimiento y la ejecución de casos.

`test_isolation.py` comprueba que intentos de conexión, resolución de nombres, descarga y apertura de históricos fallan antes del acceso. Los almacenes temporales con datos sintéticos siguen funcionando.

Las regresiones con precios históricos están en `tests/integration` y se ejecutan con `--suite historical`. Los datos de los catálogos JSON y los esquemas siguen disponibles como configuración de prueba; las series de precios se generan dentro de los casos unitarios.
