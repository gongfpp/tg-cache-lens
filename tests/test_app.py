import asyncio
from textual.widgets import Button, DataTable, Input, Select, Static
from tg_cache_lens.app import CacheLens

async def done(app):
    await asyncio.wait_for(app.workers.wait_for_complete(),60)
    await asyncio.sleep(.1)

async def test_click_scan_preview_export_filter(cache,tmp_path,monkeypatch):
    opened=[]
    from tg_cache_lens import core
    def preview(item,output,timeout):
        target=core.export(item,output,'mp4',timeout)
        opened.append(target)
        return target
    monkeypatch.setattr(core,'preview',preview)
    app=CacheLens(cache,tmp_path/'out',autostart=False)
    async with app.run_test(size=(132,40)) as pilot:
        await pilot.click('#deep'); await done(app); await pilot.pause()
        assert len(app.items)==3 and app.items[0].status=='ready'
        assert not app.query_one('#preview',Button).disabled
        await pilot.click('#preview'); await done(app)
        assert len(opened)==1 and opened[0].exists()
        await pilot.click('#export'); await done(app)
        assert len(list((tmp_path/'out').glob('*.mp4')))==1
        assert await pilot.click('#batch'); await done(app)
        assert len(list((tmp_path/'out').glob('*.mp4')))==2
        app.query_one('#filter',Select).value='partial'; await pilot.pause()
        assert len(app.visible_items)==1
        assert app.query_one('#preview',Button).disabled
        app.query_one('#search',Input).value='no-match'; await pilot.pause()
        assert app.query_one(DataTable).row_count==0

async def test_empty_and_small_terminal(tmp_path):
    app=CacheLens(tmp_path,tmp_path/'out',autostart=False)
    async with app.run_test(size=(85,36)) as pilot:
        await pilot.click('#scan'); await done(app)
        assert app.query_one('#export',Button).disabled
        assert 'compact' in app.screen.classes
