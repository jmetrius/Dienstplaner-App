"""
SQLite schema and initialization for hospital shift scheduling.
Entities: clinics, employees, qualifications, shifts, plus employee–qualification links.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, timedelta
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

# Manual schedule: which qualifications may fill which slots (matches qualifications.name).
SHIFT_SLOTS_HAUSDienst: frozenset[str] = frozenset({"2_dienst", "3_dienst"})
SHIFT_SLOTS_ZNA: frozenset[str] = frozenset({"zna_dr_a", "zna_dr_b"})

# Canonical clinics: (code, display name) — fixed order for UI.
CLINIC_CODE_ORDER: tuple[str, ...] = (
    "kardiologie",
    "rhythmologie",
    "nephrologie",
    "haemat_onkologie",
    "pneumologie",
    "gastroenterologie",
    "geriatrie",
    "imc",
    "zna",
)
CANONICAL_CLINICS: tuple[tuple[str, str], ...] = (
    ("kardiologie", "Kardiologie"),
    ("rhythmologie", "Rhythmologie"),
    ("nephrologie", "Nephrologie"),
    ("haemat_onkologie", "Hämatologie/Onkologie"),
    ("pneumologie", "Pneumologie"),
    ("gastroenterologie", "Gastroenterologie"),
    ("geriatrie", "Geriatrie"),
    ("imc", "IMC"),
    ("zna", "ZNA"),
)

# Qualification labels (unique in DB); order matches UI columns.
CANONICAL_QUALIFICATIONS: tuple[str, ...] = (
    "Hausdienst",
    "Notaufnahme",
    "Notaufnahme-Facharztstandard",
)

# Absence categories (DB code -> UI label).
ABSENCE_CATEGORY_CODES: tuple[str, ...] = ("sick", "vacation", "holiday", "other")
ABSENCE_CATEGORY_LABELS: dict[str, str] = {
    "sick": "Krankheit",
    "vacation": "Urlaub",
    "holiday": "Feiertag",
    "other": "Sonstiges",
}

# Shift preference: soft constraint for scheduling.
PREFERENCE_CODES: tuple[str, ...] = ("prefer_work", "prefer_off")
PREFERENCE_LABELS: dict[str, str] = {
    "prefer_work": "Schicht bevorzugt",
    "prefer_off": "Keine Schicht",
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

CREATE TABLE IF NOT EXISTS employee_absences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other'
        CHECK (category IN ('sick', 'vacation', 'holiday', 'other')),
    notes TEXT,
    CHECK (start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (end_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (start_date <= end_date)
);

CREATE INDEX IF NOT EXISTS idx_absences_employee ON employee_absences(employee_id);
CREATE INDEX IF NOT EXISTS idx_absences_range ON employee_absences(start_date, end_date);

CREATE TABLE IF NOT EXISTS employee_shift_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    pref_date TEXT NOT NULL,
    preference TEXT NOT NULL CHECK (preference IN ('prefer_work', 'prefer_off')),
    notes TEXT,
    CHECK (pref_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
);

CREATE INDEX IF NOT EXISTS idx_pref_employee ON employee_shift_preferences(employee_id);
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


def _migrate_shift_preferences_single_date(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='employee_shift_preferences'"
    ).fetchone()
    if exists is None:
        return
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(employee_shift_preferences)").fetchall()
    }
    if "pref_date" in cols:
        return
    if "start_date" not in cols:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE employee_shift_preferences_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            pref_date TEXT NOT NULL,
            preference TEXT NOT NULL CHECK (preference IN ('prefer_work', 'prefer_off')),
            notes TEXT,
            CHECK (pref_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
        )
        """
    )
    conn.execute(
        """
        INSERT INTO employee_shift_preferences_new (id, employee_id, pref_date, preference, notes)
        SELECT id, employee_id, start_date, preference, notes
        FROM employee_shift_preferences
        """
    )
    conn.execute("DROP TABLE employee_shift_preferences")
    conn.execute("ALTER TABLE employee_shift_preferences_new RENAME TO employee_shift_preferences")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pref_employee ON employee_shift_preferences(employee_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pref_date ON employee_shift_preferences(pref_date)"
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


def month_date_bounds(year: int, month: int) -> tuple[str, str]:
    """Inclusive ISO start and end dates for the calendar month."""
    start = f"{year:04d}-{month:02d}-01"
    last = calendar.monthrange(year, month)[1]
    end = f"{year:04d}-{month:02d}-{last:02d}"
    return start, end


def list_employee_options(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, name FROM employees ORDER BY name COLLATE NOCASE"
        )
    )


def list_absences_overlapping_month(
    conn: sqlite3.Connection, *, year: int, month: int
) -> list[sqlite3.Row]:
    month_start, month_end = month_date_bounds(year, month)
    sql = """
        SELECT
            a.id,
            a.employee_id,
            e.name AS employee_name,
            a.start_date,
            a.end_date,
            a.category,
            a.notes
        FROM employee_absences a
        INNER JOIN employees e ON e.id = a.employee_id
        WHERE a.start_date <= ? AND a.end_date >= ?
        ORDER BY a.start_date, e.name COLLATE NOCASE
    """
    return list(conn.execute(sql, (month_end, month_start)))


def list_preferences_overlapping_month(
    conn: sqlite3.Connection, *, year: int, month: int
) -> list[sqlite3.Row]:
    month_start, month_end = month_date_bounds(year, month)
    sql = """
        SELECT
            p.id,
            p.employee_id,
            e.name AS employee_name,
            p.pref_date,
            p.preference,
            p.notes
        FROM employee_shift_preferences p
        INNER JOIN employees e ON e.id = p.employee_id
        WHERE p.pref_date >= ? AND p.pref_date <= ?
        ORDER BY p.pref_date, e.name COLLATE NOCASE
    """
    return list(conn.execute(sql, (month_start, month_end)))


def insert_absence(
    conn: sqlite3.Connection,
    *,
    employee_id: int,
    start_date: str,
    end_date: str,
    category: str,
    notes: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO employee_absences
            (employee_id, start_date, end_date, category, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (employee_id, start_date, end_date, category, notes or None),
    )
    return int(cur.lastrowid)


def update_absence(
    conn: sqlite3.Connection,
    absence_id: int,
    *,
    employee_id: int,
    start_date: str,
    end_date: str,
    category: str,
    notes: str | None,
) -> None:
    conn.execute(
        """
        UPDATE employee_absences
        SET employee_id = ?, start_date = ?, end_date = ?, category = ?, notes = ?
        WHERE id = ?
        """,
        (employee_id, start_date, end_date, category, notes or None, absence_id),
    )


def delete_absence(conn: sqlite3.Connection, absence_id: int) -> None:
    conn.execute("DELETE FROM employee_absences WHERE id = ?", (absence_id,))


def insert_shift_preference(
    conn: sqlite3.Connection,
    *,
    employee_id: int,
    pref_date: str,
    preference: str,
    notes: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO employee_shift_preferences
            (employee_id, pref_date, preference, notes)
        VALUES (?, ?, ?, ?)
        """,
        (employee_id, pref_date, preference, notes or None),
    )
    return int(cur.lastrowid)


def update_shift_preference(
    conn: sqlite3.Connection,
    pref_id: int,
    *,
    employee_id: int,
    pref_date: str,
    preference: str,
    notes: str | None,
) -> None:
    conn.execute(
        """
        UPDATE employee_shift_preferences
        SET employee_id = ?, pref_date = ?, preference = ?, notes = ?
        WHERE id = ?
        """,
        (employee_id, pref_date, preference, notes or None, pref_id),
    )


def delete_shift_preference(conn: sqlite3.Connection, pref_id: int) -> None:
    conn.execute("DELETE FROM employee_shift_preferences WHERE id = ?", (pref_id,))


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


def qualification_names_for_shift_slot(shift_slot: str) -> tuple[str, ...]:
    """Exact qualification.name values required to be selectable for this slot."""
    if shift_slot in SHIFT_SLOTS_HAUSDienst:
        return ("Hausdienst",)
    if shift_slot in SHIFT_SLOTS_ZNA:
        return ("Notaufnahme", "Notaufnahme-Facharztstandard")
    return ()


def list_employees_eligible_for_schedule_slot(
    conn: sqlite3.Connection, shift_slot: str
) -> list[sqlite3.Row]:
    names = qualification_names_for_shift_slot(shift_slot)
    if not names:
        return []
    placeholders = ",".join("?" * len(names))
    sql = f"""
        SELECT DISTINCT e.id, e.name
        FROM employees e
        INNER JOIN employee_qualifications eq ON eq.employee_id = e.id
        INNER JOIN qualifications q ON q.id = eq.qualification_id
        WHERE e.active = 1 AND q.name IN ({placeholders})
        ORDER BY e.name COLLATE NOCASE
    """
    return list(conn.execute(sql, names))


def get_employee_name(conn: sqlite3.Connection, employee_id: int) -> str | None:
    row = conn.execute(
        "SELECT name FROM employees WHERE id = ?", (employee_id,)
    ).fetchone()
    return str(row["name"]) if row else None


def load_month_shift_employee_ids(
    conn: sqlite3.Connection,
    *,
    clinic_id: int,
    year: int,
    month: int,
) -> dict[tuple[str, str], int]:
    """(shift_date, shift_slot) -> employee_id for assigned shifts in the month."""
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_exclusive = f"{year + 1:04d}-01-01"
    else:
        end_exclusive = f"{year:04d}-{month + 1:02d}-01"

    sql = """
        SELECT shift_date, shift_slot, employee_id
        FROM shifts
        WHERE clinic_id = ?
          AND shift_date >= ? AND shift_date < ?
          AND employee_id IS NOT NULL
    """
    out: dict[tuple[str, str], int] = {}
    for row in conn.execute(sql, (clinic_id, start, end_exclusive)):
        key = (str(row["shift_date"]), str(row["shift_slot"]))
        out[key] = int(row["employee_id"])
    return out


def load_month_shift_rows_all_clinics(
    conn: sqlite3.Connection,
    *,
    year: int,
    month: int,
) -> list[sqlite3.Row]:
    """Assigned shifts in the month for all clinics."""
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_exclusive = f"{year + 1:04d}-01-01"
    else:
        end_exclusive = f"{year:04d}-{month + 1:02d}-01"
    sql = """
        SELECT
            s.clinic_id AS schedule_clinic_id,
            s.shift_date,
            s.shift_slot,
            s.employee_id,
            e.clinic_id AS employee_clinic_id
        FROM shifts s
        INNER JOIN employees e ON e.id = s.employee_id
        WHERE s.shift_date >= ? AND s.shift_date < ?
          AND s.employee_id IS NOT NULL
    """
    return list(conn.execute(sql, (start, end_exclusive)))


def list_active_employees_with_qualifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    sql = """
        SELECT
            e.id,
            e.name,
            e.clinic_id,
            e.max_shifts_per_month,
            GROUP_CONCAT(q.name) AS qualification_names
        FROM employees e
        LEFT JOIN employee_qualifications eq ON eq.employee_id = e.id
        LEFT JOIN qualifications q ON q.id = eq.qualification_id
        WHERE e.active = 1
        GROUP BY e.id
        ORDER BY e.name COLLATE NOCASE
    """
    return list(conn.execute(sql))


def load_solver_month_input(
    conn: sqlite3.Connection,
    *,
    clinic_id: int,
    year: int,
    month: int,
) -> dict[str, object]:
    """
    Return normalized month input for the automatic solver.
    Keys:
      - dates: list[str]
      - employees: list[dict]
      - fixed_assignments: dict[(date_iso, slot_code), employee_id]
      - external_assignments_by_date: dict[date_iso, list[(employee_id, clinic_id)]]
      - absences_by_employee: dict[employee_id, set[date_iso]]
      - preferences: dict[(employee_id, date_iso), preference_code]
    """
    _, days_in_month = calendar.monthrange(year, month)
    dates = [f"{year:04d}-{month:02d}-{d:02d}" for d in range(1, days_in_month + 1)]

    clinic_code_by_id: dict[int, str] = {}
    for row in list_clinics(conn):
        clinic_code_by_id[int(row["id"])] = str(row["code"])

    employees: list[dict[str, object]] = []
    for row in list_active_employees_with_qualifications(conn):
        raw_names = str(row["qualification_names"] or "")
        quals = {x for x in raw_names.split(",") if x}
        clinic_id = int(row["clinic_id"])
        employees.append(
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "clinic_id": clinic_id,
                "clinic_code": clinic_code_by_id.get(clinic_id, ""),
                "max_shifts_per_month": int(row["max_shifts_per_month"]),
                "qualifications": quals,
            }
        )

    fixed_assignments = load_month_shift_employee_ids(
        conn, clinic_id=clinic_id, year=year, month=month
    )

    external_assignments_by_date: dict[str, list[tuple[int, int]]] = {}
    for row in load_month_shift_rows_all_clinics(conn, year=year, month=month):
        schedule_clinic_id = int(row["schedule_clinic_id"])
        if schedule_clinic_id == clinic_id:
            continue
        iso = str(row["shift_date"])
        bucket = external_assignments_by_date.setdefault(iso, [])
        bucket.append((int(row["employee_id"]), int(row["employee_clinic_id"])))

    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)
    absences_by_employee: dict[int, set[str]] = {}
    for row in list_absences_overlapping_month(conn, year=year, month=month):
        employee_id = int(row["employee_id"])
        start_y, start_m, start_d = (int(x) for x in str(row["start_date"]).split("-", 2))
        end_y, end_m, end_d = (int(x) for x in str(row["end_date"]).split("-", 2))
        a_start = date(start_y, start_m, start_d)
        a_end = date(end_y, end_m, end_d)
        cur = max(month_start, a_start)
        end = min(month_end, a_end)
        while cur <= end:
            absences_by_employee.setdefault(employee_id, set()).add(cur.isoformat())
            cur += timedelta(days=1)

    preferences: dict[tuple[int, str], str] = {}
    for row in list_preferences_overlapping_month(conn, year=year, month=month):
        preferences[(int(row["employee_id"]), str(row["pref_date"]))] = str(
            row["preference"]
        )

    employee_clinic_by_id: dict[int, int] = {}
    employee_name_by_id: dict[int, str] = {}
    for row in conn.execute("SELECT id, clinic_id, name FROM employees"):
        employee_id = int(row["id"])
        employee_clinic_by_id[employee_id] = int(row["clinic_id"])
        employee_name_by_id[employee_id] = str(row["name"])

    return {
        "dates": dates,
        "employees": employees,
        "fixed_assignments": fixed_assignments,
        "external_assignments_by_date": external_assignments_by_date,
        "absences_by_employee": absences_by_employee,
        "preferences": preferences,
        "employee_clinic_by_id": employee_clinic_by_id,
        "employee_name_by_id": employee_name_by_id,
    }


def replace_month_shift_assignments(
    conn: sqlite3.Connection,
    *,
    clinic_id: int,
    year: int,
    month: int,
    assignments: dict[tuple[str, str], int | None],
) -> None:
    """Replace all assignments for clinic/month in caller transaction."""
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_exclusive = f"{year + 1:04d}-01-01"
    else:
        end_exclusive = f"{year:04d}-{month + 1:02d}-01"
    conn.execute(
        """
        DELETE FROM shifts
        WHERE clinic_id = ?
          AND shift_date >= ? AND shift_date < ?
        """,
        (clinic_id, start, end_exclusive),
    )
    for (shift_date, shift_slot), employee_id in sorted(assignments.items()):
        if employee_id is None or shift_slot not in SHIFT_SLOT_CODES:
            continue
        conn.execute(
            """
            INSERT INTO shifts
                (clinic_id, employee_id, shift_slot, shift_date, start_time, end_time)
            VALUES (?, ?, ?, ?, '00:00', '00:00')
            """,
            (clinic_id, int(employee_id), shift_slot, shift_date),
        )


def set_shift_assignment(
    conn: sqlite3.Connection,
    clinic_id: int,
    shift_date: str,
    shift_slot: str,
    employee_id: int | None,
) -> None:
    """Assign or clear one slot. Persists within caller's transaction."""
    if employee_id is None:
        conn.execute(
            """
            DELETE FROM shifts
            WHERE clinic_id = ? AND shift_date = ? AND shift_slot = ?
            """,
            (clinic_id, shift_date, shift_slot),
        )
        return
    row = conn.execute(
        """
        SELECT id FROM shifts
        WHERE clinic_id = ? AND shift_date = ? AND shift_slot = ?
        """,
        (clinic_id, shift_date, shift_slot),
    ).fetchone()
    if row is not None:
        conn.execute(
            """
            UPDATE shifts SET employee_id = ?
            WHERE clinic_id = ? AND shift_date = ? AND shift_slot = ?
            """,
            (employee_id, clinic_id, shift_date, shift_slot),
        )
    else:
        conn.execute(
            """
            INSERT INTO shifts
                (clinic_id, employee_id, shift_slot, shift_date, start_time, end_time)
            VALUES (?, ?, ?, ?, '00:00', '00:00')
            """,
            (clinic_id, employee_id, shift_slot, shift_date),
        )


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
        _migrate_shift_preferences_single_date(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pref_date ON employee_shift_preferences(pref_date)"
        )
        conn.commit()
    finally:
        conn.close()
    return path.resolve()
