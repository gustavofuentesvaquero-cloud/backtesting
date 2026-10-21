# Ejecución automática de pruebas

## Control local antes de cada commit

El repositorio utiliza `.githooks/pre-commit`, activado mediante esta configuración local de Git:

```console
git config --local core.hooksPath .githooks
```

El hook selecciona el Python del entorno `.venv` del proyecto y ejecuta `scripts.check_staged`. Esta herramienta copia el contenido preparado en el índice de Git a un directorio temporal y ejecuta allí ambos grupos de pruebas sin red. Si fallan, falta el ejecutor preparado o no se obtiene un informe correcto, devuelve un error al hook y el commit se interrumpe.

Los cambios sin preparar no se incluyen en la copia. Preparar también las pruebas y sus fixtures con `git add` permite comprobar exactamente el estado que se va a registrar. El resultado puede tener un número de casos diferente del árbol de trabajo si quedan modificaciones sin preparar.

Para probar el control sin registrar un commit:

```console
git hook run pre-commit
```

Los resultados se guardan en `artifacts/pre-commit/checks.json` y `checks.log`. El JSON incluye el hash del inventario preparado y confirma que el paquete se importó desde la copia temporal.

La configuración `core.hooksPath` es local a cada clon. En otro clon hay que repetir el comando de activación; en Linux también hay que dar permiso de ejecución al hook:

```console
chmod +x .githooks/pre-commit
```

## GitHub Actions

`.github/workflows/checks.yml` define ejecución en cada `push`, en aperturas o actualizaciones de pull requests y mediante activación manual. No hay filtros de ramas ni de rutas.

La matriz contiene cuatro trabajos independientes con Python 3.12:

| Sistema | Grupo |
|---|---|
| Windows | Unitarias |
| Windows | Integración histórica |
| Linux | Unitarias |
| Linux | Integración histórica |

Cada trabajo instala las versiones de `requirements.lock`, instala el proyecto y ejecuta `pip check`. Las unitarias se ejecutan sin depender de los históricos; la integración verifica primero los cinco archivos congelados. Los trabajos unitarios también construyen y comprueban el wheel instalado.

`fail-fast: false` permite que los otros trabajos terminen aunque uno falle. No se permite ignorar errores. Cada trabajo tiene un límite de 20 minutos y publica sus artefactos incluso si falla, con un nombre distinto para cada combinación de sistema y grupo.

## Estado comprobado

- Hook activado en el repositorio interior elegido como raíz del proyecto y ejecutado correctamente con `git hook run pre-commit`.
- Suite del árbol de trabajo: **121 unitarias y 12 históricas**, **133 casos correctos**. Los tres casos nuevos verifican el comportamiento del control sobre contenido preparado, cambios sin preparar y ausencia del ejecutor en el índice.
- YAML analizado y comprobado: disparadores, matriz de cuatro combinaciones, propagación de fallos y publicación de artefactos.
- El remoto `origin` está conectado a [gustavofuentesvaquero-cloud/backtesting](https://github.com/gustavofuentesvaquero-cloud/backtesting), con rama principal `main`. Cada push dispara el workflow; consultar los resultados de cada commit en [GitHub Actions](https://github.com/gustavofuentesvaquero-cloud/backtesting/actions). La evidencia local anterior no sustituye el resultado remoto.

Los informes locales están en `artifacts/preparation/automation-tests.json`, `automation-tests.log` y `workflow-audit.json`. Las dependencias de validación del YAML se aislaron en `artifacts/ci-validation`, sin modificar los locks del proyecto.

Referencias: [eventos de GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) y [hooks de Git](https://git-scm.com/docs/githooks).
