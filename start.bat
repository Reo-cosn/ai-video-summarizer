@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---- Locate Python (PATH first, then common conda locations) ----
set "PY=python"
where python >nul 2>nul && goto gotpy
if exist "%LOCALAPPDATA%\miniconda3\python.exe" (
  set "PY=%LOCALAPPDATA%\miniconda3\python.exe"
  goto gotpy
)
if exist "%USERPROFILE%\miniconda3\python.exe" (
  set "PY=%USERPROFILE%\miniconda3\python.exe"
  goto gotpy
)
goto nopython

:gotpy

rem ---- First run: create venv and install dependencies ----
if exist .venv goto run
echo [1/2] First run: creating virtual environment...
%PY% -m venv .venv
if errorlevel 1 goto venvfail
echo [2/2] Installing dependencies (takes 2-3 minutes)...
.venv\Scripts\python -m pip install -r requirements.txt --default-timeout 120
if errorlevel 1 goto pipfail

:run
echo.
echo Starting AI video summarizer...
echo   URL    : http://127.0.0.1:7860 (browser opens automatically)
echo   Loading: Gradio takes about 5-10 seconds to import. Please wait...
echo.
set PYTHONIOENCODING=utf-8
set GRADIO_ANALYTICS_ENABLED=False
.venv\Scripts\python app.py 2> launch_error.log
if errorlevel 1 goto appfail
goto end

:nopython
echo.
echo [ERROR] Python not found. Please install Python 3.10+ from https://www.python.org
pause
exit /b 1

:venvfail
echo.
echo [ERROR] Failed to create virtual environment.
pause
exit /b 1

:pipfail
echo.
echo [ERROR] pip install failed. Try China mirror manually:
echo    .venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pause
exit /b 1

:appfail
echo.
echo [ERROR] app.py exited with an error, details below:
type launch_error.log
pause
exit /b 1

:end
