@echo off
:: ═══════════════════════════════════════════════════════════════════
::  MineRun Agent – Instalador para Windows
::  Doble clic para instalar. El agente arrancará solo con Windows.
:: ═══════════════════════════════════════════════════════════════════
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title MineRun Agent – Instalación

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   MineRun Agent  –  Instalador       ║
echo  ╚══════════════════════════════════════╝
echo.

:: ── 1. Comprobar Python ──────────────────────────────────────────────────────
echo [1/4] Comprobando Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Python no encontrado.
    echo  Descargalo desde https://www.python.org/downloads/
    echo  Marca la casilla "Add Python to PATH" al instalarlo.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  ✓ %%v encontrado

:: ── 2. Instalar dependencias ─────────────────────────────────────────────────
echo.
echo [2/4] Instalando dependencias...
python -m pip install --quiet -r requirements.txt
if %errorlevel% neq 0 (
    echo  ERROR instalando dependencias.
    pause
    exit /b 1
)
echo  ✓ Dependencias instaladas

:: ── 3. Verificar .env y credentials.json ────────────────────────────────────
echo.
echo [3/4] Comprobando archivos de configuracion...

if not exist ".env" (
    echo  ERROR: Falta el archivo .env
    echo  Copia .env.example a .env y rellena tus credenciales.
    pause
    exit /b 1
)
echo  ✓ .env encontrado

if not exist "credentials.json" (
    echo  ERROR: Falta credentials.json (clave de Google)
    pause
    exit /b 1
)
echo  ✓ credentials.json encontrado

:: ── 4. Registrar en el Programador de tareas de Windows ─────────────────────
echo.
echo [4/4] Registrando el agente en el Programador de tareas...

:: Obtener ruta absoluta de python y del script
for /f "tokens=*" %%p in ('where pythonw 2^>nul') do set PYTHONW=%%p
if "!PYTHONW!"=="" (
    for /f "tokens=*" %%p in ('where python') do set PYTHONW=%%p
)
set SCRIPT="%~dp0agent.py"
set TASK_NAME=MineRunAgent

:: Borrar tarea antigua si existe
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: Crear tarea: arranca con Windows (al iniciar sesión) y se repite cada 24h
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"!PYTHONW!\" %SCRIPT%" ^
  /sc onlogon ^
  /ru "%USERNAME%" ^
  /f >nul 2>&1

if %errorlevel% neq 0 (
    echo  ERROR al registrar la tarea. Prueba a ejecutar como Administrador.
    pause
    exit /b 1
)
echo  ✓ Tarea "%TASK_NAME%" registrada (arranca con Windows)

:: ── Lanzar el agente ahora mismo ────────────────────────────────────────────
echo.
echo  Arrancando el agente ahora...
start "MineRun Agent" /min pythonw "%~dp0agent.py"

echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║  ✓  Instalacion completada                           ║
echo  ║                                                      ║
echo  ║  El agente ya esta corriendo en segundo plano.       ║
echo  ║  Se reiniciara automaticamente cada vez que          ║
echo  ║  enciendas el ordenador.                             ║
echo  ║                                                      ║
echo  ║  Log:       agent.log  (en esta carpeta)             ║
echo  ║  Desinstalar: uninstall.bat                          ║
echo  ╚══════════════════════════════════════════════════════╝
echo.
pause
