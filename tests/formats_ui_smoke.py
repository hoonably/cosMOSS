"""Review/merge/export SRT and VTT, then summarize through isolated fake CLIs.

Run against the QA server with formats-fake-bin first on PATH.
"""
import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright
from ui_helpers import choose_option


async def main():
    base = sys.argv[1]
    engine = sys.argv[2] if len(sys.argv)>2 else 'webkit'
    output = Path(sys.argv[3]) if len(sys.argv)>3 else None
    assert ':18761' in base, 'Use the isolated QA server with fake CLIs'
    errors = []
    with tempfile.TemporaryDirectory(prefix='cosmoss-formats-ui-') as temporary:
        folder = Path(temporary).resolve()
        files = {
            'srt':('1\n00:00:01,250 --> 00:00:03,500\n지민: 첫 발언\n본문에서 수빈: 표기는 유지합니다.\n\n2\n00:00:04,000 --> 00:00:06,125\n수빈: 다음 발언\n'),
            'vtt':('WEBVTT\n\nNOTE 이 메모는 보존합니다.\n\ncue-1\n00:01.250 --> 00:03.500 align:start\n<v 지민><i>첫 발언</i>\n본문에서 수빈: 표기는 유지합니다.\n\ncue-2\n00:04.000 --> 00:06.125\n<v 수빈>다음 발언\n'),
        }
        for fmt,text in files.items():
            (folder/f'기존 자막 {fmt.upper()}.{fmt}').write_text(text)
        (folder/'기존 텍스트.txt').write_text('기존 TXT도 계속 표시됩니다.')
        async with async_playwright() as pw:
            options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
            browser=await getattr(pw,engine).launch(headless=True,**options)
            page=await browser.new_page(viewport={'width':1440,'height':950},device_scale_factor=2,accept_downloads=True)
            page.set_default_timeout(12000)
            page.on('pageerror',lambda error:errors.append(str(error)))
            import json
            await page.add_init_script('window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>('+json.dumps({'path':str(folder),'name':folder.name})+')},revealTextFile:{postMessage:async value=>{window.__revealed=value.path;return true;}}}};')
            await page.route('**/api/devices',lambda r:r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_key':'mlx-8bit','setup':{'phase':'idle'}}]))
            await page.route('**/api/local-model',lambda r:r.fulfill(json={'phase':'idle'}))
            await page.goto(base)
            await page.wait_for_function("document.querySelector('#note-count').textContent==='3'")
            assert await page.locator('#device-select, #chunk-minutes, #manage-upload-devices').count()==0
            if output:await page.screenshot(path=str(output.with_name(output.stem+'-list.png')))
            for fmt in ('srt','vtt'):
                file=folder/f'기존 자막 {fmt.upper()}.{fmt}'
                original=file.read_bytes()
                await page.get_by_role('button',name='기존 자막 '+fmt.upper()+' 편집',exact=True).click()
                await page.locator('#note-dialog').wait_for(state='visible')
                assert await page.locator('#text-document-format').inner_text()==fmt.upper()
                assert await page.locator('#save-txt, #export-toggle').count()==0
                assert await page.locator('#retry-result-save').is_hidden()
                assert await page.locator('#text-document-body').input_value()==files[fmt]
                await page.click('#reveal-txt')
                await page.wait_for_function('(path)=>window.__revealed===path',arg=str(file))
                assert await page.evaluate('window.__revealed')==str(file)
                assert file.read_bytes()==original
                edited=files[fmt].replace('첫 발언','수정한 첫 발언')
                await page.fill('#text-document-body',edited)
                await page.wait_for_function('!state.pendingChanges && !state.savePromise')
                assert file.read_text()==edited
                await page.click('#edit-speakers')
                assert await page.locator('.speaker-choice').count()==2
                await page.locator('[data-select-speaker="S02"]').drag_to(page.locator('[data-select-speaker="S01"]'))
                await page.locator('#speaker-merge-dialog').wait_for(state='visible')
                await page.click('#confirm-speaker-merge')
                await page.fill('#speaker-name','민수')
                assert file.read_text()==edited
                await page.click('#save-speakers')
                await page.locator('#speaker-dialog').wait_for(state='hidden')
                expected=edited.replace('\n지민: ','\n민수: ').replace('\n수빈: ','\n민수: ') if fmt=='srt' else edited.replace('<v 지민>','<v 민수>').replace('<v 수빈>','<v 민수>')
                assert file.read_text()==expected
                assert await page.locator('#text-document-body').input_value()==expected
                if output:await page.screenshot(path=str(output.with_name(output.stem+'-'+fmt+'.png')))
                await page.click('#close-note')
                await page.locator('#note-dialog').wait_for(state='hidden')
                assert await page.locator('#summary-source').inner_text()==file.name
                provider='codex' if fmt=='srt' else 'claude'
                await choose_option(page,'#summary-provider',provider)
                await page.locator('#run-summary').wait_for(state='visible')
                await page.wait_for_function("!document.querySelector('#run-summary').disabled")
                await page.click('#run-summary')
                await page.wait_for_function("state.summaryStates[state.current.id]?.phase==='done'",timeout=15000)
                result=await page.evaluate('state.summaryStates[state.current.id]')
                assert not result['stale']
                assert result['result']['provider']==provider
                markdown=Path(result['result']['markdown_path'])
                assert markdown.parent==folder
                assert markdown.read_text().startswith('# 테스트 요약')
                assert result['result']['source_hash']==hashlib.sha256(file.read_bytes()).hexdigest()
                assert file.read_text()==expected
                await page.click('#reveal-summary')
                await page.wait_for_function('(path)=>window.__revealed===path',arg=str(markdown))
                assert await page.evaluate('window.__revealed')==str(markdown)
                if output:await page.screenshot(path=str(output.with_name(output.stem+'-'+fmt+'-summary.png')))
            assert not errors,errors
            await browser.close()
    print(f'PASS {engine}: TXT/SRT/VTT library, original format editor/Finder/export, real drag merge, timing/body preservation, Codex and Claude fake CLI Markdown saves and correct source hashes')


if __name__=='__main__':asyncio.run(main())
