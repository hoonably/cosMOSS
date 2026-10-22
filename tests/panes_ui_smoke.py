"""Fixed stage separators, ignored legacy widths and responsive layout."""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


PANES = ('.workflow-device-column', '#stage-review', '.summary-panel')


async def sizes(page):
    return [await page.locator(s).evaluate('(e)=>e.getBoundingClientRect().width') for s in PANES]


async def main():
    base, engine = sys.argv[1:3]
    output = Path(sys.argv[3]) if len(sys.argv)>3 else None
    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser = await getattr(pw,engine).launch(headless=True,**options)
        page = await browser.new_page(viewport={'width':1440,'height':950},device_scale_factor=2)
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        await page.add_init_script('localStorage.setItem("cosmoss.stageWidths.v1","[0.6,0.25,0.15]")')
        await page.route('**/api/devices',lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes',lambda r:r.fulfill(json=[]))
        await page.route('**/api/text-library**',lambda r:r.fulfill(json=[]))
        await page.route('**/api/summary/providers',lambda r:r.fulfill(json=[]))
        await page.goto(base)
        assert await page.locator('.stage-divider').count()==2
        for width in (1100,1440,1920):
            await page.set_viewport_size({'width':width,'height':950})
            before=await sizes(page)
            assert max(before)-min(before)<1,before
            for divider in await page.locator('.stage-divider').all():
                assert await divider.get_attribute('tabindex') is None
                assert await divider.evaluate('(e)=>getComputedStyle(e).cursor')!='col-resize'
                rect=await divider.bounding_box()
                x,y=rect['x']+.5,rect['y']+100
                await page.mouse.move(x,y)
                await page.mouse.down()
                await page.mouse.move(x+100,y,steps=5)
                await page.mouse.up()
            assert all(abs(a-b)<1 for a,b in zip(before,await sizes(page)))
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        await page.reload()
        restored=await sizes(page)
        assert max(restored)-min(restored)<1,restored
        if output:
            await page.screenshot(path=str(output))
        for width in (900,600,390):
            await page.set_viewport_size({'width':width,'height':950})
            assert await page.locator('.stage-divider:visible').count()==0
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        await browser.close()
    print(f'PASS {engine}: fixed separators, no drag/focus controls, equal widths, legacy preferences ignored, responsive layout')


if __name__=='__main__':
    asyncio.run(main())
