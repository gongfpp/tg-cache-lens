import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import time
import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from tg_cache_lens.gui import Window
from tg_cache_lens import core
from conftest import write_map

@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(cache, tmp_path, app):
    w = Window(cache, tmp_path/'exports')
    w.show()
    yield w
    wait_for(lambda: not any(j.isRunning() for j in w.jobs))
    w.close()
    app.processEvents()


def wait_for(condition, timeout=30):
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        QTest.qWait(10)
        time.sleep(0.01)
    assert condition()


def click(button):
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def click_row(window, index=0):
    item = window.list.item(index)
    window.list.scrollToItem(item)
    QTest.mouseClick(window.list.viewport(), Qt.MouseButton.LeftButton,
                    pos=window.list.visualItemRect(item).center())


def test_gui_selection_filter_export(window):
    window.scan()
    wait_for(lambda: len(window.rows)==3 and not window.busy)
    assert not window.images and window.thumb_job is None
    click(window.thumbnails_button)
    wait_for(lambda: len(window.images)>=2)
    good = next(w for w in window.widgets.values() if w.media.status=='covered')
    partial = next(w for w in window.widgets.values() if w.media.status=='partial')
    assert not partial.check.isEnabled()
    click(good.check)
    window.search.setText('1002')
    assert len(window.checked_rows()) == 1
    assert '含隐藏 1 项' in window.selection.text()
    assert sum(not window.list.item(n).isHidden() for n in range(3)) == 1
    window.search.clear()
    click(window.export_selected)
    wait_for(lambda: window.last_results is not None and not window.busy)
    assert len(window.last_results[0])==1 and not window.last_results[1]
    assert core.inspect(window.last_results[0][0], deep=True).status=='unknown'
    window.export_all.trigger()
    wait_for(lambda: not window.busy)
    assert len(list(window.output.glob('*.mp4')))==2


def test_gui_preview_and_failure(window):
    window.scan(); wait_for(lambda: len(window.rows)==3 and not window.busy)
    frames=[]
    window.video.videoSink().videoFrameChanged.connect(lambda frame: frames.append(frame.isValid()))
    click_row(window)
    wait_for(lambda: window.preview_path is not None and not window.busy)
    wait_for(lambda: any(frames))
    assert window.player.duration() > 0
    window.player.setPosition(500)
    wait_for(lambda: window.player.position() >= 500)
    path=window.current.path
    write_map(path, path.stat().st_size+100, [(0,path.stat().st_size)])
    click(window.export_one)
    wait_for(lambda: window.last_results is not None and not window.busy)
    assert not window.last_results[0] and len(window.last_results[1])==1
    assert not list(window.output.glob('*.mp4'))
    assert not window.errors_button.isHidden()


def test_idle_auto_scan_and_preview_privacy(window, monkeypatch):
    calls = []
    def locate():
        calls.append(True)
        return window.cache
    monkeypatch.setattr(core, 'auto_root', locate)
    QTest.qWait(50)
    assert not calls and not window.rows and not window.jobs
    assert window.preview_panel.isHidden()
    click(window.scan_button)
    wait_for(lambda: len(window.rows) == 3 and not window.busy)
    assert calls == [True]
    assert window.current is None
    assert not window.images and window.thumb_job is None
    assert window.player.source().isEmpty() and window.preview_panel.isHidden()
    assert all(w.thumb.isHidden() for w in window.widgets.values())
    window.list.setCurrentRow(1)
    assert window.player.source().isEmpty() and window.preview_panel.isHidden()


def test_media_and_actual_size_filters(window):
    from dataclasses import replace
    rows = core.scan(window.cache)
    video = next(r for r in rows if r.status == 'covered')
    animation = replace(video, path=video.path.with_name('animation'), audio='', duration=3, width=128, height=128)
    sparse = replace(video, path=video.path.with_name('sparse'), size=100*1024**2, cached=2*1024**2)
    window.scanned([video, animation, sparse])
    def visible():
        return [window.list.item(n).data(Qt.ItemDataRole.UserRole).path.name for n in range(window.list.count()) if not window.list.item(n).isHidden()]
    assert 'animation' not in visible() and 'sparse' in visible()
    window.media_filter.setCurrentIndex(1)
    assert visible() == ['animation']
    window.media_filter.setCurrentIndex(2)
    window.min_size.setValue(1); window.max_size.setValue(3)
    assert visible() == ['sparse']
    window.min_size.setValue(3); window.max_size.setValue(1)
    assert visible() == [] and '不能大于' in window.status.text()
    window.min_size.setValue(0); window.max_size.setValue(0)
    assert len(visible()) == 3


def test_unknown_size_excluded_only_when_range_set(window):
    window.scanned(core.scan(window.cache))
    window.sort.setCurrentIndex(1)
    assert window.list.item(2).data(Qt.ItemDataRole.UserRole).cached_size is None
    window.max_size.setValue(1)
    assert window.list.item(2).isHidden()
    window.max_size.setValue(0)
    assert not window.list.item(2).isHidden()


def test_thumbnails_follow_filter_changes_without_retry_loop(window, monkeypatch):
    import threading
    from PySide6.QtGui import QImage
    from tg_cache_lens.gui import Thumbnails
    started, release = threading.Event(), threading.Event()
    calls = []
    def generate(job):
        for row in job.rows:
            calls.append(row.path.name)
            if len(calls) == 1:
                started.set()
                if not release.wait(5): return
            # Simulate one unreadable thumbnail as well as a successful frame.
            if '1002' not in row.path.name:
                image = QImage(16, 16, QImage.Format.Format_RGB32)
                image.fill(Qt.GlobalColor.blue)
                job.ready.emit(str(row.path), image)
    monkeypatch.setattr(Thumbnails, 'run', generate)
    window.scan(); wait_for(lambda: not window.busy)
    window.search.setText('1001')
    click(window.thumbnails_button)
    try:
        wait_for(started.is_set)
        window.search.clear()
    finally:
        release.set()
    wait_for(lambda: len(calls) == 3 and window.thumb_job is None)
    window.search.setText('1002'); window.search.clear()
    QTest.qWait(50)
    assert len(calls) == 3 and len(window.images) == 2


def test_dismiss_preview_while_preparing_discards_result(window, monkeypatch):
    import threading
    started, release = threading.Event(), threading.Event()
    outputs = []
    def prepare(row, output, *args):
        started.set()
        if not release.wait(5): raise RuntimeError('test timed out')
        path = Path(output)/'preview.mp4'
        path.write_bytes(b'prepared preview')
        outputs.append(path)
        return path
    window.scan(); wait_for(lambda: not window.busy)
    monkeypatch.setattr(core, 'export', prepare)
    click_row(window)
    try:
        wait_for(started.is_set)
        assert window.preview_button.isEnabled()
        click(window.preview_button)
        assert window.preview_panel.isHidden()
    finally:
        release.set()
    wait_for(lambda: not window.busy)
    assert outputs and not outputs[0].exists()
    assert window.preview_path is None and window.player.source().isEmpty()


def test_export_failure_details_do_not_start_preview(window, monkeypatch):
    from tg_cache_lens.gui import QMessageBox
    messages = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda parent, title, message: messages.append(message))
    window.exported(([], ['文件已变化，请重新扫描']))
    click(window.errors_button)
    assert messages == ['文件已变化，请重新扫描']
    assert window.preview_panel.isHidden() and not window.jobs


def test_auto_rescan_closes_preview_and_rejects_old_thumbnail(window, monkeypatch):
    from PySide6.QtGui import QImage
    window.scan(); wait_for(lambda: not window.busy)
    click_row(window)
    wait_for(lambda: window.preview_path is not None and not window.busy)
    previous_preview = window.preview_path
    monkeypatch.setattr(core, 'auto_root', lambda: window.cache)
    click(window.scan_button)
    assert window.preview_panel.isHidden() and window.player.source().isEmpty()
    assert not previous_preview.exists()
    window.thumbnail(object(), 'old-scan', QImage())
    assert 'old-scan' not in window.images
    wait_for(lambda: not window.busy)
    assert len(window.rows) == 3 and not window.images


def test_duration_filters_intersect_size_and_type(window):
    from dataclasses import replace
    video = next(row for row in core.scan(window.cache) if row.status == 'covered')
    rows = [replace(video, path=video.path.with_name('short'), duration=10, size=3*1024**2, cached=2*1024**2),
            replace(video, path=video.path.with_name('long'), duration=120, size=3*1024**2, cached=2*1024**2),
            replace(video, path=video.path.with_name('unknown-time'), duration=None),
            replace(video, path=video.path.with_name('sticker'), duration=10, audio='', width=128, height=128)]
    window.scanned(rows)
    def visible():
        return [window.list.item(n).data(Qt.ItemDataRole.UserRole).path.name
                for n in range(window.list.count()) if not window.list.item(n).isHidden()]
    assert visible() == ['short', 'long', 'unknown-time']
    window.min_duration.setValue(10); window.max_duration.setValue(10)
    assert visible() == ['short']
    window.min_size.setValue(1); window.max_size.setValue(3)
    assert visible() == ['short']
    window.min_size.setValue(3)
    assert visible() == []
    window.min_size.setValue(0); window.max_size.setValue(0)
    window.media_filter.setCurrentIndex(2)
    assert visible() == ['short', 'sticker']
    window.min_duration.setValue(11)
    assert visible() == [] and '时长最小值' in window.status.text()
    window.min_duration.setValue(0); window.max_duration.setValue(0)
    assert len(visible()) == 4


def test_rescan_resets_thumbnails_and_click_opens_partial_details(window):
    window.scan(); wait_for(lambda: not window.busy)
    click(window.thumbnails_button)
    wait_for(lambda: window.thumb_job is None)
    assert window.images and window.thumbnails_button.text() == '隐藏缩略图'
    window.scan(); wait_for(lambda: not window.busy)
    assert not window.thumbnails_button.isChecked() and not window.images
    assert window.current is None and window.preview_panel.isHidden()
    assert all(widget.thumb.isHidden() for widget in window.widgets.values())
    partial = next(n for n in range(window.list.count())
                   if window.list.item(n).data(Qt.ItemDataRole.UserRole).status == 'partial')
    click_row(window, partial)
    assert not window.preview_panel.isHidden() and not window.play.isEnabled()
    assert window.player.source().isEmpty() and not window.busy
    click(window.preview_button)
    assert window.preview_panel.isHidden()
    click_row(window, partial)
    assert not window.preview_panel.isHidden()


def test_checkbox_selection_does_not_open_preview(window):
    window.scan(); wait_for(lambda: not window.busy)
    good = next(w for w in window.widgets.values() if w.media.status == 'covered')
    click(good.check)
    assert window.selected_count() == 1
    assert window.preview_panel.isHidden() and window.player.source().isEmpty()


def test_click_another_video_while_preview_prepares(window, monkeypatch):
    import shutil
    import threading
    source = window.cache/'telegram-cloud-document-1-1001_partial'
    other = window.cache/'second-video_partial'
    shutil.copyfile(source, other)
    shutil.copyfile(Path(str(source)+'.meta'), Path(str(other)+'.meta'))
    window.scan(); wait_for(lambda: not window.busy)
    indices = [n for n in range(window.list.count())
               if window.list.item(n).data(Qt.ItemDataRole.UserRole).status == 'covered']
    export = core.export
    started, release = threading.Event(), threading.Event()
    outputs = []
    def prepare(*args):
        if not outputs:
            started.set()
            if not release.wait(5): raise RuntimeError('test timed out')
        path = export(*args)
        outputs.append(path)
        return path
    monkeypatch.setattr(core, 'export', prepare)
    click_row(window, indices[0])
    try:
        wait_for(started.is_set)
        click_row(window, indices[1])
        requested = window.list.item(indices[1]).data(Qt.ItemDataRole.UserRole).path
        assert window.pending_preview == requested
    finally:
        release.set()
    wait_for(lambda: len(outputs) == 2 and not window.busy)
    assert not outputs[0].exists() and outputs[1] == window.preview_path
    assert window.current.path == requested and not window.preview_panel.isHidden()
