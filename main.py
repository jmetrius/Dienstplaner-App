"""
PyQt6 entry point: main window with a monthly schedule table (days × shift types).
"""

from __future__ import annotations

import calendar
import sys
from datetime import date

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database import (
    SHIFT_SLOT_CODES,
    SHIFT_SLOT_LABELS,
    get_connection,
    get_first_clinic_id,
    init_database,
    load_month_assignments,
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dienstplaner")
        self.resize(1000, 640)

        today = date.today()
        self._year = today.year
        self._month = today.month

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

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

        self._rebuild_schedule_table()

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
