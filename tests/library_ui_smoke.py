"""Review real temporary TXT files through the backend without running transcription/AI."""
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    base = sys.argv[1]
    engine = sys.argv[2] if len(sys.argv) > 2 else 'webkit'
    output = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    with tempfile.TemporaryDirectory(prefix='cosmoss-library-ui-') as temporary:
        folder = Path(temporary).resolve()
        other = folder / 'other'
        other.mkdir()
        structured = folder / '예전 회의.txt'
        original = '[00:00:01] S01: 기존 녹음에서 가져온 회의 내용입니다.\n[00:00:05] S02: 화자를 지정하고 요약할 수 있습니다.\n[00:00:12] S01: 다음 할 일을 정리합니다.\n'
        structured.write_text(original)
        plain = folder / '개인 메모.TXT'
        plain.write_text('형식 없는 메모\n<script>window.__unsafe=true</script>\n')
        (other / '다른 폴더.txt').write_text('다른 저장 경로의 파일입니다.')
        for index in range(12):
            (folder / f'이전 기록 {index+1:02d}.txt').write_text('기록 목록 확인용 TXT')
        summary = {'phase': 'idle', 'default_prompt': '요약해 주세요.'}
        summaries = []
        errors = []

        async def summary_route(route):
            if route.request.method == 'POST':
                summaries.append(route.request.post_data_json)
                summary.update(phase='done', result={'provider': 'codex', 'text': '기존 TXT 요약', 'source_hash': hashlib.sha256(structured.read_bytes()).hexdigest(), 'completed_at': '2026-10-19T12:00:00Z'})
            await route.fulfill(json=summary)

        async def delayed_edit(route):
            if route.request.method == 'PATCH':
                await asyncio.sleep(.35)
            await route.continue_()

        async with async_playwright() as pw:
            options = {'executable_path': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
            browser = await getattr(pw, engine).launch(headless=True, **options)
            page = await browser.new_page(viewport={'width': 1440, 'height': 950}, device_scale_factor=2)
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.add_init_script('''window.webkit={messageHandlers:{
                getSaveLocation:{postMessage:async()=>(LOCATION)},
                chooseSaveLocation:{postMessage:async()=>(NEXT)},
                revealTextFile:{postMessage:async value=>{window.__revealed=value.path;return true;}}
            }};'''.replace('LOCATION', json.dumps({'path': str(folder), 'name': folder.name})).replace('NEXT', json.dumps({'path': str(other), 'name': 'other'})))
            await page.route('**/api/summary/providers', lambda r: r.fulfill(json=[{'id': 'codex', 'name': 'Codex', 'ready': True, 'installed': True, 'message': '로그인됨'}]))
            await page.route('**/api/notes/txt_*', delayed_edit)
            await page.route('**/api/notes/*/summary', summary_route)
            await page.goto(base)
            await page.locator('.note-item').first.wait_for()
            cards = await page.locator('.note-item').evaluate_all('els=>els.map(el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width}})')
            assert len(cards) == 14, cards
            assert all(abs(card['x']-cards[0]['x'])<1 and abs(card['width']-cards[0]['width'])<1 for card in cards)
            assert all(cards[i]['y']<cards[i+1]['y'] for i in range(len(cards)-1)), 'Notes did not form a vertical list'
            assert await page.locator('#note-list').evaluate('el=>el.scrollHeight>el.clientHeight'), 'Long note list did not scroll'
            if output: await page.screenshot(path=str(output.with_name(output.stem+'-list.png')))
            await page.get_by_role('button', name='예전 회의 편집', exact=True).click()
            await page.locator('#text-document-editor').wait_for(state='visible')
            assert await page.locator('#note-dialog').is_visible()
            assert await page.evaluate('document.activeElement.id') == 'note-dialog', 'Opening the note focused a form control'
            await page.set_viewport_size({'width':1100,'height':720})
            assert await page.locator('#note-dialog').evaluate('el=>el.getBoundingClientRect().width<=innerWidth-60 && el.getBoundingClientRect().height<=innerHeight-60')
            await page.set_viewport_size({'width':1440,'height':950})
            box = await page.locator('#note-dialog').bounding_box()
            assert box['width'] >= 1000, box
            body_box = await page.locator('#text-document-body').bounding_box()
            assert body_box['width'] >= box['width']-64, 'Editor did not fill the wide note window'
            assert await page.locator('#text-document-body').input_value() == original
            assert structured.read_text() == original
            assert await page.locator('.player-card').is_hidden()
            assert await page.locator('#delete-note').is_hidden()
            assert await page.locator('#note-title').is_editable()
            assert await page.locator('#transcript-views, #editing-hint').count() == 0
            identity=await page.evaluate('state.current.id')
            await page.fill('#note-title','취소할 파일명')
            await page.keyboard.press('Escape')
            assert await page.locator('#note-dialog').is_visible()
            assert await page.locator('#note-title').input_value() == '예전 회의'
            await page.fill('#note-title','이름을 바꾼 회의')
            await page.keyboard.press('Enter')
            await page.wait_for_function('!state.pendingChanges && !state.savePromise && state.current.title==="이름을 바꾼 회의"')
            renamed=folder/'이름을 바꾼 회의.txt'
            assert renamed.read_text() == original and not structured.exists()
            assert await page.evaluate('state.current.id') == identity
            assert await page.get_by_role('button',name='이름을 바꾼 회의 편집',exact=True).count() == 1
            await page.fill('#note-title','이전 기록 01')
            await page.keyboard.press('Enter')
            await page.wait_for_function('document.querySelector("#save-state").textContent==="저장 실패"')
            assert (folder/'이전 기록 01.txt').read_text() == '기록 목록 확인용 TXT'
            assert renamed.read_text() == original
            await page.locator('#note-title').focus()
            await page.keyboard.press('Escape')
            edited=original+'임시로 덧붙인 내용\n'
            await page.fill('#text-document-body',edited)
            await page.fill('#note-title','예전 회의')
            await page.locator('#reveal-txt').click()  # Blur commits, Finder waits for rename.
            await page.wait_for_function('!state.pendingChanges && !state.savePromise && state.current.title==="예전 회의"')
            assert structured.read_text() == edited and not renamed.exists()
            await page.fill('#text-document-body',original)
            await page.wait_for_function('!state.pendingChanges && !state.savePromise')
            await page.click('#reveal-txt')
            await page.wait_for_function('Boolean(window.__revealed)')
            assert await page.evaluate('window.__revealed') == str(structured)
            await page.click('#edit-speakers')
            await page.locator('[data-select-speaker=S02]').click()
            assert await page.locator('.speaker-preview-row.highlighted').count() == 1
            await page.fill('#speaker-name', '지민')
            await page.click('#save-speakers')
            await page.locator('#speaker-dialog').wait_for(state='hidden')
            assert structured.read_text() == original.replace('S02:', '지민:')
            if output: await page.screenshot(path=str(output))
            await page.fill('#text-document-body', '첫 번째 편집\n')
            await page.wait_for_function('Boolean(state.savePromise)')
            await page.fill('#text-document-body', '연속 편집의 마지막 내용\n')
            await page.wait_for_function('!state.pendingChanges && !state.savePromise')
            assert structured.read_text() == '연속 편집의 마지막 내용\n'
            await page.fill('#text-document-body', '닫으면서 저장한 내용\n')
            await page.click('#close-note')
            await page.locator('#note-dialog').wait_for(state='hidden')
            assert structured.read_text() == '닫으면서 저장한 내용\n'
            before = structured.read_bytes()
            assert await page.evaluate('state.current.title') == '예전 회의'
            await page.click('#run-summary')
            await page.get_by_text('기존 TXT 요약', exact=True).wait_for()
            assert summaries and structured.read_bytes() == before
            await page.get_by_role('button', name='개인 메모 편집', exact=True).click()
            await page.wait_for_function('state.current?.title==="개인 메모"')
            assert await page.locator('#note-title').input_value() == '개인 메모'
            assert not await page.evaluate('Boolean(state.pendingChanges || state.savePromise)'), 'Opening a different file queued a rename'
            assert await page.locator('#edit-speakers').is_disabled()
            assert await page.evaluate('window.__unsafe') is None
            plain.write_text('Finder에서 수정한 최신 내용')
            await page.evaluate('loadNotes()')
            await page.wait_for_function("document.querySelector('#text-document-body').value==='Finder에서 수정한 최신 내용'")
            plain.write_text('Finder 버튼 직전에 바뀐 내용')
            await page.click('#reveal-txt')
            await page.wait_for_function("document.querySelector('#text-document-body').value==='Finder 버튼 직전에 바뀐 내용'")
            added = folder / '새로 넣은 파일.txt'
            added.write_text('자동 갱신')
            await page.get_by_role('button', name='새로 넣은 파일', exact=False).first.wait_for(timeout=12000)
            added.unlink()
            await page.get_by_role('button', name='새로 넣은 파일', exact=False).first.wait_for(state='detached', timeout=12000)
            await page.keyboard.press('Escape')
            await page.locator('#note-dialog').wait_for(state='hidden')
            await page.click('#save-location')
            await page.get_by_role('button', name='다른 폴더', exact=False).first.wait_for()
            assert await page.get_by_role('button', name='예전 회의', exact=False).count() == 0
            await page.get_by_role('button', name='다른 폴더 편집', exact=True).click()
            await page.wait_for_function("document.querySelector('#text-document-body').value==='다른 저장 경로의 파일입니다.'")
            await page.locator('#text-document-body').focus()
            (other / '다른 폴더.txt').write_text('외부 앱에서 바꾼 내용')
            await page.fill('#text-document-body', '오래된 화면에서 작성한 내용')
            await page.wait_for_function("document.querySelector('#text-document-save-state').textContent==='저장 실패'")
            assert (other / '다른 폴더.txt').read_text() == '외부 앱에서 바꾼 내용'
            await page.click('#close-note')
            await page.locator('#note-close-error').wait_for(state='visible')
            assert await page.locator('#note-dialog').is_visible(), 'Failed save discarded the editor'
            assert await page.locator('#text-document-body').input_value() == '오래된 화면에서 작성한 내용'
            assert not errors, errors
            await browser.close()
    print(f'PASS {engine}: rename/Enter/blur/Escape/collision/identity, single editor, TXT bytes, speakers, edits, Finder, summary, live refresh and conflicts')


if __name__ == '__main__':
    asyncio.run(main())
