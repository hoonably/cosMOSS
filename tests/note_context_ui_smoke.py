"""Styled file menu, real pointer/keyboard interactions and stale-path dedup."""
import asyncio
import copy
import sys
from pathlib import Path
from playwright.async_api import async_playwright


async def main():
    base,engine,png=sys.argv[1:4]
    stamp='2026-10-23T01:00:00Z'
    path='/Users/tester/Downloads/회의.txt'
    old=dict(id='a'*32,title='이전 기록',status='done',text_path=path,created_at=stamp,output_format='txt')
    new={**old,'id':'b'*32,'title':'현재 기록','created_at':'2026-10-23T02:00:00Z','text_hash':'abc'}
    file=dict(id='txt_'+'c'*32,title='회의',status='done',text_path=path,created_at=stamp,source_type='text_file',
              output_format='txt',managed_note_id=new['id'],file_version='1792700000123456789:123',file_size=123)
    files=[file];calls=[];errors=[]
    async def action(route):
        body=route.request.post_data_json;calls.append((route.request.url.rsplit('/',1)[-1],body))
        if calls[-1][0]=='trash':
            files[:]=[f for f in files if f['text_path']!=path]
            return await route.fulfill(status=204)
        format=body['output_format']
        converted={**file,'id':'txt_'+'d'*32,'text_path':path.replace('.txt','.'+format),'title':'변환본',
                   'output_format':format,'managed_note_id':None}
        files.append(converted)
        await route.fulfill(status=201,json=converted)
    async with async_playwright() as pw:
        options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser=await getattr(pw,engine).launch(headless=True,**options)
        page=await browser.new_page(viewport={'width':1440,'height':950},device_scale_factor=2)
        page.set_default_timeout(10000);page.on('pageerror',lambda e:errors.append(str(e)))
        await page.route('**/api/devices',lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes',lambda r:r.fulfill(json=[new,old]))
        await page.route('**/api/text-library**',lambda r:r.fulfill(json=files))
        await page.route('**/api/summary/providers',lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes/*/convert',action)
        await page.route('**/api/notes/*/trash',action)
        await page.goto(base)
        await page.locator('.note-item').wait_for()
        assert await page.locator('.note-item').count()==1
        assert '현재 기록' in await page.locator('.note-item').inner_text()
        await page.evaluate('document.addEventListener("contextmenu",e=>window.__contextPrevented=e.defaultPrevented)')
        await page.locator('.note-item').click(button='right')
        assert await page.evaluate('window.__contextPrevented')
        assert await page.locator('#note-dialog').is_hidden()
        assert await page.locator('#note-context-menu [role=menuitem]:visible').count()==3
        assert await page.locator('[data-convert-format=txt]').is_hidden()
        await page.screenshot(path=png)
        await page.keyboard.press('Escape')
        assert await page.locator('#note-context-menu').is_hidden()
        await page.locator('.note-select').press('Shift+F10')
        await page.locator('[data-convert-format=srt]').click()
        await page.wait_for_function('document.querySelectorAll(".note-item").length===2')
        assert calls[0][1]['output_format']=='srt'
        assert calls[0][1]['file_version']==file['file_version']
        # Each of the three source types offers exactly the other two targets.
        for format in ('txt','srt','vtt'):
            new['output_format']=format
            await page.evaluate('loadNotes()')
            row=page.locator(f'.note-item[data-id="{new["id"]}"]')
            await row.click(button='right')
            targets=await page.locator('#note-context-menu [data-convert-format]:visible').evaluate_all('(els)=>els.map(e=>e.dataset.convertFormat)')
            assert set(targets)=={'txt','srt','vtt'}-{format}
            await page.keyboard.press('Escape')
        new['output_format']='txt'
        await page.evaluate('loadNotes()')
        await page.locator(f'.note-item[data-id="{new["id"]}"]').click(button='right')
        await page.locator('#trash-note-file').click()
        await page.wait_for_function('document.querySelectorAll(".note-item").length===1')
        await page.evaluate('loadNotes()')
        assert await page.locator(f'.note-item[data-id="{new["id"]}"]').count()==0
        assert await page.locator(f'.note-item[data-id="{old["id"]}"]').count()==0
        assert calls[-1][0]=='trash'
        assert not errors,errors
        await browser.close()
    print(engine,'PASS: duplicate path hidden, custom menu, keyboard, six choices, conversion, recoverable deletion and no ghost record')


asyncio.run(main())
