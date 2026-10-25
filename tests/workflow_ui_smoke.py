"""Exercise the three stages with synthetic notes and no external AI requests."""
import asyncio
import copy
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from ui_helpers import choose_option

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.summaries import DEFAULT_QUERY
from src.summary_preview import preview_document


async def main():
    base = sys.argv[1]
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    engine = sys.argv[3] if len(sys.argv) > 3 else 'chromium'
    note = {
        'id': 'e' * 32, 'title': 'cosMOSS · 다음 버전 회의', 'status': 'done', 'created_at': '2026-10-19T02:30:00Z',
        'original_filename': 'product-meeting.m4a', 'device_name': '이 Mac', 'chunk_count': 1, 'duration': 842,
        'processing_seconds': 108, 'preprocessing_seconds': 1, 'segments': [
            {'id': 0, 'start': 0, 'end': 34, 'speaker': 'S01', 'text': '오늘은 녹음을 정리하는 흐름을 같이 살펴볼게요. 장치를 고르고, 기록을 확인한 다음 요약까지 이어지면 좋겠어요.'},
            {'id': 1, 'start': 34, 'end': 62, 'speaker': 'S02', 'text': '화자 이름을 먼저 지정할 수 있으면 좋겠어요. 제가 말한 부분을 한 번에 확인하고 이름을 붙이는 거죠.'},
            {'id': 2, 'start': 62, 'end': 96, 'speaker': 'S01', 'text': '좋아요. 이번 주 금요일까지 화자 설정을 마무리하고, 다음 주에 사용해 보면서 다시 다듬어요.'},
            {'id': 3, 'start': 96, 'end': 120, 'speaker': 'S02', 'text': '요약은 제가 확인할게요. 결정한 내용과 누가 무엇을 해야 하는지가 잘 나오면 충분할 것 같아요.'},
        ], 'speaker_names': {}, 'raw_segments': [], 'text_path': None, 'text_hash': None,
    }
    note['raw_segments'] = copy.deepcopy(note['segments'])
    summary = {'phase': 'idle', 'default_prompt': DEFAULT_QUERY}
    calls = []
    polls = 0
    errors = []
    async with async_playwright() as pw:
        options = {'executable_path': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width': 1480, 'height': 950}, device_scale_factor=2)
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.add_init_script('''window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>({path:'/Users/tester/Downloads',name:'Downloads'})},revealTextFile:{postMessage:async value=>{window.__revealed=value.path;return true;}}}};''')
        await page.route('**/api/summary/providers', lambda r: r.fulfill(json=[
            {'id':'codex','name':'Codex','installed':True,'ready':True,'message':'로그인됨'},
            {'id':'claude','name':'Claude','installed':False,'ready':False,'message':'CLI를 설치해 주세요.','install':'npm install -g @anthropic-ai/claude-code','login':'claude auth login','url':'https://code.claude.com/docs/en/quickstart'},
        ]))
        await page.route('**/api/summary/default-prompt', lambda r:r.fulfill(json={'default_prompt':DEFAULT_QUERY}))
        await page.route('**/api/summary/models**', lambda r:r.fulfill(json={'provider':'codex','models':[{'id':'current','name':'GPT Current','efforts':['medium'],'default_effort':'medium'}],'selection':{'model':'current','effort':'medium'}}))
        await page.route('**/api/devices', lambda r: r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_key':'mlx-8bit','model':'moss-mtd','remote_port':8001,'setup':{'phase':'ready'}}]))
        await page.route('**/api/local-model', lambda r: r.fulfill(json={'phase':'ready','model_key':'mlx-8bit','cache_dir':'/Users/tester/.cache/huggingface/hub'}))
        await page.route('**/api/notes', lambda r: r.fulfill(json=[note]))
        async def notes(route):
            nonlocal summary, polls
            path = route.request.url.split('/api/notes/')[1].split('/', 1)
            action = path[1] if len(path) > 1 else ''
            method = route.request.method
            if action.startswith('summary/preview'):
                from urllib.parse import parse_qs, urlsplit
                kind = parse_qs(urlsplit(route.request.url).query).get('kind',['markdown'])[0]
                return await route.fulfill(json=preview_document(summary['result'],kind))
            if action == 'media': return await route.fulfill(status=404)
            if method == 'POST': calls.append((action, route.request.post_data_json))
            if action in ('text-file', 'speakers'):
                values = route.request.post_data_json
                if action == 'speakers': note['speaker_names'].update(values['speaker_names'])
                note['text_path'] = '/Users/tester/Downloads/cosMOSS-회의-eeeeeeee.txt'
                note['text_hash'] = 'hash-' + str(len(calls))
                return await route.fulfill(json=note)
            if action == 'summary/cancel':
                summary['phase'] = 'cancelled'
                return await route.fulfill(json=summary)
            if action == 'summary':
                if method == 'POST':
                    body=route.request.post_data_json
                    summary.update(phase='running', provider=body['provider'], additional_prompt=body['prompt'],
                        use_default_prompt=body['use_default_prompt'],
                        prompt='\n\n'.join(p for p in (DEFAULT_QUERY if body['use_default_prompt'] else '',body['prompt']) if p))
                    note['text_path'] = '/Users/tester/Downloads/cosMOSS-회의-eeeeeeee.txt'
                    polls = 0
                    return await route.fulfill(status=202, json=summary)
                if summary['phase'] == 'running':
                    polls += 1
                    if polls > 2:
                        summary.update(phase='done', result={'markdown_path':'/Users/tester/Downloads/cosMOSS-회의-eeeeeeee_요약.md','corrected_path':'/Users/tester/Downloads/cosMOSS-회의-eeeeeeee_보정.txt','corrected_text':'[01:02] 지민: 금요일까지 화자 설정을 마무리하겠습니다.','text':'# 핵심 내용\n녹음에서 노트 확인, 화자 지정, AI 요약으로 이어지는 흐름을 정리했습니다.\n\n결정 사항\n• 금요일까지 화자 설정 기능 완성 [01:02]\n• 다음 주 실제 사용 후 개선\n\n할 일\n• 지민: 화자 설정 마무리 · 금요일\n• 수빈: 요약 결과 확인', 'provider':'codex','source_hash':note['text_hash'],'completed_at':'2026-10-19T03:00:00Z'})
                return await route.fulfill(json=summary)
            if method == 'PATCH':
                note.update(route.request.post_data_json)
                note['text_hash'] = 'edited'
            return await route.fulfill(json=note)
        await page.route('**/api/notes/*', notes)
        await page.route('**/api/notes/*/**', notes)
        await page.route('**/api/text-library**', lambda route: route.fulfill(json=[{
            'id': 'txt_' + 'd' * 32, 'title': note['title'], 'status': 'done', 'source_type': 'text_file',
            'text_path': '/Users/tester/Downloads/cosMOSS-회의-eeeeeeee.txt', 'output_format': 'txt',
            'created_at': note['created_at'], 'updated_at': note['created_at'],
        }]))
        await page.goto(base)
        assert await page.locator('#use-default-summary-prompt').is_checked()
        assert await page.locator('#summary-prompt').input_value() == ''
        await page.locator('#show-default-summary-prompt').click()
        await page.wait_for_function('document.querySelector("#default-summary-prompt-content").value.includes("Markdown 문서 하나")')
        assert await page.locator('#default-summary-prompt-content').input_value() == DEFAULT_QUERY
        if output:
            await page.locator('#default-summary-prompt-dialog').screenshot(path=str(output.with_name(output.stem+'-default-prompt.png')))
        await page.keyboard.press('Escape')
        assert await page.locator('#default-summary-prompt-dialog').is_hidden()
        await page.locator('.note-item').wait_for()
        assert await page.locator('.note-item').count() == 1, 'Recording and result file appeared twice'
        assert await page.locator('.note-status').inner_text() == 'TXT'
        assert len(calls) == 1 and calls[0][0] == 'text-file', 'Legacy result was not automatically linked'
        calls.clear()
        await page.locator('.note-select').click()
        await page.wait_for_function('state.current && !state.openingNote')
        assert await page.locator('#note-dialog').is_hidden(), 'Selecting a note opened the editor'
        assert await page.locator('.note-select').get_attribute('aria-pressed') == 'true'
        await page.locator('#run-summary:not([disabled])').wait_for(state='attached')
        assert not calls, 'Selection saved or summarized without an explicit action'
        await page.locator('.note-edit').click()
        try:
            await page.locator('#editor').wait_for(state='visible', timeout=10000)
        except Exception as error:
            raise AssertionError({'toast': await page.locator('#toast').inner_text(), 'errors': errors,
                                  'current': await page.evaluate('state.current?.id')}) from error
        assert await page.locator('#note-dialog').is_visible()
        assert await page.locator('#transcript-views, #editing-hint, [data-transcript-view]').count() == 0
        assert await page.locator('#save-txt, #export-toggle').count() == 0
        assert await page.locator('#retry-result-save').is_hidden()
        assert await page.locator('.player-card').is_hidden()
        assert '.m4a' not in await page.locator('#note-meta').inner_text()
        assert await page.locator('#note-dialog').evaluate('el=>el.clientWidth>=1000')
        assert await page.locator('#welcome').is_visible(), 'Device stage disappeared'
        assert await page.locator('.summary-panel').is_visible()
        await page.locator('#edit-speakers').click()
        await page.locator('#speaker-dialog').wait_for(state='visible')
        assert await page.locator('.speaker-preview-row.highlighted').count() == 2
        assert await page.locator('.speaker-preview-row.highlighted p').first.evaluate('el=>getComputedStyle(el).color') == 'rgb(17, 146, 90)'
        assert await page.locator('#speaker-name').evaluate('el=>el.closest(".speaker-choice").dataset.selectSpeaker') == 'S01'
        assert await page.locator('#speaker-name-label').count() == 0
        await page.locator('#speaker-name').fill('지민')
        await page.locator('[data-select-speaker="S02"]').click()
        assert await page.evaluate('document.activeElement.id') == 'speaker-name'
        assert await page.locator('#speaker-name').evaluate('el=>el.closest(".speaker-choice").dataset.selectSpeaker') == 'S02'
        assert await page.locator('[data-preview-speaker="S01"].highlighted').count() == 0
        assert await page.locator('[data-preview-speaker="S02"].highlighted').count() == 2
        await page.locator('#speaker-name').fill('수빈')
        assert not calls, 'Draft names were saved before explicit Save'
        if output: await page.screenshot(path=str(output.with_name(output.stem + '-speakers.png')))
        await page.locator('#cancel-speakers').click()
        assert not calls
        await page.locator('#edit-speakers').click()
        assert await page.locator('#speaker-name').input_value() == ''
        await page.locator('#speaker-name').fill('지민')
        await page.locator('[data-select-speaker="S02"]').click()
        await page.locator('#speaker-name').fill('수빈')
        await page.locator('#save-speakers').click()
        await page.locator('#speaker-dialog').wait_for(state='hidden')
        assert note['speaker_names'] == {'S01':'지민','S02':'수빈'}
        assert calls[0][0] == 'speakers' and calls[0][1]['directory'] == '/Users/tester/Downloads'
        await page.locator('#reveal-txt').click()
        await page.wait_for_function('window.__revealed?.endsWith(".txt")')
        await page.locator('#close-note').click()
        await page.locator('#note-dialog').wait_for(state='hidden')
        await choose_option(page, '#summary-provider', 'claude')
        assert await page.locator('#run-summary').is_enabled()
        assert 'CLI 연결하기' in await page.locator('#run-summary').inner_text()
        assert 'claude auth login' in await page.locator('#cli-setup-command').inner_text()
        await choose_option(page, '#summary-provider', 'codex')
        prompt = page.locator('#summary-prompt')
        assert await prompt.input_value() == ''
        assert await page.locator('#use-default-summary-prompt').is_checked()
        async def fits():
            await page.wait_for_function('document.querySelector("#summary-prompt").scrollHeight <= document.querySelector("#summary-prompt").clientHeight + 1')
            assert await prompt.evaluate('(e)=>getComputedStyle(e).overflowY') == 'hidden'
        await fits()
        initial_height = await prompt.evaluate('(e)=>e.clientHeight')
        await prompt.fill(DEFAULT_QUERY + '\n추가 요청을 입력하고 모든 내용을 확인합니다.' * 25)
        await fits()
        assert await prompt.evaluate('(e)=>e.clientHeight') > initial_height
        assert await page.locator('.summary-scroll').evaluate('(e)=>e.scrollHeight>e.clientHeight')
        await prompt.fill('발표자별로 간결하게 정리해 주세요.')
        await fits()
        assert abs(await prompt.evaluate('(e)=>e.clientHeight') - initial_height) <= 2
        await page.set_viewport_size({'width':1100,'height':950})
        await fits()
        await prompt.fill(DEFAULT_QUERY)
        await fits()
        assert await prompt.evaluate('(e)=>e.clientHeight') > initial_height
        await page.set_viewport_size({'width':1480,'height':950})
        await fits()
        await prompt.fill('발표자별로 간결하게 정리해 주세요.')
        await page.locator('#use-default-summary-prompt').uncheck()
        assert await prompt.input_value() == '발표자별로 간결하게 정리해 주세요.'
        assert await page.locator('#summary-prompt-label').inner_text() == '요청 내용'
        await page.locator('.summary-scroll').evaluate('(e)=>e.scrollTop=0')
        if output:
            await page.screenshot(path=str(output.with_name(output.stem + '-prompt.png')), animations='disabled')
        assert 'CLI로 요약하기' in await page.locator('#run-summary').inner_text()
        await page.locator('#run-summary').click()
        assert calls[-1][1]['use_default_prompt'] is False
        assert calls[-1][1]['prompt'] == '발표자별로 간결하게 정리해 주세요.'
        await page.locator('#cancel-summary').wait_for(state='visible')
        await page.locator('#cancel-summary').click()
        await page.locator('#summary-status').filter(has_text='취소했습니다').wait_for()
        await page.locator('#use-default-summary-prompt').check()
        await page.locator('#run-summary').click()
        assert calls[-1][1]['use_default_prompt'] is True
        assert calls[-1][1]['prompt'] == '발표자별로 간결하게 정리해 주세요.'
        await page.locator('#summary-result-wrap').wait_for(state='visible')
        assert await page.locator('#summary-result').count() == 0
        await page.locator('#preview-summary').click()
        await page.locator('#summary-preview-content').filter(has_text='결정 사항').wait_for()
        await page.keyboard.press('Escape')
        await page.locator('#reveal-summary').click()
        await page.wait_for_function('window.__revealed?.endsWith("_요약.md")')
        # Legacy two-file results can still reveal their previous correction file.
        await page.locator('#reveal-corrected').click()
        await page.wait_for_function('window.__revealed?.endsWith("_보정.txt")')
        await page.locator('#preview-corrected').click()
        await page.locator('#summary-preview-content pre').filter(has_text='금요일까지').wait_for()
        await page.keyboard.press('Escape')
        assert calls[-1][1]['directory'] == '/Users/tester/Downloads'
        assert await page.locator('#review-path-note').text_content() == '기본 경로에 있는 txt, srt, vtt 파일을 불러옵니다.'
        assert await page.locator('#summary-path-note').text_content() == '보정 전사문을 포함한 요약 Markdown을 저장합니다.'
        if output:
            await page.locator('#toast').evaluate("el => el.className = 'toast'")
            await page.screenshot(path=str(output), animations='disabled')
        await page.locator('.note-edit').click()
        await page.locator('.segment-text').first.fill('본문을 수정했습니다.')
        await page.wait_for_function('document.querySelector("#summary-stale").classList.contains("hidden") === false')
        await page.locator('#close-note').click()
        await page.locator('#note-dialog').wait_for(state='hidden')
        assert not errors, errors
        for width in (1100, 1480):
            await page.set_viewport_size({'width':width,'height':900})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Horizontal overflow at {width}'
            assert await page.evaluate('document.documentElement.scrollHeight <= innerHeight'), f'Whole page scroll at {width}'
            areas = [await page.locator(selector).bounding_box() for selector in ('.workflow-device-column', '#stage-review', '.summary-panel')]
            assert areas[0]['x'] < areas[1]['x'] < areas[2]['x']
            assert await page.locator('.stage-path-note').evaluate_all('els=>els.every(el=>el.scrollWidth<=el.clientWidth && el.clientHeight<=15)')
            if output and width == 1100:
                await page.screenshot(path=str(output.with_name(output.stem + '-minimum.png')), animations='disabled')
        await browser.close()
    print(f'PASS {engine}: three stages, all-speaker highlighting, cancel/save, TXT/Finder, provider selection, summary run/cancel, stale detection, sizing')


if __name__ == '__main__': asyncio.run(main())
