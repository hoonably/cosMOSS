"""WebKit QA using an actual supplied recording, never microphone auto-grant."""
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def run(base, sample, output, screenshots):
    errors = []
    async with async_playwright() as pw:
        browser = await pw.webkit.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 950}, accept_downloads=True)
        page.on('pageerror', lambda error: errors.append(str(error)))
        location = json.dumps({'path': str(output), 'name': output.name})
        await page.add_init_script('window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>(' + location + ')}}};')
        await page.route('**/api/summary/providers', lambda route: route.fulfill(json=[]))
        await page.route('**/api/devices', lambda route: route.fulfill(json=[{'id': 'local', 'name': '이 Mac', 'kind': 'local', 'backend': 'mlx', 'setup': {'phase': 'idle'}}]))
        await page.route('**/api/local-model', lambda route: route.fulfill(json={'phase': 'idle'}))
        await page.goto(base)
        await page.click('#open-app-settings')
        await page.locator('#app-settings-dialog [data-open-voice-profiles]').click()
        await page.wait_for_function("document.querySelector('#voice-profile-count').textContent==='1'")
        assert await page.evaluate('document.activeElement.id') == 'voice-profiles-title'
        await page.fill('#voice-profile-name', 'UI 확인')
        await page.set_input_files('#voice-enroll-file', str(sample))
        await page.click('#save-voice-profile')
        await page.wait_for_function("document.querySelector('#voice-profile-count').textContent==='2' && !document.querySelector('#voice-profile-name').disabled")
        row = page.locator('.voice-profile-row').filter(has=page.locator('input[value="UI 확인"]'))
        await row.locator('input').fill('공유 확인')
        await row.locator('[data-voice-action="rename"]').click()
        await page.wait_for_function("document.querySelector('#voice-profile-message').textContent==='프로필 이름을 저장했습니다.'")
        row = page.locator('.voice-profile-row').filter(has=page.locator('input[value="공유 확인"]'))
        async with page.expect_download() as download_event:
            await row.locator('[data-voice-action="export"]').click()
        download = await download_event.value
        assert download.suggested_filename.endswith('.mossvoice')
        portable = Path(sample).with_name('ui-roundtrip.mossvoice')
        await download.save_as(portable)
        assert json.loads(portable.read_bytes())['name'] == '공유 확인'
        await row.locator('[data-voice-action="delete"]').click()
        assert await page.locator('.voice-profile-row').count() == 2
        await row.locator('[data-voice-action="delete"]').click()
        await page.wait_for_function("document.querySelector('#voice-profile-count').textContent==='1'")
        await page.set_input_files('#import-voice-profile', str(portable))
        await page.wait_for_function("document.querySelector('#voice-profile-count').textContent==='2' && !document.querySelector('#voice-profile-name').disabled")
        await page.fill('#voice-match-threshold', '.83')
        await page.uncheck('#voice-auto-enabled')
        await page.click('#save-voice-options')
        await page.wait_for_function("document.querySelector('#voice-profile-message').textContent==='자동 이름 지정 설정을 저장했습니다.'")
        for width, height in ((1440, 950), (1100, 720)):
            await page.set_viewport_size({'width': width, 'height': height})
            dialog = page.locator('#voice-profiles-dialog')
            assert await dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
            await dialog.screenshot(path=str(screenshots / f'voice-profiles-{width}.png'))
        await page.click('#close-voice-profiles')
        await page.locator('#app-settings-dialog [data-open-voice-profiles]').click()
        await page.wait_for_function("document.querySelector('#voice-match-threshold').value==='0.83'")
        assert not await page.is_checked('#voice-auto-enabled')
        await page.click('#close-voice-profiles')
        await page.click('#close-app-settings')
        await page.get_by_role('button', name='실제 녹음 확인 txt', exact=False).click()
        await page.click('#edit-speakers')
        await page.fill('#speaker-name', 'S01')
        await page.click('#identify-voice-speakers')
        await page.wait_for_function("!document.querySelector('#identify-voice-speakers').disabled")
        # Two identical profiles with different names should remain ambiguous.
        assert '추가로 지정할 이름을 찾지 못했습니다.' in await page.locator('#speaker-voice-status').inner_text()
        await page.locator('#speaker-dialog [data-open-voice-profiles]').click()
        row = page.locator('.voice-profile-row').filter(has=page.locator('input[value="공유 확인"]'))
        await row.locator('[data-voice-action="delete"]').click()
        await row.locator('[data-voice-action="delete"]').click()
        await page.wait_for_function("document.querySelector('#voice-profile-count').textContent==='1'")
        await page.click('#close-voice-profiles')
        await page.click('#identify-voice-speakers')
        await page.wait_for_function("document.querySelector('#speaker-name').value==='파이프라인 확인'")
        await page.click('#save-speakers')
        await page.locator('#speaker-dialog').wait_for(state='hidden')
        assert not errors, errors
        await browser.close()
    print('PASS WebKit: real file enrollment, rename, portable download/import, delete confirmation, persisted options, ambiguity, manual name preview/save, 1100px dialog')


if __name__ == '__main__':
    asyncio.run(run(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])))
