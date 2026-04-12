"""
SQLite schema and initialization for hospital shift scheduling.
Entities: clinics, employees, qualifications, shifts, plus employee–qualification links.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

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

# Canonical clinics: (code, display name) — fixed order for UI.
CLINIC_CODE_ORDER: tuple[str, ...] = (
    "kardiologie",
    "rhythmologie",
    "nephrologie",
    "haemat_onkologie",
    "pneumologie",
    "gastroenterologie",
)
CANONICAL_CLINICS: tuple[tuple[str, str], ...] = (
    ("kardiologie", "Kardiologie"),
    ("rhythmologie", "Rhythmologie"),
    ("nephrologie", "Nephrologie"),
    ("haemat_onkologie", "Hämatologie/Onkologie"),
    ("pneumologie", "Pneumologie"),
    ("gastroenterologie", "Gastroenterologie"),
)

# Qualification labels (unique in DB); order matches UI columns.
CANONICAL_QUALIFICATIONS: tuple[str, ...] = (
    "Hausdienst",
    "Notaufnahme",
    "Notaufnahme-Facharztstandard",
)

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
    clinic_id INTEGER NOT NULL REFERENCES clinics(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    max_shifts_per_month INTEGER NOT NULL DEFAULT 6 CHECK (max_shifts_per_month >= 0)
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


def _seed_reference_data(conn: sqlite3.Connection) -> None:
    for code, name in CANONICAL_CLINICS:
        conn.execute(
            "INSERT OR IGNORE INTO clinics (code, name) VALUES (?, ?)",
            (code, name),
        )
    for qname in CANONICAL_QUALIFICATIONS:
        conn.execute(
            "INSERT OR IGNORE INTO qualifications (name) VALUES (?)",
            (qname,),
        )


def _migrate_employees_to_v2(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(employees)").fetchall()]
    if not cols:
        return
    if "name" in cols and "max_shifts_per_month" in cols:
        return

    _seed_reference_data(conn)
    fallback = conn.execute("SELECT id FROM clinics ORDER BY id LIMIT 1").fetchone()
    if fallback is None:
        return
    fallback_id = int(fallback[0])

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE employees_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clinic_id INTEGER NOT NULL REFERENCES clinics(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            max_shifts_per_month INTEGER NOT NULL DEFAULT 6 CHECK (max_shifts_per_month >= 0)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO employees_new (id, clinic_id, name, active, max_shifts_per_month)
        SELECT
            e.id,
            COALESCE(e.clinic_id, ?),
            CASE
                WHEN TRIM(COALESCE(e.first_name, '') || ' ' || COALESCE(e.last_name, '')) = ''
                THEN 'Unbenannt'
                ELSE TRIM(COALESCE(e.first_name, '') || ' ' || COALESCE(e.last_name, ''))
            END,
            e.active,
            6
        FROM employees e
        """,
        (fallback_id,),
    )
    conn.execute("DROP TABLE employees")
    conn.execute("ALTER TABLE employees_new RENAME TO employees")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_employees_clinic ON employees(clinic_id)"
    )
    conn.execute("PRAGMA foreign_keys = ON")


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_first_clinic_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM clinics ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def list_clinics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT id, name, code FROM clinics ORDER BY name COLLATE NOCASE"))


def list_canonical_clinics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Clinics from CLINIC_CODE_ORDER only, in that order."""
    rows = list_clinics(conn)
    by_code = {str(r["code"]): r for r in rows}
    return [by_code[c] for c in CLINIC_CODE_ORDER if c in by_code]


def list_qualifications_ordered(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    out: list[sqlite3.Row] = []
    for name in CANONICAL_QUALIFICATIONS:
        row = conn.execute(
            "SELECT id, name FROM qualifications WHERE name = ?",
            (name,),
        ).fetchone()
        if row is not None:
            out.append(row)
    return out


def list_employees(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    sql = """
        SELECT
            e.id,
            e.name,
            e.active,
            e.clinic_id,
            e.max_shifts_per_month,
            c.name AS clinic_name,
            GROUP_CONCAT(eq.qualification_id) AS qual_ids
        FROM employees e
        LEFT JOIN clinics c ON c.id = e.clinic_id
        LEFT JOIN employee_qualifications eq ON eq.employee_id = e.id
        GROUP BY e.id
        ORDER BY e.name COLLATE NOCASE
    """
    return list(conn.execute(sql))


def set_employee_qualifications(
    conn: sqlite3.Connection,
    employee_id: int,
    qualification_ids: Iterable[int],
) -> None:
    conn.execute(
        "DELETE FROM employee_qualifications WHERE employee_id = ?",
        (employee_id,),
    )
    for qid in qualification_ids:
        conn.execute(
            """
            INSERT INTO employee_qualifications (employee_id, qualification_id)
            VALUES (?, ?)
            """,
            (employee_id, int(qid)),
        )


def insert_employee(
    conn: sqlite3.Connection,
    *,
    name: str,
    clinic_id: int,
    active: bool,
    max_shifts_per_month: int,
    qualification_ids: Iterable[int],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO employees (name, clinic_id, active, max_shifts_per_month)
        VALUES (?, ?, ?, ?)
        """,
        (name, clinic_id, 1 if active else 0, max_shifts_per_month),
    )
    eid = int(cur.lastrowid)
    set_employee_qualifications(conn, eid, qualification_ids)
    return eid


def update_employee(
    conn: sqlite3.Connection,
    employee_id: int,
    *,
    name: str,
    clinic_id: int,
    active: bool,
    max_shifts_per_month: int,
    qualification_ids: Iterable[int],
) -> None:
    conn.execute(
        """
        UPDATE employees
        SET name = ?, clinic_id = ?, active = ?, max_shifts_per_month = ?
        WHERE id = ?
        """,
        (name, clinic_id, 1 if active else 0, max_shifts_per_month, employee_id),
    )
    set_employee_qualifications(conn, employee_id, qualification_ids)


def delete_employee(conn: sqlite3.Connection, employee_id: int) -> None:
    conn.execute("DELETE FROM employees WHERE id = ?", (employee_id,))


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
        SELECT s.shift_date, s.shift_slot, e.name
        FROM shifts s
        INNER JOIN employees e ON e.id = s.employee_id
        WHERE s.clinic_id = ?
          AND s.shift_date >= ? AND s.shift_date < ?
    """
    out: dict[tuple[str, str], str] = {}
    for row in conn.execute(sql, (clinic_id, start, end_exclusive)):
        key = (str(row["shift_date"]), str(row["shift_slot"]))
        out[key] = str(row["name"])
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
        _seed_reference_data(conn)
        _migrate_employees_to_v2(conn)
        conn.commit()
    finally:
        conn.close()
    return path.resolve()
