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
    w = Window(cache, tmp_path/'exports', autostart=False)
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


def test_gui_selection_filter_export(window):
    click(window.scan_button)
    wait_for(lambda: len(window.rows)==3 and not window.busy)
    wait_for(lambda: len(window.images)>=2)
    good = next(w for w in window.widgets.values() if w.media.status=='covered')
    partial = next(w for w in window.widgets.values() if w.media.status=='partial')
    assert not partial.check.isEnabled()
    click(good.check)
    window.search.setText('1002')
    assert len(window.checked_rows()) == 1
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
    click(window.play)
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
