"""
SQLite schema and initialization for hospital shift scheduling.
Entities: clinics, employees, qualifications, shifts, plus employee–qualification links.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "dienstplaner.db"

# Stable codes stored in shifts.shift_slot; labels are for the UI only.
SHIFT_SLOT_CODES: tuple[str, ...] = (
    "2_dienst",
    "3_dienst",
    "zna_dr_a",
    "zna_dr_b",
)
SHIFT_SLOT_LABELS: dict[str, str] = {
    "2_dienst": "2. Dienst",
    "3_dienst": "3. Dienst",
    "zna_dr_a": "ZNA DR A",
    "zna_dr_b": "ZNA DR B",
}

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clinics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS qualifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clinic_id INTEGER REFERENCES clinics(id) ON DELETE SET NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS employee_qualifications (
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    qualification_id INTEGER NOT NULL REFERENCES qualifications(id) ON DELETE CASCADE,
    PRIMARY KEY (employee_id, qualification_id)
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clinic_id INTEGER NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    qualification_id INTEGER REFERENCES qualifications(id) ON DELETE SET NULL,
    shift_slot TEXT NOT NULL DEFAULT '2_dienst',
    shift_date TEXT NOT NULL,
    start_time TEXT NOT NULL DEFAULT '00:00',
    end_time TEXT NOT NULL DEFAULT '00:00',
    notes TEXT,
    CHECK (shift_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (shift_slot IN ('2_dienst', '3_dienst', 'zna_dr_a', 'zna_dr_b')),
    UNIQUE (clinic_id, shift_date, shift_slot)
);

CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(shift_date);
CREATE INDEX IF NOT EXISTS idx_shifts_clinic ON shifts(clinic_id);
CREATE INDEX IF NOT EXISTS idx_employees_clinic ON employees(clinic_id);
"""


def _migrate_shifts_shift_slot(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(shifts)").fetchall()}
    if "shift_slot" not in cols:
        conn.execute(
            "ALTER TABLE shifts ADD COLUMN shift_slot TEXT NOT NULL DEFAULT '2_dienst'"
        )


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_first_clinic_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM clinics ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def load_month_assignments(
    conn: sqlite3.Connection,
    *,
    clinic_id: int,
    year: int,
    month: int,
) -> dict[tuple[str, str], str]:
    """
    Returns mapping (shift_date 'YYYY-MM-DD', shift_slot) -> employee display name.
    Only rows with an assigned employee are included.
    """
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_exclusive = f"{year + 1:04d}-01-01"
    else:
        end_exclusive = f"{year:04d}-{month + 1:02d}-01"

    sql = """
        SELECT s.shift_date, s.shift_slot, e.first_name, e.last_name
        FROM shifts s
        INNER JOIN employees e ON e.id = s.employee_id
        WHERE s.clinic_id = ?
          AND s.shift_date >= ? AND s.shift_date < ?
    """
    out: dict[tuple[str, str], str] = {}
    for row in conn.execute(sql, (clinic_id, start, end_exclusive)):
        key = (str(row["shift_date"]), str(row["shift_slot"]))
        out[key] = f'{row["last_name"]}, {row["first_name"]}'
    return out


def init_database(db_path: str | Path | None = None) -> Path:
    """
    Create the database file (if needed) and apply the schema.
    Returns the resolved path to the database file.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate_shifts_shift_slot(conn)
        conn.commit()
    finally:
        conn.close()
    return path.resolve()
