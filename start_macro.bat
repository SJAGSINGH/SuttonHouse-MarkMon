@echo off
setlocal

REM ==========================================
REM Sutton House Macro — Local Start (Gunicorn)
REM ==========================================

REM Move to project root (this .bat location)
cd /d "%~dp0"

REM Optional: activate virtual environment
if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

REM Local fallback if PORT not set
if "%PORT%"=="" (
    set PORT=5000
)

echo.
echo Starting Sutton House Macro on port %PORT%
echo.

gunicorn ^
  -k gthread ^
  -w 1 ^
  --threads 20 ^
  --timeout 120 ^
  --graceful-timeout 30 ^
  --keep-alive 5 ^
  --bind 0.0.0.0:%PORT% ^
  app:app

echo.
echo Macro server stopped.
pause
endlocal
