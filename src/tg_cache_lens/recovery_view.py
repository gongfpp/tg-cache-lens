from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)
from .cli import size
from .recovery import ranges_for


class RangeBar(QWidget):
    def __init__(self):
        super().__init__()
        self.ranges = []
        self.total = 0
        self.setFixedHeight(14)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#393e48'))
        if self.total:
            for a, b in self.ranges:
                x = int(self.width()*a/self.total)
                painter.fillRect(x, 0, max(1, int(self.width()*b/self.total)-x), self.height(), QColor('#548efa'))


class RecoveryView(QWidget):
    def __init__(self, preview, export):
        super().__init__()
        self.clips = []
        layout = QVBoxLayout(self)
        self.title = QLabel(); self.title.setObjectName('title')
        layout.addWidget(self.title)
        self.caption = QLabel(); layout.addWidget(self.caption)
        self.bar = RangeBar(); layout.addWidget(self.bar)
        self.notice = QLabel(); self.notice.setWordWrap(True); layout.addWidget(self.notice)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['选择', '原片时间', '时长'])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 46); self.table.setColumnWidth(2, 70)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda r, c: preview(self.clips[r]))
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for label, callback in [('全选片段', lambda: self.check_all(True)), ('清空', lambda: self.check_all(False))]:
            button = QPushButton(label); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch()
        self.watch = QPushButton('预览片段')
        self.watch.clicked.connect(lambda: preview(self.clips[self.table.currentRow()]))
        actions.addWidget(self.watch)
        layout.addLayout(actions)
        self.save = QPushButton(); self.save.setObjectName('primary')
        self.save.clicked.connect(lambda: export(self.selected()))
        layout.addWidget(self.save)
        self.table.itemChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self.refresh)
        self.refresh()

    def reset(self, row):
        self.title.setText(row.key)
        self.bar.total = max(row.total or 0, row.size)
        self.bar.ranges = ranges_for(row.fingerprint)
        self.bar.update()
        self.caption.setText(f'缓存字节分布    已缓存 {row.cached_size or 0:,} 字节   /   范围 {self.bar.total:,} 字节')
        self.populate([])
        self.notice.setText('正在查找可独立解码的片段…')

    def populate(self, clips):
        self.table.blockSignals(True)
        self.clips = clips
        self.table.setRowCount(len(clips))
        for i, clip in enumerate(clips):
            check = QTableWidgetItem()
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(i, 0, check)
            self.table.setItem(i, 1, QTableWidgetItem(f'{clip.start:.2f} – {clip.start + clip.length:.2f} 秒'))
            self.table.setItem(i, 2, QTableWidgetItem(f'{clip.length:.2f} 秒'))
        self.table.blockSignals(False)
        if clips: self.table.selectRow(0)
        self.notice.setText(f'已验证可播放片段 · {len(clips)} 段' if clips else '没有找到可独立解码的片段')
        self.refresh()

    def check_all(self, checked):
        self.table.blockSignals(True)
        for i in range(len(self.clips)):
            self.table.item(i, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self.refresh()

    def selected(self):
        return [clip for i, clip in enumerate(self.clips) if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def refresh(self):
        selected = self.selected()
        self.save.setText(f'导出 {len(selected)} 个片段 · {sum(c.length for c in selected):.2f} 秒')
        self.save.setEnabled(bool(selected))
        self.watch.setEnabled(0 <= self.table.currentRow() < len(self.clips))
