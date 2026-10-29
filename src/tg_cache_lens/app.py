from __future__ import annotations

from pathlib import Path
import tempfile

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, ProgressBar, Select, Static

from . import core

COLORS = {"ready": "#70e1b0", "covered": "#85c4fa", "partial": "#ffac91", "unknown": "#e3c878", "invalid": "#ff879c"}

def size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB": return f"{n:.1f} {unit}"
        n /= 1000

class CacheLens(App):
    TITLE = "CACHE LENS"
    SUB_TITLE = "Telegram · local video cache"
    CSS = """
    Screen { background: #101720; color: #d9e3ee; }
    Header { background: #172332; color: #70e1b0; }
    Footer { background: #172332; }
    #summary { height: 3; padding: 1 2; color: #70e1b0; }
    #filters { height: 3; margin: 0 2 1 2; }
    #search { width: 1fr; }
    #filter { width: 27; margin-left: 1; }
    #workspace { height: 1fr; margin: 0 2; }
    #table { width: 3fr; height: 1fr; border: solid #29394c; background: #101720; }
    DataTable > .datatable--header { background: #1b2b3b; color: #9bb0c8; text-style: bold; }
    DataTable > .datatable--cursor { background: #25465a; color: #ffffff; }
    #side { width: 2fr; min-width: 28; padding: 0 1; margin-left: 1; border: solid #29394c; }
    #details-scroll { height: 1fr; }
    #details { height: auto; padding: 1 1; }
    #actions { height: 11; }
    #actions Button { width: 100%; margin: 0 0 1 0; }
    #output-row { height: 3; margin: 1 2 0 2; }
    #output { width: 1fr; }
    #format { width: 22; margin-left: 1; }
    #toolbar { height: 3; margin: 1 2 0 2; }
    #toolbar Button { margin-right: 1; }
    #progress { width: 1fr; padding: 1 1; }
    #status { height: 2; padding: 0 2; color: #9bb0c8; }
    Button { background: #1b2b3b; border: tall #1b2b3b; }
    Button.-primary { background: #267f69; border: tall #267f69; }
    Input, SelectCurrent { background: #172332; border: tall #29394c; }
    .compact #workspace { layout: vertical; }
    .compact #table { width: 100%; height: 1fr; }
    .compact #side { width: 100%; height: 11; margin: 1 0 0 0; }
    .compact #actions { layout: horizontal; height: 3; }
    .compact #actions Button { width: 1fr; margin: 0 1 0 0; }
    .compact #details-scroll { height: 5; }
    .compact #details { height: auto; }
    """
    BINDINGS = [("q", "quit", "退出"), ("f5", "scan", "快速扫描"), ("f6", "deep", "深度验证"),
                ("p", "preview", "预览"), ("e", "export", "导出当前"), ("a", "batch", "导出完整"),
                ("slash", "search", "搜索")]

    def __init__(self, cache=None, output=None, timeout=600, autostart=True):
        super().__init__()
        self.cache = Path(cache) if cache else core.default_root()
        self.output = Path(output) if output else core.default_output()
        self.timeout = timeout
        self.autostart = autostart
        self.items = []
        self.visible_items = []
        self.busy = False
        self.preview_dir = tempfile.TemporaryDirectory(prefix="tg-cache-preview-")

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("扫描缓存，找回已完整下载的视频", id="summary")
        with Horizontal(id="filters"):
            yield Input(placeholder="搜索文件名  /", id="search")
            yield Select([("全部", "all"), ("完整 / 待验证", "complete"), ("数据不全", "partial"),
                          ("完整性未知", "unknown"), ("无法验证", "invalid")], value="all", allow_blank=False, id="filter")
        with Horizontal(id="workspace"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                with VerticalScroll(id="details-scroll"):
                    yield Static("选择视频查看详情", id="details", markup=False)
                with Vertical(id="actions"):
                    yield Button("▶  预览  P", id="preview")
                    yield Button("↓  导出当前  E", id="export", variant="primary")
                    yield Button("↓  导出全部完整  A", id="batch")
        with Horizontal(id="output-row"):
            yield Input(value=str(self.output), placeholder="导出文件夹", id="output")
            yield Select([("MP4", "mp4"), ("MKV", "mkv"), ("原格式", "original")], value="mp4", allow_blank=False, id="format")
        with Horizontal(id="toolbar"):
            yield Button("扫描 F5", id="scan")
            yield Button("验证 F6", id="deep")
            yield ProgressBar(total=100, show_eta=False, id="progress")
        yield Static("准备就绪", id="status", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one(DataTable).add_columns("状态", "视频", "大小", "覆盖", "时长")
        self.query_one(DataTable).focus()
        self.update_actions()
        if self.autostart: self.action_scan()

    def on_resize(self, event):
        self.screen.set_class(event.size.width < 100, "compact")

    @property
    def selected(self):
        i = self.query_one(DataTable).cursor_row
        return self.visible_items[i] if 0 <= i < len(self.visible_items) else None

    def update_actions(self):
        item = self.selected
        usable = item is not None and item.status in ("ready", "covered")
        for name in ("preview", "export"):
            self.query_one(f"#{name}", Button).disabled = self.busy or not usable
        self.query_one("#batch", Button).disabled = self.busy or not any(i.status in ("ready", "covered") for i in self.items)
        for name in ("scan", "deep"):
            self.query_one(f"#{name}", Button).disabled = self.busy

    def show_details(self):
        i = self.selected
        if i:
            duration = f"{i.duration:.1f} 秒" if i.duration is not None else "未知"
            text = f"{core.LABELS[i.status]}\n\n{i.width} × {i.height}   {i.codec.upper() or '—'}\n{duration}   ·   {size(i.size)}\n缓存覆盖 {i.coverage}   ·   {i.copies} 份\n\n{i.detail}\n\n{i.path.name}"
        else: text = "暂无匹配的视频\n\nF5 扫描缓存 · F6 深度验证"
        self.query_one("#details", Static).update(text)
        self.update_actions()

    def refresh_table(self):
        query = self.query_one("#search", Input).value.casefold()
        mode = self.query_one("#filter", Select).value
        self.visible_items = [i for i in self.items if query in i.path.name.casefold() and
            (mode == "all" or mode == i.status or mode == "complete" and i.status in ("ready", "covered"))]
        table = self.query_one(DataTable)
        table.clear()
        for i in self.visible_items:
            label = Text(core.LABELS[i.status], style=COLORS[i.status])
            name = i.path.name.replace("telegram-cloud-document-", "…")
            if len(name) > 34: name = name[:31]+"…"
            duration = f"{int(i.duration)//60}:{int(i.duration)%60:02}" if i.duration is not None else "—"
            table.add_row(label, name, size(i.size), i.coverage, duration)
        ready = sum(i.status == "ready" for i in self.items)
        covered = sum(i.status == "covered" for i in self.items)
        partial = sum(i.status == "partial" for i in self.items)
        self.query_one("#summary", Static).update(f"{len(self.items)} 项视频   ·   {ready} 完整   ·   {covered} 待验证   ·   {partial} 数据不全")
        self.show_details()

    def on_data_table_row_highlighted(self, _): self.show_details()
    def on_input_changed(self, event):
        if event.input.id == "search": self.refresh_table()
    def on_select_changed(self, event):
        if event.select.id == "filter": self.refresh_table()
    def on_button_pressed(self, event):
        getattr(self, "action_"+event.button.id)()

    def action_search(self): self.query_one("#search", Input).focus()
    def action_scan(self): self.start("scan")
    def action_deep(self): self.start("deep")
    def action_preview(self): self.start("preview")
    def action_export(self): self.start("export")
    def action_batch(self): self.start("batch")

    def start(self, operation):
        if self.busy: return
        selected = self.selected
        if operation in ("preview", "export") and (not selected or selected.status not in ("ready", "covered")): return
        output_value = self.query_one("#output", Input).value.strip()
        if not output_value:
            self.query_one("#status", Static).update("请填写导出文件夹")
            return
        self.busy = True
        self.update_actions()
        self.query_one("#status", Static).update("处理中…")
        self.perform(operation, selected, Path(output_value).expanduser(), self.query_one("#format", Select).value)

    def progress(self, done, total, message):
        self.query_one("#progress", ProgressBar).update(total=max(1, total), progress=done)
        self.query_one("#status", Static).update(message)

    @work(thread=True, exclusive=True)
    def perform(self, operation, selected, output, fmt):
        try:
            if operation in ("scan", "deep"):
                items = core.scan(self.cache, operation == "deep", lambda *a: self.call_from_thread(self.progress, *a), self.timeout)
                self.call_from_thread(self.finish_scan, items)
            elif operation == "preview":
                target = core.preview(selected, self.preview_dir.name, self.timeout)
                self.call_from_thread(self.progress, 1, 1, f"已用默认播放器打开：{target.name}")
            else:
                candidates = [selected] if operation == "export" else [i for i in self.items if i.status in ("ready", "covered")]
                failed = []
                for n, item in enumerate(candidates):
                    self.call_from_thread(self.progress, n, len(candidates), f"验证并导出 {n+1}/{len(candidates)} · {item.path.name}")
                    try: core.export(item, output, fmt, self.timeout)
                    except (OSError, RuntimeError, ValueError) as e: failed.append(f"{item.path.name}: {e}")
                if failed:
                    output.mkdir(parents=True, exist_ok=True)
                    (output/"export-errors.txt").write_text("\n\n".join(failed), encoding="utf-8")
                message = f"已导出 {len(candidates)-len(failed)} 项 · 失败 {len(failed)} 项 → {output}"
                self.call_from_thread(self.progress, len(candidates), len(candidates), message)
        except Exception as e:
            self.call_from_thread(self.progress, 0, 1, str(e))
        finally: self.call_from_thread(self.finish_work)

    def finish_scan(self, items):
        self.items = items
        self.refresh_table()

    def finish_work(self):
        self.busy = False
        self.update_actions()

    def on_unmount(self): self.preview_dir.cleanup()
