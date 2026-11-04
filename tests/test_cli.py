import json
from tg_cache_lens.__main__ import main
from tg_cache_lens import cli


def test_gui_receives_unspecified_directories_for_settings_restore(monkeypatch):
    calls = []
    monkeypatch.setattr('tg_cache_lens.__main__.launch_gui', lambda *args: calls.append(args) or 0)
    assert main(['gui']) == 0
    assert calls == [(None, None, 600)]


def test_json_compatibility(cache, capsys):
    assert main(['--cache', str(cache), '--scan', '--deep']) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r['status'] for r in rows} == {'ready', 'partial', 'unknown'}


def test_scan_without_cache_uses_auto_root(cache, capsys, monkeypatch):
    from tg_cache_lens import core
    monkeypatch.setattr(core, 'auto_root', lambda: cache)
    assert main(['--scan']) == 0
    assert len(json.loads(capsys.readouterr().out)) == 3


def test_script_export_and_rejection(cache, tmp_path, capsys):
    output = tmp_path / 'out'
    assert main(['export', '--cache', str(cache), '--output', str(output), '--all-complete']) == 0
    assert len(list(output.glob('*.mp4'))) == 1
    assert main(['export', '--file', str(cache/'telegram-cloud-document-1-1002_partial'), '--output', str(output)]) == 1
    assert len(list(output.glob('*.mp4'))) == 1


def test_simple_menu_single_and_batch(cache, tmp_path, monkeypatch, capsys):
    answers = iter(['1', 'e', '', '/1002', '/', 'a', 'q'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    assert cli.interactive(cache, tmp_path/'out') == 0
    output = capsys.readouterr().out
    assert '第 1/1 页 · 1 项 · 搜索：1002' in output
    assert len(list((tmp_path/'out').glob('*.mp4'))) == 2
