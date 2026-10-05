@echo off
rem ============================================================================
rem  Arranque manual de norlex-ia (por si no quieres depender de la app .NET)
rem  Uso: doble clic a este archivo, o desde consola: iniciar-norlex-ia.bat
rem  El servicio queda escuchando en http://127.0.0.1:8099
rem ============================================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [X] Falta el entorno virtual. Instalalo primero con:
    echo      powershell -ExecutionPolicy Bypass -File instalar-windows.ps1
    echo.
    pause
    exit /b 1
)
echo norlex-ia escuchando en http://127.0.0.1:8099  (Ctrl+C para detener)
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8099
