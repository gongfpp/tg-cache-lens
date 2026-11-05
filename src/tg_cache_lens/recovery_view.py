from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QTabWidget, QFrame)
from .cli import size
from .recovery import ranges_for


def clip_time(seconds):
    centiseconds = round(seconds * 100)
    whole, fraction = divmod(centiseconds, 100)
    hours, remaining = divmod(whole, 3600)
    minutes, seconds = divmod(remaining, 60)
    text = f'{hours:02}:{minutes:02}:{seconds:02}'
    return text + (f'.{fraction:02}' if fraction else '')


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
                painter.fillRect(x, 0, max(1, int(self.width()*b/self.total)-x), self.height(), QColor('#24d5cc'))


class RecoveryView(QWidget):
    def __init__(self, preview, export, export_cached, analyze):
        super().__init__()
        self.clips = []
        self.analyzing = False
        self.setObjectName('inspectorPanel'); self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self); layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(7)
        self.title = QLabel(); self.title.setObjectName('panelTitle')
        layout.addWidget(self.title)
        self.player_area = QWidget(); self.player_area.setMinimumHeight(130); layout.addWidget(self.player_area, 3)
        self.tabs = QTabWidget(); self.tabs.setObjectName('detailTabs')
        clips_panel = QWidget(); clips_layout = QVBoxLayout(clips_panel); clips_layout.setContentsMargins(0, 5, 0, 0)
        self.notice = QLabel(); self.notice.setWordWrap(True); self.notice.setObjectName('muted'); clips_layout.addWidget(self.notice)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['选择', '片段范围', '时长', '大小', '状态'])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 45); self.table.setColumnWidth(2, 88); self.table.setColumnWidth(3, 68); self.table.setColumnWidth(4, 70)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda r, c: preview(self.clips[r]))
        clips_layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.analyze = QPushButton('分析片段'); self.analyze.clicked.connect(analyze); actions.addWidget(self.analyze)
        for label, callback in [('全选片段', lambda: self.check_all(True)), ('清空', lambda: self.check_all(False))]:
            button = QPushButton(label); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch()
        self.watch = QPushButton('预览片段')
        self.watch.clicked.connect(lambda: preview(self.clips[self.table.currentRow()]))
        actions.addWidget(self.watch)
        clips_layout.addLayout(actions)
        self.tabs.addTab(clips_panel, '缓存片段（0）')
        info = QWidget(); info_layout = QVBoxLayout(info)
        self.caption = QLabel(); self.caption.setWordWrap(True); info_layout.addWidget(self.caption)
        self.bar = RangeBar(); info_layout.addWidget(self.bar)
        info_layout.addStretch()
        self.tabs.addTab(info, '合并信息')
        layout.addWidget(self.tabs, 2)
        self.export_cached = QPushButton('导出视频')
        self.export_cached.clicked.connect(export_cached); layout.addWidget(self.export_cached)
        self.save = QPushButton(); self.save.setObjectName('outlinePrimary')
        self.save.clicked.connect(lambda: export(self.selected()))
        layout.addWidget(self.save)
        self.table.itemChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self.refresh)
        self.refresh()

    def reset(self, row):
        self.title.setText('视频预览：  ' + row.key)
        self.bar.total = max(row.total or 0, row.size)
        self.bar.ranges = ranges_for(row.fingerprint)
        self.bar.update()
        self.caption.setText(f'缓存字节分布    已缓存 {row.cached_size or 0:,} 字节   /   范围 {self.bar.total:,} 字节')
        self.populate([])
        from .recovery import can_export_cached
        direct = can_export_cached(row)
        self.export_cached.setVisible(direct)
        self.analyze.setVisible(True)
        self.notice.setText('正在查找可独立解码的片段…')

    def populate(self, clips):
        self.notice.show()
        self.table.blockSignals(True)
        self.clips = []
        self.table.setRowCount(0)
        for clip in clips: self._insert_clip(clip)
        self.table.blockSignals(False)
        if clips: self.table.selectRow(0)
        self.tabs.setTabText(0, f'缓存片段（{len(clips)}）')
        self.notice.setText(f'可播放片段 · {len(clips)} 段' if clips else '没有找到可独立解码的片段')
        self.refresh()

    def _insert_clip(self, clip):
        index = next((i for i, current in enumerate(self.clips) if current.start > clip.start), len(self.clips))
        self.clips.insert(index, clip)
        self.table.insertRow(index)
        check = QTableWidgetItem()
        check.setCheckState(Qt.CheckState.Checked)
        self.table.setItem(index, 0, check)
        self.table.setItem(index, 1, QTableWidgetItem(f'{clip_time(clip.start)} – {clip_time(clip.start + clip.length)}'))
        self.table.setItem(index, 2, QTableWidgetItem(clip_time(clip.length) + ('' if clip.audio else ' · 无声')))
        self.table.setItem(index, 3, QTableWidgetItem(size(clip.path.stat().st_size)))
        self.table.setItem(index, 4, QTableWidgetItem('可导出'))

    def append_clip(self, clip):
        self.append_clips([clip])

    def append_clips(self, clips):
        self.table.blockSignals(True)
        for clip in clips: self._insert_clip(clip)
        self.table.blockSignals(False)
        if self.table.currentRow() < 0: self.table.selectRow(0)
        self.tabs.setTabText(0, f'缓存片段（{len(self.clips)}）')
        self.notice.show()
        self.notice.setText(f'正在分析 · 已找到 {len(self.clips)} 个片段')
        self.refresh()

    def analysis_finished(self, stopped=False):
        self.notice.show()
        self.notice.setText(('已停止分析' if stopped else '分析完成') + f' · {len(self.clips)} 个片段' if self.clips else
                            ('已停止分析' if stopped else '没有找到可独立解码的片段'))
        self.refresh()

    def set_analyzing(self, active):
        self.analyzing = active
        self.analyze.setEnabled(not active)
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
        self.save.setText(f'合并导出 {len(selected)} 个片段 · {sum(c.length for c in selected):.2f} 秒')
        self.save.setEnabled(bool(selected))
        self.watch.setEnabled(0 <= self.table.currentRow() < len(self.clips))
