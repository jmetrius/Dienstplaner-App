"""
PyQt6 entry point: main window with schedule and employee management tabs.
"""

from __future__ import annotations

import calendar
import sqlite3
import sys
from collections.abc import Callable
from datetime import date

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database import (
    CANONICAL_QUALIFICATIONS,
    SHIFT_SLOT_CODES,
    SHIFT_SLOT_LABELS,
    delete_employee,
    get_connection,
    get_first_clinic_id,
    init_database,
    insert_employee,
    list_canonical_clinics,
    list_employees,
    list_qualifications_ordered,
    load_month_assignments,
    update_employee,
)

N_QUAL = len(CANONICAL_QUALIFICATIONS)
COL_NAME = 0
COL_QUAL_BASE = 1
COL_CLINIC = COL_QUAL_BASE + N_QUAL
COL_MAX_SHIFTS = COL_CLINIC + 1
COL_ACTIVE = COL_MAX_SHIFTS + 1
N_COLS = COL_ACTIVE + 1


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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dienstplaner")
        self.resize(1280, 680)

        today = date.today()
        self._year = today.year
        self._month = today.month

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)

        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        self._schedule_tab = self._build_schedule_tab()
        self._tabs.addTab(self._schedule_tab, "Schedule")

        self._employees_page = EmployeeListPage(on_changed=self._rebuild_schedule_table)
        self._tabs.addTab(self._employees_page, "Employees")

        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._rebuild_schedule_table()

    def _on_tab_changed(self, index: int) -> None:
        if index == 0:
            self._rebuild_schedule_table()

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
        self._schedule_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._schedule_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        self._schedule_table.verticalHeader().setDefaultSectionSize(26)
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

        _, days_in_month = calendar.monthrange(self._year, self._month)
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

        assignments: dict[tuple[str, str], str] = {}
        conn = get_connection()
        try:
            clinic_id = get_first_clinic_id(conn)
            if clinic_id is not None:
                assignments = load_month_assignments(
                    conn, clinic_id=clinic_id, year=self._year, month=self._month
                )
        finally:
            conn.close()

        today_bg = QBrush(QColor("#e8f4fc"))
        for d in range(1, days_in_month + 1):
            day_dt = date(self._year, self._month, d)
            iso = day_dt.isoformat()
            is_today = day_dt == today
            for col, slot in enumerate(SHIFT_SLOT_CODES):
                text = assignments.get((iso, slot), "")
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                )
                if is_today:
                    item.setBackground(today_bg)
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self._schedule_table.setItem(d - 1, col, item)


def main() -> int:
    init_database()

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
