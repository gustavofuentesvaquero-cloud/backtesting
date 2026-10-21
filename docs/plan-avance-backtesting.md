# Plan de avance: backtesting fiable y comparación de estrategias

Fecha: 21 de octubre de 2026.

## Objetivo

Obtener un flujo reproducible que permita comparar estrategias sobre los mismos datos, fechas, capital y costes. Cada resultado debe poder explicarse desde sus señales y operaciones, y distinguir el rendimiento utilizado para desarrollar la estrategia del obtenido en periodos reservados para evaluarla.

La primera entrega será una comparación de la estrategia macro actual, una regla sencilla de tendencia y comprar y mantener. El alcance inicial será de acciones con barras diarias; los futuros y los cortos se incorporarán cuando sus reglas específicas de contabilidad, financiación y ejecución estén verificadas. Esta reducción de alcance es una propuesta para acelerar una primera referencia fiable.

## Punto de partida comprobado

Existe almacenamiento Parquet/DuckDB, un motor histórico barra a barra con operaciones y curva de patrimonio, un motor de eventos con simulación L1, ledger y replay, y un flujo diario que exporta a Excel. Hay pruebas y documentación de las fases 1 a 9.

Las principales brechas que condicionan el plan son:

- **Dos implementaciones de las reglas.** `actualizar_diario.py` utiliza `run_stock_macro_backtest`, que calcula niveles al final del histórico y los aplica a las últimas barras. `RollingBacktestEngine` desplaza los indicadores a la sesión anterior. Además, la evaluación diaria incorpora filtros y objetivos que no coinciden con el motor histórico. Véanse `src/market_catalog/miners_strategy.py:589` y `src/market_catalog/backtest_engine.py:114`.
- **Causalidad pendiente de verificar de extremo a extremo.** Las zonas de control se seleccionan usando el cierre de la sesión que se está simulando; las entradas nuevas pueden tomar beneficio con el máximo o mínimo de toda esa sesión sin conocer si ocurrió antes de entrar. Véase `src/market_catalog/backtest_engine.py:383`.
- **Capital y riesgo incompletos.** El tamaño por riesgo usa el capital inicial; el tamaño basado en ATR no usa la distancia al stop realmente aplicado. En las entradas se descuentan comisiones, sin reservar el efectivo necesario para comprar acciones. Hace falta definir el modelo de cuenta y aplicar sus restricciones.
- **Reproducibilidad insuficiente.** La descarga no fija explícitamente la política de ajuste de precios y sustituye los Parquet sin un manifiesto que identifique el conjunto de datos. Varias pruebas dependen del histórico local y de conteos exactos de barras u operaciones.
- **Entorno pendiente de estabilizar.** `pyproject.toml` declara solamente `jsonschema`, aunque el paquete importa también DuckDB, pandas, NumPy y openpyxl. La descarga requiere yfinance. El README describe un estado anterior del proyecto.

Verificación realizada: lectura del código y de las pruebas; intento de ejecución con el Python del sistema y con el runtime disponible. El primero resuelve a un alias inaccesible y el segundo no tiene DuckDB; los ocho módulos de pruebas fallaron al importar. No se ejecutaron sus casos ni se verificó el contenido de los Parquet. Esta revisión no certifica los resultados actuales.

## Fase 1 — Base reproducible

Esfuerzo orientativo: 1–2 días de desarrollo.

1. Elegir una raíz del proyecto y registrar un primer estado en Git antes de reorganizar las carpetas duplicadas.
2. Declarar las dependencias de ejecución y las opcionales de descarga; fijar un entorno reproducible y documentar su instalación.
3. Actualizar el README con los flujos realmente disponibles y sus límites.
4. Separar pruebas unitarias con datos sintéticos de pruebas de integración con históricos congelados. Las pruebas unitarias deben funcionar sin red.
5. Establecer una ejecución automática de la suite en cada cambio.

**Criterio de salida:** en un entorno limpio, una instalación documentada permite importar el paquete y ejecutar las pruebas sin depender de archivos o paquetes no declarados. Los fallos funcionales quedan identificados, y la referencia aceptada queda versionada.

## Fase 2 — Contrato y calidad de datos

Esfuerzo orientativo: 2–3 días.

1. Congelar un conjunto de datos inicial para dos acciones del catálogo, después de verificar su identidad e histórico.
2. Guardar procedencia, identificador del instrumento, símbolo del proveedor, fechas, zona horaria, fecha de descarga, política de ajustes y checksum.
3. Validar fechas únicas y ordenadas, valores finitos, coherencia OHLC, volumen y sesiones esperadas. Diferenciar cierres de mercado de datos ausentes.
4. Decidir explícitamente cómo tratar splits y dividendos, y evitar contabilizarlos dos veces.
5. Mantener separados los datos descargados y los conjuntos congelados usados en experimentos.
6. Para la ampliación a MGC: identificar contratos, vencimientos, regla de rollover y diferencias entre una serie continua y precios negociables.

**Criterio de salida:** cada ejecución identifica exactamente qué datos utilizó; los errores de calidad producen un diagnóstico antes de simular.

## Fase 3 — Ejecución, contabilidad y riesgo verificables

Esfuerzo orientativo: 4–6 días.

1. Escribir el contrato temporal: qué información está disponible al decidir, cuándo se envía la orden y desde cuándo puede ejecutarse.
2. Elegir una política explícita para velas diarias ambiguas. Si entrada y objetivo pueden haberse tocado en distinto orden, usar una hipótesis conservadora o datos de mayor frecuencia; registrar la hipótesis en el resultado.
3. Eliminar el uso del cierre actual para decisiones que se simulan antes de ese cierre.
4. Dimensionar por patrimonio disponible y distancia real entre entrada y stop, incluyendo multiplicador y costes. Aplicar límites de efectivo, exposición y margen según el modelo de cuenta.
5. Verificar comisiones, deslizamiento, gaps, stops, posiciones abiertas al final y conciliación de patrimonio. Incorporar préstamo de valores y financiación antes de comparar cortos.
6. Añadir casos sintéticos calculables a mano y pruebas de causalidad: cambiar datos posteriores a una fecha no puede alterar las decisiones ni el estado hasta esa fecha. Comparar antes del cierre forzado de fin de muestra.

**Criterio de salida:** efectivo, posiciones, costes y patrimonio concilian según la precisión definida; los casos ambiguos siguen una regla documentada y las pruebas del motor detectan anticipación.

## Fase 4 — Estrategias intercambiables y reglas compartidas

Esfuerzo orientativo: 3–4 días.

1. Extraer la lógica de señal del motor histórico mediante un contrato pequeño de estrategia, reutilizando lo útil de la API existente.
2. Versionar parámetros y reglas de la estrategia macro; precisar qué filtros, soportes, objetivos y stops forman parte de ella.
3. Implementar comprar y mantener como referencia y una regla sencilla de tendencia como segunda estrategia.
4. Compartir la lógica de señales entre backtesting y evaluación diaria, con la misma información disponible para cada fecha.
5. Mantener aparte las operaciones reales registradas por el usuario y la cartera simulada. El informe debe identificar qué representa cada posición.

**Criterio de salida:** cambiar de estrategia no exige editar ejecución ni contabilidad; las mismas entradas y parámetros producen las mismas señales en ambos flujos.

## Fase 5 — Comparación y validación temporal

Esfuerzo orientativo: 4–6 días.

1. Crear un ejecutor de experimentos con configuración guardada: estrategia, parámetros, datos, fechas, capital, tamaño, costes y versión del código.
2. Reservar por anticipado periodos de desarrollo, validación y prueba final. Respetar el orden temporal y usar histórico anterior únicamente como calentamiento.
3. Comparar en fechas comunes y con restricciones de capital y costes coherentes. Mostrar también exposición y tiempo en mercado para interpretar las diferencias.
4. Generar retorno neto, CAGR, drawdown, Sharpe, número de operaciones, expectativa, rotación y costes, junto a curvas de patrimonio y registro de operaciones.
5. Evaluar estabilidad por subperiodos, parámetros cercanos y escenarios de mayores costes. Registrar todos los experimentos para evitar seleccionar solo los favorables.
6. Añadir evaluación con ventanas temporales sucesivas cuando el flujo básico esté comprobado. Los parámetros se eligen usando únicamente el pasado de cada ventana.

**Criterio de salida:** una configuración reproduce la tabla y las curvas; el informe separa resultados de desarrollo y evaluación, e identifica muestras con pocas operaciones o supuestos limitantes. No se exige que una estrategia sea rentable para considerar válida la herramienta.

## Fase 6 — Informe y uso cotidiano

Esfuerzo orientativo: 2–3 días.

1. Exponer un comando de comparación y exportar configuración, manifiesto, métricas, curva y operaciones a una carpeta por experimento.
2. Generar un informe legible con comparación de estrategias, resultados por periodo y supuestos.
3. Conectar el diario Excel a los resultados verificados y mostrar fecha del dato y estado de actualización, especialmente cuando se utilice caché.

**Criterio de salida:** otra persona puede ejecutar un experimento documentado y reconstruir sus resultados sin editar código.

## Primer bloque de trabajo

Orden recomendado para las primeras cinco jornadas de desarrollo:

1. Instalación reproducible, dependencias y referencia de pruebas.
2. Conjunto de datos congelado y primer manifiesto de calidad.
3. Contrato temporal y casos de entrada/salida en una vela diaria.
4. Corrección de causalidad y casos de riesgo/capital.
5. Ejecución de referencia guardada, revisión de diferencias y ajuste de las estimaciones restantes.

La primera comparación útil debe esperar a que estén cerradas las reglas de ejecución y contabilidad. El orden de dependencia es: entorno y datos → simulación verificada → estrategias → comparación → informe.

## Ampliaciones posteriores

Una vez establecida la comparación diaria, abordar futuros, cortos y cartera multiactivo con sus reglas específicas. Si se utiliza el motor de eventos para estos experimentos, incluir primero el replay de hechos contables externos: actualmente se registran, pero `replay_session` reproduce los frames de mercado sin reinyectar esos hechos.

La conexión efectiva a IBKR, recuperación tras cortes y optimización del motor de eventos quedan para una entrega posterior. El adaptador actual contiene comprobación de socket e inyección manual de quotes; la recepción del protocolo del proveedor necesita implementación y validación.

El esfuerzo total orientativo es de 16–24 jornadas de desarrollo concentrado, aproximadamente 4–6 semanas con margen de integración. Se revisará tras el primer bloque, al disponer de una suite ejecutable y datos auditados.
