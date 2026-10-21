# Raíz del proyecto y referencia anterior a la reorganización

Decisión registrada el 21 de octubre de 2026.

## Raíz elegida

La raíz operativa del paquete y de su historial Git es:

```text
C:\Users\gusta\OneDrive\Documents\ChatGPT\motor backtesting\motor de backtesting oro\motor de backtesting oro
```

Esta carpeta reúne `pyproject.toml`, `src`, `tests`, `data`, `scripts` y el repositorio Git que ya contiene la referencia original del proyecto. Todos los comandos de instalación, pruebas y control de versiones se ejecutan desde ella.

El directorio exterior `motor backtesting` contiene otro repositorio Git sin commits y un README que apunta a esta raíz. Se mantiene como carpeta contenedora. Las carpetas con nombres duplicados conservan su ubicación durante este paso.

Desde la carpeta exterior, seleccionar la raíz con PowerShell:

```powershell
Set-Location -LiteralPath '.\motor de backtesting oro\motor de backtesting oro'
git rev-parse --show-toplevel
```

## Estados registrados

1. **Estado original:** commit `a94f6b65436adbce272398a1ace1c6c47f82268b`, con los 86 archivos originales anteriores a los preparativos. Se contrastó con la copia inicial y su manifiesto de hashes.
2. **Estado preparado:** commit identificado por la etiqueta local `before-directory-reorganization`. Incluye dependencias fijadas, herramientas de preparación, pruebas separadas, históricos de regresión congelados y esta decisión de raíz.

La etiqueta señala el punto de recuperación previo a cualquier traslado. Su historial conserva también el estado original. El entorno `.venv`, los paquetes descargados y los artefactos de verificación permanecen como archivos locales ignorados; el código, la configuración y los datos de referencia necesarios para reconstruir el entorno están versionados.

Comprobar las referencias sin modificar archivos:

```console
git log --oneline --decorate -2
git show --stat before-directory-reorganization
git status --short
```

## Verificación del estado preparado

- Ejecución de las 126 pruebas: 114 unitarias y 12 históricas.
- Verificación de los históricos congelados frente a su manifiesto.
- Revisión de diferencias y ausencia de cambios en los datos operativos y el Excel original.
- Confirmación de que el commit preparado contiene los archivos de trabajo previstos y de que el árbol de trabajo queda limpio.

Estos controles delimitan el punto de partida para la reorganización. Al cambiar de ubicación el paquete habrá que volver a comprobar rutas y lanzadores y recrear el entorno virtual en su ubicación definitiva.
