"""Check equal workflow columns, resizing, persistence and responsive limits."""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


KEY = 'cosmoss.stageWidths.v1'
PANES = ('.workflow-device-column', '#stage-review', '.summary-panel')


async def sizes(page):
    return [await page.locator(selector).evaluate('el=>el.getBoundingClientRect().width') for selector in PANES]


async def settled(page):
    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')


async def assert_equal(page):
    await settled(page)
    values = await sizes(page)
    assert max(values) - min(values) < 1, values


async def drag(page, index, delta):
    handle = page.locator('.stage-splitter').nth(index)
    box = await handle.bounding_box()
    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + delta, y, steps=8)
    await page.mouse.up()
    await settled(page)


async def main():
    base = sys.argv[1]
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    engine = sys.argv[3] if len(sys.argv) > 3 else 'chromium'
    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width':1440,'height':950}, device_scale_factor=2)
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        text = '영역의 너비를 줄여도 이 전사 문장은 잘리지 않고 모두 보여야 합니다. ' * 12
        note = {'id':'a'*32,'title':'너비 조절 테스트','status':'done','created_at':'2026-10-19T00:00:00Z','original_filename':'example.wav','device_name':'이 Mac','duration':60,'chunk_count':1,'speaker_names':{},'raw_segments':[], 'segments':[{'id':0,'start':0,'end':60,'speaker':'S01','text':text}]}
        await page.route('**/api/devices', lambda r:r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_key':'mlx-8bit','setup':{'phase':'idle'}}]))
        await page.route('**/api/local-model', lambda r:r.fulfill(json={'phase':'idle'}))
        await page.route('**/api/notes', lambda r:r.fulfill(json=[note]))
        await page.route('**/api/notes/' + note['id'], lambda r:r.fulfill(json=note))
        await page.route('**/api/notes/*/media', lambda r:r.fulfill(status=404))
        await page.route('**/api/notes/*/summary', lambda r:r.fulfill(json={'phase':'idle','default_prompt':'요약해 주세요.'}))
        await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[{'id':'codex','name':'Codex','ready':True,'installed':True,'message':'로그인됨'}]))
        await page.route('**/api/text-library**', lambda route: route.fulfill(json=[]))
        await page.goto(base)
        await page.locator('.note-item').click()
        await page.locator('#editor').wait_for(state='visible')
        for width in (1100, 1440, 1920):
            await page.set_viewport_size({'width':width,'height':950})
            await assert_equal(page)
        await page.set_viewport_size({'width':1440,'height':950})
        await assert_equal(page)
        await page.locator('#close-note').click()
        await page.locator('#note-dialog').wait_for(state='hidden')
        if output: await page.screenshot(path=str(output))
        original = await sizes(page)
        await drag(page, 0, 110)
        left = await sizes(page)
        assert abs(left[0] - original[0] - 110) < 1 and abs(left[1] - original[1] + 110) < 1, (original,left)
        assert abs(left[2] - original[2]) < 1, (original,left)
        await page.locator('.note-item').click()
        assert await page.locator('.segment-text').evaluate('el=>el.clientHeight+1>=el.scrollHeight'), 'Transcript text was clipped in the note window'
        await page.locator('#close-note').click()
        await page.locator('#note-dialog').wait_for(state='hidden')
        await drag(page, 1, 70)
        right = await sizes(page)
        assert abs(right[0] - left[0]) < 1 and abs(right[1] - left[1] - 70) < 1 and abs(right[2] - left[2] + 70) < 1, (left,right)
        saved = await page.evaluate(f'localStorage.getItem({KEY!r})')
        assert saved
        await page.reload()
        await settled(page)
        restored = await sizes(page)
        assert all(abs(a-b) < 1 for a,b in zip(right,restored)), (right,restored)
        handle = page.locator('.stage-splitter').nth(1)
        await handle.focus()
        await handle.press('ArrowLeft')
        keyboard = await sizes(page)
        assert abs(keyboard[1] - restored[1] + 20) < 1
        await handle.press('Shift+ArrowRight')
        assert abs((await sizes(page))[1] - keyboard[1] - 60) < 1
        await handle.press('Home')
        assert abs((await sizes(page))[1] - 320) < 1
        await handle.press('End')
        assert abs((await sizes(page))[2] - 280) < 1
        assert await page.locator('.stage-path-note').evaluate_all('els=>els.every(el=>el.scrollWidth<=el.clientWidth && el.clientHeight<=15)'), 'Path captions wrapped or overflowed at minimum pane width'
        await page.evaluate("renderSaveLocation({path:'/Users/tester/Documents/'+'아주 긴 폴더 이름 '.repeat(12),name:'아주 긴 폴더 이름 '.repeat(12)})")
        assert await page.locator('.stage-path-note').evaluate_all('els=>els.every(el=>el.scrollWidth<=el.clientWidth && el.clientHeight<=15)'), 'Long folder name pushed caption outside pane'
        assert await page.locator('#save-location-name').evaluate('el=>el.scrollWidth>el.clientWidth'), 'Long toolbar path should truncate'
        await page.evaluate("renderSaveLocation({path:'/Users/tester/Downloads',name:'Downloads'})")
        wide = await sizes(page)
        await page.set_viewport_size({'width':1100,'height':900})
        await settled(page)
        narrow = await sizes(page)
        assert all(size >= minimum-1 for size,minimum in zip(narrow,(260,320,280))), narrow
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Window resize caused horizontal overflow'
        await page.set_viewport_size({'width':1440,'height':950})
        await settled(page)
        assert all(abs(a-b)<1 for a,b in zip(wide,await sizes(page))), 'Viewport resizing overwrote the saved proportions'

        # Escape cancels an in-progress drag without changing its saved preference.
        before = await sizes(page)
        box = await handle.bounding_box()
        x,y = box['x']+4, box['y']+100
        await page.mouse.move(x,y)
        await page.mouse.down()
        await page.mouse.move(x-100,y,steps=5)
        await page.keyboard.press('Escape')
        await page.mouse.up()
        await settled(page)
        assert all(abs(a-b)<1 for a,b in zip(before,await sizes(page)))
        assert not await page.locator('body').evaluate("el=>el.classList.contains('resizing-stages')")
        await handle.dblclick()
        await assert_equal(page)
        assert await page.evaluate(f'localStorage.getItem({KEY!r})') is None
        await handle.press('ArrowLeft')
        await handle.press('Enter')
        await assert_equal(page)
        for width in (900, 600):
            await page.set_viewport_size({'width':width,'height':900})
            assert await page.locator('.stage-splitter:visible').count() == 0
        await page.set_viewport_size({'width':1440,'height':950})
        await assert_equal(page)
        await page.evaluate(f'localStorage.setItem({KEY!r},"[0,-2,4]")')
        await page.reload()
        await assert_equal(page)
        assert not errors, errors
        await browser.close()
    print(f'PASS {engine}: equal defaults, both drag boundaries, text reflow, persistence, keyboard, limits, viewport changes, cancellation, reset, narrow layout and invalid preferences')


if __name__ == '__main__': asyncio.run(main())
