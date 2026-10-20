"""Exercise environment-only installation and deletion recovery without SSH/GPU work."""
import asyncio
import copy
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    base, engine = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'webkit'
    output = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    missing = {'ready': False, 'uv': {'ready': True, 'version': 'uv 0.9.25', 'path': '/home/test/.local/bin/uv'},
               'venv': {'ready': False, 'path': '/home/test/.local/share/cosmoss/workers/test/.venv'},
               'packages': {'ready': False, 'missing': ['torch'], 'versions': {}}}
    runtimes = {key: copy.deepcopy(missing) for key in ('local', 'test')}
    setups = {key: {'phase': 'idle', 'runtime': runtimes[key]} for key in runtimes}
    polls, installed, errors = {}, [], []
    devices = [{'id': 'local', 'kind': 'local', 'name': '이 Mac', 'backend': 'mlx', 'model_key': 'mlx-8bit'},
               {'id': 'test', 'kind': 'ssh', 'name': '연구실 3090', 'host': 'lab-3090', 'backend': 'cuda', 'model_key': 'cuda-original'}]

    async def local(route):
        await advance('local')
        await route.fulfill(json=setups['local'])

    async def advance(key):
        if key in polls:
            polls[key] += 1
            if polls[key] >= 2:
                runtimes[key]['ready'] = True
                for component in ('uv', 'venv', 'packages'):
                    runtimes[key][component]['ready'] = True
                runtimes[key]['venv']['version'] = '3.12.12'
                runtimes[key]['packages'].update(missing=[], versions={'torch': '2.8.0+cu128'})
                setups[key] = {'phase': 'idle', 'runtime': runtimes[key], 'model_ready': False}
                del polls[key]

    async def environment(route):
        key = route.request.url.split('/devices/')[1].split('/')[0]
        if route.request.method == 'POST':
            installed.append(key)
            polls[key] = 0
            setups[key] = {'phase': 'installing_uv', 'runtime': runtimes[key]}
            return await route.fulfill(status=202, json=setups[key])
        await route.fulfill(json=runtimes[key])

    async def worker(route):
        await advance('test')
        await route.fulfill(json=setups['test'])

    async with async_playwright() as pw:
        options = {'executable_path': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width': 1280, 'height': 860}, device_scale_factor=2)
        page.on('pageerror', lambda error: errors.append(str(error)))
        requests = []
        page.on('request', lambda request: requests.append(request.url))
        await page.route('**/api/devices', lambda r: r.fulfill(json=[{**d, 'setup': setups[d['id']]} for d in devices]))
        await page.route('**/api/notes', lambda r: r.fulfill(json=[]))
        await page.route('**/api/summary/providers', lambda r: r.fulfill(json=[]))
        await page.route('**/api/local-model', local)
        await page.route('**/api/devices/*/environment', environment)
        await page.route('**/api/devices/test/worker', worker)
        await page.route('**/api/text-library**', lambda route: route.fulfill(json=[]))
        await page.goto(base)
        await page.locator('[data-device-id=test] [data-device-action=environment]').click()
        await page.wait_for_function("document.querySelectorAll('#environment-components .environment-component-status').length===3")
        assert await page.locator('#environment-components .environment-component-status').nth(1).inner_text() == '설치 필요'
        if output: await page.screenshot(path=str(output))
        await page.click('#install-environment')
        await page.wait_for_function("[...document.querySelectorAll('#environment-components .environment-component-status')].every(el=>el.textContent==='준비됨')")
        assert installed == ['test']
        assert '모델 준비 필요' in await page.locator('[data-device-id=test] .device-card-state').inner_text()
        await page.click('#close-environment')
        assert 'uv · venv ✓' in await page.locator('[data-device-id=test] .device-environment-link').inner_text()
        runtimes['test']['ready'] = runtimes['test']['venv']['ready'] = runtimes['test']['packages']['ready'] = False
        await page.locator('[data-device-id=test] [data-device-action=environment]').click()
        await page.wait_for_function("document.querySelectorAll('#environment-components .environment-component-status')[1].textContent==='설치 필요'")
        await page.click('#install-environment')
        await page.wait_for_function("[...document.querySelectorAll('#environment-components .environment-component-status')].every(el=>el.textContent==='준비됨')")
        await page.click('#close-environment')
        await page.locator('[data-device-id=local] [data-device-action=environment]').click()
        await page.click('#install-environment')
        await page.wait_for_function("[...document.querySelectorAll('#environment-components .environment-component-status')].every(el=>el.textContent==='준비됨')")
        assert installed == ['test', 'test', 'local']
        assert not any('/gpus' in url or '/model/prepare' in url for url in requests)
        assert not errors, errors
        await browser.close()
    print(f'PASS {engine}: component status, environment-only install, deleted-venv detection/repair, Mac installation')


if __name__ == '__main__':
    asyncio.run(main())
