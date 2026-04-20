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
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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
    QTreeWidget,
    QTreeWidgetItem,
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
    SHIFT_SLOTS_HAUSDienst,
    SHIFT_SLOTS_ZNA,
    WEEKDAY_CODES,
    WEEKDAY_LABELS,
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
    replace_employee_same_day_exclusions,
    set_shift_assignment,
    update_absence,
    update_employee,
    update_shift_preference,
)
from solver import SolverConfig, SolverResult, SolverSolution, solve_month_schedule

N_QUAL = len(CANONICAL_QUALIFICATIONS)
N_WEEKDAYS = len(WEEKDAY_CODES)
COL_NAME = 0
COL_QUAL_BASE = 1
COL_BLOCKED_BASE = COL_QUAL_BASE + N_QUAL
COL_CLINIC = COL_BLOCKED_BASE + N_WEEKDAYS
COL_MAX_SHIFTS = COL_CLINIC + 1
COL_INCOMPATIBLE = COL_MAX_SHIFTS + 1
COL_ACTIVE = COL_INCOMPATIBLE + 1
N_COLS = COL_ACTIVE + 1


def _iso_from_qdate(d: QDate) -> str:
    return f"{d.year()}-{d.month():02d}-{d.day():02d}"


def _qdate_from_iso(s: str) -> QDate:
    y, m, dd = (int(x) for x in s.split("-", 2))
    return QDate(y, m, dd)


class MultiSelectComboBox(QComboBox):
    selection_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        line = self.lineEdit()
        if line is not None:
            line.setReadOnly(True)
            line.setPlaceholderText("Select employee(s)")
        self.activated.connect(self._on_item_activated)

    def add_check_item(self, text: str, data: int, checked: bool = False) -> None:
        self.addItem(text, data)
        idx = self.count() - 1
        item = self.model().item(idx, 0)
        if item is None:
            return
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        item.setData(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
            Qt.ItemDataRole.CheckStateRole,
        )
        self._refresh_display_text()
        self.setCurrentIndex(-1)

    def checked_ids(self) -> set[int]:
        out: set[int] = set()
        for idx in range(self.count()):
            item = self.model().item(idx, 0)
            if item is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                raw = self.itemData(idx)
                if raw is None:
                    continue
                out.add(int(raw))
        return out

    def set_checked(self, employee_id: int, checked: bool) -> None:
        for idx in range(self.count()):
            raw = self.itemData(idx)
            if raw is None or int(raw) != employee_id:
                continue
            item = self.model().item(idx, 0)
            if item is None:
                return
            next_state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            if item.checkState() == next_state:
                return
            item.setCheckState(next_state)
            self._refresh_display_text()
            self.selection_changed.emit()
            return

    def _on_item_activated(self, row: int) -> None:
        if row < 0:
            return
        item = self.model().item(row, 0)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        self._refresh_display_text()
        self.setCurrentIndex(-1)
        self.selection_changed.emit()

    def _refresh_display_text(self) -> None:
        names: list[str] = []
        for idx in range(self.count()):
            item = self.model().item(idx, 0)
            if item is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                names.append(self.itemText(idx))
        text = ", ".join(names)
        line = self.lineEdit()
        if line is not None:
            line.setText(text)


class AbsencesPreferencesPage(QWidget):
    """
    Month-scoped management of hard absences and soft shift preferences.
    Rows overlap the selected month (ranges may extend outside it).
    """

    A_COL_EMP = 0
    A_COL_START = 1
    A_COL_END = 2
    A_COL_CAT = 3
    A_COL_NOTES = 4

    P_COL_EMP = 0
    P_COL_START = 1
    P_COL_END = 2
    P_COL_PREF = 3
    P_COL_NOTES = 4

    def __init__(
        self, on_month_changed: Callable[[int, int], None] | None = None
    ) -> None:
        super().__init__()
        self._on_month_changed = on_month_changed
        today = date.today()
        self._year = today.year
        self._month = today.month
        self._employees: list[sqlite3.Row] = []
        self._employee_name_by_id: dict[int, str] = {}
        self._loaded_absence_ids: set[int] = set()
        self._loaded_preference_ids: set[int] = set()
        self._abs_parent_by_employee_id: dict[int, QTreeWidgetItem] = {}
        self._pref_parent_by_employee_id: dict[int, QTreeWidgetItem] = {}

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
            "outside the month). Preferences: date ranges are supported and are expanded "
            "to single days for the solver. Quick add supports entries like 1,3,5-8."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        root.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Vertical)

        abs_box = QGroupBox("Absences (cannot work — e.g. sick leave, vacation)")
        abs_lay = QVBoxLayout(abs_box)
        abs_bar = QHBoxLayout()
        self._btn_abs_add = QPushButton("Add range")
        self._btn_abs_quick_add = QPushButton("Quick add days/ranges")
        self._btn_abs_del = QPushButton("Delete selected range")
        self._btn_abs_add.clicked.connect(self._add_absence_row)
        self._btn_abs_quick_add.clicked.connect(self._quick_add_absence_ranges)
        self._btn_abs_del.clicked.connect(self._delete_absence_selected)
        abs_bar.addWidget(self._btn_abs_add)
        abs_bar.addWidget(self._btn_abs_quick_add)
        abs_bar.addWidget(self._btn_abs_del)
        abs_bar.addStretch(1)
        abs_lay.addLayout(abs_bar)

        self._abs_tree = QTreeWidget()
        self._abs_tree.setColumnCount(5)
        self._abs_tree.setHeaderLabels(
            ["Employee", "From", "To", "Category", "Notes"]
        )
        self._abs_tree.setAlternatingRowColors(True)
        self._abs_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._abs_tree.setRootIsDecorated(True)
        self._abs_tree.setItemsExpandable(True)
        self._abs_tree.setUniformRowHeights(False)
        ah = self._abs_tree.header()
        ah.setSectionResizeMode(self.A_COL_EMP, QHeaderView.ResizeMode.Stretch)
        ah.setSectionResizeMode(self.A_COL_START, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(self.A_COL_END, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(self.A_COL_CAT, QHeaderView.ResizeMode.ResizeToContents)
        ah.setSectionResizeMode(self.A_COL_NOTES, QHeaderView.ResizeMode.Stretch)
        abs_lay.addWidget(self._abs_tree)
        splitter.addWidget(abs_box)

        pref_box = QGroupBox("Shift preferences (date ranges)")
        pref_lay = QVBoxLayout(pref_box)
        pref_bar = QHBoxLayout()
        self._btn_pref_add = QPushButton("Add range")
        self._btn_pref_quick_add = QPushButton("Quick add days/ranges")
        self._btn_pref_del = QPushButton("Delete selected range")
        self._btn_pref_add.clicked.connect(self._add_pref_row)
        self._btn_pref_quick_add.clicked.connect(self._quick_add_preference_ranges)
        self._btn_pref_del.clicked.connect(self._delete_pref_selected)
        pref_bar.addWidget(self._btn_pref_add)
        pref_bar.addWidget(self._btn_pref_quick_add)
        pref_bar.addWidget(self._btn_pref_del)
        pref_bar.addStretch(1)
        pref_lay.addLayout(pref_bar)

        self._pref_tree = QTreeWidget()
        self._pref_tree.setColumnCount(5)
        self._pref_tree.setHeaderLabels(
            ["Employee", "From", "To", "Preference", "Notes"]
        )
        self._pref_tree.setAlternatingRowColors(True)
        self._pref_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._pref_tree.setRootIsDecorated(True)
        self._pref_tree.setItemsExpandable(True)
        self._pref_tree.setUniformRowHeights(False)
        ph = self._pref_tree.header()
        ph.setSectionResizeMode(self.P_COL_EMP, QHeaderView.ResizeMode.Stretch)
        ph.setSectionResizeMode(self.P_COL_START, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(self.P_COL_END, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(self.P_COL_PREF, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(self.P_COL_NOTES, QHeaderView.ResizeMode.Stretch)
        pref_lay.addWidget(self._pref_tree)
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
        if self._on_month_changed is not None:
            self._on_month_changed(self._year, self._month)
        self.reload()

    def _next_month(self) -> None:
        if self._month == 12:
            self._month = 1
            self._year += 1
        else:
            self._month += 1
        if self._on_month_changed is not None:
            self._on_month_changed(self._year, self._month)
        self.reload()

    def _go_today_month(self) -> None:
        t = date.today()
        self._year, self._month = t.year, t.month
        if self._on_month_changed is not None:
            self._on_month_changed(self._year, self._month)
        self.reload()

    def set_month(self, year: int, month: int) -> None:
        if (self._year, self._month) == (year, month):
            return
        self._year, self._month = year, month
        self.reload()

    def _month_title(self) -> str:
        return date(self._year, self._month, 1).strftime("%B %Y")

    def reload(self) -> None:
        self._title.setText(self._month_title())
        conn = get_connection()
        try:
            self._employees = list_employee_options(conn)
            self._employee_name_by_id = {
                int(row["id"]): str(row["name"]) for row in self._employees
            }
            abs_rows = list_absences_overlapping_month(
                conn, year=self._year, month=self._month
            )
            pref_rows = list_preferences_overlapping_month(
                conn, year=self._year, month=self._month
            )
        finally:
            conn.close()

        self._loaded_absence_ids = {int(row["id"]) for row in abs_rows}
        self._loaded_preference_ids = {int(row["id"]) for row in pref_rows}

        self._abs_tree.clear()
        self._pref_tree.clear()
        self._abs_parent_by_employee_id.clear()
        self._pref_parent_by_employee_id.clear()
        for employee in self._employees:
            employee_id = int(employee["id"])
            employee_name = str(employee["name"])
            self._abs_parent_by_employee_id[employee_id] = self._make_parent_item(
                self._abs_tree, employee_id, employee_name
            )
            self._pref_parent_by_employee_id[employee_id] = self._make_parent_item(
                self._pref_tree, employee_id, employee_name
            )

        for ar in abs_rows:
            self._append_absence_row(ar)
        for pr in pref_rows:
            self._append_preference_row(pr)
        self._refresh_parent_summaries(self._abs_parent_by_employee_id, self._abs_tree)
        self._refresh_parent_summaries(self._pref_parent_by_employee_id, self._pref_tree)

    def _make_parent_item(
        self, tree: QTreeWidget, employee_id: int, employee_name: str
    ) -> QTreeWidgetItem:
        parent = QTreeWidgetItem([employee_name, "", "", "", ""])
        parent.setData(self.A_COL_EMP, Qt.ItemDataRole.UserRole, employee_id)
        tree.addTopLevelItem(parent)
        parent.setExpanded(False)
        return parent

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

    def _default_pref_dates(self) -> tuple[QDate, QDate]:
        d = self._default_pref_date()
        return d, d

    def _selected_parent(self, tree: QTreeWidget) -> QTreeWidgetItem | None:
        item = tree.currentItem()
        parent = item if item and item.parent() is None else item.parent() if item else None
        if parent is not None:
            return parent
        return tree.topLevelItem(0)

    def _day_ranges_prompt(self, employee_name: str, target_label: str) -> str | None:
        last_day = calendar.monthrange(self._year, self._month)[1]
        text, ok = QInputDialog.getText(
            self,
            f"Quick add {target_label}",
            (
                f"Enter day numbers or ranges for {employee_name} in {self._month_title()}.\n"
                f"Examples: 1,3,5-8,15 (allowed days: 1-{last_day})"
            ),
        )
        if not ok:
            return None
        value = text.strip()
        if not value:
            return None
        return value

    def _parse_day_ranges_input(
        self, raw: str
    ) -> tuple[list[tuple[QDate, QDate]], str | None]:
        last_day = calendar.monthrange(self._year, self._month)[1]
        tokens = [token.strip() for token in raw.replace(";", ",").split(",")]
        days: set[int] = set()
        for token in tokens:
            if not token:
                continue
            if "-" in token:
                left, right = (part.strip() for part in token.split("-", 1))
                if not left.isdigit() or not right.isdigit():
                    return [], f"Invalid range '{token}'. Use format like 5-8."
                start = int(left)
                end = int(right)
                if start > end:
                    return [], f"Invalid range '{token}': start must be <= end."
                if start < 1 or end > last_day:
                    return [], f"Range '{token}' is outside valid days 1-{last_day}."
                for day in range(start, end + 1):
                    days.add(day)
            else:
                if not token.isdigit():
                    return [], f"Invalid day '{token}'. Use numbers like 1,3,15."
                day = int(token)
                if day < 1 or day > last_day:
                    return [], f"Day '{token}' is outside valid days 1-{last_day}."
                days.add(day)

        if not days:
            return [], "Please enter at least one valid day."

        sorted_days = sorted(days)
        ranges: list[tuple[QDate, QDate]] = []
        start_day = sorted_days[0]
        end_day = start_day
        for day in sorted_days[1:]:
            if day == end_day + 1:
                end_day = day
                continue
            ranges.append(
                (
                    QDate(self._year, self._month, start_day),
                    QDate(self._year, self._month, end_day),
                )
            )
            start_day = day
            end_day = day
        ranges.append(
            (
                QDate(self._year, self._month, start_day),
                QDate(self._year, self._month, end_day),
            )
        )
        return ranges, None

    def _quick_add_absence_ranges(self) -> None:
        if not self._employees:
            QMessageBox.information(
                self,
                "Employees",
                "Add employees on the Employees tab first.",
            )
            return
        parent = self._selected_parent(self._abs_tree)
        if parent is None:
            return
        employee_name = parent.text(self.A_COL_EMP)
        user_input = self._day_ranges_prompt(employee_name, "absence ranges")
        if user_input is None:
            return
        ranges, err = self._parse_day_ranges_input(user_input)
        if err is not None:
            QMessageBox.warning(self, "Quick add", err)
            return
        for start_date, end_date in ranges:
            child = QTreeWidgetItem(["  -", "", "", "", ""])
            child.setData(self.A_COL_EMP, Qt.ItemDataRole.UserRole, None)
            parent.addChild(child)
            de_s = QDateEdit()
            de_s.setCalendarPopup(True)
            de_s.setDisplayFormat("yyyy-MM-dd")
            de_s.setDate(start_date)
            de_e = QDateEdit()
            de_e.setCalendarPopup(True)
            de_e.setDisplayFormat("yyyy-MM-dd")
            de_e.setDate(end_date)
            self._abs_tree.setItemWidget(child, self.A_COL_START, de_s)
            self._abs_tree.setItemWidget(child, self.A_COL_END, de_e)
            self._abs_tree.setItemWidget(
                child, self.A_COL_CAT, self._make_category_combo("other")
            )
            self._abs_tree.setItemWidget(child, self.A_COL_NOTES, QLineEdit(""))
        parent.setExpanded(True)
        self._refresh_parent_summaries(self._abs_parent_by_employee_id, self._abs_tree)

    def _quick_add_preference_ranges(self) -> None:
        if not self._employees:
            QMessageBox.information(
                self,
                "Employees",
                "Add employees on the Employees tab first.",
            )
            return
        parent = self._selected_parent(self._pref_tree)
        if parent is None:
            return
        employee_name = parent.text(self.P_COL_EMP)
        user_input = self._day_ranges_prompt(employee_name, "preference ranges")
        if user_input is None:
            return
        ranges, err = self._parse_day_ranges_input(user_input)
        if err is not None:
            QMessageBox.warning(self, "Quick add", err)
            return
        for start_date, end_date in ranges:
            child = QTreeWidgetItem(["  -", "", "", "", ""])
            child.setData(self.P_COL_EMP, Qt.ItemDataRole.UserRole, None)
            parent.addChild(child)
            de_s = QDateEdit()
            de_s.setCalendarPopup(True)
            de_s.setDisplayFormat("yyyy-MM-dd")
            de_s.setDate(start_date)
            de_e = QDateEdit()
            de_e.setCalendarPopup(True)
            de_e.setDisplayFormat("yyyy-MM-dd")
            de_e.setDate(end_date)
            self._pref_tree.setItemWidget(child, self.P_COL_START, de_s)
            self._pref_tree.setItemWidget(child, self.P_COL_END, de_e)
            self._pref_tree.setItemWidget(
                child, self.P_COL_PREF, self._make_preference_combo("prefer_off")
            )
            self._pref_tree.setItemWidget(child, self.P_COL_NOTES, QLineEdit(""))
        parent.setExpanded(True)
        self._refresh_parent_summaries(self._pref_parent_by_employee_id, self._pref_tree)

    def _append_absence_row(self, row: sqlite3.Row | None) -> None:
        if row is None:
            return
        employee_id = int(row["employee_id"])
        parent = self._abs_parent_by_employee_id.get(employee_id)
        if parent is None:
            return
        child = QTreeWidgetItem(["", "", "", "", ""])
        child.setData(self.A_COL_EMP, Qt.ItemDataRole.UserRole, int(row["id"]))
        parent.addChild(child)
        child.setText(self.A_COL_EMP, "  -")
        d_start, d_end = self._default_month_dates()
        de_s = QDateEdit()
        de_s.setCalendarPopup(True)
        de_s.setDisplayFormat("yyyy-MM-dd")
        de_e = QDateEdit()
        de_e.setCalendarPopup(True)
        de_e.setDisplayFormat("yyyy-MM-dd")
        de_s.setDate(_qdate_from_iso(str(row["start_date"])))
        de_e.setDate(_qdate_from_iso(str(row["end_date"])))
        self._abs_tree.setItemWidget(child, self.A_COL_START, de_s)
        self._abs_tree.setItemWidget(child, self.A_COL_END, de_e)

        cat = str(row["category"])
        self._abs_tree.setItemWidget(child, self.A_COL_CAT, self._make_category_combo(cat))

        notes = str(row["notes"] or "")
        self._abs_tree.setItemWidget(child, self.A_COL_NOTES, QLineEdit(notes))

    def _append_preference_row(self, row: sqlite3.Row | None) -> None:
        if row is None:
            return
        employee_id = int(row["employee_id"])
        parent = self._pref_parent_by_employee_id.get(employee_id)
        if parent is None:
            return
        child = QTreeWidgetItem(["", "", "", "", ""])
        child.setData(self.P_COL_EMP, Qt.ItemDataRole.UserRole, int(row["id"]))
        parent.addChild(child)
        child.setText(self.P_COL_EMP, "  -")

        de_s = QDateEdit()
        de_s.setCalendarPopup(True)
        de_s.setDisplayFormat("yyyy-MM-dd")
        de_e = QDateEdit()
        de_e.setCalendarPopup(True)
        de_e.setDisplayFormat("yyyy-MM-dd")
        de_s.setDate(_qdate_from_iso(str(row["start_date"])))
        de_e.setDate(_qdate_from_iso(str(row["end_date"])))
        self._pref_tree.setItemWidget(child, self.P_COL_START, de_s)
        self._pref_tree.setItemWidget(child, self.P_COL_END, de_e)

        pref = str(row["preference"])
        self._pref_tree.setItemWidget(
            child, self.P_COL_PREF, self._make_preference_combo(pref)
        )

        notes = str(row["notes"] or "")
        self._pref_tree.setItemWidget(child, self.P_COL_NOTES, QLineEdit(notes))

    def _refresh_parent_summaries(
        self, parent_map: dict[int, QTreeWidgetItem], tree: QTreeWidget
    ) -> None:
        for parent in parent_map.values():
            count = parent.childCount()
            parent.setText(1, f"{count} range(s)")
            parent.setText(2, "")
            parent.setText(3, "")
            parent.setText(4, "")
        tree.sortItems(0, Qt.SortOrder.AscendingOrder)

    def _add_absence_row(self) -> None:
        if not self._employees:
            QMessageBox.information(
                self,
                "Employees",
                "Add employees on the Employees tab first.",
            )
            return
        item = self._abs_tree.currentItem()
        parent = item if item and item.parent() is None else item.parent() if item else None
        if parent is None:
            parent = self._abs_tree.topLevelItem(0)
            if parent is None:
                return

        child = QTreeWidgetItem(["  -", "", "", "", ""])
        child.setData(self.A_COL_EMP, Qt.ItemDataRole.UserRole, None)
        parent.addChild(child)
        parent.setExpanded(True)
        d_start, d_end = self._default_month_dates()
        de_s = QDateEdit()
        de_s.setCalendarPopup(True)
        de_s.setDisplayFormat("yyyy-MM-dd")
        de_s.setDate(d_start)
        de_e = QDateEdit()
        de_e.setCalendarPopup(True)
        de_e.setDisplayFormat("yyyy-MM-dd")
        de_e.setDate(d_end)
        self._abs_tree.setItemWidget(child, self.A_COL_START, de_s)
        self._abs_tree.setItemWidget(child, self.A_COL_END, de_e)
        self._abs_tree.setItemWidget(child, self.A_COL_CAT, self._make_category_combo("other"))
        self._abs_tree.setItemWidget(child, self.A_COL_NOTES, QLineEdit(""))
        self._abs_tree.setCurrentItem(child)
        self._refresh_parent_summaries(self._abs_parent_by_employee_id, self._abs_tree)

    def _add_pref_row(self) -> None:
        if not self._employees:
            QMessageBox.information(
                self,
                "Employees",
                "Add employees on the Employees tab first.",
            )
            return
        item = self._pref_tree.currentItem()
        parent = item if item and item.parent() is None else item.parent() if item else None
        if parent is None:
            parent = self._pref_tree.topLevelItem(0)
            if parent is None:
                return

        child = QTreeWidgetItem(["  -", "", "", "", ""])
        child.setData(self.P_COL_EMP, Qt.ItemDataRole.UserRole, None)
        parent.addChild(child)
        parent.setExpanded(True)
        d_start, d_end = self._default_pref_dates()
        de_s = QDateEdit()
        de_s.setCalendarPopup(True)
        de_s.setDisplayFormat("yyyy-MM-dd")
        de_s.setDate(d_start)
        de_e = QDateEdit()
        de_e.setCalendarPopup(True)
        de_e.setDisplayFormat("yyyy-MM-dd")
        de_e.setDate(d_end)
        self._pref_tree.setItemWidget(child, self.P_COL_START, de_s)
        self._pref_tree.setItemWidget(child, self.P_COL_END, de_e)
        self._pref_tree.setItemWidget(
            child, self.P_COL_PREF, self._make_preference_combo("prefer_off")
        )
        self._pref_tree.setItemWidget(child, self.P_COL_NOTES, QLineEdit(""))
        self._pref_tree.setCurrentItem(child)
        self._refresh_parent_summaries(self._pref_parent_by_employee_id, self._pref_tree)

    @staticmethod
    def _item_record_id(item: QTreeWidgetItem, col: int) -> int | None:
        raw = item.data(col, Qt.ItemDataRole.UserRole)
        return int(raw) if raw is not None else None

    def _persist_absences_to_conn(self, conn: sqlite3.Connection) -> str | None:
        if not self._employees:
            return "No employees defined."
        seen_ids: set[int] = set()
        for employee_id, parent in self._abs_parent_by_employee_id.items():
            for i in range(parent.childCount()):
                child = parent.child(i)
                w_s = self._abs_tree.itemWidget(child, self.A_COL_START)
                w_e = self._abs_tree.itemWidget(child, self.A_COL_END)
                w_cat = self._abs_tree.itemWidget(child, self.A_COL_CAT)
                w_notes = self._abs_tree.itemWidget(child, self.A_COL_NOTES)
                if (
                    not isinstance(w_s, QDateEdit)
                    or not isinstance(w_e, QDateEdit)
                    or not isinstance(w_cat, QComboBox)
                    or not isinstance(w_notes, QLineEdit)
                ):
                    continue
                start = _iso_from_qdate(w_s.date())
                end = _iso_from_qdate(w_e.date())
                if start > end:
                    return (
                        "Absences: end date must be on or after the start date "
                        f"(employee {self._employee_name_by_id.get(employee_id, employee_id)})."
                    )
                cat_raw = w_cat.currentData()
                category = str(cat_raw) if cat_raw is not None else "other"
                notes = w_notes.text().strip() or None
                rid = self._item_record_id(child, self.A_COL_EMP)
                if rid is None:
                    insert_absence(
                        conn,
                        employee_id=employee_id,
                        start_date=start,
                        end_date=end,
                        category=category,
                        notes=notes,
                    )
                else:
                    update_absence(
                        conn,
                        rid,
                        employee_id=employee_id,
                        start_date=start,
                        end_date=end,
                        category=category,
                        notes=notes,
                    )
                    seen_ids.add(rid)
        for deleted_id in sorted(self._loaded_absence_ids - seen_ids):
            delete_absence(conn, deleted_id)
        return None

    def _persist_preferences_to_conn(self, conn: sqlite3.Connection) -> str | None:
        if not self._employees:
            return "No employees defined."
        prepared_rows: list[tuple[int | None, int, str, str, str, str | None]] = []
        per_employee_ranges: dict[int, list[tuple[str, str, str]]] = {}
        for employee_id, parent in self._pref_parent_by_employee_id.items():
            for i in range(parent.childCount()):
                child = parent.child(i)
                w_s = self._pref_tree.itemWidget(child, self.P_COL_START)
                w_e = self._pref_tree.itemWidget(child, self.P_COL_END)
                w_pf = self._pref_tree.itemWidget(child, self.P_COL_PREF)
                w_notes = self._pref_tree.itemWidget(child, self.P_COL_NOTES)
                if (
                    not isinstance(w_s, QDateEdit)
                    or not isinstance(w_e, QDateEdit)
                    or not isinstance(w_pf, QComboBox)
                    or not isinstance(w_notes, QLineEdit)
                ):
                    continue
                start = _iso_from_qdate(w_s.date())
                end = _iso_from_qdate(w_e.date())
                if start > end:
                    return (
                        "Preferences: end date must be on or after the start date "
                        f"(employee {self._employee_name_by_id.get(employee_id, employee_id)})."
                    )
                p_raw = w_pf.currentData()
                preference = str(p_raw) if p_raw is not None else "prefer_off"
                notes = w_notes.text().strip() or None
                rid = self._item_record_id(child, self.P_COL_EMP)
                prepared_rows.append((rid, employee_id, start, end, preference, notes))
                per_employee_ranges.setdefault(employee_id, []).append(
                    (start, end, preference)
                )

        for employee_id, ranges in per_employee_ranges.items():
            for i in range(len(ranges)):
                left_start, left_end, left_pref = ranges[i]
                for j in range(i + 1, len(ranges)):
                    right_start, right_end, right_pref = ranges[j]
                    overlaps = left_start <= right_end and right_start <= left_end
                    if overlaps and left_pref != right_pref:
                        employee_name = self._employee_name_by_id.get(
                            employee_id, f"#{employee_id}"
                        )
                        return (
                            "Preferences: overlapping ranges with conflicting values are "
                            f"not allowed ({employee_name}: {left_start}..{left_end} vs "
                            f"{right_start}..{right_end})."
                        )

        seen_ids: set[int] = set()
        for rid, employee_id, start, end, preference, notes in prepared_rows:
            if rid is None:
                insert_shift_preference(
                    conn,
                    employee_id=employee_id,
                    start_date=start,
                    end_date=end,
                    preference=preference,
                    notes=notes,
                )
            else:
                update_shift_preference(
                    conn,
                    rid,
                    employee_id=employee_id,
                    start_date=start,
                    end_date=end,
                    preference=preference,
                    notes=notes,
                )
                seen_ids.add(rid)
        for deleted_id in sorted(self._loaded_preference_ids - seen_ids):
            delete_shift_preference(conn, deleted_id)
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
        item = self._abs_tree.currentItem()
        if item is None or item.parent() is None:
            QMessageBox.information(self, "Delete", "Select an absence range to delete.")
            return
        parent = item.parent()
        parent.removeChild(item)
        self._refresh_parent_summaries(self._abs_parent_by_employee_id, self._abs_tree)

    def _delete_pref_selected(self) -> None:
        item = self._pref_tree.currentItem()
        if item is None or item.parent() is None:
            QMessageBox.information(
                self, "Delete", "Select a preference range to delete."
            )
            return
        parent = item.parent()
        parent.removeChild(item)
        self._refresh_parent_summaries(self._pref_parent_by_employee_id, self._pref_tree)


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
            + [f"{WEEKDAY_LABELS[d]} verfügbar" for d in WEEKDAY_CODES]
            + ["Klinik", "Max. Schichtanzahl", "No same-day with (#id,...)", "Active"]
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
        for c in range(COL_QUAL_BASE, COL_QUAL_BASE + N_QUAL):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        for c in range(COL_BLOCKED_BASE, COL_CLINIC):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_CLINIC, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_MAX_SHIFTS, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_INCOMPATIBLE, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_ACTIVE, QHeaderView.ResizeMode.ResizeToContents)
        for i, weekday in enumerate(WEEKDAY_CODES):
            item = self._table.horizontalHeaderItem(COL_BLOCKED_BASE + i)
            if item is not None:
                item.setToolTip(
                    f"{WEEKDAY_LABELS[weekday]} availability: checked = available, unchecked = blocked."
                )
        incompatible_item = self._table.horizontalHeaderItem(COL_INCOMPATIBLE)
        if incompatible_item is not None:
            incompatible_item.setToolTip(
                "Rare hard solver rule: listed employee IDs (#id) cannot be assigned on "
                "the same day as this employee. Solver-only enforcement."
            )
        layout.addWidget(self._table, stretch=1)

        self._clinics: list[sqlite3.Row] = []
        self._qual_rows: list[sqlite3.Row] = []
        self._employee_name_by_id: dict[int, str] = {}
        self._employee_row_by_id: dict[int, int] = {}
        self._syncing_incompatible = False

        self.reload()

    @staticmethod
    def _parse_qual_ids(raw: object) -> set[int]:
        if raw is None or raw == "":
            return set()
        return {int(x) for x in str(raw).split(",") if x.strip().isdigit()}

    @staticmethod
    def _parse_weekday_ids(raw: object) -> set[int]:
        if raw is None or raw == "":
            return set()
        out: set[int] = set()
        for token in str(raw).split(","):
            token = token.strip()
            if not token.isdigit():
                continue
            day = int(token)
            if day in WEEKDAY_CODES:
                out.add(day)
        return out

    @staticmethod
    def _parse_same_day_exclusion_ids(raw: object) -> set[int]:
        if raw is None or raw == "":
            return set()
        out: set[int] = set()
        for token in str(raw).split(","):
            token = token.strip()
            if token.isdigit():
                out.add(int(token))
        return out

    def _make_incompatible_combo(
        self, employee_id: int | None, selected_ids: set[int]
    ) -> MultiSelectComboBox:
        combo = MultiSelectComboBox()
        selected = set(selected_ids)
        if employee_id is not None:
            selected.discard(employee_id)
        for other_id, other_name in sorted(
            self._employee_name_by_id.items(), key=lambda item: item[1]
        ):
            if employee_id is not None and other_id == employee_id:
                continue
            combo.add_check_item(other_name, other_id, checked=(other_id in selected))
        return combo

    def reload(self) -> None:
        conn = get_connection()
        try:
            self._clinics = list_canonical_clinics(conn)
            self._qual_rows = list_qualifications_ordered(conn)
            rows = list_employees(conn)
        finally:
            conn.close()

        self._employee_name_by_id = {int(row["id"]): str(row["name"]) for row in rows}

        self._table.setRowCount(0)
        for emp in rows:
            self._append_row_from_db(emp)
        if self._table.rowCount() == 0:
            self._append_empty_row()
        self._rebuild_employee_row_lookup()

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

    def _weekday_checkboxes(self, blocked: set[int]) -> list[QCheckBox]:
        boxes: list[QCheckBox] = []
        for weekday in WEEKDAY_CODES:
            cb = QCheckBox()
            cb.setChecked(weekday not in blocked)
            cb.setProperty("weekday", weekday)
            boxes.append(cb)
        return boxes

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

        blocked_weekdays = self._parse_weekday_ids(emp["blocked_weekdays"])
        for i, chk in enumerate(self._weekday_checkboxes(blocked_weekdays)):
            self._table.setCellWidget(r, COL_BLOCKED_BASE + i, chk)

        cid = emp["clinic_id"]
        clinic_id = int(cid) if cid is not None else None
        self._table.setCellWidget(r, COL_CLINIC, self._make_clinic_combo(clinic_id))

        spin = QSpinBox()
        spin.setRange(0, 62)
        spin.setValue(int(emp["max_shifts_per_month"]))
        self._table.setCellWidget(r, COL_MAX_SHIFTS, spin)

        exclusion_ids = self._parse_same_day_exclusion_ids(emp["same_day_excluded_employee_ids"])
        incompatible_widget = self._make_incompatible_combo(eid, exclusion_ids)
        incompatible_widget.selection_changed.connect(self._mirror_incompatible_from_sender)
        self._table.setCellWidget(
            r,
            COL_INCOMPATIBLE,
            incompatible_widget,
        )

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

        for i, chk in enumerate(self._weekday_checkboxes(set())):
            self._table.setCellWidget(r, COL_BLOCKED_BASE + i, chk)

        self._table.setCellWidget(r, COL_CLINIC, self._make_clinic_combo(None))

        spin = QSpinBox()
        spin.setRange(0, 62)
        spin.setValue(6)
        self._table.setCellWidget(r, COL_MAX_SHIFTS, spin)

        incompatible_widget = self._make_incompatible_combo(None, set())
        incompatible_widget.selection_changed.connect(self._mirror_incompatible_from_sender)
        self._table.setCellWidget(
            r,
            COL_INCOMPATIBLE,
            incompatible_widget,
        )

        act = QCheckBox()
        act.setChecked(True)
        self._table.setCellWidget(r, COL_ACTIVE, act)

    def _add_row(self) -> None:
        self._append_empty_row()
        self._rebuild_employee_row_lookup()

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

    def _collect_blocked_weekdays(self, row: int) -> list[int]:
        blocked: list[int] = []
        for i in range(N_WEEKDAYS):
            w = self._table.cellWidget(row, COL_BLOCKED_BASE + i)
            if not isinstance(w, QCheckBox):
                continue
            raw = w.property("weekday")
            try:
                weekday = int(raw)
            except (TypeError, ValueError):
                continue
            if weekday in WEEKDAY_CODES and not w.isChecked():
                blocked.append(weekday)
        return blocked

    def _rebuild_employee_row_lookup(self) -> None:
        self._employee_row_by_id.clear()
        for row in range(self._table.rowCount()):
            employee_id = self._row_employee_id(row)
            if employee_id is not None:
                self._employee_row_by_id[employee_id] = row

    def _mirror_incompatible_from_sender(self) -> None:
        sender = self.sender()
        for row in range(self._table.rowCount()):
            if self._table.cellWidget(row, COL_INCOMPATIBLE) is sender:
                self._mirror_incompatible_selection(row)
                return

    def _mirror_incompatible_selection(self, row: int) -> None:
        if self._syncing_incompatible:
            return
        source_id = self._row_employee_id(row)
        if source_id is None:
            return
        source_widget = self._table.cellWidget(row, COL_INCOMPATIBLE)
        if not isinstance(source_widget, MultiSelectComboBox):
            return
        selected_ids = source_widget.checked_ids()
        self._syncing_incompatible = True
        try:
            for target_id, target_row in self._employee_row_by_id.items():
                if target_id == source_id:
                    continue
                target_widget = self._table.cellWidget(target_row, COL_INCOMPATIBLE)
                if not isinstance(target_widget, MultiSelectComboBox):
                    continue
                target_widget.set_checked(source_id, target_id in selected_ids)
        finally:
            self._syncing_incompatible = False

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
            all_employee_ids: set[int] = set()

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
                blocked_weekdays = self._collect_blocked_weekdays(r)

                eid = self._row_employee_id(r)
                if eid is None:
                    new_id = insert_employee(
                        conn,
                        name=name,
                        clinic_id=clinic_id,
                        active=active,
                        max_shifts_per_month=max_shifts,
                        qualification_ids=qual_ids,
                        blocked_weekdays=blocked_weekdays,
                    )
                    name_item.setData(Qt.ItemDataRole.UserRole, new_id)
                    all_employee_ids.add(new_id)
                else:
                    update_employee(
                        conn,
                        eid,
                        name=name,
                        clinic_id=clinic_id,
                        active=active,
                        max_shifts_per_month=max_shifts,
                        qualification_ids=qual_ids,
                        blocked_weekdays=blocked_weekdays,
                    )
                    all_employee_ids.add(eid)

            pairs: set[tuple[int, int]] = set()
            for r in range(self._table.rowCount()):
                employee_id = self._row_employee_id(r)
                if employee_id is None:
                    continue
                w_incompat = self._table.cellWidget(r, COL_INCOMPATIBLE)
                if not isinstance(w_incompat, MultiSelectComboBox):
                    continue
                target_ids = w_incompat.checked_ids()
                for target_id in target_ids:
                    if target_id == employee_id or target_id not in all_employee_ids:
                        continue
                    pair = (
                        (employee_id, target_id)
                        if employee_id < target_id
                        else (target_id, employee_id)
                    )
                    pairs.add(pair)
            replace_employee_same_day_exclusions(conn, pairs)
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
        self._rebuild_employee_row_lookup()
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
        get_manual_fixed_assignments: Callable[[int, int], dict[tuple[str, str], int]],
        on_apply: Callable[[], None],
        on_month_changed: Callable[[int, int], None] | None = None,
    ) -> None:
        super().__init__()
        self._get_month = get_month
        self._get_manual_fixed_assignments = get_manual_fixed_assignments
        self._on_apply = on_apply
        self._on_month_changed = on_month_changed
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

        self._btn_prev_month = QPushButton("‹")
        self._btn_prev_month.setFixedWidth(44)
        self._btn_prev_month.setToolTip("Previous month")
        self._btn_prev_month.clicked.connect(self._prev_month)
        self._btn_next_month = QPushButton("›")
        self._btn_next_month.setFixedWidth(44)
        self._btn_next_month.setToolTip("Next month")
        self._btn_next_month.clicked.connect(self._next_month)
        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)
        nav_row.addWidget(self._btn_prev_month)
        nav_row.addStretch(1)
        nav_row.addWidget(self._title)
        nav_row.addStretch(1)
        nav_row.addWidget(self._btn_next_month)
        root.addLayout(nav_row)

        self._btn_today_month = QPushButton("Today")
        self._btn_today_month.setToolTip("Jump to current month")
        self._btn_today_month.clicked.connect(self._go_today_month)
        today_row = QHBoxLayout()
        today_row.addStretch(1)
        today_row.addWidget(self._btn_today_month)
        today_row.addStretch(1)
        root.addLayout(today_row)

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
        self._spin_max_fairness_spread = QSpinBox()
        self._spin_max_fairness_spread.setRange(0, 31)
        self._spin_max_fairness_spread.setValue(1)
        self._spin_mix_balance_weight = QSpinBox()
        self._spin_mix_balance_weight.setRange(0, 100)
        self._spin_mix_balance_weight.setValue(4)
        self._spin_one_day_gap_penalty = QSpinBox()
        self._spin_one_day_gap_penalty.setRange(0, 200)
        self._spin_one_day_gap_penalty.setValue(5)
        self._chk_soft_clinic_rule = QCheckBox("Clinic/day rule soft")
        self._chk_soft_clinic_rule.setChecked(False)
        self._spin_clinic_duplicate_penalty = QSpinBox()
        self._spin_clinic_duplicate_penalty.setRange(0, 500)
        self._spin_clinic_duplicate_penalty.setValue(20)
        self._spin_clinic_duplicate_penalty.setEnabled(False)
        self._chk_soft_clinic_rule.toggled.connect(
            self._spin_clinic_duplicate_penalty.setEnabled
        )

        max_solutions_tip = (
            "Maximum number of alternative solutions to generate. Higher values "
            "give more options but increase solver runtime."
        )
        time_limit_tip = (
            "Maximum solve time per solution attempt in seconds. If reached, the "
            "best solution found so far is returned."
        )
        prefer_off_tip = (
            "Penalty applied when someone is assigned on a day marked as "
            "'prefer off'. Higher values enforce off-day wishes more strongly."
        )
        prefer_work_tip = (
            "Reward for assigning someone on a day marked as 'prefer work'. "
            "Higher values prioritize these wishes more."
        )
        fairness_spread_tip = (
            "Hard limit for the difference between most- and least-assigned "
            "employees in the month."
        )
        mix_balance_tip = (
            "Weight for balancing Hausdienst vs ZNA shifts among employees who "
            "are qualified for both groups."
        )
        one_day_gap_tip = (
            "Penalty for patterns like work-rest-work with exactly one day gap "
            "between two shifts."
        )
        clinic_soft_tip = (
            "If enabled, same-clinic duplicates on a day become a soft penalty "
            "instead of a hard infeasibility."
        )
        clinic_duplicate_tip = (
            "Penalty per extra same-clinic assignment on a day when the "
            "'Clinic/day rule soft' option is enabled."
        )

        lbl_max_solutions = QLabel("Max solutions")
        lbl_max_solutions.setToolTip(max_solutions_tip)
        self._spin_max_solutions.setToolTip(max_solutions_tip)
        settings.addWidget(lbl_max_solutions)
        settings.addWidget(self._spin_max_solutions)
        lbl_time_limit = QLabel("Time limit")
        lbl_time_limit.setToolTip(time_limit_tip)
        self._spin_time_limit.setToolTip(time_limit_tip)
        settings.addWidget(lbl_time_limit)
        settings.addWidget(self._spin_time_limit)
        lbl_prefer_off = QLabel("Prefer-off penalty")
        lbl_prefer_off.setToolTip(prefer_off_tip)
        self._spin_prefer_off_penalty.setToolTip(prefer_off_tip)
        settings.addWidget(lbl_prefer_off)
        settings.addWidget(self._spin_prefer_off_penalty)
        lbl_prefer_work = QLabel("Prefer-work reward")
        lbl_prefer_work.setToolTip(prefer_work_tip)
        self._spin_prefer_work_reward.setToolTip(prefer_work_tip)
        settings.addWidget(lbl_prefer_work)
        settings.addWidget(self._spin_prefer_work_reward)
        lbl_fairness_spread = QLabel("Max fairness spread")
        lbl_fairness_spread.setToolTip(fairness_spread_tip)
        self._spin_max_fairness_spread.setToolTip(fairness_spread_tip)
        settings.addWidget(lbl_fairness_spread)
        settings.addWidget(self._spin_max_fairness_spread)
        lbl_mix_balance = QLabel("Mix balance weight")
        lbl_mix_balance.setToolTip(mix_balance_tip)
        self._spin_mix_balance_weight.setToolTip(mix_balance_tip)
        settings.addWidget(lbl_mix_balance)
        settings.addWidget(self._spin_mix_balance_weight)
        lbl_one_day_gap = QLabel("One-day-gap penalty")
        lbl_one_day_gap.setToolTip(one_day_gap_tip)
        self._spin_one_day_gap_penalty.setToolTip(one_day_gap_tip)
        settings.addWidget(lbl_one_day_gap)
        settings.addWidget(self._spin_one_day_gap_penalty)
        self._chk_soft_clinic_rule.setToolTip(clinic_soft_tip)
        settings.addWidget(self._chk_soft_clinic_rule)
        lbl_clinic_duplicate = QLabel("Clinic duplicate penalty")
        lbl_clinic_duplicate.setToolTip(clinic_duplicate_tip)
        self._spin_clinic_duplicate_penalty.setToolTip(clinic_duplicate_tip)
        settings.addWidget(lbl_clinic_duplicate)
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
        self._btn_export = QPushButton("Export CSV")
        self._btn_export.clicked.connect(self._export_selected_solution)
        self._btn_details = QPushButton("Show details")
        self._btn_details.clicked.connect(self._show_solution_details)
        self._solution_label = QLabel("No solution loaded.")

        controls.addWidget(self._btn_solve)
        controls.addWidget(self._btn_prev_solution)
        controls.addWidget(self._btn_next_solution)
        controls.addWidget(self._btn_export)
        controls.addWidget(self._btn_details)
        controls.addStretch(1)
        controls.addWidget(self._solution_label)
        root.addLayout(controls)

        stats_box = QGroupBox("Solution statistics")
        stats_layout = QGridLayout(stats_box)
        stats_layout.setContentsMargins(10, 8, 10, 8)
        stats_layout.setHorizontalSpacing(16)
        stats_layout.setVerticalSpacing(6)
        self._stat_labels: dict[str, QLabel] = {}
        stat_tooltips = {
            "objective": "Total optimization score of the current solution (lower is better).",
            "preference_penalty": "Total penalty from violated 'prefer off' wishes.",
            "preference_reward": "Total reward from honored 'prefer work' wishes.",
            "fairness_spread": (
                "Difference between the most- and least-assigned employees "
                "in this solution."
            ),
            "mix_ratio_deviation": (
                "Aggregate Hausdienst/ZNA mix imbalance for dual-qualified employees."
            ),
            "one_day_gap_count": (
                "Number of work-rest-work patterns with exactly one day gap."
            ),
            "clinic_duplicate_count": (
                "Number of clinic/day duplicate assignments (only relevant when clinic rule is soft)."
            ),
        }
        stats_specs = [
            ("Objective", "objective"),
            ("Preference penalty", "preference_penalty"),
            ("Preference reward", "preference_reward"),
            ("Fairness spread", "fairness_spread"),
            ("Mix ratio deviation", "mix_ratio_deviation"),
            ("One-day gaps", "one_day_gap_count"),
            ("Clinic duplicates (soft)", "clinic_duplicate_count"),
        ]
        for idx, (title, key) in enumerate(stats_specs):
            row = idx // 2
            col = (idx % 2) * 2
            label = QLabel(f"{title}:")
            value = QLabel("—")
            tip = stat_tooltips.get(key, "")
            if tip:
                label.setToolTip(tip)
                value.setToolTip(tip)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            stats_layout.addWidget(label, row, col)
            stats_layout.addWidget(value, row, col + 1)
            self._stat_labels[key] = value
        root.addWidget(stats_box)

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
        self._clear_solution_stats()
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
            self._clear_solution_stats()
            self._update_solution_controls()

    def _prev_month(self) -> None:
        if self._month == 1:
            year, month = self._year - 1, 12
        else:
            year, month = self._year, self._month - 1
        self.set_month(year, month)
        if self._on_month_changed is not None:
            self._on_month_changed(year, month)

    def _next_month(self) -> None:
        if self._month == 12:
            year, month = self._year + 1, 1
        else:
            year, month = self._year, self._month + 1
        self.set_month(year, month)
        if self._on_month_changed is not None:
            self._on_month_changed(year, month)

    def _go_today_month(self) -> None:
        today = date.today()
        year, month = today.year, today.month
        self.set_month(year, month)
        if self._on_month_changed is not None:
            self._on_month_changed(year, month)

    def _update_title(self) -> None:
        self._title.setText(date(self._year, self._month, 1).strftime("Solver - %B %Y"))

    def _update_solution_controls(self) -> None:
        has = bool(self._solutions)
        can_switch = has and len(self._solutions) > 1 and not self._running
        self._btn_prev_solution.setEnabled(can_switch)
        self._btn_next_solution.setEnabled(can_switch)
        self._btn_export.setEnabled(has and not self._running)
        self._btn_details.setEnabled(has and not self._running)
        self._btn_solve.setEnabled(not self._running)
        self._btn_prev_month.setEnabled(not self._running)
        self._btn_next_month.setEnabled(not self._running)
        self._btn_today_month.setEnabled(not self._running)
        self._spin_max_solutions.setEnabled(not self._running)
        self._spin_time_limit.setEnabled(not self._running)
        self._spin_prefer_off_penalty.setEnabled(not self._running)
        self._spin_prefer_work_reward.setEnabled(not self._running)
        self._spin_max_fairness_spread.setEnabled(not self._running)
        self._spin_mix_balance_weight.setEnabled(not self._running)
        self._spin_one_day_gap_penalty.setEnabled(not self._running)
        self._chk_soft_clinic_rule.setEnabled(not self._running)
        self._spin_clinic_duplicate_penalty.setEnabled(
            (not self._running) and self._chk_soft_clinic_rule.isChecked()
        )

    def _clear_solution_stats(self) -> None:
        for label in self._stat_labels.values():
            label.setText("—")

    def _update_solution_stats_panel(self) -> None:
        if not self._solutions:
            self._clear_solution_stats()
            return
        current = self._solutions[self._solution_index]
        breakdown = current.objective_breakdown
        self._stat_labels["objective"].setText(str(current.objective_value))
        self._stat_labels["preference_penalty"].setText(
            str(breakdown.get("preference_penalty", 0))
        )
        self._stat_labels["preference_reward"].setText(
            str(breakdown.get("preference_reward", 0))
        )
        self._stat_labels["fairness_spread"].setText(str(breakdown.get("fairness_spread", 0)))
        self._stat_labels["mix_ratio_deviation"].setText(
            str(breakdown.get("mix_ratio_deviation", 0))
        )
        self._stat_labels["one_day_gap_count"].setText(
            str(breakdown.get("one_day_gap_count", 0))
        )
        self._stat_labels["clinic_duplicate_count"].setText(
            str(breakdown.get("clinic_duplicate_count", 0))
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

        # Treat manual preplanning from the Schedule tab as fixed solver assignments.
        manual_fixed_assignments = self._get_manual_fixed_assignments(self._year, self._month)
        solver_input["fixed_assignments"] = {
            (str(k[0]), str(k[1])): int(v) for k, v in manual_fixed_assignments.items()
        }

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
        self._clear_solution_stats()
        self._log_window.clear()
        self._log(f"Starting solver for {self._year:04d}-{self._month:02d} ...")
        self._running = True
        self._update_solution_controls()
        solver_config = SolverConfig(
            max_solutions=int(self._spin_max_solutions.value()),
            time_limit_seconds=float(self._spin_time_limit.value()),
            prefer_off_penalty=int(self._spin_prefer_off_penalty.value()),
            prefer_work_reward=int(self._spin_prefer_work_reward.value()),
            max_fairness_spread=int(self._spin_max_fairness_spread.value()),
            mix_balance_weight=int(self._spin_mix_balance_weight.value()),
            one_day_gap_penalty=int(self._spin_one_day_gap_penalty.value()),
            clinic_uniqueness_soft=self._chk_soft_clinic_rule.isChecked(),
            clinic_duplicate_penalty=int(self._spin_clinic_duplicate_penalty.value()),
        )
        self._log(
            "Settings: "
            f"max_solutions={solver_config.max_solutions}, "
            f"time_limit={solver_config.time_limit_seconds:.1f}s, "
            f"prefer_off_penalty={solver_config.prefer_off_penalty}, "
            f"prefer_work_reward={solver_config.prefer_work_reward}, "
            f"max_fairness_spread={solver_config.max_fairness_spread}, "
            f"mix_balance_weight={solver_config.mix_balance_weight}, "
            f"one_day_gap_penalty={solver_config.one_day_gap_penalty}, "
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
            self._clear_solution_stats()
            self._update_solution_controls()
            return

        self._solutions = result.solutions
        self._solution_index = 0
        self._log(result.message)
        if not self._solutions:
            self._solution_label.setText("No solution loaded.")
            self._render_solution_preview()
            self._clear_solution_stats()
            self._update_solution_controls()
            QMessageBox.information(self, "Solver", result.message)
            return

        self._update_solution_label()
        self._update_solution_stats_panel()
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
        self._update_solution_stats_panel()
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

    def _show_solution_details(self) -> None:
        if not self._solutions:
            QMessageBox.information(self, "Solver", "No solution loaded.")
            return
        current = self._solutions[self._solution_index]

        dialog = QDialog(self)
        dialog.setWindowTitle("Solution details")
        dialog.resize(900, 520)
        layout = QVBoxLayout(dialog)

        meta = QLabel(
            f"Solution {self._solution_index + 1}/{len(self._solutions)} "
            f"- objective {current.objective_value}"
        )
        layout.addWidget(meta)

        table = QTableWidget()
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        headers = [
            "Employee",
            "Assigned shifts",
            "Assigned Hausdienst",
            "Assigned ZNA",
            "prefer_work honored",
            "prefer_work missed",
            "prefer_off honored",
            "prefer_off violated",
            "Preference matches",
            "Preference misses",
        ]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        th = table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for idx in range(1, len(headers)):
            th.setSectionResizeMode(idx, QHeaderView.ResizeMode.ResizeToContents)

        employee_ids = sorted(
            self._employee_names.keys(), key=lambda eid: self._employee_names.get(eid, "")
        )
        assigned_hausdienst_counts: dict[int, int] = {}
        assigned_zna_counts: dict[int, int] = {}
        for (_iso_date, slot), assigned_employee in current.assignments.items():
            employee_id = int(assigned_employee)
            if slot in SHIFT_SLOTS_HAUSDienst:
                assigned_hausdienst_counts[employee_id] = (
                    assigned_hausdienst_counts.get(employee_id, 0) + 1
                )
            elif slot in SHIFT_SLOTS_ZNA:
                assigned_zna_counts[employee_id] = (
                    assigned_zna_counts.get(employee_id, 0) + 1
                )
        table.setRowCount(len(employee_ids))
        for row_idx, employee_id in enumerate(employee_ids):
            pref_stats = current.employee_preference_stats.get(employee_id, {})
            matches = int(pref_stats.get("preference_matches", 0))
            misses = int(pref_stats.get("preference_misses", 0))
            values = [
                self._employee_names.get(employee_id, f"#{employee_id}"),
                str(current.assigned_shift_counts.get(employee_id, 0)),
                str(assigned_hausdienst_counts.get(employee_id, 0)),
                str(assigned_zna_counts.get(employee_id, 0)),
                str(pref_stats.get("prefer_work_honored", 0)),
                str(pref_stats.get("prefer_work_missed", 0)),
                str(pref_stats.get("prefer_off_honored", 0)),
                str(pref_stats.get("prefer_off_violated", 0)),
                str(matches),
                str(misses),
            ]
            for col_idx, value in enumerate(values):
                table.setItem(row_idx, col_idx, QTableWidgetItem(value))

        layout.addWidget(table, stretch=1)

        summary = current.preference_summary
        summary_label = QLabel(
            "Preference totals: "
            f"prefer_work honored {summary.get('prefer_work_honored', 0)}/"
            f"{summary.get('prefer_work_total', 0)}, "
            f"prefer_off honored {summary.get('prefer_off_honored', 0)}/"
            f"{summary.get('prefer_off_total', 0)}."
        )
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dialog.accept)
        close_row.addWidget(btn_close)
        layout.addLayout(close_row)
        dialog.exec()

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
        _, days_in_month = calendar.monthrange(self._year, self._month)
        rows: list[list[str]] = []
        for day in range(1, days_in_month + 1):
            iso = date(self._year, self._month, day).isoformat()
            weekday = date.fromisoformat(iso).strftime("%A")
            row: list[str] = [iso, weekday]
            for slot in SHIFT_SLOT_CODES:
                employee_id = current.assignments.get((iso, slot))
                if employee_id is None:
                    row.append("")
                    continue
                emp_id = int(employee_id)
                row.append(self._employee_names.get(emp_id, f"#{emp_id}"))
            rows.append(row)
        try:
            with open(filename, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["date", "weekday"]
                    + [SHIFT_SLOT_LABELS[slot] for slot in SHIFT_SLOT_CODES]
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

        self._absences_page = AbsencesPreferencesPage(
            on_month_changed=self._set_month_from_tabs,
        )
        self._employees_page = EmployeeListPage(
            on_changed=self._refresh_after_employee_edit,
        )
        self._solver_page = SolverTabPage(
            get_month=lambda: (self._year, self._month),
            get_manual_fixed_assignments=self._collect_manual_fixed_assignments,
            on_apply=self._refresh_after_employee_edit,
            on_month_changed=self._set_month_from_tabs,
        )
        self._tabs.addTab(self._employees_page, "Employees")
        self._tabs.addTab(self._absences_page, "Absences & preferences")
        self._tabs.addTab(self._solver_page, "Solver")

        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._rebuild_schedule_table()

    def _refresh_after_employee_edit(self) -> None:
        self._rebuild_schedule_table()
        self._absences_page.set_month(self._year, self._month)
        self._solver_page.set_month(self._year, self._month)

    def _set_month_from_tabs(self, year: int, month: int) -> None:
        if (self._year, self._month) == (year, month):
            return
        self._year, self._month = year, month
        self._rebuild_schedule_table()
        self._absences_page.set_month(year, month)
        self._solver_page.set_month(year, month)

    def _on_tab_changed(self, index: int) -> None:
        if index == 0:
            self._rebuild_schedule_table()
        elif self._tabs.widget(index) is self._absences_page:
            self._absences_page.set_month(self._year, self._month)
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

    def _collect_manual_fixed_assignments(
        self, year: int, month: int
    ) -> dict[tuple[str, str], int]:
        assignments: dict[tuple[str, str], int] = {}
        if (year, month) == (self._year, self._month):
            for row in range(self._schedule_table.rowCount()):
                for col in range(self._schedule_table.columnCount()):
                    widget = self._schedule_table.cellWidget(row, col)
                    if not isinstance(widget, QComboBox):
                        continue
                    iso = widget.property("shift_date_iso")
                    slot = widget.property("shift_slot")
                    if not isinstance(iso, str) or not isinstance(slot, str):
                        continue
                    raw = widget.currentData()
                    if raw is None:
                        continue
                    assignments[(iso, slot)] = int(raw)
            return assignments

        conn = get_connection()
        try:
            clinic_id = get_first_clinic_id(conn)
            if clinic_id is None:
                return {}
            return load_month_shift_employee_ids(
                conn, clinic_id=clinic_id, year=year, month=month
            )
        finally:
            conn.close()

    def _month_title(self) -> str:
        return date(self._year, self._month, 1).strftime("%B %Y")

    def _rebuild_schedule_table(self) -> None:
        self._title.setText(self._month_title())
        self._absences_page.set_month(self._year, self._month)
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
