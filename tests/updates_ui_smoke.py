"""Native update bridge and editor-save gating with no network update or AI run."""
import asyncio
import sys
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as pw:
        browser = await pw.webkit.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.add_init_script('''
            window.updateChecks = 0;
            window.webkit = {messageHandlers: {
                getAppInfo: {postMessage: async () => ({version:'1.0.0', updatesAvailable:true, canCheckForUpdates:false})},
                checkForUpdates: {postMessage: async () => { window.updateChecks++; return true; }}
            }};
        ''')
        await page.route('**/api/notes', lambda r: r.fulfill(json=[]))
        await page.route('**/api/text-library**', lambda r: r.fulfill(json=[]))
        await page.route('**/api/summary/providers', lambda r: r.fulfill(json=[]))
        await page.route('**/api/devices', lambda r: r.fulfill(json=[]))
        await page.goto(sys.argv[1])
        await page.click('#open-developer-info')
        await page.locator('#check-app-update').wait_for()
        assert await page.locator('#app-version').inner_text() == 'v1.0.0'
        assert await page.locator('#check-app-update').is_enabled()
        assert await page.evaluate('document.activeElement.id') == 'developer-dialog-title'
        await page.click('#check-app-update')
        assert await page.evaluate('window.updateChecks') == 1
        assert not await page.locator('#developer-dialog').evaluate('el=>el.open')
        assert await page.evaluate('window.prepareForAppUpdate()')
        for field, value in [('uploading', True), ('uploadPreparation', {'id':'test'}), ('speakerDraft', {'id':'test'}),
                             ('speakerSaving', True), ('summarySubmitting', True), ('addingDevice', True)]:
            await page.evaluate('([key,value])=>state[key]=value', [field,value])
            assert not await page.evaluate('window.prepareForAppUpdate()'), field
            await page.evaluate('key=>state[key]=null',field)
        await page.evaluate("state.current={id:'fixture', title:'노트', status:'done'}; state.pendingChanges={id:'fixture',changes:{title:'저장할 제목'}}")
        writes = []
        async def save(route):
            writes.append(route.request.post_data_json)
            await route.fulfill(json={'id':'fixture','title':'저장할 제목','status':'done','segments':[],'speaker_names':{},
                                      'created_at':'2026-10-25T00:00:00+00:00','updated_at':'2026-10-25T00:00:00+00:00'})
        await page.route('**/api/notes/fixture', save)
        assert await page.evaluate('window.prepareForAppUpdate()')
        assert writes == [{'title':'저장할 제목'}]
        await page.unroute('**/api/notes/fixture', save)
        await page.route('**/api/notes/fixture', lambda r: r.fulfill(status=409,json={'detail':'외부에서 수정되었습니다.'}))
        await page.evaluate("state.pendingChanges={id:'fixture',changes:{title:'충돌'}}")
        assert not await page.evaluate('window.prepareForAppUpdate()')
        assert await page.evaluate('!!state.pendingChanges')
        assert not errors, errors
        await browser.close()
    print('PASS WebKit: native update button/version, focus, work gates, editor flush, conflict blocks relaunch')


if __name__ == '__main__':
    asyncio.run(main())
