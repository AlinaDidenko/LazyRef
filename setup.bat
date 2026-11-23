@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo LazyRef is ready. Open Start LazyRef.vbs to launch without a console.
pause
exit /b 0
:fail
echo Setup failed. Check Python and your internet connection.
pause
exit /b 1
