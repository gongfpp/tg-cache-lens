"""Standalone Qt window; all media work runs away from the GUI thread."""
from pathlib import Path
from datetime import datetime
import getpass
from hashlib import sha256
import os
import re
import subprocess
import sys
import tempfile
import threading

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl, QSize, QSizeF, QSettings, QEvent
from PySide6.QtGui import QPixmap, QImage, QIcon, QDesktopServices, QPalette, QColor
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QListWidget, QListWidgetItem, QCheckBox,
    QSplitter, QSlider, QToolButton, QMenu, QFileDialog, QProgressBar, QPlainTextEdit, QStackedWidget, QDoubleSpinBox, QMessageBox,
    QGraphicsView, QGraphicsScene, QFrame, QSizePolicy, QDialog, QInputDialog, QTabWidget)
from . import core, recovery
from .recovery_view import RecoveryView
from .cli import duration, size, batch

ELIGIBLE = ('ready', 'covered')
COLUMN_DURATION = 52
COLUMN_CACHED = 76
COLUMN_STATUS = 104
COLUMN_TIME = 122
COLUMN_PERCENT = 68


def exportable(row):
    return row.status in ELIGIBLE or recovery.can_export_cached(row)


def status_label(row):
    return '可导出' if exportable(row) else core.LABELS[row.status]


def cached_label(row):
    return size(row.cached_size) if row.cached_size is not None else '缓存大小未知'


def exported_states(rows, output):
    """Recognize this application's output names without opening media files."""
    try:
        names = [entry.name for entry in Path(output).iterdir() if entry.is_file()]
    except OSError:
        return {}
    states = {}
    remaining = set(names)
    for row in sorted(rows, key=lambda item: len(item.key), reverse=True):
        base = re.sub(r'[^\w.-]', '_', row.key)
        full = re.compile(rf'^{re.escape(base)}(?:-cached-unknown)?(?:-\d+)?\.(?:mp4|mkv|mov|webm|avi)$', re.IGNORECASE)
        partial = re.compile(rf'^{re.escape(base)}-partial-\d+(?:\.\d+)?s-\d+(?:\.\d+)?s(?:-\d+)?\.mp4$', re.IGNORECASE)
        full_names = {name for name in remaining if full.fullmatch(name)}
        partial_names = {name for name in remaining if partial.fullmatch(name)}
        remaining.difference_update(full_names | partial_names)
        if full_names:
            states[str(row.path)] = '已导出'
        elif partial_names:
            states[str(row.path)] = '已导出片段'
    return states


STYLE = '''
QWidget { color:#e9f0f2; font-size:13px; font-family:'Microsoft YaHei UI'; }
QMainWindow, QWidget#surface, QDialog { background:#10171a; }
QPushButton,QToolButton,QLineEdit,QComboBox,QDoubleSpinBox { background:#202b30; border:1px solid #526168; border-radius:5px; padding:5px 8px; }
QPushButton:hover,QToolButton:hover { background:#2c3b40; border-color:#71858a; }
QPushButton:pressed,QToolButton:pressed { background:#193a3a; }
QWidget#toolbarCard, QWidget#footerCard, QWidget#filterCard, QWidget#listCard { background:#192226; border:1px solid #354147; border-radius:5px; }
QWidget#filterCard { background:#182125; }
QWidget#listCard { background:#171f23; }
QLineEdit { background:#121a1e; selection-background-color:#167b78; }
QComboBox::drop-down { border:0; width:22px; }
QComboBox::down-arrow { image:url(CHEVRON_ICON); width:12px; height:12px; }
QPushButton#primary, QToolButton#primary { background:#13bcb6; color:#071c1c; font-weight:600; border:1px solid #39e4dc; padding:8px 14px; }
QPushButton#primary:hover, QToolButton#primary:hover { background:#32d5ce; }
QPushButton#secondaryAction { border:1px solid #6e7b7f; padding:8px 14px; font-weight:600; }
QPushButton#filterAction { color:#b4fffb; border-color:#22d7d0; background:#18413f; }

QPushButton:checked,QPushButton#primary,QToolButton#primary { background:#13bcb6; color:white; }
QPushButton:disabled,QToolButton:disabled { color:#838996; background:#2b2e33; }
QListWidget,QTableWidget,QPlainTextEdit,QComboBox QAbstractItemView,QMenu { background:#151e22; border:1px solid #3b404a; color:#e1e5eb; selection-background-color:#194947; }
QHeaderView::section { background:#202a2e; color:#c1cdd0; border:0; border-right:1px solid #354147; padding:6px; }
QListWidget::item { border-bottom:1px solid #373b42; }
QListWidget::item:selected { background:#194947; }
QLabel#muted { color:#a9b3c2; }
QLabel#title { font-size:17px; font-weight:600; }
QLabel#panelTitle { font-size:14px; font-weight:600; padding:3px 2px; }
QWidget#inspectorPanel { background:#182126; border:1px solid #37464c; border-radius:5px; }
QPushButton#outlinePrimary { background:#173c3b; color:#e9fafa; border:1px solid #24d5cc; min-height:28px; }
QTabWidget::pane { border:1px solid #37464c; background:#182126; }
QTabBar::tab { background:#202b30; padding:7px 14px; color:#b8c7cc; }
QTabBar::tab:selected { color:#24d5cc; border-bottom:2px solid #24d5cc; }
QToolButton[autoRaise="true"] { border:0; border-radius:0; background:transparent; padding:5px; }
QToolButton[autoRaise="true"]:hover { color:#24d5cc; }
QSplitter::handle { background:#10171a; }

QCheckBox::indicator { width:16px; height:16px; border:1px solid #a2b2b6; border-radius:3px; background:#182226; }
QCheckBox::indicator:checked { background:#22d6ce; border-color:#42f2e9; image:url(CHECK_ICON); }
QCheckBox::indicator:indeterminate { background:#418f8a; border-color:#42d6ce; }
QCheckBox::indicator:disabled { border-color:#344145; background:#192125; }
QLabel#location { background:#151e22; border:1px solid #526168; border-radius:4px; padding:6px; }
QPushButton#outputPath { background:#151e22; text-align:left; font-weight:normal; }
QFrame#emptyPreview { background:#182126; border:1px solid #37464c; border-radius:5px; }
QLabel#poster { background:#16181b; color:#9ba7b8; }
QProgressBar { background:#333843; max-height:4px; border:none; }
QProgressBar::chunk { background:#24d5cc; }
QSlider::groove:horizontal { height:4px; background:#454951; }
QSlider::sub-page:horizontal { background:#24d5cc; }
QSlider::handle:horizontal { background:#dde5f2; width:12px; border-radius:6px; margin:-4px 0; }
'''


AnalysisCancelled = core.WorkCancelled


class Job(QThread):
    progress = Signal(int, int, str)
    result = Signal(object)
    failed = Signal(str)
    clip_ready = Signal(object)
    row_ready = Signal(object)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.streaming = False
        self.cancel_event = threading.Event()

    def requestInterruption(self):
        self.cancel_event.set()
        super().requestInterruption()

    def run(self):
        def report(*args):
            if self.isInterruptionRequested(): raise AnalysisCancelled()
            self.progress.emit(*args)
        try:
            with core.cancel_scope(self.cancel_event):
                result = self.operation(report)
            if not self.isInterruptionRequested():
                self.result.emit(result)
            elif isinstance(result, list) and not self.streaming:
                for item in result:
                    if isinstance(item, recovery.Clip): item.path.unlink(missing_ok=True)
        except AnalysisCancelled:
            pass
        except Exception as e: self.failed.emit(str(e))


class Thumbnails(QThread):
    ready = Signal(str, QImage)

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.rows = rows

    def run(self):
        for row in self.rows:
            if self.isInterruptionRequested(): return
            try:
                result = subprocess.run([core.binary('ffmpeg'), '-nostdin', '-v', 'error', '-threads', '1',
                    '-i', str(row.path), '-frames:v', '1', '-vf', 'scale=640:360:force_original_aspect_ratio=decrease',
                    '-f', 'image2pipe', '-c:v', 'mjpeg', '-threads', '1', '-'], capture_output=True, timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                picture = QImage.fromData(result.stdout)
                if not picture.isNull(): self.ready.emit(str(row.path), picture)
            except (OSError, RuntimeError, subprocess.TimeoutExpired): pass


class ElidedLabel(QLabel):
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setMinimumWidth(0)
        self.setToolTip(text)

    def setText(self, text):
        self.full_text = text
        self.setToolTip(text)
        self.refresh()

    def refresh(self):
        super().setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideMiddle, max(20, self.width())))

    def resizeEvent(self, event):
        self.refresh()
        super().resizeEvent(event)


class VideoPane(QGraphicsView):
    """Draw the video inside the viewport, including during splitter resizing."""
    def __init__(self):
        super().__init__()
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self.surface = QGraphicsVideoItem()
        scene.addItem(self.surface)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        bounds = self.viewport().rect()
        self.setSceneRect(0, 0, bounds.width(), bounds.height())
        self.surface.setSize(QSizeF(bounds.size()))


class MediaList(QListWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.fit_rows)

    def fit_rows(self):
        width = self.viewport().width()
        for index in range(self.count()):
            widget = self.itemWidget(self.item(index))
            if widget is not None: widget.setFixedWidth(width)


class MediaRow(QWidget):
    def __init__(self, media, changed):
        super().__init__()
        self.media = media
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(12)
        self.check = QCheckBox()
        self.check.setAccessibleName('选择 ' + media.path.name)
        self.check.setEnabled(exportable(media))
        self.check.toggled.connect(changed)
        self.thumb = QLabel('暂无画面')
        self.thumb.setObjectName('poster')
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setFixedSize(62, 38)
        text = QHBoxLayout()
        name = ElidedLabel(media.key)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        name.setStyleSheet('font-weight: 600')
        self.exported_badge = QLabel()
        self.exported_badge.setStyleSheet('color:#73d5bd; padding:2px 5px;')
        self.exported_badge.hide()
        meta = self.duration_label = QLabel(duration(media.duration)); meta.setFixedWidth(COLUMN_DURATION)
        amount = self.cached_amount = ElidedLabel(size(media.cached_size) if media.cached_size is not None else '未知')
        amount.setFixedWidth(COLUMN_CACHED); amount.setObjectName('muted')
        timestamp = media.source_modified_ns
        cached_time = QLabel(datetime.fromtimestamp(timestamp / 1_000_000_000).strftime('%Y-%m-%d %H:%M') if timestamp else '未知')
        if timestamp: cached_time.setToolTip(datetime.fromtimestamp(timestamp / 1_000_000_000).strftime('%Y-%m-%d %H:%M:%S'))
        cached_time.setFixedWidth(COLUMN_TIME); cached_time.setObjectName('muted')
        meta.setObjectName('muted')
        state = self.state = QLabel(core.LABELS[media.status])
        state.setFixedWidth(COLUMN_STATUS)
        state.setFixedHeight(24)
        self.refresh_state()
        percent = QLabel(media.coverage); percent.setFixedWidth(COLUMN_PERCENT); percent.setObjectName('muted')
        text.addWidget(name, 1); text.addWidget(self.exported_badge); text.addWidget(meta); text.addWidget(percent); text.addWidget(cached_time); text.addWidget(state); text.addWidget(amount)
        layout.addWidget(self.check); layout.addWidget(self.thumb); layout.addLayout(text, 1)

    def refresh_state(self):
        status = self.media.status
        foreground, background = (('#9ce9c0', '#204c38') if status == 'ready' else
                                  ('#9cd9ed', '#194355') if exportable(self.media) else
                                  ('#f1c587', '#49391f') if status == 'partial' else
                                  ('#c6d1d3', '#303e43'))
        self.state.setText(status_label(self.media))
        self.state.setStyleSheet(f'color:{foreground}; background:{background}; border-radius:4px; padding:2px 3px;')

    def picture(self, image):
        self.thumb.setPixmap(QPixmap.fromImage(image).scaled(self.thumb.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))


class PreviewStack(QStackedWidget):
    def showEvent(self, event):
        window = self.window()
        if hasattr(window, 'empty_preview'):
            window.empty_preview.hide()
            window.splitter.setSizes([900, 500, 0])
        super().showEvent(event)


class Window(QMainWindow):
    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() and hasattr(self, 'list'):
            self.refresh_exported()

    def __init__(self, cache=None, output=None, timeout=600, autostart=False):
        super().__init__()
        self.settings = QSettings('TG Cache Lens', 'Cache Lens')
        identity = (os.environ.get('USERDOMAIN', '') + '\\' + getpass.getuser()).encode('utf-8')
        user_key = sha256(identity).hexdigest()[:16]
        fallback_file = (Path(sys.executable).resolve().parent.parent / f'Cache Lens.{user_key}.settings.ini'
                         if getattr(sys, 'frozen', False)
                         else Path(__file__).resolve().parents[2] / '.local' / f'settings-{user_key}.ini')
        self.fallback_settings = QSettings(str(fallback_file), QSettings.Format.IniFormat)
        last_directory = (self.fallback_settings.value('last_cache_directory', '', type=str)
                          or self.settings.value('last_cache_directory', '', type=str))
        last_output = (self.fallback_settings.value('last_output_directory', '', type=str)
                       or self.settings.value('last_output_directory', '', type=str))
        self.cache = Path(cache or last_directory or core.default_root())
        self.output = Path(output or last_output or core.default_output()).expanduser()
        self.timeout = timeout
        self.rows = []; self.widgets = {}; self.images = {}; self.jobs = []; self.thumb_attempted = set()
        self.active_job = None; self.preview_analysis_job = None; self.thumb_job = None; self.busy = False; self.closing = False
        self.export_hold_job = None; self.export_held_paths = set(); self.deferred_removals = set()
        self.inspection_id = 0
        self.current = None; self.preview_path = None; self.pending_preview = None; self.last_results = None
        self.failed_export_rows = []
        self.failed_export_clips = ()
        self.temp = tempfile.TemporaryDirectory(prefix='cache-lens-gui-')
        self.desktop_owner = None; self.retired_desktop = []; self.retirement_pending = False
        self.setWindowTitle('Cache Lens')
        self.resize(1440, 900)
        self.setMinimumSize(900, 640)
        icon_path = Path(__file__).parent / 'icon.svg'
        self.setWindowIcon(QIcon(str(icon_path)))
        surface = QWidget(); surface.setObjectName('surface'); self.setCentralWidget(surface)
        layout = QVBoxLayout(surface); layout.setContentsMargins(10, 10, 10, 10); layout.setSpacing(8)
        toolbar = QHBoxLayout()
        title = QLabel('Cache Lens'); title.setObjectName('title')
        logo = QLabel(); logo.setPixmap(QIcon(str(icon_path)).pixmap(28, 28))
        toolbar.addWidget(logo); toolbar.addWidget(title); toolbar.addWidget(QLabel('  |  Telegram 视频缓存查看与恢复')); toolbar.addStretch()
        layout.addLayout(toolbar)
        directory_card = QWidget(); directory_card.setObjectName('toolbarCard')
        directory = QHBoxLayout(directory_card); directory.setContentsMargins(12,7,12,7); directory.addWidget(QLabel('当前缓存目录：'))
        self.location = ElidedLabel(''); self.location.setObjectName('location'); self.location.setText(str(self.cache))
        directory.addWidget(self.location, 1)
        self.scan_button = QPushButton('自动扫描缓存'); self.scan_button.clicked.connect(self.auto_scan)
        self.cache_button = QPushButton('选择缓存目录…'); self.cache_button.clicked.connect(self.choose_cache)
        self.cache_button.setText('选择目录'); directory.addWidget(self.cache_button)
        self.rescan_button = QPushButton('↻  重新扫描目录'); self.rescan_button.clicked.connect(lambda: self.scan())
        directory.addWidget(self.rescan_button)
        self.stop_button = QPushButton('■  停止分析'); self.stop_button.clicked.connect(self.stop_analysis); self.stop_button.setEnabled(False)
        directory.addWidget(self.stop_button); layout.addWidget(directory_card)
        self.scan_button.hide()
        tools = QHBoxLayout()
        self.filter_buttons = []
        for label in ('全部', '可导出', '数据不全', '其他'):
            button = QPushButton(label); button.setCheckable(True)
            button.clicked.connect(lambda checked=False, b=button: self.filter_changed(b))
            self.filter_buttons.append(button); button.hide()
        self.filter_buttons[0].setChecked(True)
        tools.addStretch()
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索文件、路径或编码'); self.search.setAccessibleName('搜索视频')
        self.search.setMinimumWidth(150); self.search.textChanged.connect(self.apply_filter)
        self.sort = QComboBox(); self.sort.addItems(['默认排序', '大小优先', '时长优先', '名称排序', '缓存时间', '状态', '缓存覆盖率'])
        self.sort.hide(); self.sort_column = None; self.sort_descending = False; self.column_buttons = {}
        self.sort.currentIndexChanged.connect(self.sort_rows)
        tools.addWidget(self.sort)
        self.filter_toggle = QPushButton('筛选'); self.filter_toggle.setObjectName('filterAction'); self.filter_toggle.clicked.connect(self.apply_filter)
        drawer = QWidget(); drawer.setObjectName('filterCard'); drawer_layout = QVBoxLayout(drawer); drawer_layout.setContentsMargins(9,6,9,6)
        layout.addWidget(drawer)
        filters = QHBoxLayout()
        self.media_filter = QComboBox()
        self.media_filter.addItems(['视频', '疑似动图 / 贴纸', '全部类型'])
        self.media_filter.setAccessibleName('媒体类型')
        self.media_filter.setToolTip('疑似动图：无音轨、30 秒以内、宽高不超过 512；全部类型可查看被排除的项')
        self.media_filter.currentIndexChanged.connect(self.apply_filter)
        filters.addWidget(QLabel('媒体类型')); filters.addWidget(self.media_filter)
        filters.addWidget(QLabel('缓存大小'))
        self.min_size = QDoubleSpinBox(); self.max_size = QDoubleSpinBox()
        for field, name in ((self.min_size, '最小缓存大小'), (self.max_size, '最大缓存大小')):
            field.setRange(0, 1048576); field.setDecimals(2); field.setSuffix(' MiB')
            field.setSpecialValueText('不限'); field.setAccessibleName(name)
            field.setKeyboardTracking(False); field.valueChanged.connect(self.apply_filter)
            field.setMinimumWidth(110)
        filters.addWidget(self.min_size); filters.addWidget(QLabel('—')); filters.addWidget(self.max_size)
        filters.addWidget(QLabel('时长'))
        self.min_duration = QDoubleSpinBox(); self.max_duration = QDoubleSpinBox()
        for field, name in ((self.min_duration, '最小时长'), (self.max_duration, '最大时长')):
            field.setRange(0, 604800); field.setDecimals(2); field.setSuffix(' 秒')
            field.setSpecialValueText('不限'); field.setAccessibleName(name)
            field.setToolTip('读取媒体中的时长；缺片文件的时长不代表缓存可完整播放的时长')
            field.setKeyboardTracking(False); field.valueChanged.connect(self.apply_filter)
            field.setMinimumWidth(110)
        filters.addWidget(self.min_duration); filters.addWidget(QLabel('—')); filters.addWidget(self.max_duration)
        filters.addWidget(self.search, 1); filters.addWidget(self.filter_toggle)
        drawer_layout.addLayout(filters)
        self.thumbnails_button = QPushButton('隐藏缩略图'); self.thumbnails_button.setCheckable(True); self.thumbnails_button.setChecked(True)
        self.thumbnails_button.toggled.connect(self.toggle_thumbnails)
        self.thumbnails_button.hide()
        filter_menu = QMenu(self)
        for button in self.filter_buttons:
            filter_menu.addAction(button.text(), button.click)
        filter_menu.addSeparator()
        filter_menu.addAction('自动扫描缓存', self.auto_scan)
        filter_menu.addAction('显示 / 隐藏缩略图', self.thumbnails_button.click)
        self.filter_toggle.setMenu(filter_menu)
        splitter = self.splitter = QSplitter(); layout.addWidget(splitter, 1)
        left = QWidget(); left.setObjectName('listCard'); left_layout = QVBoxLayout(left); left_layout.setContentsMargins(0,8,0,5); left_layout.setSpacing(6)
        self.list = MediaList(); self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self.select)
        self.list.itemClicked.connect(self.open_preview)
        self.list.itemActivated.connect(self.open_preview)
        bulk = QHBoxLayout()
        self.select_all = QCheckBox('全选当前结果'); self.select_all.setTristate(True)
        self.select_all.clicked.connect(self.toggle_visible_checks); self.select_all.setContentsMargins(10, 0, 0, 0)
        bulk.addWidget(self.select_all)
        invert = QPushButton('反选'); invert.clicked.connect(self.invert_visible); invert.hide()
        clear = QPushButton('清空'); clear.clicked.connect(self.clear_checks); clear.hide()
        bulk.addStretch(); left_layout.addLayout(bulk)
        header = QHBoxLayout(); header.setContentsMargins(36,0,8,0); header.setSpacing(6)
        self.thumb_header = QLabel('缩略图'); self.thumb_header.setFixedWidth(68); header.addWidget(self.thumb_header)
        for label, width, mode in [('文件 ID', 0, 3), ('时长', COLUMN_DURATION, 2), ('已缓存', COLUMN_PERCENT, 6), ('缓存时间', COLUMN_TIME, 4), ('状态', COLUMN_STATUS, 5), ('大小', COLUMN_CACHED, 1)]:
            field = QLabel(label) if mode is None else QToolButton()
            if mode is not None:
                field.setText(label + ' ↕'); field.setAutoRaise(True)
                field.clicked.connect(lambda checked=False, m=mode: self.sort_by_column(m))
                self.column_buttons[mode] = (field, label)
            if width:
                field.setFixedWidth(width)
                header.addWidget(field)
            else:
                name_header = QHBoxLayout(); name_header.setContentsMargins(0,0,0,0)
                name_header.addWidget(field); name_header.addStretch()
                header.addLayout(name_header, 1)
        left_layout.addLayout(header)
        left_layout.addWidget(self.list)
        self.selection = QLabel('已选 0 项'); self.selection.setObjectName('muted'); left_layout.addWidget(self.selection)
        splitter.addWidget(left)
        right = self.preview_panel = QWidget(); right.setObjectName('inspectorPanel'); right_layout = QVBoxLayout(right); right_layout.setContentsMargins(10,10,10,10); right_layout.setSpacing(7)
        self.stack = QStackedWidget(); self.stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.poster = QLabel('扫描后选择一个视频'); self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter); self.poster.setObjectName('poster')
        self.poster.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.video = VideoPane()
        self.stack.addWidget(self.poster); self.stack.addWidget(self.video); right_layout.addWidget(self.stack, 1)
        self.player = QMediaPlayer(self); self.audio = QAudioOutput(self); self.audio.setVolume(0.5)
        self.player.setAudioOutput(self.audio); self.player.setVideoOutput(self.video.surface)
        controls = QHBoxLayout()
        self.play = QPushButton('▶'); self.play.setAccessibleName('播放'); self.play.setToolTip('播放 / 暂停')
        self.play.clicked.connect(self.toggle_play); self.play.setFixedWidth(36)
        self.seek = QSlider(Qt.Orientation.Horizontal); self.seek.setAccessibleName('播放进度'); self.seek.setRange(0,0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.clock = QLabel('00:00 / 00:00'); self.clock.setMinimumWidth(100)
        self.mute = QPushButton('静音'); self.mute.setCheckable(True); self.mute.toggled.connect(self.audio.setMuted)
        controls.addWidget(self.play); controls.addWidget(self.seek, 1); controls.addWidget(self.clock); controls.addWidget(self.mute)
        self.volume = QSlider(Qt.Orientation.Horizontal); self.volume.setRange(0,100); self.volume.setValue(50)
        self.volume.setMaximumWidth(64); self.volume.setAccessibleName('音量'); self.volume.valueChanged.connect(lambda v:self.audio.setVolume(v/100))
        controls.addWidget(self.volume)
        full = QPushButton('全屏'); full.clicked.connect(self.fullscreen); controls.addWidget(full)
        right_layout.addLayout(controls)
        self.player.positionChanged.connect(self.position)
        self.player.durationChanged.connect(lambda value: self.seek.setRange(0,value))
        self.player.playbackStateChanged.connect(lambda state: self.play.setText('Ⅱ' if state == QMediaPlayer.PlaybackState.PlayingState else '▶'))
        self.player.errorOccurred.connect(lambda error, message: self.show_error('播放失败：' + message))
        self.name = ElidedLabel('请选择视频'); self.name.setObjectName('title'); self.name.setMinimumHeight(28)
        self.metadata = QLabel(''); self.metadata.setObjectName('muted')
        self.state = QLabel(''); self.state.setWordWrap(True)
        right_layout.insertWidget(0, self.name)
        self.preview_tabs = QTabWidget(); info_page = QWidget(); info_layout = QVBoxLayout(info_page)
        info_layout.addWidget(self.metadata); info_layout.addWidget(self.state); info_layout.addStretch()
        clips_page = QWidget(); clips_info = QVBoxLayout(clips_page)
        self.clips_label = QLabel(); self.clips_label.setObjectName('muted'); clips_info.addWidget(self.clips_label); clips_info.addStretch()
        self.preview_tabs.addTab(clips_page, '缓存片段（0）'); self.preview_tabs.addTab(info_page, '合并信息'); right_layout.addWidget(self.preview_tabs, 1)
        self.details_button = QPushButton('▸ 文件详情'); self.details_button.setCheckable(True)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(110); self.details.hide()
        self.details_button.toggled.connect(self.toggle_details)
        right_layout.addWidget(self.details_button); right_layout.addWidget(self.details)
        self.export_one = QPushButton('导出此视频'); self.export_one.setObjectName('primary'); self.export_one.clicked.connect(lambda: self.export_rows([self.current] if self.current else []))
        right_layout.addWidget(self.export_one)
        self.inspector = PreviewStack()
        self.recovery = RecoveryView(self.watch_clip, self.save_clips, self.save_cached, self.analyze_clips)
        clip_layout = QVBoxLayout(self.recovery.player_area); clip_layout.setContentsMargins(0,0,0,0)
        self.clip_stack = QStackedWidget(); self.clip_stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.clip_poster = QLabel('选择片段以预览'); self.clip_poster.setObjectName('poster'); self.clip_poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clip_video = VideoPane(); self.clip_stack.addWidget(self.clip_poster); self.clip_stack.addWidget(self.clip_video); clip_layout.addWidget(self.clip_stack, 1)
        self.clip_player = QMediaPlayer(self); self.clip_audio = QAudioOutput(self)
        self.clip_player.setAudioOutput(self.clip_audio); self.clip_player.setVideoOutput(self.clip_video.surface)
        self.clip_player.errorOccurred.connect(lambda error, message: self.show_error('预览失败：' + message))
        clip_controls = QHBoxLayout()
        self.clip_pause = QPushButton('▶'); self.clip_pause.setAccessibleName('播放片段'); self.clip_pause.setToolTip('播放 / 暂停')
        self.clip_pause.setFixedWidth(36); self.clip_pause.setEnabled(False)
        self.clip_pause.clicked.connect(lambda: self.clip_player.pause() if self.clip_player.isPlaying() else self.clip_player.play())
        self.clip_player.playingChanged.connect(lambda on: self.clip_pause.setText('Ⅱ' if on else '▶'))
        self.clip_seek = QSlider(Qt.Orientation.Horizontal)
        self.clip_player.durationChanged.connect(lambda d: self.clip_seek.setRange(0, d))
        self.clip_player.positionChanged.connect(self.clip_seek.setValue)
        self.clip_clock = QLabel('00:00 / 00:00')
        self.clip_player.positionChanged.connect(lambda ms: self.clip_clock.setText(f'{duration(ms/1000)} / {duration(self.clip_player.duration()/1000)}'))
        self.clip_seek.sliderMoved.connect(self.clip_player.setPosition)
        close_clip = QPushButton('关闭预览'); close_clip.clicked.connect(self.close_clip)
        self.clip_mute = QPushButton('静音'); self.clip_mute.setCheckable(True); self.clip_mute.toggled.connect(self.clip_audio.setMuted)
        self.clip_volume = QSlider(Qt.Orientation.Horizontal); self.clip_volume.setRange(0, 100)
        self.clip_volume.setValue(50); self.clip_volume.setMaximumWidth(55); self.clip_volume.setAccessibleName('片段音量')
        self.clip_volume.valueChanged.connect(lambda value: self.clip_audio.setVolume(value / 100))
        clip_full = QPushButton('全屏'); clip_full.clicked.connect(self.fullscreen_clip)
        clip_controls.addWidget(self.clip_pause); clip_controls.addWidget(self.clip_clock); clip_controls.addWidget(self.clip_seek, 1)
        clip_controls.addWidget(self.clip_mute); clip_controls.addWidget(self.clip_volume); clip_controls.addWidget(clip_full); clip_controls.addWidget(close_clip)
        clip_layout.addLayout(clip_controls)
        self.inspector.addWidget(right); self.inspector.addWidget(self.recovery)
        splitter.addWidget(self.inspector); self.inspector.hide(); right.hide()
        self.empty_preview = QFrame(); self.empty_preview.setObjectName('emptyPreview')
        empty_layout = QVBoxLayout(self.empty_preview); empty_layout.setContentsMargins(10, 10, 10, 10)
        empty_layout.addWidget(QLabel('视频预览'))
        empty_screen = QLabel('选择视频以预览'); empty_screen.setAlignment(Qt.AlignmentFlag.AlignCenter); empty_screen.setObjectName('poster')
        empty_layout.addWidget(empty_screen, 3)
        empty_layout.addWidget(QLabel('缓存片段'))
        empty_clips = QLabel('暂无片段'); empty_clips.setAlignment(Qt.AlignmentFlag.AlignCenter); empty_clips.setObjectName('poster')
        empty_layout.addWidget(empty_clips, 2)
        splitter.addWidget(self.empty_preview); splitter.setSizes([900, 0, 500])
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.hide(); layout.addWidget(self.progress)
        status_line = QHBoxLayout()
        self.status = QLabel('选择目录或重新扫描目录'); self.status.setObjectName('muted')
        self.status.setWordWrap(True); status_line.addWidget(self.status, 1)
        self.errors_button = QPushButton('查看失败原因'); self.errors_button.hide(); self.errors_button.clicked.connect(self.show_failures)
        self.retry_button = QPushButton('重试失败项'); self.retry_button.hide(); self.retry_button.clicked.connect(self.retry_failed)
        status_line.addWidget(self.retry_button); status_line.addWidget(self.errors_button); layout.addLayout(status_line)
        footer_card = QWidget(); footer_card.setObjectName('footerCard')
        footer = QHBoxLayout(footer_card); footer.setContentsMargins(12,9,12,9); footer.addWidget(QLabel('导出目录：'))
        self.output_button = QPushButton(); self.output_button.setObjectName('outputPath'); self.output_button.clicked.connect(self.choose_output); self.output_button.setMinimumWidth(240)
        self.update_output(); footer.addWidget(self.output_button)
        self.choose_output_button = QPushButton('选择目录'); self.choose_output_button.clicked.connect(self.choose_output); footer.addWidget(self.choose_output_button)
        self.open_output = QPushButton('打开文件夹'); self.open_output.clicked.connect(self.reveal_output); footer.addWidget(self.open_output)
        footer.addStretch()
        self.format = QComboBox(); self.format.addItems(['MP4', 'MKV', '原格式']); footer.addWidget(self.format)
        self.export_selected = QToolButton(); self.export_selected.setObjectName('primary')
        self.export_selected.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        menu = QMenu(self); self.export_all = menu.addAction('导出当前筛选结果')
        self.export_all.triggered.connect(lambda: self.export_rows([self.list.item(n).data(Qt.ItemDataRole.UserRole) for n in range(self.list.count()) if not self.list.item(n).isHidden() and exportable(self.list.item(n).data(Qt.ItemDataRole.UserRole))]))
        menu.addAction('选择当前列表中可导出的项', self.select_visible)
        menu.addAction('取消全部勾选', self.clear_checks)
        self.skip_exported_action = menu.addAction('跳过已导出视频'); self.skip_exported_action.setCheckable(True)
        self.export_selected.setMenu(menu); self.export_selected.clicked.connect(lambda: self.export_rows(self.checked_rows()))
        footer.addWidget(self.export_selected)
        self.export_filtered = QPushButton('导出全部筛选结果'); self.export_filtered.setObjectName('secondaryAction'); self.export_filtered.clicked.connect(self.export_all.trigger); footer.addWidget(self.export_filtered); layout.addWidget(footer_card)
        folder_icon = QIcon(str(Path(__file__).parent / 'folder.svg'))
        self.cache_button.setIcon(folder_icon); self.choose_output_button.setIcon(folder_icon)
        self.setStyleSheet(STYLE.replace('CHECK_ICON', (Path(__file__).parent / 'check.svg').as_posix()).replace('CHEVRON_ICON', (Path(__file__).parent / 'chevron.svg').as_posix())); self.update_actions()
        if autostart: QTimer.singleShot(0, self.scan)

    def auto_scan(self):
        self.scan(automatic=True)

    def dismiss(self):
        if self.failed_export_clips:
            self.failed_export_clips = ()
            self.retry_button.hide()
        if self.preview_analysis_job is not None and self.preview_analysis_job is self.active_job:
            self.preview_analysis_job.requestInterruption()
        self.close_clip()
        self.inspection_id += 1
        self.pending_preview = None
        self.inspector.hide()
        self.empty_preview.show()
        self.preview_panel.hide()
        self.clear_preview()
        for clip in self.recovery.clips:
            self.remove_temporary(clip.path)
        self.recovery.populate([])

    def open_preview(self, item):
        if item.isHidden(): return
        row = item.data(Qt.ItemDataRole.UserRole)
        if self.current is not row: self.select(item)
        if not self.inspector.isHidden(): return
        self.empty_preview.hide()
        self.inspector.show()
        self.splitter.setSizes([900, 500, 0])
        if row.status == 'partial' or (row.status == 'unknown' and getattr(row, 'desktop_source', None)):
            self.inspector.setCurrentWidget(self.recovery)
            self.recovery.reset(row)
            self.status.setText("准备预览…")
        else:
            self.inspector.setCurrentWidget(self.preview_panel)
            self.preview_panel.show()
        if row.status == 'unknown' and getattr(row, 'desktop_source', None) and recovery.can_export_cached(row):
            self.recovery.notice.hide()
            self.status.setText('正在预览')
            self.preview_cached(row)
        else:
            self.start_inspection()

    def analyze_clips(self):
        if not self.busy and self.current:
            self.close_clip()
            for clip in self.recovery.clips: self.remove_temporary(clip.path)
            self.recovery.populate([])
            self.recovery.notice.setText('正在查找可独立解码的片段…')
            self.start_inspection()

    def save_cached(self):
        if self.busy or not self.current: return
        self.export_rows([self.current])

    def start_inspection(self):
        row = self.current
        if self.busy:
            self.pending_preview = row.path
        elif row.status == 'partial' or (row.status == 'unknown' and getattr(row, 'desktop_source', None)):
            request = self.inspection_id
            self.recovery.set_analyzing(True)
            self.task(lambda progress: recovery.recover(row, self.temp.name, self.timeout, progress=progress,
                      on_clip=QThread.currentThread().clip_ready.emit),
                      lambda clips: self.recovery_ready(request, row, clips), analysis=True,
                      on_clip=lambda clip: self.recovery_clip(request, row, clip),
                      on_failure=lambda: self.recovery_failed(request, row))
            self.preview_analysis_job = self.active_job
        elif row.status in ELIGIBLE:
            self.toggle_play()

    def recovery_ready(self, request, row, clips):
        if self.closing or request != self.inspection_id or self.current is not row or self.inspector.isHidden():
            for clip in clips: self.remove_temporary(clip.path)
            return
        self.recovery.analysis_finished()
        self.filter_summary()

    def recovery_clip(self, request, row, clip):
        if self.closing or request != self.inspection_id or self.current is not row or self.inspector.isHidden():
            self.remove_temporary(clip.path)
            return
        self.recovery.append_clip(clip)
        if self.busy: self.recovery.save.setEnabled(False)

    def recovery_failed(self, request, row):
        if request == self.inspection_id and self.current is row:
            self.close_clip()
            for clip in self.recovery.clips: self.remove_temporary(clip.path)
            self.recovery.populate([])

    def save_clips(self, clips):
        if self.busy or not clips: return
        self.close_clip()
        row, output = self.current, self.output
        held = tuple(clips)
        self.failed_export_clips = ()
        self.failed_export_rows = []
        self.retry_button.hide()
        self.export_held_paths = {clip.path for clip in held}
        self.recovery.setEnabled(False)
        self.list.setEnabled(False)
        def finished(result):
            self.exported(result)
            if result[1] and self.current is row and all(clip in self.recovery.clips for clip in held):
                self.failed_export_clips = held
                self.retry_button.show()
        def failed():
            if self.current is row and all(clip in self.recovery.clips for clip in held):
                self.failed_export_clips = held
                self.retry_button.show()
        self.task(lambda progress: recovery.export_merged_clips(row, held, output, self.timeout), finished,
                  on_failure=failed)
        self.export_hold_job = self.active_job

    def preview_cached(self, row):
        self.clip_audio.setVolume(self.clip_volume.value() / 100); self.clip_audio.setMuted(self.clip_mute.isChecked())
        self.clip_player.setSource(QUrl.fromLocalFile(str(row.path)))
        self.clip_stack.setCurrentWidget(self.clip_video)
        self.clip_pause.setEnabled(True)
        self.clip_player.play()

    def watch_clip(self, clip):
        self.clip_audio.setVolume(self.clip_volume.value() / 100); self.clip_audio.setMuted(self.clip_mute.isChecked())
        self.clip_player.setSource(QUrl.fromLocalFile(str(clip.path)))
        self.clip_stack.setCurrentWidget(self.clip_video)
        self.clip_pause.setEnabled(True)
        self.clip_player.play()

    def close_clip(self):
        self.clip_player.stop(); self.clip_player.setSource(QUrl())
        self.clip_stack.setCurrentWidget(self.clip_poster)
        self.clip_pause.setEnabled(False); self.clip_pause.setText('▶'); self.clip_clock.setText('00:00 / 00:00')

    def fullscreen(self):
        if not self.preview_path: return
        self.fullscreen_player(self.player, self.video)

    def fullscreen_clip(self):
        if self.clip_player.source().isEmpty(): return
        self.fullscreen_player(self.clip_player, self.clip_video)

    def fullscreen_player(self, player, embedded):
        dialog = QDialog(self); layout = QVBoxLayout(dialog); view = VideoPane(); layout.addWidget(view)
        player.setVideoOutput(view.surface); dialog.showFullScreen(); dialog.exec()
        player.setVideoOutput(embedded.surface); dialog.deleteLater()

    def toggle_visible_checks(self):
        boxes = [self.list.itemWidget(self.list.item(n)).check for n in range(self.list.count())
                 if not self.list.item(n).isHidden() and exportable(self.list.item(n).data(Qt.ItemDataRole.UserRole))]
        checked = not all(box.isChecked() for box in boxes)
        for box in boxes:
            box.blockSignals(True)
            box.setChecked(checked)
            box.blockSignals(False)
        self.update_actions()

    def invert_visible(self):
        for n in range(self.list.count()):
            item = self.list.item(n)
            box = self.list.itemWidget(item).check
            if not item.isHidden() and box.isEnabled():
                box.blockSignals(True); box.toggle(); box.blockSignals(False)
        self.update_actions()

    def clear_preview(self):
        self.player.stop(); self.player.setSource(QUrl())
        if self.preview_path: self.remove_temporary(self.preview_path)
        self.preview_path = None

    def remove_temporary(self, path):
        if path in self.export_held_paths:
            self.deferred_removals.add(path)
            return
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            # Windows multimedia may keep the file open briefly after stop().
            QTimer.singleShot(100, lambda: self.remove_temporary(path))

    def retire_desktop_snapshots(self):
        self.retirement_pending = False
        if self.closing:
            return  # closeEvent cleans the enclosing temporary directory.
        if not any(isinstance(job, Thumbnails) and job.isRunning() for job in self.jobs):
            remaining = []
            for owner in self.retired_desktop:
                try:
                    owner.cleanup()
                except PermissionError:
                    remaining.append(owner)
            self.retired_desktop = remaining
        if self.retired_desktop and not self.retirement_pending:
            self.retirement_pending = True
            QTimer.singleShot(100, self.retire_desktop_snapshots)

    def toggle_thumbnails(self, visible):
        if hasattr(self, 'thumb_header'): self.thumb_header.setVisible(visible)
        self.thumbnails_button.setText('隐藏缩略图' if visible else '显示缩略图')
        for widget in self.widgets.values(): widget.thumb.setVisible(visible)
        if visible: self.start_thumbnails()
        elif self.thumb_job:
            self.thumb_job.requestInterruption(); self.thumb_job = None

    def start_thumbnails(self):
        if not self.thumbnails_button.isChecked() or self.busy or self.closing: return
        if self.thumb_job and self.thumb_job.isRunning(): return
        rows = [self.list.item(n).data(Qt.ItemDataRole.UserRole) for n in range(self.list.count())
                if not self.list.item(n).isHidden() and str(self.list.item(n).data(Qt.ItemDataRole.UserRole).path) not in self.thumb_attempted]
        if not rows: return
        job = Thumbnails(rows, self); self.thumb_job = job; self.jobs.append(job)
        job.ready.connect(lambda path, image, source=job: self.thumbnail(source, path, image))
        job.finished.connect(self.thumbnail_finished); job.start()

    def toggle_details(self, visible):
        self.details.setVisible(visible); self.details_button.setText(('▾' if visible else '▸') + ' 文件详情')

    def update_output(self):
        self.output_button.setText(str(self.output)); self.output_button.setToolTip(str(self.output))
        if hasattr(self, 'list'):
            self.refresh_exported()

    def refresh_exported(self):
        states = exported_states(self.rows, self.output)
        for path, widget in self.widgets.items():
            label = states.get(path, '')
            widget.exported_badge.setText(label)
            widget.exported_badge.setVisible(bool(label))

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择导出目录', str(self.output if self.output.exists() else self.output.parent))
        if path:
            self.output = Path(path)
            self.save_directory('last_output_directory', self.output)
            self.update_output()

    def save_directory(self, key, directory):
        self.settings.setValue(key, str(directory))
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError or self.fallback_settings.contains(key):
            self.fallback_settings.setValue(key, str(directory))
            self.fallback_settings.sync()

    def choose_cache(self):
        path = QFileDialog.getExistingDirectory(self, '选择 Telegram 缓存目录', str(self.cache))
        if path:
            self.cache = Path(path)
            self.save_directory('last_cache_directory', self.cache)
            self.scan()

    def reveal_output(self):
        cache_root = self.cache.parent if self.cache.is_file() else self.cache
        if self.output.resolve().is_relative_to(cache_root.resolve()):
            self.status.setText('请选择缓存目录以外的导出位置')
            return
        try:
            self.output.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            self.status.setText('无法访问导出目录，请选择其他可写目录')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output.resolve())))

    def filter_changed(self, selected):
        for button in self.filter_buttons: button.setChecked(button is selected)
        self.apply_filter()

    def apply_filter(self):
        query = self.search.text().lower()
        kind = next((i for i,b in enumerate(self.filter_buttons) if b.isChecked()), 0)
        for n in range(self.list.count()):
            item = self.list.item(n); row = item.data(Qt.ItemDataRole.UserRole)
            match = kind == 0 or (kind == 1 and exportable(row)) or (kind == 2 and row.status == 'partial') or (kind == 3 and not exportable(row) and row.status != 'partial')
            media_type = self.media_filter.currentIndex()
            type_match = media_type == 2 or (media_type == 0 and not row.suspected_animation) or (media_type == 1 and row.suspected_animation)
            lower, upper = self.min_size.value()*1024**2, self.max_size.value()*1024**2
            size_match = (lower == 0 and upper == 0) or (row.cached_size is not None
                and row.cached_size >= lower and (upper == 0 or row.cached_size <= upper))
            shortest, longest = self.min_duration.value(), self.max_duration.value()
            time_match = (shortest == 0 and longest == 0) or (row.duration is not None
                and row.duration >= shortest and (longest == 0 or row.duration <= longest))
            item.setHidden(not (match and type_match and size_match and time_match) or query not in f'{row.source_path} {row.key} {row.codec} {row.kind}'.lower())
        if self.list.currentItem() and self.list.currentItem().isHidden(): self.list.setCurrentItem(None)
        if not self.busy: self.filter_summary()
        self.start_thumbnails()
        self.update_actions()

    def filter_summary(self):
        for name, lower, upper in (('大小', self.min_size, self.max_size), ('时长', self.min_duration, self.max_duration)):
            if upper.value() and lower.value() > upper.value():
                self.status.setText(f'{name}最小值不能大于最大值')
                return
        visible = sum(not self.list.item(n).isHidden() for n in range(self.list.count()))
        self.status.setText(f'当前 {visible} 项，共扫描 {len(self.rows)} 项' if self.rows else '未找到可识别的视频')

    def sort_rows(self):
        selected = set(str(r.path) for r in self.checked_rows())
        mode = self.sort.currentIndex()
        key = ((lambda r: r.cached_size) if mode == 1 else (lambda r: r.duration) if mode == 2 else
               (lambda r: r.source_modified_ns) if mode == 4 else (lambda r: status_label(r)) if mode == 5 else
               (lambda r: min(1, r.cached / r.total) if r.total and r.cached is not None else None) if mode == 6 else
               (lambda r: r.key.casefold()))
        if mode == 0:
            rows = self.rows
        else:
            known = [r for r in self.rows if key(r) is not None]
            unknown = [r for r in self.rows if key(r) is None]
            rows = sorted(known, key=key, reverse=self.sort_descending) + unknown
        self.populate(rows, selected)

    def sort_by_column(self, mode):
        self.sort_descending = not self.sort_descending if self.sort_column == mode else mode != 3
        self.sort_column = mode
        for column, (button, label) in self.column_buttons.items():
            button.setText(label + (' ↓' if self.sort_descending else ' ↑') if column == mode else label + ' ↕')
        if self.sort.currentIndex() == mode:
            self.sort_rows()
        else:
            self.sort.setCurrentIndex(mode)

    def populate(self, rows, checked=()):
        self.list.clear(); self.widgets.clear()
        for row in rows:
            item = QListWidgetItem(); item.setData(Qt.ItemDataRole.UserRole, row); item.setSizeHint(QSize(200, 48))
            widget = MediaRow(row, self.update_actions)
            widget.thumb.setVisible(self.thumbnails_button.isChecked())
            self.list.addItem(item); self.list.setItemWidget(item, widget); self.widgets[str(row.path)] = widget
            widget.check.setChecked(str(row.path) in checked)
            if str(row.path) in self.images: widget.picture(self.images[str(row.path)])
        self.refresh_exported()
        self.apply_filter()
        QTimer.singleShot(0, self.list.fit_rows)

    def selected_count(self): return len(self.checked_rows())

    def checked_rows(self): return [w.media for w in self.widgets.values() if w.check.isChecked()]

    def select_visible(self):
        for n in range(self.list.count()):
            item = self.list.item(n); widget = self.list.itemWidget(item)
            if not item.isHidden() and widget.check.isEnabled():
                widget.check.blockSignals(True); widget.check.setChecked(True); widget.check.blockSignals(False)
        self.update_actions()

    def clear_checks(self):
        for widget in self.widgets.values():
            widget.check.blockSignals(True); widget.check.setChecked(False); widget.check.blockSignals(False)
        self.update_actions()

    def update_actions(self):
        count = self.selected_count()
        hidden = sum(self.list.item(n).isHidden() and self.list.itemWidget(self.list.item(n)).check.isChecked() for n in range(self.list.count()))
        amount = sum(row.cached_size or 0 for row in self.checked_rows())
        self.selection.setText(f'已选 {count} 项 · {size(amount)}' + (f'（含隐藏 {hidden} 项）' if hidden else ''))
        self.export_selected.setText(f'导出选中视频（{count}）')
        # Keep the dropdown reachable even with no selection.
        self.export_selected.setEnabled(not self.busy and bool(self.rows))
        self.export_all.setEnabled(not self.busy and any(exportable(r) for r in self.rows))
        permitted = self.current is not None and exportable(self.current)
        eligible = sum(not self.list.item(n).isHidden() and exportable(self.list.item(n).data(Qt.ItemDataRole.UserRole)) for n in range(self.list.count()))
        selected_visible = count - hidden
        state = Qt.CheckState.Unchecked
        if selected_visible:
            state = Qt.CheckState.Checked if selected_visible == eligible else Qt.CheckState.PartiallyChecked
        self.select_all.setCheckState(state)
        self.select_all.setEnabled(eligible > 0)
        self.select_all.setText(f'视频文件（共 {len(self.rows)} 个，已筛选 {sum(not self.list.item(n).isHidden() for n in range(self.list.count()))} 个）')
        self.thumbnails_button.setEnabled(self.thumbnails_button.isChecked() or (not self.busy and bool(self.rows)))
        self.play.setEnabled(not self.busy and permitted)
        self.export_one.setEnabled(not self.busy and permitted)
        for widget in (self.scan_button, self.cache_button, self.rescan_button, self.output_button, self.choose_output_button, self.format, self.sort): widget.setEnabled(not self.busy)
        visible_exportable = sum(not self.list.item(n).isHidden() and exportable(self.list.item(n).data(Qt.ItemDataRole.UserRole)) for n in range(self.list.count()))
        self.export_filtered.setText(f'导出全部筛选结果（{visible_exportable}）')
        self.export_filtered.setEnabled(not self.busy and visible_exportable > 0)
        self.stop_button.setEnabled(self.busy and getattr(self, 'analysis_job', False))
        self.retry_button.setEnabled(not self.busy)

    def select(self, item, previous=None):
        self.dismiss()
        self.clear_preview()
        self.stack.setCurrentWidget(self.poster); self.poster.clear(); self.seek.setRange(0,0); self.clock.setText('00:00 / 00:00')
        self.current = item.data(Qt.ItemDataRole.UserRole) if item else None
        row = self.current
        if row:
            self.name.setText('视频预览：  ' + row.key)
            self.clips_label.setText('可导出' if exportable(row) else '暂无可导出片段')
            self.metadata.setText(f'{row.width} × {row.height} · {row.codec.upper() or "—"} · {cached_label(row)} · {duration(row.duration)}')
            self.state.setText(status_label(row))
            self.update_details()
            self.set_poster()
        else:
            self.name.setText('视频预览：请选择视频'); self.metadata.clear(); self.state.clear(); self.details.clear(); self.poster.setText('没有视频')
        self.update_actions()

    def update_details(self):
        row = self.current
        if row:
            dates = []
            if not getattr(row, 'desktop_source', None):
                try:
                    st = row.path.stat()
                    dates = [f'修改时间：{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}']
                    if hasattr(st, 'st_birthtime'):
                        dates.append(f'创建时间：{datetime.fromtimestamp(st.st_birthtime):%Y-%m-%d %H:%M:%S}')
                except OSError:
                    pass
            info = chr(10).join(dates)
            source = getattr(row, 'desktop_source', None)
            source_path = source.database if source else row.path
            size_label = '已知范围' if source else '文件大小'
            self.details.setPlainText(f'{row.detail}\n{size_label}：{size(row.size)} · 已缓存：{cached_label(row)}\n覆盖率：{row.coverage} · 合并缓存：{row.copies} 份\n{info}\n来源：{source_path}')

    def set_poster(self):
        image = self.images.get(str(self.current.path)) if self.current else None
        if image is not None:
            self.poster.setPixmap(QPixmap.fromImage(image).scaled(self.poster.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else: self.poster.setText('暂无画面')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'poster'): self.set_poster()

    def position(self, value):
        if not self.seek.isSliderDown(): self.seek.setValue(value)
        self.clock.setText(f'{duration(value/1000)} / {duration(self.player.duration()/1000)}')

    def stop_analysis(self):
        if self.active_job and getattr(self, 'analysis_job', False):
            self.stopping_analysis = True
            self.active_job.requestInterruption()
            self.stop_button.setEnabled(False)
            self.status.setText('正在停止分析…')

    def task(self, operation, done, analysis=False, on_clip=None, on_failure=None, on_row=None):
        if self.busy: return
        self.analysis_job = analysis; self.stopping_analysis = False; self.busy = True; self.update_actions(); self.progress.show()
        self.progress.setRange(0,0); self.errors_button.hide()
        job = Job(operation, self); self.active_job = job; self.jobs.append(job)
        job.streaming = on_clip is not None
        if on_clip is not None: job.clip_ready.connect(on_clip)
        if on_row is not None: job.row_ready.connect(on_row)
        if on_failure is not None: job.failed.connect(lambda message: on_failure())
        job.progress.connect(self.on_progress)
        job.result.connect(done); job.failed.connect(self.show_error)
        job.finished.connect(self.task_finished); job.start()

    def task_finished(self):
        self.list.setEnabled(True); self.recovery.setEnabled(True)
        if self.export_hold_job is self.active_job:
            self.export_hold_job = None
            self.export_held_paths.clear()
            pending, self.deferred_removals = self.deferred_removals, set()
            for path in pending: self.remove_temporary(path)
        if self.stopping_analysis and self.preview_analysis_job is self.active_job:
            self.recovery.analysis_finished(stopped=True)
        if self.preview_analysis_job is self.active_job:
            self.preview_analysis_job = None
            self.recovery.set_analyzing(False)
        if self.stopping_analysis: self.status.setText('分析已停止')
        self.analysis_job = False
        self.busy = False; self.active_job = None; self.progress.hide(); self.update_actions()
        self.recovery.refresh()
        for widget in self.widgets.values(): widget.refresh_state()
        if self.current:
            self.state.setText(status_label(self.current))
            self.update_details()
        if self.closing: self.close()
        else:
            if self.pending_preview is not None:
                pending, self.pending_preview = self.pending_preview, None
                if self.current and self.current.path == pending and not self.inspector.isHidden(): self.start_inspection()
            self.start_thumbnails()

    def on_progress(self, n, total, text):
        if self.sender() is not self.active_job:
            return
        self.scan_has_progress = True
        self.progress.setRange(0, total if total > 0 else 0)
        if total > 0: self.progress.setValue(n)
        self.status.setText(text if '导出' in text or '分析' in text else f'正在扫描 {n}/{total}')

    def show_error(self, message):
        self.status.setText(message)
        if not self.inspector.isHidden() and self.inspector.currentWidget() is self.recovery:
            self.recovery.notice.setText(message)

    def scan(self, automatic=False):
        if self.busy: return
        try:
            root = core.auto_root() if automatic else self.cache
            from . import desktop_scan
            desktop_root = desktop_scan.resolve_tdata(root)
            passcode = ''
            data_name = None
            if desktop_root:
                from .desktop_crypto import key_file, key_names, load_modern_key
                names = key_names(desktop_root)
                if len(names) > 1 and not Path(root).is_file():
                    data_name, accepted = QInputDialog.getItem(self, '选择 Telegram 数据',
                        '选择要扫描的数据', names, max(0, names.index('data')) if 'data' in names else 0, False)
                    if not accepted:
                        return
                else:
                    data_name = desktop_scan.select_data_name(desktop_root, root)
                try:
                    load_modern_key(key_file(desktop_root, data_name))
                except ValueError as exc:
                    if '本地密码错误' not in str(exc):
                        raise
                    passcode, accepted = QInputDialog.getText(self, 'Telegram 本地密码',
                        '请输入本地密码', QLineEdit.EchoMode.Password)
                    if not accepted:
                        return
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))
            return
        self.dismiss()
        self.failed_export_rows = []
        self.failed_export_clips = ()
        self.retry_button.hide()
        previous = self.desktop_owner
        owner = tempfile.TemporaryDirectory(prefix='desktop-', dir=self.temp.name) if desktop_root else None
        activated = False
        self.status.setText('正在定位并扫描…' if automatic else '正在扫描…')
        self.scan_has_progress = False
        if self.thumb_job:
            self.thumb_job.requestInterruption(); self.thumb_job = None
        def operation(progress):
            if desktop_root:
                job = QThread.currentThread()
                rows = desktop_scan.scan(desktop_root, owner.name, passcode, progress=progress, timeout=self.timeout, data_name=data_name, cancelled=job.isInterruptionRequested, on_row=job.row_ready.emit)
                progress(len(rows), len(rows), '扫描完成')
            else:
                rows = core.scan(root, progress=progress, timeout=self.timeout)
            if QThread.currentThread().isInterruptionRequested():
                raise AnalysisCancelled()
            return root, rows, owner
        def activate():
            nonlocal activated
            if activated: return
            activated = True
            self.scanned([])
            self.desktop_owner = owner
            self.cache = root
            self.location.setText(str(root))
            if previous:
                self.retired_desktop.append(previous)
                if not self.retirement_pending: self.retire_desktop_snapshots()
        def row_ready(row):
            if self.closing or self.stopping_analysis: return
            activate()
            self.append_scan_row(row)
        def done(result):
            _, rows, _ = result
            if activated and desktop_root:
                self.rows = rows
                self.update_filter_counts()
                self.refresh_exported()
                self.apply_filter()
                self.filter_summary()
            else:
                activate()
                self.scanned(rows)
            warnings = getattr(rows, 'warnings', [])
            if warnings:
                self.last_results = ([], warnings)
                self.errors_button.setVisible(True)
                self.status.setText(f'找到 {len(rows)} 项 · {len(warnings)} 项读取失败')
        self.task(operation, done, analysis=True, on_row=row_ready if desktop_root else None)
        job = self.active_job
        def discard_unused():
            if not activated and owner:
                owner.cleanup()
        job.finished.connect(discard_unused)
        QTimer.singleShot(10000, lambda: self.directory_wait(job))

    def append_scan_row(self, row):
        if self.closing or self.stopping_analysis: return
        self.rows.append(row)
        item = QListWidgetItem(); item.setData(Qt.ItemDataRole.UserRole, row); item.setSizeHint(QSize(200, 48))
        widget = MediaRow(row, self.update_actions)
        widget.thumb.setVisible(self.thumbnails_button.isChecked())
        self.list.addItem(item); self.list.setItemWidget(item, widget); self.widgets[str(row.path)] = widget
        widget.setFixedWidth(self.list.viewport().width())
        if not getattr(self, '_scan_refresh_pending', False):
            self._scan_refresh_pending = True
            QTimer.singleShot(150, self.refresh_scan_rows)

    def refresh_scan_rows(self):
        self._scan_refresh_pending = False
        if self.closing: return
        status = self.status.text()
        self.refresh_exported()
        self.apply_filter()
        # A delayed batch can arrive after a scan failed or was stopped;
        # filtering must not replace that outcome with the item count.
        if not self.busy: self.status.setText(status)

    def directory_wait(self, job):
        if self.active_job is job and self.busy and not self.scan_has_progress:
            message = ('正在等待读取目录；若 macOS 显示访问其他应用数据的提示，请先确认。'
                       if sys.platform == 'darwin' else '正在等待读取目录，请稍候。')
            self.status.setText(message)

    def scanned(self, rows):
        self.rows = rows; self.images.clear(); self.thumb_attempted.clear()
        self.sort.blockSignals(True); self.sort.setCurrentIndex(0); self.sort.blockSignals(False)
        self.populate(rows)
        self.update_filter_counts()
        self.apply_filter()
        self.filter_summary()

    def update_filter_counts(self):
        rows = self.rows
        counts = [len(rows), sum(exportable(r) for r in rows), sum(r.status == 'partial' for r in rows), sum(not exportable(r) and r.status != 'partial' for r in rows)]
        for button, name, count in zip(self.filter_buttons, ('全部','可导出','数据不全','其他'), counts): button.setText(f'{name} {count}')

    def thumbnail_finished(self):
        if self.closing: self.close()
        elif self.sender() is self.thumb_job:
            self.thumb_attempted.update(str(row.path) for row in self.thumb_job.rows)
            self.thumb_job = None
            self.start_thumbnails()

    def thumbnail(self, source, path, image):
        if source is not self.thumb_job: return
        self.images[path] = image
        self.thumb_attempted.add(path)
        if path in self.widgets: self.widgets[path].picture(image)
        if self.current and str(self.current.path) == path and not self.preview_path: self.set_poster()

    def toggle_play(self):
        if not self.current or self.busy: return
        if self.preview_path:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState: self.player.pause()
            else: self.player.play()
            return
        row = self.current
        self.status.setText('正在准备播放…')
        request = self.inspection_id
        self.task(lambda progress: (request, row.path, core.export(row, self.temp.name, 'mp4', self.timeout)), self.prepared)

    def prepared(self, result):
        request, source, path = result
        if request != self.inspection_id or self.closing or self.inspector.isHidden() or not self.current or self.current.path != source:
            self.remove_temporary(path)
            return
        self.preview_path = path
        self.stack.setCurrentWidget(self.video); self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play(); self.status.setText('正在播放')

    def export_rows(self, rows):
        if self.busy: return
        if not rows:
            self.status.setText('请先勾选视频，或选择“导出全部筛选结果”'); return
        if self.skip_exported_action.isChecked():
            states = exported_states(rows, self.output)
            rows = [row for row in rows if states.get(str(row.path)) != '已导出']
            if not rows:
                self.status.setText('所选视频均已导出'); return
        self.close_clip()
        if self.preview_path:
            self.clear_preview()
            self.stack.setCurrentWidget(self.poster)
            self.set_poster()
        fmt = ('mp4', 'mkv', 'original')[self.format.currentIndex()]
        output = self.output
        failed = []
        self.failed_export_clips = ()
        self.retry_button.hide()
        self.status.setText('正在导出…')
        def finished(result):
            self.failed_export_rows = failed
            self.exported(result)
            self.retry_button.setVisible(bool(failed))
        self.task(lambda progress: batch(rows, output, fmt, self.timeout, progress, failed.append), finished)

    def retry_failed(self):
        if self.busy: return
        if self.failed_export_clips:
            self.save_clips(self.failed_export_clips)
        elif self.failed_export_rows:
            self.export_rows(self.failed_export_rows)

    def exported(self, result):
        self.last_results = result
        paths, errors = result
        self.refresh_exported()
        self.status.setText(f'成功导出 {len(paths)} 项 · 失败 {len(errors)} 项')
        self.errors_button.setVisible(bool(errors))

    def show_failures(self):
        if self.last_results:
            QMessageBox.warning(self, '失败原因', '\n\n'.join(self.last_results[1]))

    def closeEvent(self, event):
        running = [job for job in self.jobs if job.isRunning()]
        if running:
            self.closing = True
            for job in running:
                if isinstance(job, Thumbnails): job.requestInterruption()
            self.status.setText('当前处理结束后关闭窗口…'); event.ignore(); return
        self.player.stop(); self.player.setSource(QUrl())
        self.close_clip()
        try:
            self.temp.cleanup()
        except PermissionError:
            # Qt's Windows media backend may release the file asynchronously.
            self.closing = True
            self.status.setText('正在关闭播放器并清理临时文件…')
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        event.accept()


def launch(cache=None, output=None, timeout=600):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Cache Lens'); app.setOrganizationName('TG Cache Lens')
    app.setStyle('Fusion')
    palette = QPalette(); palette.setColor(QPalette.ColorRole.Window, QColor('#10171a'))
    palette.setColor(QPalette.ColorRole.WindowText, QColor('#ecedf0')); palette.setColor(QPalette.ColorRole.Base, QColor('#272a30'))
    palette.setColor(QPalette.ColorRole.Text, QColor('#ecedf0')); palette.setColor(QPalette.ColorRole.Highlight, QColor('#13bcb6'))
    app.setPalette(palette)
    window = Window(cache, output, timeout); window.show()
    return app.exec()
