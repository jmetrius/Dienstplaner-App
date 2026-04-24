@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENV_DIR=%SCRIPT_DIR%\.venv"
set "REQ_FILE=%SCRIPT_DIR%\requirements.txt"

if not exist "%REQ_FILE%" (
  echo requirements.txt not found at "%REQ_FILE%".
  exit /b 1
)

set "PYTHON_BOOTSTRAP="
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PYTHON_BOOTSTRAP=py -3"
)
if not defined PYTHON_BOOTSTRAP (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python was not found in PATH.
    exit /b 1
  )
  set "PYTHON_BOOTSTRAP=python"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
  if exist "%VENV_DIR%" (
    echo Existing virtual environment looks incomplete. Rebuilding...
    rmdir /s /q "%VENV_DIR%"
  )
  echo Creating virtual environment in "%VENV_DIR%"...
  %PYTHON_BOOTSTRAP% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo Standard venv creation failed. Trying virtualenv fallback...
    %PYTHON_BOOTSTRAP% -m pip install --user virtualenv
    if errorlevel 1 (
      echo Failed to install virtualenv fallback.
      exit /b 1
    )
    %PYTHON_BOOTSTRAP% -m virtualenv "%VENV_DIR%"
    if errorlevel 1 (
      echo Failed to create virtual environment via virtualenv.
      exit /b 1
    )
  )
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
  echo Failed to activate virtual environment.
  exit /b 1
)

python -m pip --version >nul 2>&1
if errorlevel 1 (
  echo pip is unavailable in the virtual environment.
  exit /b 1
)

echo Checking requirements with pip...
set "DRY_RUN_OUTPUT=%TEMP%\dienstplaner_pip_dry_run_%RANDOM%_%RANDOM%.log"
python -m pip install --dry-run -r "%REQ_FILE%" > "%DRY_RUN_OUTPUT%" 2>&1
set "DRY_RUN_EXIT=%ERRORLEVEL%"

if not "%DRY_RUN_EXIT%"=="0" (
  findstr /C:"no such option: --dry-run" "%DRY_RUN_OUTPUT%" >nul 2>&1
  if not errorlevel 1 (
    echo pip does not support --dry-run; installing requirements directly...
    python -m pip install -r "%REQ_FILE%"
    set "INSTALL_EXIT=%ERRORLEVEL%"
    del /q "%DRY_RUN_OUTPUT%" >nul 2>&1
    if not "%INSTALL_EXIT%"=="0" exit /b %INSTALL_EXIT%
    goto :after_requirements
  )
  type "%DRY_RUN_OUTPUT%"
  del /q "%DRY_RUN_OUTPUT%" >nul 2>&1
  exit /b %DRY_RUN_EXIT%
)

findstr /C:"Would install" "%DRY_RUN_OUTPUT%" >nul 2>&1
if not errorlevel 1 (
  echo Installing/updating requirements...
  python -m pip install -r "%REQ_FILE%"
  set "INSTALL_EXIT=%ERRORLEVEL%"
  del /q "%DRY_RUN_OUTPUT%" >nul 2>&1
  if not "%INSTALL_EXIT%"=="0" exit /b %INSTALL_EXIT%
) else (
  echo Requirements already up to date.
  del /q "%DRY_RUN_OUTPUT%" >nul 2>&1
)

:after_requirements

if "%DIENSTPLANER_SKIP_LAUNCH%"=="1" (
  echo Skipping app launch because DIENSTPLANER_SKIP_LAUNCH=1.
  exit /b 0
)

python "%SCRIPT_DIR%\main.py"
exit /b %errorlevel%
