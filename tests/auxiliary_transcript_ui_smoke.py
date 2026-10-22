"""The auxiliary transcript opens on request and never replaces the note body."""
import asyncio
from pathlib import Path
import sys
import tempfile

from playwright.async_api import async_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.store import NoteStore


async def main():
    base,png,engine=sys.argv[1:4]
    with tempfile.TemporaryDirectory() as folder:
        store=NoteStore(Path(folder)/'notes.db')
        note=store.create(dict(id='a'*32,title='보조 대본 비교',status='done',duration=60,
            original_filename='a.wav',source_path=folder+'/a.wav'))
        note=store.update(note['id'],
            segments=[dict(id=0,start=0,end=10,speaker='S01',text='MOSS 본문 유지')],
            raw_segments=[dict(id=0,start=0,end=10,speaker='S01',text='MOSS 본문 유지')],
            speaker_names={'S01':'S01'},pipeline_result=dict(pipeline='moss-qwen-sequential',
            has_auxiliary_transcript=True,batch=1,chunk_count=1,inference_seconds=5))
    errors=[];requests=[]
    async with async_playwright() as pw:
        options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser=await getattr(pw,engine).launch(headless=True,**options)
        page=await browser.new_page(viewport=dict(width=1440,height=1000))
        page.on('pageerror',lambda error:errors.append(str(error)))
        async def auxiliary(route):
            requests.append(route.request.url)
            await route.fulfill(json=dict(text='Qwen 별도 대본',aligned=False))
        await page.route('**/api/notes/*/auxiliary-transcript',auxiliary)
        await page.route('**/api/devices',lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes',lambda r:r.fulfill(json=[note]))
        await page.route('**/api/notes/'+note['id'],lambda r:r.fulfill(json=note))
        await page.goto(base)
        await page.evaluate('id=>openNote(id)',note['id'])
        assert not requests
        await page.locator('#auxiliary-transcript summary').click()
        await page.wait_for_function("$('#auxiliary-transcript-text').textContent==='Qwen 별도 대본'")
        assert len(requests)==1
        assert await page.locator('.segment-text').first.input_value()=='MOSS 본문 유지'
        assert 'download=true' in await page.locator('#download-auxiliary-transcript').get_attribute('href')
        await page.screenshot(path=png,full_page=True)
        await page.evaluate("()=>{state.current={...state.current,id:'file',source_type:'text_file',text_body:'기존 TXT'};renderWorkspace();}")
        assert await page.locator('#auxiliary-transcript').is_hidden()
        assert not errors,errors
        await browser.close()
    print(engine,'PASS: explicit auxiliary open, separate body, TXT link, hidden on file switch')


asyncio.run(main())
