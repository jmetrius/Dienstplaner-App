@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENV_DIR=%SCRIPT_DIR%\.venv"
set "REQ_FILE=%SCRIPT_DIR%\requirements.txt"
set "HASH_FILE=%VENV_DIR%\.requirements.sha256"

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

set "CURRENT_HASH="
for /f "usebackq delims=" %%H in (`python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path(r'%REQ_FILE%').read_bytes()).hexdigest())"`) do set "CURRENT_HASH=%%H"
if not defined CURRENT_HASH (
  echo Failed to compute requirements hash.
  exit /b 1
)

set "INSTALLED_HASH="
if exist "%HASH_FILE%" (
  set /p INSTALLED_HASH=<"%HASH_FILE%"
)

if /I not "%CURRENT_HASH%"=="%INSTALLED_HASH%" (
  echo Installing/updating requirements...
  python -m pip install -r "%REQ_FILE%"
  if errorlevel 1 exit /b 1
  > "%HASH_FILE%" echo %CURRENT_HASH%
) else (
  echo Requirements already up to date.
)

if "%DIENSTPLANER_SKIP_LAUNCH%"=="1" (
  echo Skipping app launch because DIENSTPLANER_SKIP_LAUNCH=1.
  exit /b 0
)

python "%SCRIPT_DIR%\main.py"
exit /b %errorlevel%
