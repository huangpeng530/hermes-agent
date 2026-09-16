@echo off
rem weixin_watchdog_launcher.bat - runs the weixin watchdog, logs to weixin\watchdog.stdout.log
rem HERMES_HOME may be exported by the gateway launcher; default fallback below.
setlocal
if "%HERMES_HOME%"=="" set "HERMES_HOME=E:\BACK-AI\Hermes-win"
set "PY=%HERMES_HOME%\hermes-agent\venv\Scripts\python.exe"
set "WD=%HERMES_HOME%\hermes-agent\tools\windows-weixin\weixin_watchdog.py"
set "OUT=%HERMES_HOME%\weixin"
if not exist "%OUT%" mkdir "%OUT%"
"%PY%" "%WD%" >> "%OUT%\watchdog.stdout.log" 2>&1
endlocal
exit /b 0
