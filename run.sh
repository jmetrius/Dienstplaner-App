#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_ACTIVATE="${VENV_DIR}/bin/activate"

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "requirements.txt not found at ${REQ_FILE}" >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python was not found in PATH." >&2
  exit 1
fi

if [[ -d "${VENV_DIR}" && ( ! -x "${VENV_PYTHON}" || ! -f "${VENV_ACTIVATE}" ) ]]; then
  echo "Detected incomplete virtual environment. Recreating ${VENV_DIR}..."
  rm -rf "${VENV_DIR}"
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment in ${VENV_DIR}..."
  if ! "${PYTHON_BIN}" -m venv "${VENV_DIR}"; then
    echo "Built-in venv failed; trying virtualenv fallback..."
    if ! "${PYTHON_BIN}" -m pip install --user virtualenv; then
      echo "Failed to install virtualenv fallback." >&2
      exit 1
    fi
    "${PYTHON_BIN}" -m virtualenv "${VENV_DIR}"
  fi
fi

# shellcheck disable=SC1091
source "${VENV_ACTIVATE}"

if ! python -m pip --version >/dev/null 2>&1; then
  echo "pip is unavailable in the virtual environment." >&2
  exit 1
fi

echo "Checking requirements with pip..."
set +e
dry_run_output="$(python -m pip install --dry-run -r "${REQ_FILE}" 2>&1)"
dry_run_exit=$?
set -e

if [[ ${dry_run_exit} -ne 0 ]]; then
  if [[ "${dry_run_output}" == *"no such option: --dry-run"* ]]; then
    echo "pip does not support --dry-run; installing requirements directly..."
    python -m pip install -r "${REQ_FILE}"
  else
    printf '%s\n' "${dry_run_output}" >&2
    exit ${dry_run_exit}
  fi
elif [[ "${dry_run_output}" == *"Would install"* ]]; then
  echo "Installing/updating requirements..."
  python -m pip install -r "${REQ_FILE}"
else
  echo "Requirements already up to date."
fi

if [[ "${DIENSTPLANER_SKIP_LAUNCH:-0}" == "1" ]]; then
  echo "Skipping app launch because DIENSTPLANER_SKIP_LAUNCH=1."
  exit 0
fi

exec python "${SCRIPT_DIR}/main.py"
