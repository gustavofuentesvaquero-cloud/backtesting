@echo off
chcp 65001 > nul
title Actualizador Diario - Oro y Acciones

:: 1. Fijar el directorio de trabajo al directorio donde se encuentra este archivo .bat
cd /d "%~dp0"

echo ==============================================================================
echo   MOTOR DE BACKTESTING Y SEGUIMIENTO DIARIO DE MINERAS DE ORO
echo   Activos configurados: GOLD, NEM, MGC y BE
echo   Seguimiento y simulacion con las reglas actuales del motor
echo ==============================================================================
echo.
echo  Iniciando proceso automatizado de 1 clic...
echo.

:: 2. Priorizar el entorno virtual preparado para este proyecto.
if exist ".venv\Scripts\python.exe" goto :use_venv
set "PY_CMD=python"
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_python
set "PY_CMD=py -3"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto :run_python
echo [ERROR] Ejecuta scripts\setup.ps1 con Python 3.12 para preparar el entorno.
pause
exit /b 1

:use_venv
set "PY_CMD=.venv\Scripts\python.exe"

:run_python

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
