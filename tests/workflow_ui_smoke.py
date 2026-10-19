"""Exercise the three stages with synthetic notes and no external AI requests."""
import asyncio
import copy
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from ui_helpers import choose_option


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
    summary = {'phase': 'idle', 'default_prompt': '한국어로 핵심 내용, 결정 사항, 담당자별 할 일을 정리해 주세요. 중요한 내용에는 타임스탬프를 붙여 주세요.'}
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
        await page.route('**/api/devices', lambda r: r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_key':'mlx-8bit','model':'moss-mtd','remote_port':8001,'setup':{'phase':'ready'}}]))
        await page.route('**/api/local-model', lambda r: r.fulfill(json={'phase':'ready','model_key':'mlx-8bit','cache_dir':'/Users/tester/.cache/huggingface/hub'}))
        await page.route('**/api/notes', lambda r: r.fulfill(json=[note]))
        async def notes(route):
            nonlocal summary, polls
            path = route.request.url.split('/api/notes/')[1].split('/', 1)
            action = path[1] if len(path) > 1 else ''
            method = route.request.method
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
                    summary.update(phase='running', provider=route.request.post_data_json['provider'], prompt=route.request.post_data_json['prompt'])
                    note['text_path'] = '/Users/tester/Downloads/cosMOSS-회의-eeeeeeee.txt'
                    polls = 0
                    return await route.fulfill(status=202, json=summary)
                if summary['phase'] == 'running':
                    polls += 1
                    if polls > 2:
                        summary.update(phase='done', result={'markdown_path':'/Users/tester/Downloads/cosMOSS-회의-eeeeeeee-요약.md','text':'# 핵심 내용\n녹음에서 노트 확인, 화자 지정, AI 요약으로 이어지는 흐름을 정리했습니다.\n\n결정 사항\n• 금요일까지 화자 설정 기능 완성 [01:02]\n• 다음 주 실제 사용 후 개선\n\n할 일\n• 지민: 화자 설정 마무리 · 금요일\n• 수빈: 요약 결과 확인 · 기한 미정', 'provider':'codex','source_hash':note['text_hash'],'completed_at':'2026-10-19T03:00:00Z'})
                return await route.fulfill(json=summary)
            if method == 'PATCH':
                note.update(route.request.post_data_json)
                note['text_hash'] = 'edited'
            return await route.fulfill(json=note)
        await page.route('**/api/notes/*', notes)
        await page.route('**/api/notes/*/**', notes)
        await page.route('**/api/text-library**', lambda route: route.fulfill(json=[]))
        await page.goto(base)
        await page.locator('.note-item').click()
        await page.locator('#editor').wait_for(state='visible')
        assert await page.locator('#note-dialog').is_visible()
        assert await page.locator('#note-dialog').evaluate('el=>el.clientWidth>=1000')
        assert await page.locator('#welcome').is_visible(), 'Device stage disappeared'
        assert await page.locator('.summary-panel').is_visible()
        await page.locator('#edit-speakers').click()
        await page.locator('#speaker-dialog').wait_for(state='visible')
        assert await page.locator('.speaker-preview-row.highlighted').count() == 2
        assert await page.locator('.speaker-preview-row.highlighted p').first.evaluate('el=>getComputedStyle(el).color') == 'rgb(207, 64, 59)'
        await page.locator('#speaker-name').fill('지민')
        await page.locator('[data-select-speaker="S02"]').click()
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
        assert await page.locator('#run-summary').is_disabled()
        assert 'claude auth login' in await page.locator('#cli-setup-command').inner_text()
        await choose_option(page, '#summary-provider', 'codex')
        await page.locator('#summary-prompt').fill('한국어로 핵심 내용, 결정 사항, 담당자별 할 일을 정리해 주세요. 중요한 내용에는 타임스탬프를 붙여 주세요.')
        await page.locator('#run-summary').click()
        await page.locator('#cancel-summary').wait_for(state='visible')
        await page.locator('#cancel-summary').click()
        await page.locator('#summary-status').filter(has_text='취소했습니다').wait_for()
        await page.locator('#run-summary').click()
        await page.locator('#summary-result-wrap').wait_for(state='visible')
        assert '결정 사항' in await page.locator('#summary-result').inner_text()
        await page.locator('#reveal-summary').click()
        await page.wait_for_function('window.__revealed?.endsWith("-요약.md")')
        assert calls[-1][1]['directory'] == '/Users/tester/Downloads'
        assert await page.locator('#review-path-note').text_content() == '기본 경로에 있는 전사 파일을 불러옵니다.'
        assert await page.locator('#summary-path-note').text_content() == '기본 경로에 AI 요약 md파일을 저장합니다.'
        if output:
            await page.locator('#toast').evaluate("el => el.className = 'toast'")
            await page.screenshot(path=str(output), animations='disabled')
        await page.locator('.note-item').click()
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
