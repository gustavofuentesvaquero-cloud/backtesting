# Descargas y conjuntos congelados

| Ubicación | Papel | Escritura |
|---|---|---|
| `data/historical` | Caché operativa heredada; actualizaciones del diario y ejemplos | Permitida; no se usa como referencia verificada de experimentos |
| `artifacts/downloads/<adquisición>` | Nuevas respuestas del proveedor y recibos de adquisición | Una carpeta nueva por adquisición; el descargador rechaza sobrescribir |
| `data/frozen/nyse-equities-2024-v1` | Muestra aceptada BE/NEM, originales auditables y Parquet | El almacén rechaza escrituras y descargas; el creador rechaza sobrescribir |
| `tests/fixtures/historical` | Cinco regresiones heredadas con procedencia no verificada | El almacén rechaza escrituras y descargas |
| `data/metadata` | Resúmenes derivados de las fuentes aceptadas | Regenerables y contrastados con el manifiesto original |
| `artifacts/<experimento>` | Configuración e informes de una ejecución | Separados de los datos de entrada |

Los originales aceptados dentro de `source/` son copias para auditoría, no una caché que se actualice. Los experimentos verifican su conjunto y proporcionan explícitamente su ruta a `MarketDataStore`. Una actualización del diario en la caché no modifica el conjunto congelado.

`MarketDataStore.save_bars` y sus métodos de descarga comprueban la protección antes de escribir o importar el cliente de red. Rechazan cualquier destino bajo las dos raíces congeladas, así como almacenes con `manifest.json`, incluidos los abiertos antes de crear ese manifiesto. La protección corresponde a la API; no pretende impedir que un editor externo cambie archivos. Los hashes y la comparación con los originales detectan esas alteraciones posteriores.

Los dos creadores (`scripts.data_baseline` y `scripts.equity_dataset`) comparten la validación de ubicaciones: rechazan carpetas de origen y destino anidadas o coincidentes, destinos bajo `data/historical` o `artifacts/downloads` y nuevos conjuntos dentro de una referencia que ya tenga manifiesto. Estas comprobaciones ocurren antes de crear carpetas o leer los datos de entrada. Una muestra nueva necesita otra carpeta y revisión; cambiar un manifiesto para aceptar una descarga nueva no actualiza esta versión silenciosamente. Git conserva datos y contratos aceptados, mientras `artifacts` permanece ignorado.

La caché heredada se conserva en su sitio para que los flujos anteriores sigan siendo reproducibles. No se renombra ni se migra automáticamente el Excel. La separación se verifica con escenarios temporales: actualizar la descarga no cambia la referencia, escribir por la API congelada falla y el verificador detecta una corrupción directa de bytes.

## Comprobar la separación

```console
python -m scripts.check --suite all --report artifacts/preparation/data-separation-tests.json
python -m scripts.data_baseline
python -m scripts.equity_dataset
python -m scripts.equity_dataset --check-metadata data/metadata/nyse-equities-2024-v1.json
```

`tests/unit/test_frozen_storage.py` utiliza solo escenarios temporales. Comprueba lectura permitida y escritura rechazada, bloqueo de nuevas descargas dentro del conjunto, actualización independiente de la caché y detección de un manifiesto creado después de abrir el almacén. Los dos casos de ubicación ejercitan ambos creadores y prueban que los rechazos no crean carpetas ni alteran el manifiesto anterior. La suite completa consta de 157 unitarias y 14 históricas, 171 casos correctos.

Las fuentes y los destinos predeterminados siguen en carpetas distintas. La muestra BE/NEM y las regresiones mantienen sus hashes; no se han movido ni regenerado sus archivos para esta entrega. El informe de pruebas y los logs quedan en `artifacts/preparation/data-separation-tests.json` y `data-separation-tests.log`.
