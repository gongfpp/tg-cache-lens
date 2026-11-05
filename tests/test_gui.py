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
    w.thumbnails_button.setChecked(False)
    w.show()
    yield w
    wait_for(lambda: not any(j.isRunning() for j in w.jobs))
    w.close()
    wait_for(lambda: not w.isVisible())
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


def test_export_markers_follow_selected_directory_and_actual_files(window, tmp_path, monkeypatch):
    from dataclasses import replace
    from PySide6.QtWidgets import QFileDialog
    row = next(item for item in core.scan(window.cache) if item.status == 'covered')
    other = replace(row, path=row.path.with_name(row.key + '-2'))
    first = tmp_path / 'first'; first.mkdir()
    second = tmp_path / 'second'; second.mkdir()
    (first / f'{row.key}-partial-0.000s-5.000s.mp4').touch()
    (first / f'{other.key}.mp4').touch()
    window.output = first
    window.scanned([row, other])
    assert window.widgets[str(row.path)].exported_badge.text() == '已导出片段'
    assert window.widgets[str(other.path)].exported_badge.text() == '已导出'
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(second))
    monkeypatch.setattr(window, 'save_directory', lambda *args: None)
    window.choose_output()
    assert not window.widgets[str(row.path)].exported_badge.text()
    (second / f'{row.key}-cached-unknown-2.mp4').touch()
    window.exported(([second / f'{row.key}-cached-unknown-2.mp4'], []))
    assert window.widgets[str(row.path)].exported_badge.text() == '已导出'
    (second / f'{row.key}-cached-unknown-2.mp4').unlink()
    window.refresh_exported()
    assert not window.widgets[str(row.path)].exported_badge.text()


def test_output_directory_is_restored_after_restart(cache, tmp_path, app, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QFileDialog
    import tg_cache_lens.gui as gui
    settings_path = tmp_path / 'settings.ini'
    monkeypatch.setattr(gui, 'QSettings', lambda *args: QSettings(str(settings_path), QSettings.Format.IniFormat))
    # The constructor also references the enum values on QSettings.
    monkeypatch.setattr(gui.QSettings, 'Format', QSettings.Format, raising=False)
    monkeypatch.setattr(gui.QSettings, 'Status', QSettings.Status, raising=False)
    selected = tmp_path / 'selected'; selected.mkdir()
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(selected))
    first = Window(cache, tmp_path / 'original')
    first.choose_output()
    first.close()
    second = Window(cache)
    assert second.output == selected
    second.close()


def test_bulk_skip_exported_and_retry_failed_rows(window, monkeypatch):
    from dataclasses import replace
    row = next(item for item in core.scan(window.cache) if item.status == 'covered')
    other = replace(row, path=row.path.with_name('other-video'))
    window.scanned([row, other])
    window.output.mkdir(parents=True)
    (window.output / f'{row.key}.mp4').touch()
    window.skip_exported_action.setChecked(True)
    calls = []
    def fake_batch(rows, output, fmt, timeout, progress, on_failure):
        calls.append(list(rows))
        if len(calls) == 1:
            on_failure(rows[0])
            return [], ['synthetic failure']
        return [output / 'other-video.mp4'], []
    monkeypatch.setattr('tg_cache_lens.gui.batch', fake_batch)
    window.export_rows([row, other])
    wait_for(lambda: not window.busy)
    assert calls == [[other]] and window.retry_button.isVisible()
    click(window.retry_button)
    wait_for(lambda: not window.busy)
    assert calls == [[other], [other]] and not window.retry_button.isVisible()


def test_preview_cleanup_retries_a_temporarily_locked_file(window, monkeypatch):
    target = Path(window.temp.name)/'preview.mp4'
    target.write_bytes(b'synthetic')
    window.preview_path = target
    original = Path.unlink
    attempts = []
    def unlink(path, *args, **kwargs):
        if path == target and not attempts:
            attempts.append(path)
            raise PermissionError('synthetic sharing violation')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    window.clear_preview()
    assert window.preview_path is None
    wait_for(lambda: not target.exists())
    assert attempts == [target]


def test_delayed_scan_refresh_keeps_error_status(window):
    window.status.setText('找不到 ffmpeg')
    window.refresh_scan_rows()
    assert window.status.text() == '找不到 ffmpeg'


def test_open_output_does_not_create_directories_inside_cache(window, monkeypatch):
    opened = []
    monkeypatch.setattr('tg_cache_lens.gui.QDesktopServices.openUrl', lambda url: opened.append(url))
    target = window.cache/'new-output'
    window.output = target
    click(window.open_output)
    assert not target.exists() and not opened
    assert '缓存目录以外' in window.status.text()


def test_list_columns_fit_with_preview_open_at_minimum_size(window):
    from PySide6.QtCore import QPoint
    window.scan(); wait_for(lambda: not window.busy)
    window.resize(900, 640)
    window.inspector.show()
    window.preview_panel.show()
    window.splitter.setSizes([420, 660])
    QApplication.processEvents()
    for row in window.widgets.values():
        assert row.state.mapTo(row, QPoint(row.state.width(), 0)).x() <= row.width()
        assert row.state.fontMetrics().horizontalAdvance(row.state.text()) <= row.state.width()
        assert row.cached_amount.fontMetrics().horizontalAdvance(row.cached_amount.text()) <= row.cached_amount.width()


def test_column_header_toggles_sort_direction(window):
    from dataclasses import replace
    row = next(item for item in core.scan(window.cache) if item.status == 'covered')
    first = replace(row, path=row.path.with_name('first'), cached=100)
    second = replace(row, path=row.path.with_name('second'), cached=200)
    window.scanned([first, second])
    button = window.column_buttons[1][0]
    click(button)
    assert window.list.item(0).data(Qt.ItemDataRole.UserRole).path == second.path
    click(button)
    assert window.list.item(0).data(Qt.ItemDataRole.UserRole).path == first.path
    first = replace(first, total=1000, cached=100)
    second = replace(second, total=1000, cached=200)
    window.scanned([first, second])
    click(window.column_buttons[6][0])
    assert window.list.item(0).data(Qt.ItemDataRole.UserRole).path == second.path


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
    window.player.videoSink().videoFrameChanged.connect(lambda frame: frames.append(frame.isValid()))
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
        window.search.setText("no matching video")
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
    wait_for(lambda: not previous_preview.exists())
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
    assert window.thumbnails_button.isChecked()
    assert window.current is None and window.preview_panel.isHidden()
    wait_for(lambda: window.thumb_job is None)
    assert all(not widget.thumb.isHidden() for widget in window.widgets.values())
    partial = next(n for n in range(window.list.count())
                   if window.list.item(n).data(Qt.ItemDataRole.UserRole).status == 'partial')
    click_row(window, partial)
    wait_for(lambda: not window.busy)
    assert window.inspector.currentWidget() is window.recovery
    assert window.player.source().isEmpty() and not window.recovery.save.isEnabled()
    assert '没有找到' in window.recovery.notice.text()


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


@pytest.mark.parametrize('width,height', [(160, 90), (90, 160)])
def test_resized_video_stays_above_controls(window, width, height):
    from PySide6.QtGui import QImage
    from PySide6.QtMultimedia import QVideoFrame
    from PySide6.QtCore import QPoint

    window.inspector.show()
    window.preview_panel.show()
    window.stack.setCurrentWidget(window.video)
    frame = QImage(width, height, QImage.Format.Format_RGB32)
    frame.fill(Qt.GlobalColor.green)
    window.player.videoSink().setVideoFrame(QVideoFrame(frame))
    for w, h in [(1120, 760), (900, 640)]:
        window.resize(w, h)
        QApplication.processEvents()
        viewport = window.video.viewport()
        bottom = viewport.mapTo(window, viewport.rect().bottomLeft()).y()
        assert window.video.surface.size().toSize() == viewport.size()
        for control in (window.play, window.seek, window.clock, window.mute):
            assert bottom < control.mapTo(window, QPoint(0, 0)).y()
            assert window.rect().contains(control.mapTo(window, control.rect().bottomRight()))


def test_recovery_panel_exports_only_checked_clip(window, tmp_path):
    from test_recovery import broken_movie
    row = broken_movie(window.cache)
    window.scanned([row])
    window.media_filter.setCurrentIndex(2)
    click_row(window)
    wait_for(lambda: not window.busy)
    assert len(window.recovery.clips) == 2
    window.recovery.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
    click(window.recovery.save)
    wait_for(lambda: window.last_results is not None and not window.busy)
    paths, errors = window.last_results
    assert not errors and len(paths) == 1 and '4.000s' in paths[0].name
    assert core.inspect(paths[0], deep=True).duration == 2
    assert window.recovery.isEnabled() and not window.recovery.isHidden()


def test_select_all_and_invert_preserve_hidden_checks(window):
    from dataclasses import replace
    row = next(r for r in core.scan(window.cache) if r.status == 'covered')
    window.scanned([row,replace(row,path=row.path.with_name('second'))])
    click(window.select_all)
    assert len(window.checked_rows())==2
    window.search.setText('second')
    window.invert_visible()
    assert [r.path for r in window.checked_rows()]==[row.path]
    assert '含隐藏 1 项' in window.selection.text()


def test_rescan_uses_selected_directory(window, monkeypatch):
    calls = []
    def fake_scan(root, progress, timeout):
        calls.append(Path(root))
        progress(0, 0, '扫描')
        return []
    monkeypatch.setattr(core, 'scan', fake_scan)
    click(window.rescan_button)
    wait_for(lambda: not window.busy)
    assert calls == [window.cache]
    assert window.min_size.isVisible() and window.max_duration.isVisible()


def test_stop_analysis_preserves_previous_results(window):
    import threading
    started = threading.Event()
    completed = []
    previous = list(window.rows)
    def work(progress):
        started.set()
        while True:
            time.sleep(.01)
            progress(0, 1, '分析片段')
    window.task(work, completed.append, analysis=True)
    wait_for(started.is_set)
    assert window.stop_button.isEnabled()
    click(window.stop_button)
    wait_for(lambda: not window.busy)
    assert not completed and window.rows == previous
    assert '已停止' in window.status.text()
    assert not window.stop_button.isEnabled()


def test_recovery_panel_keeps_player_and_fragment_tabs_visible(window):
    from test_recovery import broken_movie
    row = broken_movie(window.cache)
    window.scanned([row])
    window.media_filter.setCurrentIndex(2)
    click_row(window)
    wait_for(lambda: not window.busy)
    assert window.inspector.currentWidget() is window.recovery
    assert window.recovery.player_area.isVisible()
    assert window.recovery.tabs.tabText(0).startswith('缓存片段')
    assert window.recovery.tabs.tabText(1) == '合并信息'
    assert window.recovery.table.columnCount() == 5
    assert not window.clip_pause.isEnabled()
    if window.recovery.clips:
        window.watch_clip(window.recovery.clips[0])
        assert window.clip_pause.isEnabled()
        window.close_clip()
        assert not window.clip_pause.isEnabled()


def test_partial_preview_arrives_before_analysis_finishes(window, monkeypatch):
    import threading
    from test_recovery import broken_movie
    from tg_cache_lens import recovery
    row = broken_movie(window.cache)
    window.scanned([row]); window.media_filter.setCurrentIndex(2)
    blocked = threading.Event(); release = threading.Event()
    checked = recovery.checked_clip
    calls = []
    def delayed(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            blocked.set()
            if not release.wait(15): raise RuntimeError('synthetic timeout')
        return checked(*args, **kwargs)
    monkeypatch.setattr(recovery, 'checked_clip', delayed)
    click_row(window)
    try:
        wait_for(lambda: blocked.is_set() and len(window.recovery.clips) == 1)
        assert window.busy and window.progress.isVisible()
        assert window.recovery.watch.isEnabled()
        assert not window.recovery.save.isEnabled()
    finally:
        release.set()
    wait_for(lambda: not window.busy)
    assert len(window.recovery.clips) == 2
    assert not window.progress.isVisible() and window.recovery.save.isEnabled()


def test_stop_keeps_already_discovered_clips(window, monkeypatch):
    import threading
    from test_recovery import broken_movie
    from tg_cache_lens import recovery
    row = broken_movie(window.cache)
    window.scanned([row]); window.media_filter.setCurrentIndex(2)
    blocked, release = threading.Event(), threading.Event()
    checked = recovery.checked_clip
    calls = []
    def delayed(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            blocked.set()
            if not release.wait(15): raise RuntimeError('synthetic timeout')
        return checked(*args, **kwargs)
    monkeypatch.setattr(recovery, 'checked_clip', delayed)
    click_row(window)
    try:
        wait_for(lambda: blocked.is_set() and len(window.recovery.clips) == 1)
        click(window.stop_button)
    finally:
        release.set()
    wait_for(lambda: not window.busy)
    assert len(window.recovery.clips) == 1 and window.recovery.clips[0].path.exists()
    assert not window.progress.isVisible() and window.recovery.save.isEnabled()


def test_fragment_export_keeps_clips_when_filter_changes(window, monkeypatch):
    import threading
    from test_recovery import broken_movie
    from tg_cache_lens import recovery
    row = broken_movie(window.cache)
    window.scanned([row]); window.media_filter.setCurrentIndex(2)
    click_row(window); wait_for(lambda: not window.busy)
    clips = list(window.recovery.clips)
    started, release = threading.Event(), threading.Event()
    def export(*args):
        started.set()
        if not release.wait(15): raise RuntimeError('synthetic timeout')
        assert all(clip.path.exists() for clip in clips)
        return [], []
    monkeypatch.setattr(recovery, 'export_merged_clips', export)
    window.save_clips(clips)
    try:
        wait_for(started.is_set)
        window.search.setText('no matching video')
        assert all(clip.path.exists() for clip in clips)
    finally:
        release.set()
    wait_for(lambda: not window.busy)
    # Completed analysis remains available for the current session even if
    # changing the filter closes the preview during export.
    assert all(clip.path.exists() for clip in clips)


@pytest.mark.parametrize('raises', [False, True])
def test_failed_fragment_merge_can_be_retried(window, monkeypatch, raises):
    from test_recovery import broken_movie
    row = broken_movie(window.cache)
    window.scanned([row]); window.media_filter.setCurrentIndex(2)
    click_row(window); wait_for(lambda: not window.busy)
    clips = list(window.recovery.clips)
    calls = []
    def export(*args):
        calls.append(args[1])
        if len(calls) == 1 and raises: raise RuntimeError('synthetic failure')
        return ([], ['synthetic failure']) if len(calls) == 1 else ([window.output/'result.mp4'], [])
    monkeypatch.setattr('tg_cache_lens.gui.recovery.export_merged_clips', export)
    window.save_clips(clips)
    wait_for(lambda: not window.busy)
    assert window.retry_button.isVisible()
    click(window.retry_button)
    wait_for(lambda: not window.busy)
    assert len(calls) == 2 and not window.retry_button.isVisible()


def test_switching_back_reuses_completed_fragment_analysis(window):
    from test_recovery import broken_movie
    first = broken_movie(window.cache/'one')
    second = broken_movie(window.cache/'two')
    window.scanned([first, second])
    window.media_filter.setCurrentIndex(2)
    click_row(window, 0)
    wait_for(lambda: not window.busy and len(window.recovery.clips) == 2)
    paths = [clip.path for clip in window.recovery.clips]
    click_row(window, 1)
    wait_for(lambda: not window.busy and len(window.recovery.clips) == 2)
    click_row(window, 0)
    assert not window.busy
    assert [clip.path for clip in window.recovery.clips] == paths
    assert all(path.exists() for path in paths)
