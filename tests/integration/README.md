# Pruebas históricas

Estos casos usan exclusivamente los Parquet congelados de `tests/fixtures/historical`, comprobados contra su manifiesto antes de abrirlos. No leen `data/historical` ni descargan cotizaciones.

Ejecutar desde la raíz del paquete:

```console
python -m scripts.check --suite historical
```

La carpeta no tiene `__init__.py` para mantenerla fuera del descubrimiento unitario `python -m unittest discover -s tests`. El comando `python -m scripts.check --suite all` ejecuta ambos grupos y bloquea conexiones de red.

Los conteos y precios concretos de estos casos son regresiones del conjunto congelado. No certifican procedencia de mercado, reglas intradía ni ausencia completa de anticipación. Véase el plan de avance.
