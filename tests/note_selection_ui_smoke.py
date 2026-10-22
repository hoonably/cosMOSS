"""Note selection, explicit editing, summary handoff and concurrent selection."""
import asyncio
import copy
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    base, engine, png = sys.argv[1:4]
    notes = [dict(id=letter*32, title=title, status='done',
                  created_at='2026-10-23T02:26:00Z', output_format='txt',
                  text_path=f'/Users/tester/Downloads/{title}.txt', text_hash=letter,
                  duration=60, chunk_count=1, speaker_names={}, raw_segments=[],
                  segments=[dict(id=0, start=0, end=5, speaker='S01', text='선택과 편집을 구분합니다.')])
             for letter, title in (('a', '제품 개발 회의'), ('b', '다음 주 발표 준비'), ('c', '읽을 수 없는 파일'))]
    summaries = {note['id']: {'phase':'idle'} for note in notes}
    delays = {}; calls = []; errors = []

    async def note_route(route):
        identity, *parts = route.request.url.split('/api/notes/')[1].split('/')
        action = '/'.join(parts)
        note = next(n for n in notes if n['id'] == identity)
        if action == 'summary':
            if route.request.method == 'POST':
                calls.append((identity, action, route.request.post_data_json))
                summaries[identity] = {'phase':'running', 'provider':'codex'}
            return await route.fulfill(json=summaries[identity])
        if route.request.method == 'PATCH':
            calls.append((identity, 'edit', route.request.post_data_json))
            note.update(route.request.post_data_json)
        response = copy.deepcopy(note)
        await asyncio.sleep(delays.get(identity, 0))
        if identity == notes[2]['id']:
            return await route.fulfill(status=409, json={'detail':'파일이 변경되었습니다.'})
        await route.fulfill(json=response)

    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width':1440,'height':950}, device_scale_factor=2)
        page.set_default_timeout(10000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.route('**/api/devices', lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes', lambda r:r.fulfill(json=notes))
        await page.route('**/api/text-library**', lambda r:r.fulfill(json=[{
            **n, 'id':'txt_'+n['id'], 'source_type':'text_file', 'managed_note_id':n['id']
        } for n in notes]))
        await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[{
            'id':'codex','name':'Codex','ready':True,'installed':True,'message':'로그인됨'
        }]))
        await page.route('**/api/notes/*', note_route)
        await page.route('**/api/notes/*/**', note_route)
        await page.goto(base)
        a, b, c = [page.locator(f'.note-item[data-id="{n["id"]}"]') for n in notes]
        await a.locator('.note-select').click()
        await page.wait_for_function('!state.openingNote && state.current?.title === "제품 개발 회의"')
        assert await page.locator('#note-dialog').is_hidden()
        assert await a.locator('.note-select').get_attribute('aria-pressed') == 'true'
        assert await page.locator('#run-summary').is_enabled()
        assert not calls, 'Selecting a note wrote to a file'
        await page.locator('#summary-prompt').fill('결정 사항을 먼저 정리해 주세요.')
        await page.locator('#run-summary').click()
        await page.locator('#cancel-summary').wait_for(state='visible')
        assert calls[-1][0:2] == (notes[0]['id'], 'summary')
        assert await page.locator('#note-dialog').is_hidden(), 'Summary opened the editor'

        await b.locator('.note-select').focus()
        await page.keyboard.press('Space')
        await page.wait_for_function('!state.openingNote && state.current?.title === "다음 주 발표 준비"')
        assert await page.locator('#note-dialog').is_hidden()
        assert await page.locator('#summary-prompt').input_value() == ''
        assert await page.locator('#run-summary').is_enabled()
        assert summaries[notes[0]['id']]['phase'] == 'running'
        await page.locator('#summary-prompt').fill('발표용으로 정리해 주세요.')
        await page.locator('#use-default-summary-prompt').uncheck()
        await a.locator('.note-select').focus()
        await page.keyboard.press('Enter')
        await page.wait_for_function('!state.openingNote && state.current?.title === "제품 개발 회의"')
        assert await page.locator('#summary-prompt').input_value() == '결정 사항을 먼저 정리해 주세요.'
        assert await page.locator('#use-default-summary-prompt').is_checked()
        assert await page.locator('#cancel-summary').is_visible()
        await b.locator('.note-select').click()
        await page.wait_for_function('!state.openingNote && state.current?.title === "다음 주 발표 준비"')
        assert await page.locator('#summary-prompt').input_value() == '발표용으로 정리해 주세요.'
        assert not await page.locator('#use-default-summary-prompt').is_checked()

        # Editing another row also selects it, but only this explicit action opens the dialog.
        await a.locator('.note-edit').click()
        await page.locator('#note-dialog').wait_for(state='visible')
        assert await page.locator('#note-title').input_value() == notes[0]['title']
        await page.locator('.segment-text').fill('저장 후에도 선택한 노트가 유지됩니다.')
        await page.locator('#close-note').click()
        await page.locator('#note-dialog').wait_for(state='hidden')
        assert any(call[0:2] == (notes[0]['id'], 'edit') for call in calls)
        assert await a.locator('.note-edit').evaluate('el=>el===document.activeElement')
        assert await a.locator('.note-select').get_attribute('aria-pressed') == 'true'

        # A slow response must not replace the last selection or allow the wrong summary to start.
        delays[notes[0]['id']] = .5
        await a.locator('.note-select').click()
        await page.wait_for_function('Boolean(state.openingNote)')
        assert await page.locator('#run-summary').is_disabled()
        await b.locator('.note-select').click()
        await page.wait_for_function('!state.openingNote && state.current?.title === "다음 주 발표 준비"')
        await asyncio.sleep(.6)
        assert await b.locator('.note-select').get_attribute('aria-pressed') == 'true'
        assert await a.locator('.note-select').get_attribute('aria-pressed') == 'false'
        assert '다음 주 발표 준비.txt' in await page.locator('#summary-source').inner_text()
        assert await page.locator('#run-summary').is_enabled()
        await c.locator('.note-select').click()
        await page.wait_for_function('!state.openingNote')
        assert '파일이 변경되었습니다.' in await page.locator('#toast').inner_text()
        assert await b.locator('.note-select').get_attribute('aria-pressed') == 'true'
        assert await page.locator('#run-summary').is_enabled()

        await b.locator('.note-select').focus()
        await page.evaluate('loadNotes()')
        assert await b.locator('.note-select').evaluate('el=>el===document.activeElement')
        assert await page.locator('#note-dialog').is_hidden()
        await page.locator('#toast').evaluate("el=>el.className='toast'")
        await page.screenshot(path=png, animations='disabled')
        for width in (1100, 1440):
            await page.set_viewport_size({'width':width,'height':900})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert await page.locator('.note-select').evaluate_all('els=>els.every(e=>e.scrollWidth<=e.clientWidth)')
            assert await page.locator('.note-edit').evaluate_all('els=>els.every(e=>e.scrollWidth<=e.clientWidth)')
        assert not errors, errors
        await browser.close()
    print(engine, 'PASS: selection-only, direct summary, explicit edit/save, keyboard, drafts, selection races, load errors and focus retention')


asyncio.run(main())
