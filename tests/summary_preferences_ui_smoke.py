"""Real preference persistence, editable prompt dialog and STEP 02 Trash controls."""
import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.summaries import markdown_document
from src.summary_preview import preview_document


async def main():
    base, engine, screenshot = sys.argv[1:4]
    with tempfile.TemporaryDirectory(prefix='cosmoss-summary-preferences-') as temporary:
        folder = Path(temporary).resolve()
        transcript = folder / '제품 개발 회의.txt'; transcript.write_text('[00:00:01] S01: 다음 주에 기능을 확인하겠습니다.\n')
        disposable = folder / '지울 메모.txt'; disposable.write_text('휴지통 버튼 확인용')
        trash = folder / 'Trash'; trash.mkdir()
        before = transcript.read_bytes()
        summary = {'phase':'idle'}
        requests, deletions, errors = [], [], []
        async with async_playwright() as pw:
            options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
            browser = await getattr(pw, engine).launch(headless=True, **options)
            page = await browser.new_page(viewport={'width':1440,'height':950}, device_scale_factor=2)
            page.set_default_timeout(12000)
            page.on('pageerror', lambda error:errors.append(str(error)))
            import json
            await page.add_init_script('''window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>(LOCATION)}}};
              Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.__copied=text;}}});'''
                .replace('LOCATION', json.dumps({'path':str(folder),'name':folder.name})))
            await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[{'id':'codex','name':'Codex','ready':True,'installed':True,'message':'로그인됨'}]))
            await page.route('**/api/summary/models**', lambda r:r.fulfill(json={'provider':'codex','models':[{'id':'current','name':'GPT Current','efforts':['medium'],'default_effort':'medium'}],'selection':{'model':'current','effort':'medium'}}))
            await page.route('**/api/devices', lambda r:r.fulfill(json=[]))

            async def summarize(route):
                if route.request.method == 'POST':
                    body = route.request.post_data_json; requests.append(body)
                    preferences = await (await page.request.get(base+'/api/summary/default-prompt')).json()
                    corrected, text = '[00:00:01] S01: 다음 주에 기능을 확인하겠습니다.', '# 제품 개발 회의\n\n## 기능 점검\n\n- 다음 주 기능 확인'
                    summary.update(phase='done',provider='codex',use_default_prompt=body['use_default_prompt'],additional_prompt=body['prompt'],
                        prompt=(preferences['default_prompt']+'\n\n' if body['use_default_prompt'] else '')+body['prompt'],
                        default_prompt=preferences['default_prompt'],result={'provider':'codex','text':text,'corrected_text':corrected,
                            'document_markdown':markdown_document(corrected,text),'source_hash':hashlib.sha256(before).hexdigest(),
                            'completed_at':'2026-10-23T06:00:00Z','markdown_path':str(folder/'제품 개발 회의_요약.md')})
                await route.fulfill(status=202 if route.request.method == 'POST' else 200,json=summary)
            await page.route('**/api/notes/*/summary', summarize)
            await page.route('**/api/notes/*/summary/preview?*', lambda r:r.fulfill(json=preview_document(summary['result'])))

            async def discard(route):
                identity = route.request.url.split('/api/notes/')[1].split('/')[0]
                note = await (await page.request.get(base+'/api/notes/'+identity)).json()
                assert route.request.post_data_json.get('file_version') or route.request.post_data_json.get('text_hash')
                source = Path(note['text_path']); source.rename(trash/source.name)
                deletions.append(identity)
                await route.fulfill(status=204)
            await page.route('**/api/notes/*/trash', discard)
            await page.goto(base)
            await page.locator('.note-trash').first.wait_for()
            assert await page.locator('.note-trash').count() == 2, {'notes':await page.evaluate('state.notes.map(n=>({id:n.id,path:n.text_path}))'),'errors':errors}
            await page.get_by_role('button',name='지울 메모 휴지통으로 이동',exact=True).click()
            await page.wait_for_function('document.querySelectorAll(".note-item").length===1')
            assert not disposable.exists() and (trash/disposable.name).exists()
            assert await page.locator('#note-dialog').is_hidden()
            assert await page.evaluate('state.current') is None

            async def open_prompt():
                await page.locator('#show-default-summary-prompt').click()
                await page.locator('#default-summary-prompt-content').wait_for(state='visible')
                await page.wait_for_function('!state.summaryPromptLoading')
                return page.locator('#default-summary-prompt-content')

            editor = await open_prompt()
            recommended = await editor.input_value()
            assert 'Markdown 문서 하나' in recommended and '<details>' in recommended
            custom = recommended + '\n\n사용자 기준: 전문용어를 정확하게 표기하세요.'
            await editor.fill(custom + '\n부가 기준을 검토하세요.'*30)
            assert await editor.evaluate('e=>e.scrollHeight<=e.clientHeight+2')
            assert await page.locator('.summary-default-body').evaluate('e=>e.scrollHeight>e.clientHeight')
            assert await page.locator('#save-default-summary-prompt').evaluate('e=>{const r=e.getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight;}')
            await page.locator('#default-summary-prompt-dialog').screenshot(path=screenshot)
            await editor.fill(custom)
            await page.locator('#save-default-summary-prompt').click()
            await page.locator('#default-summary-prompt-dialog').wait_for(state='hidden')
            assert (await (await page.request.get(base+'/api/summary/default-prompt')).json())['default_prompt'] == custom
            await page.reload()
            await page.locator('#summary-prompt-badge').filter(has_text='사용자 설정').wait_for()
            editor = await open_prompt()
            assert await editor.input_value() == custom
            await editor.fill('저장하지 않은 초안')
            await page.locator('#cancel-default-summary-prompt').click()
            assert (await (await page.request.get(base+'/api/summary/default-prompt')).json())['default_prompt'] == custom

            await page.get_by_role('button',name='제품 개발 회의 선택',exact=True).click()
            await page.wait_for_function('Boolean(state.current) && !state.openingNote')
            await page.locator('#summary-prompt').fill('할 일을 마지막에 모아 주세요.')
            await page.locator('#run-summary').click()
            await page.locator('#summary-result-wrap').wait_for(state='visible')
            assert requests[-1]['use_default_prompt'] is True
            assert requests[-1]['prompt'] == '할 일을 마지막에 모아 주세요.'
            assert summary['prompt'] == custom+'\n\n할 일을 마지막에 모아 주세요.'
            assert await page.locator('#reveal-corrected').is_hidden()
            assert await page.locator('#copy-summary').count() == 0
            assert await page.locator('#summary-result').count() == 0
            await page.locator('#preview-summary').click()
            await page.locator('#summary-preview-content h1').filter(has_text='제품 개발 회의').wait_for()
            assert not await page.locator('#summary-preview-content details').evaluate('e=>e.open')
            await page.keyboard.press('Escape')
            await page.locator('#summary-preview-dialog').wait_for(state='hidden')
            assert summary['result']['document_markdown'].startswith('# 제품 개발 회의\n')
            assert summary['result']['document_markdown'].endswith('</details>\n')
            assert transcript.read_bytes() == before

            editor = await open_prompt()
            await page.locator('#reset-default-summary-prompt').click()
            assert await editor.input_value() == recommended
            assert (await (await page.request.get(base+'/api/summary/default-prompt')).json())['default_prompt'] == custom
            await page.locator('#cancel-default-summary-prompt').click()
            editor = await open_prompt()
            assert await editor.input_value() == custom
            async def fail_save(route):
                if route.request.method == 'PUT': return await route.fulfill(status=503,json={'detail':'테스트 저장 실패'})
                await route.continue_()
            await page.route('**/api/summary/default-prompt', fail_save)
            await editor.fill('저장 실패 테스트')
            await page.locator('#save-default-summary-prompt').click()
            await page.locator('#default-summary-prompt-status').filter(has_text='테스트 저장 실패').wait_for()
            assert await page.locator('#default-summary-prompt-dialog').is_visible()
            assert (await (await page.request.get(base+'/api/summary/default-prompt')).json())['default_prompt'] == custom
            await page.unroute('**/api/summary/default-prompt', fail_save)
            await page.locator('#reset-default-summary-prompt').click()
            await page.locator('#save-default-summary-prompt').click()
            await page.locator('#default-summary-prompt-dialog').wait_for(state='hidden')
            assert not (await (await page.request.get(base+'/api/summary/default-prompt')).json())['customized']
            for width in (1100,1440):
                await page.set_viewport_size({'width':width,'height':900})
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert await page.locator('.note-trash').is_visible()
            await page.get_by_role('button',name='제품 개발 회의 휴지통으로 이동',exact=True).click()
            await page.wait_for_function('document.querySelectorAll(".note-item").length===0')
            assert await page.locator('#run-summary').is_disabled()
            assert await page.locator('#summary-result-wrap').is_hidden()
            assert await page.locator('#note-dialog').is_hidden()
            assert len(deletions) == 2
            assert (trash/transcript.name).read_bytes() == before
            assert not errors, errors
            await browser.close()
    print(engine,'PASS: STEP 02 Trash, persisted editable default, cancel/reset/save failure, appended requests, one Markdown preview and responsive layout')


asyncio.run(main())
