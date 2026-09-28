@echo off
REM Stop Y360 Admin local stack: console, mock API and PostgreSQL.
REM Data files and databases are NOT deleted.
setlocal
set HERE=%~dp0
set P=%HERE%..
cd /d "%P%"

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mock_yandex360\.py|y360-admin[\\/]venv[\\/]' } | ForEach-Object { Write-Host ('stop PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"

"%P%\tools\pg17\pgsql\bin\pg_ctl" -D "%P%\data\pgdata" stop -m fast
echo Stack stopped.
endlocal
