"""Standalone Qt window; all media work runs away from the GUI thread."""
from pathlib import Path
import subprocess
import sys
import tempfile

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl, QSize
from PySide6.QtGui import QPixmap, QImage, QIcon, QDesktopServices, QPalette, QColor
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QListWidget, QListWidgetItem, QCheckBox,
    QSplitter, QSlider, QToolButton, QMenu, QFileDialog, QProgressBar, QPlainTextEdit, QStackedWidget, QDoubleSpinBox, QMessageBox)
from . import core
from .cli import duration, size, batch

ELIGIBLE = ('ready', 'covered')


def cached_label(row):
    return size(row.cached_size) if row.cached_size is not None else '缓存大小未知'


STYLE = '''
QWidget { color: #202b30; font-size: 13px; }
QMainWindow, QWidget#surface { background: #fafbfc; }
QPushButton, QToolButton, QComboBox, QLineEdit { border: 1px solid #d4dade; border-radius: 6px; padding: 8px 12px; background: #fff; }
QPushButton:hover, QToolButton:hover { background: #eff7f6; }
QPushButton:disabled, QToolButton:disabled { color: #9ca7aa; background: #f2f4f5; }
QPushButton:checked, QToolButton#primary { background: #0c8579; color: white; border-color: #0c8579; }
QToolButton#primary:disabled { background: #b4c6c3; border-color: #b4c6c3; }
QListWidget { background: #fff; border: 1px solid #e0e5e7; border-radius: 6px; outline: 0; }
QListWidget::item { border-bottom: 1px solid #edf0f1; }
QListWidget::item:selected { background: #e0f2ef; }
QLabel#muted { color: #66757d; }
QLabel#title { font-size: 18px; font-weight: 600; }
QLabel#poster { background: #e9edef; border-radius: 6px; color: #66757d; }
QCheckBox::indicator { width: 17px; height: 17px; }
QProgressBar { border: none; background: #e3ebe9; max-height: 4px; }
QProgressBar::chunk { background: #0c8579; }
QPlainTextEdit { border: 1px solid #d4dade; border-radius: 6px; background: #f6f8f9; }
QSlider::groove:horizontal { height: 4px; background: #d9e3e1; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #0c8579; }
QSlider::handle:horizontal { background: #fff; border: 1px solid #8daba6; width: 12px; margin: -5px 0; border-radius: 6px; }
'''


class Job(QThread):
    progress = Signal(int, int, str)
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try: self.result.emit(self.operation(self.progress.emit))
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
                    '-f', 'image2pipe', '-c:v', 'mjpeg', '-threads', '1', '-'], capture_output=True, timeout=8)
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


class MediaRow(QWidget):
    def __init__(self, media, changed):
        super().__init__()
        self.media = media
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)
        self.check = QCheckBox()
        self.check.setAccessibleName('选择 ' + media.path.name)
        self.check.setEnabled(media.status in ELIGIBLE)
        self.check.toggled.connect(changed)
        self.thumb = QLabel('暂无画面')
        self.thumb.setObjectName('poster')
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setFixedSize(112, 64)
        text = QVBoxLayout()
        name = ElidedLabel(media.key)
        name.setStyleSheet('font-weight: 600')
        meta = QLabel(f'{duration(media.duration)} · {cached_label(media)}')
        meta.setObjectName('muted')
        state = self.state = QLabel(core.LABELS[media.status])
        state.setStyleSheet('color: ' + ('#0c796d' if media.status in ELIGIBLE else '#996320'))
        text.addWidget(name); text.addWidget(meta); text.addWidget(state)
        layout.addWidget(self.check); layout.addWidget(self.thumb); layout.addLayout(text, 1)

    def picture(self, image):
        self.thumb.setPixmap(QPixmap.fromImage(image).scaled(self.thumb.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))


class Window(QMainWindow):
    def __init__(self, cache=None, output=None, timeout=600, autostart=False):
        super().__init__()
        self.cache = Path(cache or core.default_root())
        self.output = Path(output or core.default_output()).expanduser()
        self.timeout = timeout
        self.rows = []; self.widgets = {}; self.images = {}; self.jobs = []; self.thumb_attempted = set()
        self.active_job = None; self.thumb_job = None; self.busy = False; self.closing = False
        self.current = None; self.preview_path = None; self.last_results = None
        self.temp = tempfile.TemporaryDirectory(prefix='cache-lens-gui-')
        self.setWindowTitle('Cache Lens')
        self.resize(1120, 760)
        self.setMinimumSize(900, 640)
        icon_path = Path(__file__).parent / 'icon.svg'
        self.setWindowIcon(QIcon(str(icon_path)))
        surface = QWidget(); surface.setObjectName('surface'); self.setCentralWidget(surface)
        layout = QVBoxLayout(surface); layout.setContentsMargins(20, 16, 20, 14); layout.setSpacing(14)
        toolbar = QHBoxLayout()
        title = QLabel('Cache Lens'); title.setObjectName('title')
        logo = QLabel(); logo.setPixmap(QIcon(str(icon_path)).pixmap(28, 28))
        toolbar.addWidget(logo); toolbar.addWidget(title); toolbar.addStretch()
        self.scan_button = QPushButton('自动扫描缓存'); self.scan_button.clicked.connect(self.auto_scan)
        self.cache_button = QPushButton('手动选择…'); self.cache_button.clicked.connect(self.choose_cache)
        toolbar.addWidget(self.scan_button); toolbar.addWidget(self.cache_button); layout.addLayout(toolbar)
        tools = QHBoxLayout()
        self.filter_buttons = []
        for label in ('全部', '可验证导出', '数据不全', '其他'):
            button = QPushButton(label); button.setCheckable(True)
            button.clicked.connect(lambda checked=False, b=button: self.filter_changed(b))
            self.filter_buttons.append(button); tools.addWidget(button)
        self.filter_buttons[0].setChecked(True)
        tools.addStretch()
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索视频'); self.search.setAccessibleName('搜索视频')
        self.search.setMaximumWidth(230); self.search.textChanged.connect(self.apply_filter)
        self.sort = QComboBox(); self.sort.addItems(['默认排序', '大小优先', '时长优先', '名称排序'])
        self.sort.currentIndexChanged.connect(self.sort_rows)
        tools.addWidget(self.sort); tools.addWidget(self.search); layout.addLayout(tools)
        filters = QHBoxLayout()
        self.media_filter = QComboBox()
        self.media_filter.addItems(['视频', '疑似动图 / 贴纸', '全部类型'])
        self.media_filter.setAccessibleName('媒体类型')
        self.media_filter.setToolTip('疑似动图：无音轨、30 秒以内、宽高不超过 512；全部类型可查看被排除的项')
        self.media_filter.currentIndexChanged.connect(self.apply_filter)
        filters.addWidget(self.media_filter)
        filters.addWidget(QLabel('已缓存大小'))
        self.min_size = QDoubleSpinBox(); self.max_size = QDoubleSpinBox()
        for field, name in ((self.min_size, '最小缓存大小'), (self.max_size, '最大缓存大小')):
            field.setRange(0, 1048576); field.setDecimals(2); field.setSuffix(' MiB')
            field.setSpecialValueText('不限'); field.setAccessibleName(name)
            field.setKeyboardTracking(False); field.valueChanged.connect(self.apply_filter)
            field.setMinimumWidth(110)
        filters.addWidget(self.min_size); filters.addWidget(QLabel('—')); filters.addWidget(self.max_size)
        filters.addStretch()
        self.thumbnails_button = QPushButton('缩略图'); self.thumbnails_button.setCheckable(True)
        self.thumbnails_button.toggled.connect(self.toggle_thumbnails); filters.addWidget(self.thumbnails_button)
        self.preview_button = QPushButton('预览'); self.preview_button.setCheckable(True)
        self.preview_button.toggled.connect(self.toggle_preview); filters.addWidget(self.preview_button)
        layout.addLayout(filters)
        splitter = self.splitter = QSplitter(); layout.addWidget(splitter, 1)
        left = QWidget(); left_layout = QVBoxLayout(left); left_layout.setContentsMargins(0,0,0,0)
        self.list = QListWidget(); self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self.select)
        left_layout.addWidget(self.list)
        self.selection = QLabel('已选 0 项'); self.selection.setObjectName('muted'); left_layout.addWidget(self.selection)
        splitter.addWidget(left)
        right = self.preview_panel = QWidget(); right_layout = QVBoxLayout(right); right_layout.setContentsMargins(16,0,0,0); right_layout.setSpacing(12)
        self.stack = QStackedWidget(); self.stack.setMinimumHeight(245)
        self.poster = QLabel('扫描后选择一个视频'); self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter); self.poster.setObjectName('poster')
        self.video = QVideoWidget()
        self.stack.addWidget(self.poster); self.stack.addWidget(self.video); right_layout.addWidget(self.stack, 1)
        self.player = QMediaPlayer(self); self.audio = QAudioOutput(self); self.audio.setVolume(0.5)
        self.player.setAudioOutput(self.audio); self.player.setVideoOutput(self.video)
        controls = QHBoxLayout()
        self.play = QPushButton('播放'); self.play.clicked.connect(self.toggle_play); self.play.setFixedWidth(70)
        self.seek = QSlider(Qt.Orientation.Horizontal); self.seek.setAccessibleName('播放进度'); self.seek.setRange(0,0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.clock = QLabel('00:00 / 00:00'); self.clock.setMinimumWidth(100)
        self.mute = QPushButton('静音'); self.mute.setCheckable(True); self.mute.toggled.connect(self.audio.setMuted)
        controls.addWidget(self.play); controls.addWidget(self.seek, 1); controls.addWidget(self.clock); controls.addWidget(self.mute)
        right_layout.addLayout(controls)
        self.player.positionChanged.connect(self.position)
        self.player.durationChanged.connect(lambda value: self.seek.setRange(0,value))
        self.player.playbackStateChanged.connect(lambda state: self.play.setText('暂停' if state == QMediaPlayer.PlaybackState.PlayingState else '播放'))
        self.player.errorOccurred.connect(lambda error, message: self.show_error('播放失败：' + message))
        self.name = ElidedLabel('请选择视频'); self.name.setObjectName('title'); self.name.setMinimumHeight(28)
        self.metadata = QLabel(''); self.metadata.setObjectName('muted')
        self.state = QLabel(''); self.state.setWordWrap(True)
        right_layout.addWidget(self.name); right_layout.addWidget(self.metadata); right_layout.addWidget(self.state)
        self.details_button = QPushButton('▸ 文件详情'); self.details_button.setCheckable(True)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(110); self.details.hide()
        self.details_button.toggled.connect(self.toggle_details)
        right_layout.addWidget(self.details_button); right_layout.addWidget(self.details)
        self.export_one = QPushButton('导出此视频'); self.export_one.clicked.connect(lambda: self.export_rows([self.current] if self.current else []))
        right_layout.addWidget(self.export_one); splitter.addWidget(right); splitter.setSizes([510, 570]); right.hide()
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.hide(); layout.addWidget(self.progress)
        status_line = QHBoxLayout()
        self.status = QLabel('点击自动扫描缓存'); self.status.setObjectName('muted')
        self.status.setWordWrap(True); status_line.addWidget(self.status, 1)
        self.errors_button = QPushButton('查看失败原因'); self.errors_button.hide(); self.errors_button.clicked.connect(self.show_failures)
        status_line.addWidget(self.errors_button); layout.addLayout(status_line)
        footer = QHBoxLayout(); footer.addWidget(QLabel('导出到'))
        self.output_button = QPushButton(); self.output_button.clicked.connect(self.choose_output); self.output_button.setMaximumWidth(340)
        self.update_output(); footer.addWidget(self.output_button)
        self.open_output = QPushButton('打开文件夹'); self.open_output.clicked.connect(self.reveal_output); footer.addWidget(self.open_output)
        footer.addStretch()
        self.format = QComboBox(); self.format.addItems(['MP4', 'MKV', '原格式']); footer.addWidget(self.format)
        self.export_selected = QToolButton(); self.export_selected.setObjectName('primary')
        self.export_selected.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        menu = QMenu(self); self.export_all = menu.addAction('导出全部完整视频')
        self.export_all.triggered.connect(lambda: self.export_rows([r for r in self.rows if r.status in ELIGIBLE]))
        menu.addAction('选择当前列表中可导出的项', self.select_visible)
        menu.addAction('取消全部勾选', self.clear_checks)
        self.export_selected.setMenu(menu); self.export_selected.clicked.connect(lambda: self.export_rows(self.checked_rows()))
        footer.addWidget(self.export_selected); layout.addLayout(footer)
        self.setStyleSheet(STYLE); self.update_actions()
        if autostart: QTimer.singleShot(0, self.scan)

    def auto_scan(self):
        self.scan(automatic=True)

    def toggle_preview(self, visible):
        self.preview_panel.setVisible(visible)
        if visible:
            self.splitter.setSizes([510, 570])
            self.toggle_play()
        else:
            self.clear_preview()
            self.stack.setCurrentWidget(self.poster)

    def clear_preview(self):
        self.player.stop(); self.player.setSource(QUrl())
        if self.preview_path: self.preview_path.unlink(missing_ok=True)
        self.preview_path = None

    def toggle_thumbnails(self, visible):
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
        self.output_button.setText(self.output.name + '  ▾'); self.output_button.setToolTip(str(self.output))

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择导出目录', str(self.output if self.output.exists() else self.output.parent))
        if path: self.output = Path(path); self.update_output()

    def choose_cache(self):
        path = QFileDialog.getExistingDirectory(self, '选择 Telegram 缓存目录', str(self.cache))
        if path: self.cache = Path(path); self.scan()

    def reveal_output(self):
        self.output.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output.resolve())))

    def filter_changed(self, selected):
        for button in self.filter_buttons: button.setChecked(button is selected)
        self.apply_filter()

    def apply_filter(self):
        query = self.search.text().lower()
        kind = next((i for i,b in enumerate(self.filter_buttons) if b.isChecked()), 0)
        for n in range(self.list.count()):
            item = self.list.item(n); row = item.data(Qt.ItemDataRole.UserRole)
            match = kind == 0 or (kind == 1 and row.status in ELIGIBLE) or (kind == 2 and row.status == 'partial') or (kind == 3 and row.status not in (*ELIGIBLE, 'partial'))
            media_type = self.media_filter.currentIndex()
            type_match = media_type == 2 or (media_type == 0 and not row.suspected_animation) or (media_type == 1 and row.suspected_animation)
            lower, upper = self.min_size.value()*1024**2, self.max_size.value()*1024**2
            size_match = (lower == 0 and upper == 0) or (row.cached_size is not None
                and row.cached_size >= lower and (upper == 0 or row.cached_size <= upper))
            item.setHidden(not (match and type_match and size_match) or query not in row.path.name.lower())
        if self.list.currentItem() and self.list.currentItem().isHidden(): self.list.setCurrentItem(None)
        if self.min_size.value() and self.max_size.value() and self.min_size.value() > self.max_size.value():
            self.status.setText('最小值不能大于最大值')
        elif self.rows and not self.busy:
            visible = sum(not self.list.item(n).isHidden() for n in range(self.list.count()))
            self.status.setText(f'{visible} / {len(self.rows)} 项')
        self.start_thumbnails()
        self.update_actions()

    def sort_rows(self):
        selected = set(str(r.path) for r in self.checked_rows())
        mode = self.sort.currentIndex()
        rows = self.rows if mode == 0 else sorted(self.rows, key=(lambda r: -(r.cached_size if r.cached_size is not None else -1)) if mode == 1 else (lambda r: -(r.duration or 0)) if mode == 2 else (lambda r: r.path.name))
        self.populate(rows, selected)

    def populate(self, rows, checked=()):
        self.list.clear(); self.widgets.clear()
        for row in rows:
            item = QListWidgetItem(); item.setData(Qt.ItemDataRole.UserRole, row); item.setSizeHint(QSize(200, 94))
            widget = MediaRow(row, self.update_actions)
            widget.thumb.setVisible(self.thumbnails_button.isChecked())
            self.list.addItem(item); self.list.setItemWidget(item, widget); self.widgets[str(row.path)] = widget
            widget.check.setChecked(str(row.path) in checked)
            if str(row.path) in self.images: widget.picture(self.images[str(row.path)])
        self.apply_filter()
        for n in range(self.list.count()):
            if not self.list.item(n).isHidden():
                self.list.setCurrentRow(n); break

    def selected_count(self): return len(self.checked_rows())

    def checked_rows(self): return [w.media for w in self.widgets.values() if w.check.isChecked()]

    def select_visible(self):
        for n in range(self.list.count()):
            item = self.list.item(n); widget = self.list.itemWidget(item)
            if not item.isHidden() and widget.check.isEnabled(): widget.check.setChecked(True)

    def clear_checks(self):
        for widget in self.widgets.values(): widget.check.setChecked(False)

    def update_actions(self):
        count = self.selected_count()
        hidden = sum(self.list.item(n).isHidden() and self.list.itemWidget(self.list.item(n)).check.isChecked() for n in range(self.list.count()))
        self.selection.setText(f'已选 {count} 项' + (f'（含隐藏 {hidden} 项）' if hidden else ''))
        self.export_selected.setText(f'导出所选（{count}）')
        # Keep the dropdown reachable even with no selection.
        self.export_selected.setEnabled(not self.busy and bool(self.rows))
        self.export_all.setEnabled(not self.busy and any(r.status in ELIGIBLE for r in self.rows))
        permitted = self.current is not None and self.current.status in ELIGIBLE
        self.preview_button.setEnabled(self.preview_button.isChecked() or (not self.busy and permitted))
        self.thumbnails_button.setEnabled(self.thumbnails_button.isChecked() or (not self.busy and bool(self.rows)))
        self.play.setEnabled(not self.busy and permitted)
        self.export_one.setEnabled(not self.busy and permitted)
        for widget in (self.scan_button, self.cache_button, self.output_button, self.format, self.sort): widget.setEnabled(not self.busy)

    def select(self, item, previous=None):
        self.preview_button.setChecked(False)
        self.clear_preview()
        self.stack.setCurrentWidget(self.poster); self.poster.clear(); self.seek.setRange(0,0); self.clock.setText('00:00 / 00:00')
        self.current = item.data(Qt.ItemDataRole.UserRole) if item else None
        row = self.current
        if row:
            self.name.setText(row.key)
            self.metadata.setText(f'{row.width} × {row.height} · {row.codec.upper() or "—"} · {cached_label(row)} · {duration(row.duration)}')
            self.state.setText(core.LABELS[row.status])
            self.update_details()
            self.set_poster()
        else:
            self.name.setText('请选择视频'); self.metadata.clear(); self.state.clear(); self.details.clear(); self.poster.setText('没有视频')
        self.update_actions()

    def update_details(self):
        row = self.current
        if row:
            self.details.setPlainText(f'{row.detail}\n文件大小：{size(row.size)} · 已缓存：{cached_label(row)}\n覆盖率：{row.coverage} · 合并缓存：{row.copies} 份\n{row.path}')

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

    def task(self, operation, done):
        if self.busy: return
        self.busy = True; self.update_actions(); self.progress.show()
        self.progress.setRange(0,0); self.errors_button.hide()
        job = Job(operation, self); self.active_job = job; self.jobs.append(job)
        job.progress.connect(self.on_progress)
        job.result.connect(done); job.failed.connect(self.show_error)
        job.finished.connect(self.task_finished); job.start()

    def task_finished(self):
        self.busy = False; self.active_job = None; self.progress.hide(); self.update_actions()
        for widget in self.widgets.values(): widget.state.setText(core.LABELS[widget.media.status])
        if self.current:
            self.state.setText(core.LABELS[self.current.status])
            self.update_details()
        if self.closing: self.close()
        else: self.start_thumbnails()

    def on_progress(self, n, total, text):
        self.scan_has_progress = True
        self.progress.setRange(0, max(1,total)); self.progress.setValue(n)
        self.status.setText(text if '导出' in text else f'正在扫描 {n}/{total}')

    def show_error(self, message):
        self.status.setText(message)

    def scan(self, automatic=False):
        if self.busy: return
        self.preview_button.setChecked(False)
        self.status.setText('正在定位并扫描…' if automatic else '正在扫描…')
        self.scan_has_progress = False
        if self.thumb_job:
            self.thumb_job.requestInterruption(); self.thumb_job = None
        cache = self.cache
        def operation(progress):
            root = core.auto_root() if automatic else cache
            return root, core.scan(root, progress=progress, timeout=self.timeout)
        def done(result):
            self.cache, rows = result
            self.scanned(rows)
        self.task(operation, done)
        job = self.active_job
        QTimer.singleShot(10000, lambda: self.directory_wait(job))

    def directory_wait(self, job):
        if self.active_job is job and self.busy and not self.scan_has_progress:
            self.status.setText('正在等待读取目录；若 macOS 显示访问其他应用数据的提示，请先确认。')

    def scanned(self, rows):
        self.rows = rows; self.images.clear(); self.thumb_attempted.clear()
        self.sort.blockSignals(True); self.sort.setCurrentIndex(0); self.sort.blockSignals(False)
        self.populate(rows)
        counts = [len(rows), sum(r.status in ELIGIBLE for r in rows), sum(r.status == 'partial' for r in rows), sum(r.status not in (*ELIGIBLE,'partial') for r in rows)]
        for button, name, count in zip(self.filter_buttons, ('全部','可验证导出','数据不全','其他'), counts): button.setText(f'{name} {count}')
        self.apply_filter()
        visible = sum(not self.list.item(n).isHidden() for n in range(self.list.count()))
        self.status.setText(f'{visible} / {len(rows)} 项' if rows else '未找到可识别的视频')

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
        self.status.setText('验证并准备播放…')
        self.task(lambda progress: (row.path, core.export(row, self.temp.name, 'mp4', self.timeout)), self.prepared)

    def prepared(self, result):
        source, path = result
        if self.closing or not self.preview_button.isChecked() or not self.current or self.current.path != source:
            path.unlink(missing_ok=True)
            return
        self.preview_path = path
        self.stack.setCurrentWidget(self.video); self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play(); self.status.setText('完整范围已确认 · 音视频解码通过')

    def export_rows(self, rows):
        if self.busy: return
        if not rows:
            self.status.setText('请先勾选视频，或从旁边菜单选择“导出全部完整视频”'); return
        fmt = ('mp4', 'mkv', 'original')[self.format.currentIndex()]
        output = self.output
        self.status.setText('验证并导出…')
        self.task(lambda progress: batch(rows, output, fmt, self.timeout, progress), self.exported)

    def exported(self, result):
        self.last_results = result
        paths, errors = result
        self.status.setText(f'成功导出 {len(paths)} 项 · 失败 {len(errors)} 项')
        self.errors_button.setVisible(bool(errors))

    def show_failures(self):
        if self.last_results:
            QMessageBox.warning(self, '导出失败原因', '\n\n'.join(self.last_results[1]))

    def closeEvent(self, event):
        running = [job for job in self.jobs if job.isRunning()]
        if running:
            self.closing = True
            for job in running:
                if isinstance(job, Thumbnails): job.requestInterruption()
            self.status.setText('当前处理结束后关闭窗口…'); event.ignore(); return
        self.player.stop(); self.player.setSource(QUrl()); self.temp.cleanup(); event.accept()


def launch(cache=None, output=None, timeout=600):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Cache Lens'); app.setOrganizationName('TG Cache Lens')
    app.setStyle('Fusion')
    palette = QPalette(); palette.setColor(QPalette.ColorRole.Window, QColor('#fafbfc'))
    palette.setColor(QPalette.ColorRole.WindowText, QColor('#202b30')); palette.setColor(QPalette.ColorRole.Base, QColor('#ffffff'))
    palette.setColor(QPalette.ColorRole.Text, QColor('#202b30')); palette.setColor(QPalette.ColorRole.Highlight, QColor('#0c8579'))
    app.setPalette(palette)
    window = Window(cache, output, timeout); window.show()
    return app.exec()
