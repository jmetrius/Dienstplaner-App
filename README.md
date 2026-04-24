# Dienstplaner

Simple Python scheduling project with a small SQLite database and solver logic.

## Project files

- `main.py`: entrypoint script.
- `solver.py`: scheduling/solver logic.
- `database.py`: database access helpers.
- `dienstplaner.db`: local SQLite database file.

## Quick start

1. Install dependencies:
   - `pip install -r requirements.txt`
2. Run the application:
   - `python main.py`

## One-command launchers (auto-venv)

- Linux/macOS (bash):
  - `bash run.sh`
- Windows (Command Prompt):
  - `run.bat`

Both launchers:
- Create `.venv` in the project folder if it does not exist.
- Use `python -m pip install -r requirements.txt`; pip skips already satisfied packages.
- Launch `main.py`.

For CI/testing without opening the GUI:
- Linux/macOS: `DIENSTPLANER_SKIP_LAUNCH=1 bash run.sh`
- Windows: `set DIENSTPLANER_SKIP_LAUNCH=1 && run.bat`
