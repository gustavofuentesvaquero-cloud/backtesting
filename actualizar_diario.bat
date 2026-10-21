@echo off
chcp 65001 > nul
title Actualizador Diario - Trading Mineras de Oro (GOLD / NEM)

:: 1. Fijar el directorio de trabajo al directorio donde se encuentra este archivo .bat
cd /d "%~dp0"

echo ==============================================================================
echo   MOTOR DE BACKTESTING Y SEGUIMIENTO DIARIO DE MINERAS DE ORO
echo   Activos: Barrick Gold (GOLD) y Newmont Corporation (NEM)
echo   Operacion: 100%% Acciones al Contado - Cero Apalancamiento - Cero Rollover
echo ==============================================================================
echo.
echo  Iniciando proceso automatizado de 1 clic...
echo.

:: 2. Detectar comando de Python disponible (python o py -3)
set "PY_CMD=python"
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    py -3 --version >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set "PY_CMD=py -3"
    ) else (
        echo.
        echo ==============================================================================
        echo  [ERROR] No se encontro Python en el sistema ni en la variable PATH.
        echo  Por favor instale Python 3.10+ y asegurese de marcar "Add Python to PATH".
        echo ==============================================================================
        echo.
        pause
        exit /b 1
    )
)

:: 3. Ejecutar el script principal de actualizacion en Python
%PY_CMD% actualizar_diario.py
set "EXIT_CODE=%ERRORLEVEL%"

:: 4. Verificar el codigo de salida
if %EXIT_CODE% NEQ 0 goto :on_error

echo.
echo ==============================================================================
echo  [OK] Actualizacion finalizada con EXITO.
echo  Abriendo el diario de trading en Excel...
echo ==============================================================================
if exist "data\diario_trading_oro.xlsx" (
    start "" "data\diario_trading_oro.xlsx"
) else (
    echo  [AVISO] No se encontro el archivo data\diario_trading_oro.xlsx
)
goto :finish

:on_error
echo.
echo ==============================================================================
echo  [ERROR] Se produjo un error durante la actualizacion [Codigo: %EXIT_CODE%].
echo  Por favor revise los detalles mostrados en la consola.
echo ==============================================================================

:finish
echo.
echo Presione cualquier tecla para cerrar esta ventana...
pause > nul
