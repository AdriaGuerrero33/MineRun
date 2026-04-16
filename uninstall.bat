@echo off
cd /d "%~dp0"
title MineRun Agent – Desinstalacion

echo.
echo  Eliminando el agente MineRun...
echo.

:: Detener proceso si esta corriendo
taskkill /f /im pythonw.exe /fi "WINDOWTITLE eq MineRun Agent" >nul 2>&1

:: Borrar tarea del Programador de tareas
schtasks /delete /tn "MineRunAgent" /f >nul 2>&1
if %errorlevel% equ 0 (
    echo  ✓ Tarea eliminada del Programador de tareas
) else (
    echo  La tarea no existia o ya habia sido eliminada
)

echo.
echo  ✓ Desinstalacion completada. El agente ya no arrancara con Windows.
echo.
pause
