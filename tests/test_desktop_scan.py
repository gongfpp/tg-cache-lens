import struct
from pathlib import Path

import xxhash
import pytest

from tg_cache_lens.desktop_scan import resolve_tdata, scan
from test_desktop_crypto import make_key_file
from test_desktop_store import write_container


def make_desktop(cache, tmp_path, password=''):
    root = tmp_path/'portable'/'tdata'
    database = root/'user_data'/'media_cache'
    version = database/'0'
    version.mkdir(parents=True)
    (database/'version').write_bytes(struct.pack('<i', 0))
    key = bytes(range(256))
    make_key_file(root/'key_datas', password, key)
    video = (cache/'telegram-cloud-document-1-1001_partial').read_bytes()
    value = struct.pack('<III', 1, 0, len(video)) + video
    place = bytes.fromhex('123456789abcde')
    record = bytearray(32)
    record[0] = 1; record[2:5] = len(value).to_bytes(3, 'little')
    record[5:12] = place
    struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x10000)
    write_container(version/'binlog', key, bytes(16) + record)
    write_container(version/'21'/'436587A9CBED', key, value)
    return root, video


def test_scan_encrypted_synthetic_media(cache, tmp_path):
    root, video = make_desktop(cache, tmp_path)
    workdir = tmp_path/'temp'; workdir.mkdir()
    assert resolve_tdata(root.parent) == root
    rows = scan(root.parent, workdir, deep=True)
    assert len(rows) == 1
    assert rows[0].status == 'unknown' and rows[0].cached_size == len(video)
    assert rows[0].path.read_bytes() == video
    rows[0].desktop_source.revalidate()


def test_timed_index_exposes_cache_time_in_scan(cache, tmp_path):
    from tg_cache_lens.desktop_crypto import read_tdef
    root, _ = make_desktop(cache, tmp_path)
    key = bytes(range(256))
    binlog = root/'user_data'/'media_cache'/'0'/'binlog'
    original = read_tdef(binlog, key)
    record = bytearray(original[16:48] + bytes(16))
    struct.pack_into('<III', record, 32, 1, 0, 1700000000)
    write_container(binlog, key, bytes([0, 1]) + bytes(14) + record)
    work = tmp_path/'work'; work.mkdir()
    row = scan(root, work)[0]
    assert row.source_modified_ns == 1700000000 * 1_000_000_000


def test_gui_exports_contiguous_cached_video_without_analysis(cache, tmp_path):
    from PySide6.QtWidgets import QApplication
    from tg_cache_lens.gui import Window
    from tg_cache_lens import core
    from test_gui import click, click_row, wait_for
    app = QApplication.instance() or QApplication([])
    root, _ = make_desktop(cache, tmp_path)
    window = Window(root.parent, tmp_path/'exports'); window.show()
    try:
        window.scan(); wait_for(lambda: not window.busy and len(window.rows) == 1)
        window.media_filter.setCurrentIndex(2)
        click_row(window)
        assert not window.busy and window.recovery.export_cached.isVisible()
        assert not window.recovery.clips
        click(window.recovery.export_cached)
        wait_for(lambda: not window.busy and window.last_results is not None)
        paths, errors = window.last_results
        assert not errors and len(paths) == 1 and 'cached-unknown' in paths[0].name
        assert core.inspect(paths[0], deep=True).duration > 0
    finally:
        window.close()
        app.processEvents()


def test_contiguous_cached_export_supports_formats_and_rechecks_source(cache, tmp_path):
    from tg_cache_lens import core, recovery
    root, video = make_desktop(cache, tmp_path)
    work = tmp_path/'work'; work.mkdir()
    row = scan(root, work)[0]
    output = tmp_path/'exports'
    paths = [recovery.export_cached(row, output, 60, fmt) for fmt in ('mp4', 'mkv', 'original')]
    assert [path.suffix for path in paths] == ['.mp4', '.mkv', '.mp4']
    assert len(set(paths)) == 3 and paths[-1].read_bytes() == video
    assert all(core.inspect(path, deep=True).status == 'unknown' for path in paths)
    write_container(root/'user_data'/'media_cache'/'0'/'21'/'436587A9CBED', bytes(range(256)), b'changed')
    with pytest.raises(RuntimeError, match='已变化'):
        recovery.export_cached(row, output)


def test_contiguous_but_corrupt_tail_offers_recoverable_clip(cache, tmp_path):
    from tg_cache_lens import recovery
    root, video = make_desktop(cache, tmp_path)
    version = root/'user_data'/'media_cache'/'0'
    key = bytes(range(256))
    truncated = video[:-5000]
    value = struct.pack('<III', 1, 0, len(truncated)) + truncated
    record = bytearray(32)
    record[0] = 1; record[2:5] = len(value).to_bytes(3, 'little')
    record[5:12] = bytes.fromhex('123456789abcde')
    struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x10000)
    write_container(version/'binlog', key, bytes(16) + record)
    write_container(version/'21'/'436587A9CBED', key, value)
    work = tmp_path/'work'; work.mkdir()
    row = scan(root, work)[0]
    assert row.status == 'unknown' and recovery.can_export_cached(row)
    with pytest.raises(RuntimeError, match='请尝试分析片段'):
        recovery.export_cached(row, tmp_path/'exports')
    assert recovery.recover(row, work)
    from tg_cache_lens.cli import batch
    paths, errors = batch([row], tmp_path/'automatic-exports', timeout=60)
    assert not errors and len(paths) == 1 and '-partial-' in paths[0].name


def test_gui_scans_and_recovers_synthetic_desktop(cache, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from tg_cache_lens.gui import Window, QInputDialog
    from test_gui import click, click_row, wait_for
    root, _ = make_desktop(cache, tmp_path, password='synthetic')
    monkeypatch.setattr(QInputDialog, 'getText', lambda *args: ('synthetic', True))
    window = Window(root.parent, tmp_path/'exports')
    window.show()
    try:
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 1)
        assert window.rows[0].status == 'unknown'
        window.media_filter.setCurrentIndex(2)
        frames = []
        window.clip_player.videoSink().videoFrameChanged.connect(lambda frame: frames.append(frame.isValid()))
        click_row(window)
        assert Path(window.clip_player.source().toLocalFile()) == window.rows[0].path
        wait_for(lambda: any(frames))
        assert window.widgets[str(window.rows[0].path)].check.isEnabled()
        window.export_rows([window.rows[0]])
        wait_for(lambda: not window.busy and window.last_results is not None)
        assert len(window.last_results[0]) == 1 and not window.last_results[1]
        window.last_results = None
        click(window.recovery.analyze)
        wait_for(lambda: not window.busy and len(window.recovery.clips) > 0)
        frames = []
        window.clip_player.videoSink().videoFrameChanged.connect(lambda frame: frames.append(frame.isValid()))
        click(window.recovery.watch)
        wait_for(lambda: any(frames))
        assert window.recovery.player_area.isVisible()
        window.close_clip()
        window.save_clips([window.recovery.clips[0]])
        wait_for(lambda: not window.busy and window.last_results is not None)
        paths, errors = window.last_results
        assert not errors and len(paths) == 1
        place = root/'user_data'/'media_cache'/'0'/'21'/'436587A9CBED'
        write_container(place, bytes(range(256)), b'changed')
        window.last_results = None
        window.save_clips([window.recovery.clips[0]])
        wait_for(lambda: not window.busy and window.last_results is not None)
        paths, errors = window.last_results
        assert not paths and errors
    finally:
        window.close()


def test_gui_opens_new_argon2_password_format(cache, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from tg_cache_lens.gui import Window, QInputDialog
    from test_gui import wait_for
    from test_desktop_crypto import make_new_key_file
    root, _ = make_desktop(cache, tmp_path)
    make_new_key_file(root/'key_datas', 'synthetic', bytes(range(256)), 1)
    monkeypatch.setattr(QInputDialog, 'getText', lambda *args: ('synthetic', True))
    window = Window(root.parent, tmp_path/'exports')
    window.show()
    try:
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 1)
        assert window.rows[0].status == 'unknown'
    finally:
        window.close()


def test_gui_rescan_releases_old_snapshots_and_keeps_current_on_failure(cache, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from tg_cache_lens import desktop_scan
    from tg_cache_lens.gui import Window
    from test_gui import wait_for
    root, _ = make_desktop(cache, tmp_path)
    window = Window(root.parent, tmp_path/'exports')
    window.show()
    try:
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 1)
        old = window.rows[0].path
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 1 and window.rows[0].path != old)
        wait_for(lambda: not old.exists())
        current = window.rows[0].path
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic scan failure')
        monkeypatch.setattr(desktop_scan, 'scan', fail)
        window.scan()
        wait_for(lambda: not window.busy and 'synthetic scan failure' in window.status.text())
        assert current.exists()
        assert len(list(Path(window.temp.name).glob('desktop-*'))) == 1
    finally:
        window.close()


@pytest.mark.parametrize('missing', ['ffmpeg', 'ffprobe'])
def test_desktop_missing_media_tool_is_reported_and_keeps_previous_results(cache, tmp_path, monkeypatch, missing, capsys):
    from PySide6.QtWidgets import QApplication
    from tg_cache_lens import core
    from tg_cache_lens.gui import Window
    from tg_cache_lens.__main__ import main
    from test_gui import wait_for
    app = QApplication.instance() or QApplication([])
    root, _ = make_desktop(cache, tmp_path)
    window = Window(root.parent, tmp_path/'exports'); window.show()
    try:
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 1)
        previous = window.rows[0]
        real_binary = core.binary
        def without_tool(name):
            if name == missing:
                raise RuntimeError(f'找不到 {name}。请安装 FFmpeg')
            return real_binary(name)
        monkeypatch.setattr(core, 'binary', without_tool)
        window.scan()
        wait_for(lambda: not window.busy and f'找不到 {missing}' in window.status.text())
        assert window.rows == [previous] and previous.path.exists()
        assert main(['--cache', str(root), '--scan']) == 1
        assert f'找不到 {missing}' in capsys.readouterr().err
    finally:
        window.close()
        app.processEvents()


def test_scan_supports_legacy_key_data_backup(cache, tmp_path):
    root, _ = make_desktop(cache, tmp_path)
    (root/'key_datas').rename(root/'key_data0')
    workdir = tmp_path/'work'; workdir.mkdir()
    assert resolve_tdata(root.parent) == root
    assert len(scan(root.parent, workdir)) == 1


def test_scan_supports_pre_multiauth_legacy_map(cache, tmp_path):
    from tg_cache_lens.desktop_crypto import legacy_key_directory
    from test_desktop_crypto import make_legacy_map
    root, video = make_desktop(cache, tmp_path)
    (root/'key_datas').unlink()
    make_legacy_map(legacy_key_directory(root, 'data')/'maps', 'synthetic', bytes(range(256)))
    workdir = tmp_path/'work'; workdir.mkdir()
    rows = scan(root.parent, workdir, passcode='synthetic')
    assert len(rows) == 1 and rows[0].path.read_bytes() == video


def test_custom_portable_data_name_and_multiple_accounts(cache, tmp_path, monkeypatch, capsys):
    import shutil
    import json
    from tg_cache_lens.__main__ import main
    root, _ = make_desktop(cache, tmp_path)
    (root/'key_datas').rename(root/'key_alphas')
    (root/'user_data').rename(root/'user_alpha')
    shutil.copytree(root/'user_alpha', root/'user_alpha#2')
    workdir = tmp_path/'work'; workdir.mkdir()
    assert len(scan(root.parent, workdir)) == 2
    # A separate default profile must not be mixed with the selected profile.
    make_key_file(root/'key_datas', '', bytes(reversed(range(256))))
    (root/'user_data').mkdir()
    monkeypatch.delenv('TG_CACHE_LENS_PASSCODE', raising=False)
    assert main(['--cache', str(root/'key_alphas'), '--scan']) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result) == 2 and all('user_alpha' in item['path'] for item in result)


def test_gui_selects_custom_profile_when_multiple_keys_exist(cache, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from tg_cache_lens.gui import Window, QInputDialog
    from test_gui import wait_for
    root, _ = make_desktop(cache, tmp_path)
    (root/'key_datas').rename(root/'key_alphas')
    (root/'user_data').rename(root/'user_alpha')
    make_key_file(root/'key_datas', '', bytes(reversed(range(256))))
    (root/'user_data').mkdir()
    monkeypatch.setattr(QInputDialog, 'getItem', lambda *args: ('alpha', True))
    window = Window(root.parent, tmp_path/'exports'); window.show()
    try:
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 1)
        assert window.rows[0].desktop_source.database.parent.name == 'user_alpha'
    finally:
        window.close()


def test_cli_scans_synthetic_desktop_with_password(cache, tmp_path, monkeypatch, capsys):
    from tg_cache_lens.__main__ import main
    import json
    root, _ = make_desktop(cache, tmp_path, password='synthetic')
    monkeypatch.setenv('TG_CACHE_LENS_PASSCODE', 'synthetic')
    assert main(['--cache', str(root.parent), '--scan', '--deep']) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]['status'] == 'unknown'
    assert rows[0]['size'] is None and rows[0]['cached_size'] > 0
    assert str(root) in rows[0]['path']


def test_small_cache_and_corrupt_entry_do_not_hide_other_videos(cache, tmp_path, capsys, monkeypatch):
    from tg_cache_lens.__main__ import main
    from tg_cache_lens import desktop_scan, desktop_store
    root, video = make_desktop(cache, tmp_path)
    database = root/'user_data'/'cache'
    version = database/'1'; version.mkdir(parents=True)
    (database/'version').write_bytes(struct.pack('<i', 1))
    records = []
    values = [b'partial:' + video, b'\x89PNG\r\n\x1a\nimage', b'broken value']
    for number, data in enumerate(values, 1):
        place = bytes([number]) + bytes(6)
        record = bytearray(32)
        record[0] = 1; record[2:5] = len(data).to_bytes(3, 'little')
        record[5:12] = place
        struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(data), 0x101, number)
        records.append(record)
        write_container(version/f'{number:X}0'/'000000000000', bytes(range(256)), data)
    write_container(version/'binlog', bytes(range(256)), bytes(16) + b''.join(records))
    write_container(version/'30'/'000000000000', bytes(range(256)), b'incorrect')
    workdir = tmp_path/'work'; workdir.mkdir()
    reads = []
    original_read = desktop_store.read_value
    def counted_read(directory, entry, key):
        reads.append(entry.key)
        return original_read(directory, entry, key)
    monkeypatch.setattr(desktop_scan, 'read_value', counted_read)
    monkeypatch.setattr(desktop_store, 'read_value', counted_read)
    submitted = []
    original_submit = desktop_scan.ThreadPoolExecutor.submit
    def counted_submit(pool, function, job):
        submitted.append(job[1][0].name)
        return original_submit(pool, function, job)
    monkeypatch.setattr(desktop_scan.ThreadPoolExecutor, 'submit', counted_submit)
    rows = scan(root, workdir)
    assert reads.count((0x101, 1)) == 1
    assert submitted[0] == 'media_cache'
    assert sorted(row.status for row in rows) == ['partial', 'unknown']
    assert len(rows.warnings) == 1 and '校验' in rows.warnings[0]
    plain = next(row for row in rows if row.status == 'partial')
    assert plain.path.read_bytes() == video
    plain.desktop_source.revalidate()
    records[0][5:12] = bytes([4]) + bytes(6)
    write_container(version/'40'/'000000000000', bytes(range(256)), values[0])
    write_container(version/'binlog', bytes(range(256)), bytes(16) + b''.join(records))
    with pytest.raises(RuntimeError, match='已变化'):
        plain.desktop_source.revalidate()
    assert main(['--cache', str(root), '--scan']) == 1
    assert '读取失败' in capsys.readouterr().err


def test_cli_recovers_synthetic_desktop_video(cache, tmp_path, monkeypatch, capsys):
    from tg_cache_lens.cli import interactive
    root, _ = make_desktop(cache, tmp_path)
    answers = iter(['1', 'c', '1', 'e', '', 'q'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    output = tmp_path/'exports'
    assert interactive(root, output) == 0
    assert len(list(output.glob('*partial-*.mp4'))) == 1
    assert '成功 1 项' in capsys.readouterr().out


def test_scan_and_recovery_do_not_modify_synthetic_tdata(cache, tmp_path):
    from hashlib import sha256
    from tg_cache_lens import recovery
    root, _ = make_desktop(cache, tmp_path)
    def snapshot():
        return {str(path.relative_to(root)): sha256(path.read_bytes()).digest()
                for path in root.rglob('*') if path.is_file()}
    before = snapshot()
    workdir = tmp_path/'work'; workdir.mkdir()
    row = scan(root, workdir)[0]
    with pytest.raises(RuntimeError, match='缓存目录以外'):
        recovery.recover(row, root/'temporary')
    assert not (root/'temporary').exists()
    clips = recovery.recover(row, workdir)
    paths, errors = recovery.export_clips(row, clips, tmp_path/'exports', 60)
    assert clips and paths and not errors
    assert snapshot() == before


def test_desktop_analysis_revalidates_source_once_after_processing(cache, tmp_path, monkeypatch):
    from tg_cache_lens import recovery
    root, _ = make_desktop(cache, tmp_path)
    work = tmp_path/'work'; work.mkdir()
    row = scan(root, work)[0]
    cls = type(row.desktop_source)
    original = cls.revalidate
    calls = []
    def counted(source):
        calls.append(True)
        return original(source)
    monkeypatch.setattr(cls, 'revalidate', counted)
    assert recovery.recover(row, work)
    assert calls == [True]


def test_gui_chinese_portable_and_output_paths(cache, tmp_path):
    from hashlib import sha256
    from PySide6.QtWidgets import QApplication
    from tg_cache_lens.gui import Window
    from test_gui import click, click_row, wait_for
    app = QApplication.instance() or QApplication([])
    root, _ = make_desktop(cache, tmp_path/'中文目录'/'便携客户端')
    output = tmp_path/'导出目录'/'视频'
    def snapshot():
        return {str(path.relative_to(root)): sha256(path.read_bytes()).digest()
                for path in root.rglob('*') if path.is_file()}
    before = snapshot()
    window = Window(root.parent, output)
    temporary = Path(window.temp.name)
    window.show()
    try:
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 1)
        window.media_filter.setCurrentIndex(2)
        click_row(window)
        click(window.recovery.analyze)
        wait_for(lambda: not window.busy and bool(window.recovery.clips))
        for _ in range(2):
            window.last_results = None
            click(window.recovery.save)
            wait_for(lambda: not window.busy and window.last_results is not None)
            assert len(window.last_results[0]) == 1 and not window.last_results[1]
        outputs = sorted(output.glob('*.mp4'))
        assert len(outputs) == 2 and outputs[0].read_bytes() == outputs[1].read_bytes()
        assert snapshot() == before
    finally:
        window.close()
        app.processEvents()
    assert not temporary.exists()


def test_cli_prompts_for_password_on_demand(cache, tmp_path, monkeypatch):
    from tg_cache_lens.cli import interactive
    root, _ = make_desktop(cache, tmp_path, password='synthetic')
    monkeypatch.delenv('TG_CACHE_LENS_PASSCODE', raising=False)
    monkeypatch.setattr('getpass.getpass', lambda _: 'synthetic')
    monkeypatch.setattr('builtins.input', lambda _: 'q')
    assert interactive(root, tmp_path/'exports') == 0


def test_damaged_account_index_reports_error_and_preserves_other_results(cache, tmp_path):
    root, _ = make_desktop(cache, tmp_path)
    database = root/'user_data#2'/'media_cache'
    version = database/'0'; version.mkdir(parents=True)
    (database/'version').write_bytes(struct.pack('<i', 0))
    (version/'binlog').write_bytes(b'corrupt')
    workdir = tmp_path/'work'; workdir.mkdir()
    rows = scan(root, workdir)
    assert len(rows) == 1
    assert len(rows.warnings) == 1 and 'user_data#2/media_cache' in rows.warnings[0]


def test_gui_shows_account_error_without_hiding_other_results(cache, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from tg_cache_lens.gui import Window, QMessageBox
    from test_gui import click, wait_for
    app = QApplication.instance() or QApplication([])
    root, _ = make_desktop(cache, tmp_path)
    damaged = root/'user_data#2'/'media_cache'/'0'
    damaged.mkdir(parents=True)
    (damaged/'binlog').write_bytes(b'corrupt')
    messages = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda parent, title, message: messages.append(message))
    window = Window(root.parent, tmp_path/'exports'); window.show()
    try:
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 1)
        assert not window.errors_button.isHidden() and '1 项读取失败' in window.status.text()
        click(window.errors_button)
        assert messages and 'user_data#2/media_cache' in messages[0]
    finally:
        window.close()
        app.processEvents()


def test_desktop_search_and_details_use_source_not_snapshot(cache, tmp_path, monkeypatch, capsys):
    import shutil
    from PySide6.QtWidgets import QApplication
    from tg_cache_lens.cli import interactive
    from tg_cache_lens.gui import Window
    from test_gui import wait_for
    app = QApplication.instance() or QApplication([])
    root, _ = make_desktop(cache, tmp_path)
    shutil.copytree(root/'user_data', root/'user_data#2')
    window = Window(root.parent, tmp_path/'exports'); window.show()
    try:
        window.scan()
        wait_for(lambda: not window.busy and len(window.rows) == 2)
        assert all(row.key == '0000000000010001-0000000000010000' for row in window.rows)
        assert all(row.source_modified_ns is None for row in window.rows)
        window.media_filter.setCurrentIndex(2)
        window.search.setText('user_data#2')
        visible = [window.list.item(n) for n in range(window.list.count()) if not window.list.item(n).isHidden()]
        assert len(visible) == 1
        window.list.setCurrentItem(visible[0])
        details = window.details.toPlainText()
        assert 'user_data#2' in details and '修改时间' not in details and '创建时间' not in details
        assert str(visible[0].data(256).path) not in details
    finally:
        window.close()
        app.processEvents()
    answers = iter(['/user_data#2', 'q'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    assert interactive(root, tmp_path/'exports') == 0
    assert '1 项 · 搜索：user_data#2' in capsys.readouterr().out


def test_unclassifiable_last_slice_does_not_hide_verified_prefix(cache, tmp_path):
    from tg_cache_lens.desktop_crypto import read_tdef
    root, video = make_desktop(cache, tmp_path)
    version = root/'user_data'/'media_cache'/'0'
    key = bytes(range(256))
    tail = b'unaligned raw last slice'
    record = bytearray(32)
    record[0] = 1; record[2:5] = len(tail).to_bytes(3, 'little')
    record[5:12] = bytes([2]) + bytes(6)
    struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(tail), 0x10001, 0x10002)
    binlog = read_tdef(version/'binlog', key)
    write_container(version/'binlog', key, binlog + record)
    write_container(version/'20'/'000000000000', key, tail)
    workdir = tmp_path/'work'; workdir.mkdir()
    rows = scan(root, workdir)
    assert len(rows) == 1 and not rows.warnings
    assert rows[0].path.read_bytes() == video
    assert rows[0].desktop_source.ambiguous == (2,)
    assert '未参与恢复' in rows[0].detail
    rows[0].desktop_source.revalidate()
    changed = b'different unused tail'
    record[2:5] = len(changed).to_bytes(3, 'little')
    struct.pack_into('<I', record, 12, xxhash.xxh32_intdigest(changed))
    write_container(version/'binlog', key, binlog + record)
    write_container(version/'20'/'000000000000', key, changed)
    with pytest.raises(RuntimeError, match='已变化'):
        rows[0].desktop_source.revalidate()


def test_scanning_multiple_videos_reuses_verified_index(cache, tmp_path, monkeypatch):
    import threading
    from tg_cache_lens import desktop_store, desktop_scan
    root, video = make_desktop(cache, tmp_path)
    version = root/'user_data'/'media_cache'/'0'
    key = bytes(range(256))
    raw = desktop_store.read_tdef(version/'binlog', key)
    value = struct.pack('<III', 1, 0, len(video)) + video
    record = bytearray(32)
    record[0] = 1; record[2:5] = len(value).to_bytes(3, 'little')
    record[5:12] = bytes([2]) + bytes(6)
    struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x20000)
    write_container(version/'binlog', key, raw + record)
    write_container(version/'20'/'000000000000', key, value)
    calls = []
    original = desktop_store.read_tdef
    def counted(path, key):
        if path.name == 'binlog': calls.append(path)
        return original(path, key)
    monkeypatch.setattr(desktop_store, 'read_tdef', counted)
    original_inspect = desktop_scan.core.inspect
    first_blocked, release, shown = threading.Event(), threading.Event(), threading.Event()
    def delayed_inspect(path, *args, **kwargs):
        if Path(path).name.startswith('00000000-'):
            first_blocked.set()
            if not release.wait(15): raise RuntimeError('synthetic timeout')
        return original_inspect(path, *args, **kwargs)
    monkeypatch.setattr(desktop_scan.core, 'inspect', delayed_inspect)
    workdir = tmp_path/'work'; workdir.mkdir()
    received, result = [], []
    def on_row(row):
        received.append(row)
        shown.set()
    thread = threading.Thread(target=lambda: result.append(scan(root, workdir, on_row=on_row)))
    thread.start()
    try:
        assert first_blocked.wait(10) and shown.wait(10)
        assert thread.is_alive() and received[0].desktop_source.base_key == (0x10001, 0x20000)
    finally:
        release.set()
        thread.join(20)
    assert not thread.is_alive()
    rows = result[0]
    assert len(rows) == 2 and len(calls) == 1


@pytest.mark.parametrize('stop', [False, True])
def test_gui_shows_scan_results_before_completion_and_keeps_selection(cache, tmp_path, monkeypatch, stop):
    import threading
    from PySide6.QtWidgets import QApplication
    from tg_cache_lens import core, desktop_scan
    from tg_cache_lens.gui import Window
    from test_gui import wait_for, click
    app = QApplication.instance() or QApplication([])
    root, _ = make_desktop(cache, tmp_path)
    row = next(item for item in core.scan(cache) if item.status == 'covered')
    ready, release = threading.Event(), threading.Event()
    def staged_scan(*args, on_row, **kwargs):
        on_row(row)
        ready.set()
        if not release.wait(15): raise RuntimeError('synthetic timeout')
        result = desktop_scan.ScanResults(); result.append(row)
        return result
    monkeypatch.setattr(desktop_scan, 'scan', staged_scan)
    window = Window(root, tmp_path/'exports')
    window.thumbnails_button.setChecked(False)
    window.show()
    try:
        window.scan()
        wait_for(lambda: ready.is_set() and len(window.rows) == 1)
        assert window.busy and window.list.count() == 1
        window.list.setCurrentItem(window.list.item(0))
        if stop: click(window.stop_button)
        release.set()
        wait_for(lambda: not window.busy)
        assert window.list.count() == 1 and window.rows[0] is row and window.current is row
    finally:
        release.set()
        window.close()
        app.processEvents()


def test_missing_and_damaged_slices_preserve_verified_video(cache, tmp_path):
    from tg_cache_lens.desktop_crypto import read_tdef
    root, video = make_desktop(cache, tmp_path)
    version = root/'user_data'/'media_cache'/'0'
    key = bytes(range(256))
    binlog = read_tdef(version/'binlog', key)
    records = []
    for number in (2, 3, 4):
        value = struct.pack('<III', 1, 0, 100) + b'invalid' if number == 4 else f'synthetic {number}'.encode()
        place = bytes([number]) + bytes(6)
        record = bytearray(32)
        record[0] = 1; record[2:5] = len(value).to_bytes(3, 'little')
        record[5:12] = place
        struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x10000 + number)
        records.append(record)
    write_container(version/'binlog', key, binlog + b''.join(records))
    write_container(version/'30'/'000000000000', key, b'corrupt')
    write_container(version/'40'/'000000000000', key, struct.pack('<III', 1, 0, 100) + b'invalid')
    workdir = tmp_path/'work'; workdir.mkdir()
    rows = scan(root, workdir)
    assert len(rows) == 1 and rows[0].path.read_bytes() == video
    assert rows[0].desktop_source.unavailable == (2, 3, 4)
    assert '缺失或损坏' in rows[0].detail
    rows[0].desktop_source.revalidate()
    write_container(version/'20'/'000000000000', key, b'synthetic 2')
    with pytest.raises(RuntimeError, match='已变化'):
        rows[0].desktop_source.revalidate()


def test_nonvideo_temporary_snapshot_is_removed(cache, tmp_path):
    from tg_cache_lens.desktop_crypto import read_tdef
    root, _ = make_desktop(cache, tmp_path)
    version = root/'user_data'/'media_cache'/'0'
    key = bytes(range(256))
    value = struct.pack('<III', 1, 0, 8) + b'notvideo'
    record = bytearray(32)
    record[0] = 1; record[2:5] = len(value).to_bytes(3, 'little')
    record[5:12] = bytes([2]) + bytes(6)
    struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x20000)
    original = read_tdef(version/'binlog', key)
    write_container(version/'binlog', key, original + record)
    write_container(version/'20'/'000000000000', key, value)
    workdir = tmp_path/'work'; workdir.mkdir()
    rows = scan(root, workdir)
    assert len(rows) == 1 and not rows.warnings
    assert sorted(path.name for path in workdir.iterdir()) == sorted([
        rows[0].path.name, rows[0].path.name + '.meta'])
