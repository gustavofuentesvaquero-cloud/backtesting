# Pruebas históricas

Estos casos usan exclusivamente los Parquet congelados de `tests/fixtures/historical`, comprobados contra su manifiesto antes de abrirlos. No leen `data/historical` ni descargan cotizaciones.

Ejecutar desde la raíz del paquete:

```console
python -m scripts.check --suite historical
```

El descubrimiento selecciona explícitamente `tests/unit` o `tests/integration`; ambas carpetas son paquetes de pruebas. `python -m scripts.check --suite unit` no descubre estas regresiones. El comando `python -m scripts.check --suite all` ejecuta ambos grupos y bloquea conexiones, DNS y los métodos de descarga del almacén.

Los conteos y precios concretos de estos casos son regresiones del conjunto congelado. No certifican procedencia de mercado, reglas intradía ni ausencia completa de anticipación. Véase el plan de avance.
