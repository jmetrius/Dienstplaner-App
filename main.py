"""
PyQt6 entry point: main window with schedule and employee management tabs.
"""

from __future__ import annotations

import calendar
import csv
import sqlite3
import sys
from collections.abc import Callable
from datetime import date

from PyQt6.QtCore import QDate, QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database import (
    ABSENCE_CATEGORY_CODES,
    ABSENCE_CATEGORY_LABELS,
    CANONICAL_QUALIFICATIONS,
    PREFERENCE_CODES,
    PREFERENCE_LABELS,
    SHIFT_SLOT_CODES,
    SHIFT_SLOT_LABELS,
    delete_absence,
    delete_employee,
    delete_shift_preference,
    get_connection,
    get_employee_name,
    get_first_clinic_id,
    init_database,
    insert_absence,
    insert_employee,
    insert_shift_preference,
    list_absences_overlapping_month,
    list_canonical_clinics,
    load_solver_month_input,
    list_employee_options,
    list_employees,
    list_employees_eligible_for_schedule_slot,
    list_clinics,
    list_preferences_overlapping_month,
    list_qualifications_ordered,
    load_month_shift_employee_ids,
    replace_month_shift_assignments,
    set_shift_assignment,
    update_absence,
    update_employee,
    update_shift_preference,
)
from solver import SolverConfig, SolverResult, SolverSolution, solve_month_schedule

N_QUAL = len(CANONICAL_QUALIFICATIONS)
COL_NAME = 0
COL_QUAL_BASE = 1
COL_CLINIC = COL_QUAL_BASE + N_QUAL
COL_MAX_SHIFTS = COL_CLINIC + 1
COL_ACTIVE = COL_MAX_SHIFTS + 1
N_COLS = COL_ACTIVE + 1


def _iso_from_qdate(d: QDate) -> str:
    return f"{d.year()}-{d.month():02d}-{d.day():02d}"


def _qdate_from_iso(s: str) -> QDate:
    y, m, dd = (int(x) for x in s.split("-", 2))
    return QDate(y, m, dd)


class AbsencesPreferencesPage(QWidget):
    """
    Month-scoped management of hard absences and soft shift preferences.
    Rows overlap the selected month (ranges may extend outside it).
    """

    A_COL_ID = 0
    A_COL_EMP = 1
    A_COL_START = 2
    A_COL_END = 3
    A_COL_CAT = 4
    A_COL_NOTES = 5

    P_COL_ID = 0
    P_COL_EMP = 1
    P_COL_DATE = 2
    P_COL_PREF = 3
    P_COL_NOTES = 4

    def __init__(self) -> None:
        super().__init__()
        today = date.today()
        self._year = today.year
        self._month = today.month
        self._employees: list[sqlite3.Row] = []

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(0, 8, 0, 0)

        self._title = QLabel()
        tf = QFont()
        tf.setPointSize(13)
        tf.setBold(True)
        self._title.setFont(tf)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._btn_prev = QPushButton("‹")
        self._btn_prev.setFixedWidth(40)
        self._btn_prev.setToolTip("Previous month")
        self._btn_prev.clicked.connect(self._prev_month)
        self._btn_next = QPushButton("›")
        self._btn_next.setFixedWidth(40)
        self._btn_next.setToolTip("Next month")
        self._btn_next.clicked.connect(self._next_month)
        self._btn_today = QPushButton("Today")
        self._btn_today.clicked.connect(self._go_today_month)

        nav = QHBoxLayout()
        nav.addWidget(self._btn_prev)
        nav.addStretch(1)
        nav.addWidget(self._title)
        nav.addStretch(1)
        nav.addWidget(self._btn_next)
        root.addLayout(nav)
        nav2 = QHBoxLayout()
        nav2.addStretch(1)
        nav2.addWidget(self._btn_today)
        nav2.addStretch(1)
        root.addLayout(nav2)

        save_bar = QHBoxLayout()
        self._btn_save_all = QPushButton("Save changes")
        self._btn_save_all.setToolTip(
            "Save all edits in absences and shift preferences together."
        )
        self._btn_save_all.clicked.connect(self._save_all_sections)
        save_bar.addStretch(1)
        save_bar.addWidget(self._btn_save_all)
        save_bar.addStretch(1)
        root.addLayout(save_bar)

        hint = QLabel(
            "Absences: ranges that overlap the selected month are listed (ranges may extend "
            "outside the month). Preferences: one calendar date per row (one shift day); "
            "use multiple rows for more wishes."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        root.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Vertical)

        abs_box = QGroupBox("Absences (cannot work — e.g. sick leave, vacation)")
        abs_lay = QVBoxLayout(abs_box)
        abs_bar = QHBoxLayout()
        self._btn_abs_add = QPushButton("Add")
        self._btn_abs_del = QPushButton("Delete selected")
        self._btn_abs_add.clicked.connect(self._add_absence_row)
        self._btn_abs_del.clicked.connect(self._delete_absence_selected)
        abs_bar.addWidget(self._btn_abs_add)
        abs_bar.addWidget(self._btn_abs_del)
        abs_bar.addStretch(1)
        abs_lay.addLayout(abs_bar)

        self._abs_table = QTableWidget()
        self._abs_table.setColumnCount(6)
        self._abs_table.setHorizontalHeaderLabels(
            ["id", "Employee", "From", "To", "Category", "Notes"]
        )
        self._abs_table.setColumnHidden(self.A_COL_ID, True)
        self._abs_table.setAlternatingRowColors(True)
        self._abs_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._abs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._abs_table.verticalHeader().setVisible(False)
        ah = self._abs_table.horizontalHeader()
        ah.setSectionResizeMode(self.A_COL_EMP, QHeaderView.ResizeMode.Stretch)
        ah.setSectionResizeMode(self.A_COL_START, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(self.A_COL_END, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(self.A_COL_CAT, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(self.A_COL_NOTES, QHeaderView.ResizeMode.Stretch)
        abs_lay.addWidget(self._abs_table)
        splitter.addWidget(abs_box)

        pref_box = QGroupBox(
            "Shift preferences (one date per wish — add several rows for several days)"
        )
        pref_lay = QVBoxLayout(pref_box)
        pref_bar = QHBoxLayout()
        self._btn_pref_add = QPushButton("Add")
        self._btn_pref_del = QPushButton("Delete selected")
        self._btn_pref_add.clicked.connect(self._add_pref_row)
        self._btn_pref_del.clicked.connect(self._delete_pref_selected)
        pref_bar.addWidget(self._btn_pref_add)
        pref_bar.addWidget(self._btn_pref_del)
        pref_bar.addStretch(1)
        pref_lay.addLayout(pref_bar)

        self._pref_table = QTableWidget()
        self._pref_table.setColumnCount(5)
        self._pref_table.setHorizontalHeaderLabels(
            ["id", "Employee", "Date", "Preference", "Notes"]
        )
        self._pref_table.setColumnHidden(self.P_COL_ID, True)
        self._pref_table.setAlternatingRowColors(True)
        self._pref_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._pref_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._pref_table.verticalHeader().setVisible(False)
        ph = self._pref_table.horizontalHeader()
        ph.setSectionResizeMode(self.P_COL_EMP, QHeaderView.ResizeMode.Stretch)
        ph.setSectionResizeMode(self.P_COL_DATE, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(self.P_COL_PREF, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(self.P_COL_NOTES, QHeaderView.ResizeMode.Stretch)
        pref_lay.addWidget(self._pref_table)
        splitter.addWidget(pref_box)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

        self.reload()

    def _prev_month(self) -> None:
        if self._month == 1:
            self._month = 12
            self._year -= 1
        else:
            self._month -= 1
        self.reload()

    def _next_month(self) -> None:
        if self._month == 12:
            self._month = 1
            self._year += 1
        else:
            self._month += 1
        self.reload()

    def _go_today_month(self) -> None:
        t = date.today()
        self._year, self._month = t.year, t.month
        self.reload()

    def _month_title(self) -> str:
        return date(self._year, self._month, 1).strftime("%B %Y")

    def reload(self) -> None:
        self._title.setText(self._month_title())
        conn = get_connection()
        try:
            self._employees = list_employee_options(conn)
            abs_rows = list_absences_overlapping_month(
                conn, year=self._year, month=self._month
            )
            pref_rows = list_preferences_overlapping_month(
                conn, year=self._year, month=self._month
            )
        finally:
            conn.close()

        self._abs_table.setRowCount(0)
        for ar in abs_rows:
            self._append_absence_row(ar)

        self._pref_table.setRowCount(0)
        for pr in pref_rows:
            self._append_preference_row(pr)

    def _make_employee_combo(self, current_id: int | None) -> QComboBox:
        cb = QComboBox()
        cb.addItem("—", None)
        for e in self._employees:
            cb.addItem(str(e["name"]), int(e["id"]))
        if current_id is not None:
            for i in range(cb.count()):
                d = cb.itemData(i)
                if d is not None and int(d) == current_id:
                    cb.setCurrentIndex(i)
                    break
        return cb

    def _make_category_combo(self, current: str) -> QComboBox:
        cb = QComboBox()
        for code in ABSENCE_CATEGORY_CODES:
            cb.addItem(ABSENCE_CATEGORY_LABELS[code], code)
        if current in ABSENCE_CATEGORY_CODES:
            cb.setCurrentIndex(ABSENCE_CATEGORY_CODES.index(current))
        return cb

    def _make_preference_combo(self, current: str) -> QComboBox:
        cb = QComboBox()
        for code in PREFERENCE_CODES:
            cb.addItem(PREFERENCE_LABELS[code], code)
        if current in PREFERENCE_CODES:
            cb.setCurrentIndex(PREFERENCE_CODES.index(current))
        return cb

    def _default_month_dates(self) -> tuple[QDate, QDate]:
        last = calendar.monthrange(self._year, self._month)[1]
        return (
            QDate(self._year, self._month, 1),
            QDate(self._year, self._month, last),
        )

    def _default_pref_date(self) -> QDate:
        td = date.today()
        if td.year == self._year and td.month == self._month:
            return QDate(td.year, td.month, td.day)
        return QDate(self._year, self._month, 1)

    def _append_absence_row(self, row: sqlite3.Row | None) -> None:
        r = self._abs_table.rowCount()
        self._abs_table.insertRow(r)
        id_it = QTableWidgetItem("")
        if row is not None:
            id_it.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
        else:
            id_it.setData(Qt.ItemDataRole.UserRole, None)
        self._abs_table.setItem(r, self.A_COL_ID, id_it)

        eid = int(row["employee_id"]) if row is not None else None
        self._abs_table.setCellWidget(
            r, self.A_COL_EMP, self._make_employee_combo(eid)
        )

        d_start, d_end = self._default_month_dates()
        de_s = QDateEdit()
        de_s.setCalendarPopup(True)
        de_s.setDisplayFormat("yyyy-MM-dd")
        de_e = QDateEdit()
        de_e.setCalendarPopup(True)
        de_e.setDisplayFormat("yyyy-MM-dd")
        if row is not None:
            de_s.setDate(_qdate_from_iso(str(row["start_date"])))
            de_e.setDate(_qdate_from_iso(str(row["end_date"])))
        else:
            de_s.setDate(d_start)
            de_e.setDate(d_end)
        self._abs_table.setCellWidget(r, self.A_COL_START, de_s)
        self._abs_table.setCellWidget(r, self.A_COL_END, de_e)

        cat = str(row["category"]) if row is not None else "other"
        self._abs_table.setCellWidget(
            r, self.A_COL_CAT, self._make_category_combo(cat)
        )

        notes = str(row["notes"] or "") if row is not None else ""
        self._abs_table.setCellWidget(r, self.A_COL_NOTES, QLineEdit(notes))

    def _append_preference_row(self, row: sqlite3.Row | None) -> None:
        r = self._pref_table.rowCount()
        self._pref_table.insertRow(r)
        id_it = QTableWidgetItem("")
        if row is not None:
            id_it.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
        else:
            id_it.setData(Qt.ItemDataRole.UserRole, None)
        self._pref_table.setItem(r, self.P_COL_ID, id_it)

        eid = int(row["employee_id"]) if row is not None else None
        self._pref_table.setCellWidget(
            r, self.P_COL_EMP, self._make_employee_combo(eid)
        )

        de = QDateEdit()
        de.setCalendarPopup(True)
        de.setDisplayFormat("yyyy-MM-dd")
        if row is not None:
            de.setDate(_qdate_from_iso(str(row["pref_date"])))
        else:
            de.setDate(self._default_pref_date())
        self._pref_table.setCellWidget(r, self.P_COL_DATE, de)

        pref = str(row["preference"]) if row is not None else "prefer_off"
        self._pref_table.setCellWidget(
            r, self.P_COL_PREF, self._make_preference_combo(pref)
        )

        notes = str(row["notes"] or "") if row is not None else ""
        self._pref_table.setCellWidget(r, self.P_COL_NOTES, QLineEdit(notes))

    def _add_absence_row(self) -> None:
        if not self._employees:
            QMessageBox.information(
                self,
                "Employees",
                "Add employees on the Employees tab first.",
            )
            return
        self._append_absence_row(None)

    def _add_pref_row(self) -> None:
        if not self._employees:
            QMessageBox.information(
                self,
                "Employees",
                "Add employees on the Employees tab first.",
            )
            return
        self._append_preference_row(None)

    def _row_record_id(self, table: QTableWidget, col: int, row: int) -> int | None:
        it = table.item(row, col)
        if it is None:
            return None
        v = it.data(Qt.ItemDataRole.UserRole)
        return int(v) if v is not None else None

    def _persist_absences_to_conn(self, conn: sqlite3.Connection) -> str | None:
        if not self._employees:
            return "No employees defined."
        for r in range(self._abs_table.rowCount()):
            w_emp = self._abs_table.cellWidget(r, self.A_COL_EMP)
            w_s = self._abs_table.cellWidget(r, self.A_COL_START)
            w_e = self._abs_table.cellWidget(r, self.A_COL_END)
            w_cat = self._abs_table.cellWidget(r, self.A_COL_CAT)
            w_notes = self._abs_table.cellWidget(r, self.A_COL_NOTES)
            if (
                not isinstance(w_emp, QComboBox)
                or not isinstance(w_s, QDateEdit)
                or not isinstance(w_e, QDateEdit)
                or not isinstance(w_cat, QComboBox)
                or not isinstance(w_notes, QLineEdit)
            ):
                continue
            raw_e = w_emp.currentData()
            if raw_e is None:
                continue
            emp_id = int(raw_e)
            start = _iso_from_qdate(w_s.date())
            end = _iso_from_qdate(w_e.date())
            if start > end:
                return (
                    "Absences: end date must be on or after the start date "
                    f"(row {r + 1})."
                )
            cat_raw = w_cat.currentData()
            category = str(cat_raw) if cat_raw is not None else "other"
            notes = w_notes.text().strip() or None
            rid = self._row_record_id(self._abs_table, self.A_COL_ID, r)
            if rid is None:
                insert_absence(
                    conn,
                    employee_id=emp_id,
                    start_date=start,
                    end_date=end,
                    category=category,
                    notes=notes,
                )
            else:
                update_absence(
                    conn,
                    rid,
                    employee_id=emp_id,
                    start_date=start,
                    end_date=end,
                    category=category,
                    notes=notes,
                )
        return None

    def _persist_preferences_to_conn(self, conn: sqlite3.Connection) -> str | None:
        if not self._employees:
            return "No employees defined."
        for r in range(self._pref_table.rowCount()):
            w_emp = self._pref_table.cellWidget(r, self.P_COL_EMP)
            w_d = self._pref_table.cellWidget(r, self.P_COL_DATE)
            w_pf = self._pref_table.cellWidget(r, self.P_COL_PREF)
            w_notes = self._pref_table.cellWidget(r, self.P_COL_NOTES)
            if (
                not isinstance(w_emp, QComboBox)
                or not isinstance(w_d, QDateEdit)
                or not isinstance(w_pf, QComboBox)
                or not isinstance(w_notes, QLineEdit)
            ):
                continue
            raw_e = w_emp.currentData()
            if raw_e is None:
                continue
            emp_id = int(raw_e)
            pref_date = _iso_from_qdate(w_d.date())
            p_raw = w_pf.currentData()
            preference = str(p_raw) if p_raw is not None else "prefer_off"
            notes = w_notes.text().strip() or None
            rid = self._row_record_id(self._pref_table, self.P_COL_ID, r)
            if rid is None:
                insert_shift_preference(
                    conn,
                    employee_id=emp_id,
                    pref_date=pref_date,
                    preference=preference,
                    notes=notes,
                )
            else:
                update_shift_preference(
                    conn,
                    rid,
                    employee_id=emp_id,
                    pref_date=pref_date,
                    preference=preference,
                    notes=notes,
                )
        return None

    def _save_all_sections(self) -> None:
        conn = get_connection()
        try:
            err = self._persist_absences_to_conn(conn)
            if err is not None:
                conn.rollback()
                QMessageBox.warning(self, "Save", err)
                return
            err = self._persist_preferences_to_conn(conn)
            if err is not None:
                conn.rollback()
                QMessageBox.warning(self, "Save", err)
                return
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        finally:
            conn.close()
        self.reload()

    def _delete_absence_selected(self) -> None:
        row = self._abs_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Delete", "Select a row to delete.")
            return
        rid = self._row_record_id(self._abs_table, self.A_COL_ID, row)
        if rid is not None:
            reply = QMessageBox.question(
                self,
                "Delete absence",
                "Remove this absence from the database?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            conn = get_connection()
            try:
                delete_absence(conn, rid)
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                QMessageBox.critical(self, "Delete failed", str(exc))
                return
            finally:
                conn.close()
        self._abs_table.removeRow(row)

    def _delete_pref_selected(self) -> None:
        row = self._pref_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Delete", "Select a row to delete.")
            return
        rid = self._row_record_id(self._pref_table, self.P_COL_ID, row)
        if rid is not None:
            reply = QMessageBox.question(
                self,
                "Delete preference",
                "Remove this preference from the database?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            conn = get_connection()
            try:
                delete_shift_preference(conn, rid)
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                QMessageBox.critical(self, "Delete failed", str(exc))
                return
            finally:
                conn.close()
        self._pref_table.removeRow(row)


class EmployeeListPage(QWidget):
    """Overview and CRUD for employees."""

    def __init__(self, on_changed: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._on_changed = on_changed
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 8, 0, 0)

        bar = QHBoxLayout()
        self._btn_add = QPushButton("Add employee")
        self._btn_add.clicked.connect(self._add_row)
        self._btn_save = QPushButton("Save changes")
        self._btn_save.clicked.connect(self._save_all)
        self._btn_delete = QPushButton("Delete selected")
        self._btn_delete.clicked.connect(self._delete_selected)
        bar.addWidget(self._btn_add)
        bar.addWidget(self._btn_save)
        bar.addWidget(self._btn_delete)
        bar.addStretch(1)
        layout.addLayout(bar)

        headers = (
            ["Name"]
            + list(CANONICAL_QUALIFICATIONS)
            + ["Klinik", "Max. Schichtanzahl", "Active"]
        )
        self._table = QTableWidget()
        self._table.setColumnCount(N_COLS)
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for c in range(COL_QUAL_BASE, COL_CLINIC):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_CLINIC, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_MAX_SHIFTS, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_ACTIVE, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table, stretch=1)

        self._clinics: list[sqlite3.Row] = []
        self._qual_rows: list[sqlite3.Row] = []

        self.reload()

    @staticmethod
    def _parse_qual_ids(raw: object) -> set[int]:
        if raw is None or raw == "":
            return set()
        return {int(x) for x in str(raw).split(",") if x.strip().isdigit()}

    def reload(self) -> None:
        conn = get_connection()
        try:
            self._clinics = list_canonical_clinics(conn)
            self._qual_rows = list_qualifications_ordered(conn)
            rows = list_employees(conn)
        finally:
            conn.close()

        self._table.setRowCount(0)
        for emp in rows:
            self._append_row_from_db(emp)
        if self._table.rowCount() == 0:
            self._append_empty_row()

    def _make_clinic_combo(self, current_id: int | None) -> QComboBox:
        cb = QComboBox()
        for c in self._clinics:
            cb.addItem(str(c["name"]), int(c["id"]))
        if cb.count() == 0:
            return cb
        if current_id is not None:
            for i in range(cb.count()):
                data = cb.itemData(i)
                if data is not None and int(data) == current_id:
                    cb.setCurrentIndex(i)
                    return cb
        cb.setCurrentIndex(0)
        return cb

    def _qual_checkboxes(self, selected: set[int]) -> list[QCheckBox]:
        boxes: list[QCheckBox] = []
        for qr in self._qual_rows:
            qid = int(qr["id"])
            cb = QCheckBox()
            cb.setChecked(qid in selected)
            cb.setProperty("qual_id", qid)
            boxes.append(cb)
        while len(boxes) < N_QUAL:
            cb = QCheckBox()
            cb.setProperty("qual_id", -1)
            cb.setEnabled(False)
            boxes.append(cb)
        return boxes[:N_QUAL]

    def _append_row_from_db(self, emp: sqlite3.Row) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)
        eid = int(emp["id"])
        name_item = QTableWidgetItem(str(emp["name"]))
        name_item.setData(Qt.ItemDataRole.UserRole, eid)
        self._table.setItem(r, COL_NAME, name_item)

        quals = self._parse_qual_ids(emp["qual_ids"])
        for i, chk in enumerate(self._qual_checkboxes(quals)):
            self._table.setCellWidget(r, COL_QUAL_BASE + i, chk)

        cid = emp["clinic_id"]
        clinic_id = int(cid) if cid is not None else None
        self._table.setCellWidget(r, COL_CLINIC, self._make_clinic_combo(clinic_id))

        spin = QSpinBox()
        spin.setRange(0, 62)
        spin.setValue(int(emp["max_shifts_per_month"]))
        self._table.setCellWidget(r, COL_MAX_SHIFTS, spin)

        act = QCheckBox()
        act.setChecked(bool(emp["active"]))
        self._table.setCellWidget(r, COL_ACTIVE, act)

    def _append_empty_row(self) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)
        ni = QTableWidgetItem("")
        ni.setData(Qt.ItemDataRole.UserRole, None)
        self._table.setItem(r, COL_NAME, ni)

        for i, chk in enumerate(self._qual_checkboxes(set())):
            self._table.setCellWidget(r, COL_QUAL_BASE + i, chk)

        self._table.setCellWidget(r, COL_CLINIC, self._make_clinic_combo(None))

        spin = QSpinBox()
        spin.setRange(0, 62)
        spin.setValue(6)
        self._table.setCellWidget(r, COL_MAX_SHIFTS, spin)

        act = QCheckBox()
        act.setChecked(True)
        self._table.setCellWidget(r, COL_ACTIVE, act)

    def _add_row(self) -> None:
        self._append_empty_row()

    def _row_employee_id(self, row: int) -> int | None:
        it = self._table.item(row, COL_NAME)
        if it is None:
            return None
        v = it.data(Qt.ItemDataRole.UserRole)
        return int(v) if v is not None else None

    def _collect_qualification_ids(self, row: int) -> list[int]:
        ids: list[int] = []
        for i in range(N_QUAL):
            w = self._table.cellWidget(row, COL_QUAL_BASE + i)
            if not isinstance(w, QCheckBox) or not w.isChecked():
                continue
            raw = w.property("qual_id")
            try:
                qid = int(raw)
            except (TypeError, ValueError):
                continue
            if qid >= 0:
                ids.append(qid)
        return ids

    def _save_all(self) -> None:
        if not self._clinics:
            QMessageBox.warning(
                self,
                "No clinics",
                "No clinics are available. Restart the application after database setup.",
            )
            return

        conn = get_connection()
        try:
            self._clinics = list_canonical_clinics(conn)
            self._qual_rows = list_qualifications_ordered(conn)

            for r in range(self._table.rowCount()):
                name_item = self._table.item(r, COL_NAME)
                if name_item is None:
                    continue
                name = name_item.text().strip()
                if not name:
                    continue

                w_clinic = self._table.cellWidget(r, COL_CLINIC)
                w_spin = self._table.cellWidget(r, COL_MAX_SHIFTS)
                w_act = self._table.cellWidget(r, COL_ACTIVE)
                if (
                    not isinstance(w_clinic, QComboBox)
                    or not isinstance(w_spin, QSpinBox)
                    or not isinstance(w_act, QCheckBox)
                ):
                    continue
                if w_clinic.count() == 0:
                    QMessageBox.warning(self, "Klinik", "No clinic selected.")
                    return
                raw_cid = w_clinic.currentData()
                if raw_cid is None:
                    QMessageBox.warning(self, "Klinik", "Each employee must have a clinic.")
                    return
                clinic_id = int(raw_cid)
                max_shifts = int(w_spin.value())
                active = w_act.isChecked()
                qual_ids = self._collect_qualification_ids(r)

                eid = self._row_employee_id(r)
                if eid is None:
                    new_id = insert_employee(
                        conn,
                        name=name,
                        clinic_id=clinic_id,
                        active=active,
                        max_shifts_per_month=max_shifts,
                        qualification_ids=qual_ids,
                    )
                    name_item.setData(Qt.ItemDataRole.UserRole, new_id)
                else:
                    update_employee(
                        conn,
                        eid,
                        name=name,
                        clinic_id=clinic_id,
                        active=active,
                        max_shifts_per_month=max_shifts,
                        qualification_ids=qual_ids,
                    )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        finally:
            conn.close()

        self.reload()
        if self._on_changed:
            self._on_changed()

    def _delete_selected(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Delete", "Select a row to delete.")
            return
        eid = self._row_employee_id(row)
        if eid is not None:
            reply = QMessageBox.question(
                self,
                "Delete employee",
                "Remove this employee from the database?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            conn = get_connection()
            try:
                delete_employee(conn, eid)
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                QMessageBox.critical(self, "Delete failed", str(exc))
                return
            finally:
                conn.close()

        self._table.removeRow(row)
        if self._table.rowCount() == 0:
            self._append_empty_row()
        if self._on_changed:
            self._on_changed()


class SolverWorker(QObject):
    log = pyqtSignal(str)
    finished = pyqtSignal(object)

    def __init__(
        self,
        *,
        year: int,
        month: int,
        clinic_id: int,
        solver_input: dict[str, object],
        config: SolverConfig,
    ) -> None:
        super().__init__()
        self._year = year
        self._month = month
        self._clinic_id = clinic_id
        self._solver_input = solver_input
        self._config = config

    def run(self) -> None:
        result = solve_month_schedule(
            year=self._year,
            month=self._month,
            clinic_id=self._clinic_id,
            solver_input=self._solver_input,
            config=self._config,
            logger=self.log.emit,
        )
        self.finished.emit(result)


class SolverTabPage(QWidget):
    def __init__(
        self,
        *,
        get_month: Callable[[], tuple[int, int]],
        on_apply: Callable[[], None],
    ) -> None:
        super().__init__()
        self._get_month = get_month
        self._on_apply = on_apply
        self._year, self._month = self._get_month()
        self._running = False
        self._current_clinic_id: int | None = None
        self._solutions: list[SolverSolution] = []
        self._solution_index = 0
        self._fixed_assignments: dict[tuple[str, str], int] = {}
        self._employee_names: dict[int, str] = {}
        self._employee_clinic_ids: dict[int, int] = {}
        self._clinic_names: dict[int, str] = {}
        self._worker_thread: QThread | None = None
        self._worker: SolverWorker | None = None

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(8, 8, 8, 8)

        self._title = QLabel()
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._title)

        settings = QHBoxLayout()
        self._spin_max_solutions = QSpinBox()
        self._spin_max_solutions.setRange(1, 20)
        self._spin_max_solutions.setValue(7)
        self._spin_time_limit = QDoubleSpinBox()
        self._spin_time_limit.setRange(1.0, 120.0)
        self._spin_time_limit.setSingleStep(1.0)
        self._spin_time_limit.setSuffix(" s")
        self._spin_time_limit.setValue(8.0)
        self._spin_prefer_off_penalty = QSpinBox()
        self._spin_prefer_off_penalty.setRange(0, 100)
        self._spin_prefer_off_penalty.setValue(8)
        self._spin_prefer_work_reward = QSpinBox()
        self._spin_prefer_work_reward.setRange(0, 100)
        self._spin_prefer_work_reward.setValue(3)
        self._spin_imbalance_weight = QSpinBox()
        self._spin_imbalance_weight.setRange(0, 100)
        self._spin_imbalance_weight.setValue(6)
        self._chk_soft_clinic_rule = QCheckBox("Clinic/day rule soft")
        self._chk_soft_clinic_rule.setChecked(False)
        self._spin_clinic_duplicate_penalty = QSpinBox()
        self._spin_clinic_duplicate_penalty.setRange(0, 500)
        self._spin_clinic_duplicate_penalty.setValue(20)
        self._spin_clinic_duplicate_penalty.setEnabled(False)
        self._chk_soft_clinic_rule.toggled.connect(
            self._spin_clinic_duplicate_penalty.setEnabled
        )

        settings.addWidget(QLabel("Max solutions"))
        settings.addWidget(self._spin_max_solutions)
        settings.addWidget(QLabel("Time limit"))
        settings.addWidget(self._spin_time_limit)
        settings.addWidget(QLabel("Prefer-off penalty"))
        settings.addWidget(self._spin_prefer_off_penalty)
        settings.addWidget(QLabel("Prefer-work reward"))
        settings.addWidget(self._spin_prefer_work_reward)
        settings.addWidget(QLabel("Fairness weight"))
        settings.addWidget(self._spin_imbalance_weight)
        settings.addWidget(self._chk_soft_clinic_rule)
        settings.addWidget(QLabel("Clinic duplicate penalty"))
        settings.addWidget(self._spin_clinic_duplicate_penalty)
        settings.addStretch(1)
        root.addLayout(settings)

        manual_note = QLabel(
            "Note: Employees assigned to clinic 'ZNA' are manual-only and are excluded "
            "from automatic solving."
        )
        manual_note.setWordWrap(True)
        manual_note.setStyleSheet("color: #555;")
        root.addWidget(manual_note)

        controls = QHBoxLayout()
        self._btn_solve = QPushButton("Solve")
        self._btn_solve.clicked.connect(self._solve_clicked)
        self._btn_prev_solution = QPushButton("Previous solution")
        self._btn_prev_solution.clicked.connect(lambda: self._move_solution(-1))
        self._btn_next_solution = QPushButton("Next solution")
        self._btn_next_solution.clicked.connect(lambda: self._move_solution(1))
        self._btn_apply = QPushButton("Apply selected solution")
        self._btn_apply.clicked.connect(self._apply_selected_solution)
        self._btn_export = QPushButton("Export CSV")
        self._btn_export.clicked.connect(self._export_selected_solution)
        self._solution_label = QLabel("No solution loaded.")

        controls.addWidget(self._btn_solve)
        controls.addWidget(self._btn_prev_solution)
        controls.addWidget(self._btn_next_solution)
        controls.addWidget(self._btn_apply)
        controls.addWidget(self._btn_export)
        controls.addStretch(1)
        controls.addWidget(self._solution_label)
        root.addLayout(controls)

        self._preview = QTableWidget()
        self._preview.setAlternatingRowColors(True)
        self._preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._preview.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._preview.verticalHeader().setDefaultSectionSize(28)
        ph = self._preview.horizontalHeader()
        ph.setStretchLastSection(True)
        for col in range(len(SHIFT_SLOT_CODES)):
            ph.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._preview, stretch=1)

        log_label = QLabel("Solver log")
        root.addWidget(log_label)
        self._log_window = QPlainTextEdit()
        self._log_window.setReadOnly(True)
        self._log_window.document().setMaximumBlockCount(2000)
        root.addWidget(self._log_window, stretch=1)

        self._update_title()
        self._render_solution_preview()
        self._update_solution_controls()

    def set_month(self, year: int, month: int) -> None:
        changed = (self._year, self._month) != (year, month)
        self._year, self._month = year, month
        self._update_title()
        if changed and not self._running:
            self._solutions = []
            self._solution_index = 0
            self._fixed_assignments = {}
            self._solution_label.setText("No solution loaded.")
            self._render_solution_preview()
            self._update_solution_controls()

    def _update_title(self) -> None:
        self._title.setText(date(self._year, self._month, 1).strftime("Solver - %B %Y"))

    def _update_solution_controls(self) -> None:
        has = bool(self._solutions)
        can_switch = has and len(self._solutions) > 1 and not self._running
        self._btn_prev_solution.setEnabled(can_switch)
        self._btn_next_solution.setEnabled(can_switch)
        self._btn_apply.setEnabled(has and not self._running)
        self._btn_export.setEnabled(has and not self._running)
        self._btn_solve.setEnabled(not self._running)
        self._spin_max_solutions.setEnabled(not self._running)
        self._spin_time_limit.setEnabled(not self._running)
        self._spin_prefer_off_penalty.setEnabled(not self._running)
        self._spin_prefer_work_reward.setEnabled(not self._running)
        self._spin_imbalance_weight.setEnabled(not self._running)
        self._chk_soft_clinic_rule.setEnabled(not self._running)
        self._spin_clinic_duplicate_penalty.setEnabled(
            (not self._running) and self._chk_soft_clinic_rule.isChecked()
        )

    def _log(self, msg: str) -> None:
        self._log_window.appendPlainText(msg)

    def _solve_clicked(self) -> None:
        if self._running:
            return
        conn = get_connection()
        try:
            clinic_id = get_first_clinic_id(conn)
            if clinic_id is None:
                QMessageBox.warning(self, "Solver", "No clinic found in database.")
                return
            solver_input = load_solver_month_input(
                conn,
                clinic_id=clinic_id,
                year=self._year,
                month=self._month,
            )
            clinics = list_clinics(conn)
        finally:
            conn.close()

        employees = list(solver_input.get("employees", []))
        if not employees:
            QMessageBox.warning(
                self,
                "Solver",
                "No active employees available for solving.",
            )
            return

        self._employee_names = {
            int(k): str(v)
            for k, v in dict(solver_input.get("employee_name_by_id", {})).items()
        }
        self._employee_clinic_ids = {
            int(k): int(v)
            for k, v in dict(solver_input.get("employee_clinic_by_id", {})).items()
        }
        self._clinic_names = {int(r["id"]): str(r["name"]) for r in clinics}
        self._fixed_assignments = {
            (str(k[0]), str(k[1])): int(v)
            for k, v in dict(solver_input.get("fixed_assignments", {})).items()
        }
        self._current_clinic_id = clinic_id
        self._solutions = []
        self._solution_index = 0
        self._log_window.clear()
        self._log(f"Starting solver for {self._year:04d}-{self._month:02d} ...")
        self._running = True
        self._update_solution_controls()
        solver_config = SolverConfig(
            max_solutions=int(self._spin_max_solutions.value()),
            time_limit_seconds=float(self._spin_time_limit.value()),
            prefer_off_penalty=int(self._spin_prefer_off_penalty.value()),
            prefer_work_reward=int(self._spin_prefer_work_reward.value()),
            imbalance_weight=int(self._spin_imbalance_weight.value()),
            clinic_uniqueness_soft=self._chk_soft_clinic_rule.isChecked(),
            clinic_duplicate_penalty=int(self._spin_clinic_duplicate_penalty.value()),
        )
        self._log(
            "Settings: "
            f"max_solutions={solver_config.max_solutions}, "
            f"time_limit={solver_config.time_limit_seconds:.1f}s, "
            f"prefer_off_penalty={solver_config.prefer_off_penalty}, "
            f"prefer_work_reward={solver_config.prefer_work_reward}, "
            f"imbalance_weight={solver_config.imbalance_weight}, "
            f"clinic_rule_soft={solver_config.clinic_uniqueness_soft}, "
            f"clinic_duplicate_penalty={solver_config.clinic_duplicate_penalty}"
        )

        self._worker_thread = QThread(self)
        self._worker = SolverWorker(
            year=self._year,
            month=self._month,
            clinic_id=clinic_id,
            solver_input=solver_input,
            config=solver_config,
        )
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.log.connect(self._log)
        self._worker.finished.connect(self._on_solver_finished)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_solver_finished(self, result_obj: object) -> None:
        self._running = False
        result = result_obj if isinstance(result_obj, SolverResult) else None
        if result is None:
            self._log("Solver failed: invalid worker result.")
            self._solutions = []
            self._solution_index = 0
            self._solution_label.setText("No solution loaded.")
            self._render_solution_preview()
            self._update_solution_controls()
            return

        self._solutions = result.solutions
        self._solution_index = 0
        self._log(result.message)
        if not self._solutions:
            self._solution_label.setText("No solution loaded.")
            self._render_solution_preview()
            self._update_solution_controls()
            QMessageBox.information(self, "Solver", result.message)
            return

        self._update_solution_label()
        self._render_solution_preview()
        self._update_solution_controls()
        QMessageBox.information(
            self,
            "Solver",
            f"{result.message} Use previous/next to browse candidates.",
        )

    def _move_solution(self, step: int) -> None:
        if not self._solutions:
            return
        self._solution_index = (self._solution_index + step) % len(self._solutions)
        self._update_solution_label()
        self._render_solution_preview()

    def _update_solution_label(self) -> None:
        if not self._solutions:
            self._solution_label.setText("No solution loaded.")
            return
        current = self._solutions[self._solution_index]
        self._solution_label.setText(
            f"Solution {self._solution_index + 1}/{len(self._solutions)} "
            f"(objective {current.objective_value})"
        )

    def _render_solution_preview(self) -> None:
        _, days_in_month = calendar.monthrange(self._year, self._month)
        self._preview.clear()
        self._preview.setRowCount(days_in_month)
        self._preview.setColumnCount(len(SHIFT_SLOT_CODES))
        self._preview.setHorizontalHeaderLabels(
            [SHIFT_SLOT_LABELS[slot] for slot in SHIFT_SLOT_CODES]
        )
        self._preview.setVerticalHeaderLabels(
            [
                date(self._year, self._month, d).strftime("%a %d")
                for d in range(1, days_in_month + 1)
            ]
        )
        if not self._solutions:
            return

        current = self._solutions[self._solution_index]
        for d in range(1, days_in_month + 1):
            iso = date(self._year, self._month, d).isoformat()
            for col, slot in enumerate(SHIFT_SLOT_CODES):
                employee_id = current.assignments.get((iso, slot))
                text = "—"
                if employee_id is not None:
                    text = self._employee_names.get(int(employee_id), f"#{employee_id}")
                self._preview.setItem(d - 1, col, QTableWidgetItem(text))

    def _apply_selected_solution(self) -> None:
        if not self._solutions:
            QMessageBox.information(self, "Solver", "No solution available to apply.")
            return
        if self._current_clinic_id is None:
            QMessageBox.warning(self, "Solver", "No clinic available for writing.")
            return
        conn = get_connection()
        try:
            replace_month_shift_assignments(
                conn,
                clinic_id=self._current_clinic_id,
                year=self._year,
                month=self._month,
                assignments=self._solutions[self._solution_index].assignments,
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            QMessageBox.critical(self, "Solver apply failed", str(exc))
            return
        finally:
            conn.close()
        self._on_apply()
        QMessageBox.information(self, "Solver", "Selected solution applied successfully.")

    def _export_selected_solution(self) -> None:
        if not self._solutions:
            QMessageBox.information(self, "Solver", "No solution available to export.")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export schedule CSV",
            f"schedule_{self._year:04d}_{self._month:02d}.csv",
            "CSV files (*.csv)",
        )
        if not filename:
            return
        current = self._solutions[self._solution_index]
        slot_order = {slot: idx for idx, slot in enumerate(SHIFT_SLOT_CODES)}
        rows: list[tuple[str, str, str, str, str, str, str, str]] = []
        for (iso, slot), employee_id in sorted(
            current.assignments.items(), key=lambda item: (item[0][0], slot_order[item[0][1]])
        ):
            weekday = date.fromisoformat(iso).strftime("%A")
            emp_id = int(employee_id)
            emp_name = self._employee_names.get(emp_id, f"#{emp_id}")
            emp_clinic_id = self._employee_clinic_ids.get(emp_id)
            clinic_name = (
                self._clinic_names.get(emp_clinic_id, f"#{emp_clinic_id}")
                if emp_clinic_id is not None
                else ""
            )
            source = "manual-fixed" if (iso, slot) in self._fixed_assignments else "solver"
            rows.append(
                (
                    iso,
                    weekday,
                    slot,
                    SHIFT_SLOT_LABELS[slot],
                    str(emp_id),
                    emp_name,
                    clinic_name,
                    source,
                )
            )
        try:
            with open(filename, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "date",
                        "weekday",
                        "slot_code",
                        "slot_label",
                        "employee_id",
                        "employee_name",
                        "employee_clinic",
                        "source",
                    ]
                )
                writer.writerows(rows)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export", "CSV export created.")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dienstplaner")
        self.resize(1280, 680)

        today = date.today()
        self._year = today.year
        self._month = today.month
        self._schedule_loading = False

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)

        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        self._schedule_tab = self._build_schedule_tab()
        self._tabs.addTab(self._schedule_tab, "Schedule")

        self._absences_page = AbsencesPreferencesPage()
        self._employees_page = EmployeeListPage(
            on_changed=self._refresh_after_employee_edit,
        )
        self._solver_page = SolverTabPage(
            get_month=lambda: (self._year, self._month),
            on_apply=self._refresh_after_employee_edit,
        )
        self._tabs.addTab(self._employees_page, "Employees")
        self._tabs.addTab(self._absences_page, "Absences & preferences")
        self._tabs.addTab(self._solver_page, "Solver")

        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._rebuild_schedule_table()

    def _refresh_after_employee_edit(self) -> None:
        self._rebuild_schedule_table()
        self._absences_page.reload()
        self._solver_page.set_month(self._year, self._month)

    def _on_tab_changed(self, index: int) -> None:
        if index == 0:
            self._rebuild_schedule_table()
        elif self._tabs.widget(index) is self._absences_page:
            self._absences_page.reload()
        elif self._tabs.widget(index) is self._solver_page:
            self._solver_page.set_month(self._year, self._month)

    def _build_schedule_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.setContentsMargins(8, 8, 8, 8)

        self._title = QLabel()
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._btn_prev = QPushButton("‹")
        self._btn_prev.setFixedWidth(44)
        self._btn_prev.setToolTip("Previous month")
        self._btn_prev.clicked.connect(self._prev_month)

        self._btn_next = QPushButton("›")
        self._btn_next.setFixedWidth(44)
        self._btn_next.setToolTip("Next month")
        self._btn_next.clicked.connect(self._next_month)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)
        nav_row.addWidget(self._btn_prev)
        nav_row.addStretch(1)
        nav_row.addWidget(self._title)
        nav_row.addStretch(1)
        nav_row.addWidget(self._btn_next)
        layout.addLayout(nav_row)

        self._btn_today = QPushButton("Today")
        self._btn_today.setToolTip("Jump to current month")
        self._btn_today.clicked.connect(self._go_today)
        today_row = QHBoxLayout()
        today_row.addStretch(1)
        today_row.addWidget(self._btn_today)
        today_row.addStretch(1)
        layout.addLayout(today_row)

        self._table_frame = QFrame()
        self._table_frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame_layout = QVBoxLayout(self._table_frame)
        frame_layout.setContentsMargins(8, 8, 8, 8)

        self._schedule_table = QTableWidget()
        self._schedule_table.setAlternatingRowColors(True)
        self._schedule_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._schedule_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._schedule_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._schedule_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        self._schedule_table.verticalHeader().setDefaultSectionSize(30)
        self._schedule_table.verticalHeader().setMinimumWidth(88)

        hdr = self._schedule_table.horizontalHeader()
        hdr.setStretchLastSection(True)
        for col in range(len(SHIFT_SLOT_CODES)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)

        frame_layout.addWidget(self._schedule_table)
        layout.addWidget(self._table_frame, stretch=1)

        return page

    def _prev_month(self) -> None:
        if self._month == 1:
            self._month = 12
            self._year -= 1
        else:
            self._month -= 1
        self._rebuild_schedule_table()

    def _next_month(self) -> None:
        if self._month == 12:
            self._month = 1
            self._year += 1
        else:
            self._month += 1
        self._rebuild_schedule_table()

    def _go_today(self) -> None:
        t = date.today()
        self._year, self._month = t.year, t.month
        self._rebuild_schedule_table()

    def _month_title(self) -> str:
        return date(self._year, self._month, 1).strftime("%B %Y")

    def _rebuild_schedule_table(self) -> None:
        self._title.setText(self._month_title())
        self._solver_page.set_month(self._year, self._month)

        _, days_in_month = calendar.monthrange(self._year, self._month)
        self._schedule_loading = True
        self._schedule_table.clear()
        self._schedule_table.setRowCount(days_in_month)
        self._schedule_table.setColumnCount(len(SHIFT_SLOT_CODES))

        headers = [SHIFT_SLOT_LABELS[code] for code in SHIFT_SLOT_CODES]
        self._schedule_table.setHorizontalHeaderLabels(headers)

        today = date.today()
        row_labels: list[str] = []
        for d in range(1, days_in_month + 1):
            day_dt = date(self._year, self._month, d)
            row_labels.append(day_dt.strftime("%a %d"))

        self._schedule_table.setVerticalHeaderLabels(row_labels)

        ids_map: dict[tuple[str, str], int] = {}
        conn = get_connection()
        try:
            clinic_id = get_first_clinic_id(conn)
            if clinic_id is not None:
                ids_map = load_month_shift_employee_ids(
                    conn, clinic_id=clinic_id, year=self._year, month=self._month
                )
            for d in range(1, days_in_month + 1):
                day_dt = date(self._year, self._month, d)
                iso = day_dt.isoformat()
                is_today = day_dt == today
                for col, slot in enumerate(SHIFT_SLOT_CODES):
                    current_id = ids_map.get((iso, slot))
                    cb = self._make_schedule_shift_combo(
                        conn,
                        clinic_id=clinic_id,
                        shift_date_iso=iso,
                        shift_slot=slot,
                        current_employee_id=current_id,
                        highlight_today=is_today,
                    )
                    self._schedule_table.setCellWidget(d - 1, col, cb)
        finally:
            conn.close()

        self._schedule_loading = False

    def _make_schedule_shift_combo(
        self,
        conn: sqlite3.Connection,
        *,
        clinic_id: int | None,
        shift_date_iso: str,
        shift_slot: str,
        current_employee_id: int | None,
        highlight_today: bool,
    ) -> QComboBox:
        cb = QComboBox()
        cb.setProperty("shift_date_iso", shift_date_iso)
        cb.setProperty("shift_slot", shift_slot)
        cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

        if clinic_id is None:
            cb.addItem("No clinic", None)
            cb.setEnabled(False)
            return cb

        eligible = list_employees_eligible_for_schedule_slot(conn, shift_slot)
        eligible_ids = {int(r["id"]) for r in eligible}

        cb.addItem("—", None)
        for row in eligible:
            cb.addItem(str(row["name"]), int(row["id"]))

        if (
            current_employee_id is not None
            and current_employee_id not in eligible_ids
        ):
            name = get_employee_name(conn, current_employee_id)
            label = (
                f"{name} (not qualified)"
                if name
                else f"#{current_employee_id} (not qualified)"
            )
            cb.addItem(label, current_employee_id)

        if current_employee_id is not None:
            for i in range(cb.count()):
                data = cb.itemData(i)
                if data is not None and int(data) == current_employee_id:
                    cb.setCurrentIndex(i)
                    break
        else:
            cb.setCurrentIndex(0)

        if highlight_today:
            cb.setStyleSheet(
                "QComboBox { background-color: #e8f4fc; font-weight: bold; }"
            )

        cb.currentIndexChanged.connect(self._on_schedule_shift_combo_changed)
        return cb

    def _on_schedule_shift_combo_changed(self, _index: int) -> None:
        if self._schedule_loading:
            return
        cb = self.sender()
        if not isinstance(cb, QComboBox) or not cb.isEnabled():
            return
        iso = cb.property("shift_date_iso")
        slot = cb.property("shift_slot")
        if not isinstance(iso, str) or not isinstance(slot, str):
            return
        raw = cb.currentData()
        employee_id = int(raw) if raw is not None else None

        conn = get_connection()
        try:
            clinic_id = get_first_clinic_id(conn)
            if clinic_id is None:
                QMessageBox.warning(
                    self,
                    "Schedule",
                    "No clinic is defined; cannot save shift assignments.",
                )
                return
            set_shift_assignment(conn, clinic_id, iso, slot, employee_id)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            QMessageBox.critical(self, "Schedule save failed", str(exc))
            self._rebuild_schedule_table()
        finally:
            conn.close()


def main() -> int:
    init_database()

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
