@echo off
REM Y360 Admin local stack: PostgreSQL + mock Yandex 360 API + NiceGUI console.
REM No autostart - run this file when needed. Stop with stop-stack.bat.
setlocal
set HERE=%~dp0
set P=%HERE%..
cd /d "%P%"

if not exist "%P%\venv\Scripts\python.exe" (
  echo [ERROR] venv not found: %P%\venv. Create it first: python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
  exit /b 1
)

echo [1/3] PostgreSQL (127.0.0.1:5432)...
"%P%\tools\pg17\pgsql\bin\pg_ctl" -D "%P%\data\pgdata" -l "%P%\data\pgdata\server.log" -o "-p 5432" status >nul 2>&1
if errorlevel 1 "%P%\tools\pg17\pgsql\bin\pg_ctl" -D "%P%\data\pgdata" -l "%P%\data\pgdata\server.log" -o "-p 5432" start

echo [1b/3] Database schema (idempotent)...
"%P%\venv\Scripts\python.exe" "%P%\db\init_db.py"

echo [2/3] Mock Yandex 360 API (http://127.0.0.1:8600)...
start "mock-yandex360" /min cmd /c "cd /d "%P%" && "%P%\venv\Scripts\python.exe" "%P%\mocks\mock_yandex360.py" --port 8600"

echo [3/3] Y360 Admin console (open http://127.0.0.1:8080)...
start "y360-admin" /min cmd /c "cd /d "%P%" && "%P%\venv\Scripts\python.exe" "%P%\web\main.py""

echo.
echo Ready. Console: http://127.0.0.1:8080
echo Stop: stop-stack.bat
endlocal
