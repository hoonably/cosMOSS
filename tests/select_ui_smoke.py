"""Exercise custom menus in both browser engines, without SSH or model downloads."""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from ui_helpers import choose_option


async def main():
    base = sys.argv[1]
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    engine = sys.argv[3] if len(sys.argv) > 3 else 'chromium'
    async with async_playwright() as pw:
        kwargs = {'executable_path': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **kwargs)
        page = await browser.new_page(viewport={'width': 1280, 'height': 860}, device_scale_factor=2)
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        catalogue = await (await page.request.get(base + '/api/model-catalog')).json()
        local = {'phase':'idle','source':'cache','model_key':'mlx-8bit','model_id':'vanch007/mlx-MOSS-Transcribe-Diarize-8bit','cache_dir':'/Users/tester/.cache/huggingface/hub','suggested_path':''}
        await page.route('**/api/devices', lambda r: r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_key':'mlx-8bit','model':'moss-mtd','remote_port':8001,'setup':local}]))
        await page.route('**/api/local-model', lambda r: r.fulfill(json={**catalogue, **local}))
        await page.route('**/api/devices/local/model-readiness**', lambda r: r.fulfill(json={'phase':'installed','ready':True,'asr_ready':True,'diarization_ready':True}))
        await page.route('**/api/notes', lambda r: r.fulfill(json=[]))
        await page.route('**/api/ssh/hosts', lambda r: r.fulfill(json={'hosts':['lab-3090','lab-a100']}))
        await page.route('**/api/summary/providers', lambda r: r.fulfill(json=[{'id':'codex','name':'Codex','installed':True,'ready':True,'message':'로그인됨'}, {'id':'claude','name':'Claude','installed':True,'ready':True,'message':'로그인됨'}]))
        await page.route('**/api/text-library**', lambda route: route.fulfill(json=[]))
        await page.goto(base)
        await page.locator('[data-device-model=local]').wait_for(state='attached')
        assert await page.locator('select:visible').count() == 0, 'A native dropdown remains visible'
        assert await page.locator('select').count() == await page.locator('.custom-select-trigger').count()
        trigger = page.locator('#summary-provider').locator('..').locator('button')
        await trigger.focus()
        await trigger.press('ArrowDown')
        await trigger.press('ArrowDown')
        assert await page.locator('#summary-provider').input_value() == 'codex', 'Arrow navigation committed early'
        await trigger.press('Escape')
        assert await page.locator('.custom-select-menu').count() == 0
        assert await page.locator('#summary-provider').input_value() == 'codex'
        await trigger.press('Enter')
        await trigger.press('End')
        await trigger.press('Enter')
        assert await page.locator('#summary-provider').input_value() == 'claude'
        await page.evaluate("document.querySelector('#summary-provider').value='codex'")
        assert 'Codex' in await trigger.inner_text(), 'Direct value assignment did not refresh label'
        await trigger.click()
        await page.locator('#review-title').click()
        assert await page.locator('.custom-select-menu').count() == 0, 'Outside click did not dismiss'

        # A periodically rebuilt device card must not retain a stale menu.
        device_trigger = page.locator('[data-device-model=local]').locator('..').locator('button')
        await device_trigger.click()
        await page.evaluate("renderDevices()")
        await page.wait_for_function("document.querySelector('.custom-select-menu')?.isConnected && document.querySelector('[data-device-model=local]').previousElementSibling.getAttribute('aria-expanded')==='true'")
        await page.locator('[data-device-model=local]').locator('..').locator('button').press('Escape')
        await page.locator('[data-device-action=local-model]').click()
        await page.locator('#prepare-local-model:not([disabled])').wait_for()
        model = page.locator('#model-choice').locator('..').locator('button')
        await model.click()
        menu = page.locator('.custom-select-menu')
        assert await menu.locator('[role=option]').count() == 3
        assert await menu.locator('[data-value="cuda-original"], [data-value="custom"]').count() == 0
        assert await page.locator('#model-custom-id, #model-folder-path').count() == 0
        await menu.locator('[role=option]').first.evaluate('el=>window.__stableOption=el')
        await page.evaluate('renderDevices()')
        assert await page.evaluate('window.__stableOption.isConnected'), 'Unrelated status update rebuilt the open menu'
        assert await menu.locator('.custom-select-badge').count() >= 1
        bounds = await menu.bounding_box()
        dialog = await page.locator('#local-model-dialog').bounding_box()
        assert bounds['x'] >= dialog['x'] and bounds['x'] + bounds['width'] <= dialog['x'] + dialog['width']
        assert bounds['y'] >= dialog['y'] and bounds['y'] + bounds['height'] <= dialog['y'] + dialog['height']
        if output:
            await menu.evaluate('el=>Promise.all(el.getAnimations().map(animation=>animation.finished))')
            await page.screenshot(path=str(output))
        await model.press('End')
        active = await model.get_attribute('aria-activedescendant')
        assert await page.locator('#' + active).get_attribute('aria-disabled') == 'false'
        await model.press('Escape')
        assert await page.locator('#local-model-dialog').evaluate('el=>el.open'), 'Escape closed the whole settings dialog'
        await choose_option(page, '#model-choice', 'mlx-4bit')
        assert await page.locator('#model-choice').input_value() == 'mlx-4bit'
        assert await page.locator('[data-knob=chunk_minutes]').is_visible()
        assert await page.locator('#model-knobs > summary').count() == 0
        await choose_option(page, '[data-knob=kv_bits]', '0')
        assert await page.locator('[data-knob=kv_bits]').input_value() == '0'
        await page.locator('#close-local-model').click()
        assert await page.locator('.custom-select-menu').count() == 0

        local['phase'] = 'installed'
        await page.evaluate('pollLocalModel()')
        await page.locator('[data-device-action=use]').click()
        await choose_option(page, 'select[name=language]', 'en')
        language = page.locator('select[name=language]').locator('..').locator('button')
        assert await language.evaluate('el=>el.offsetHeight') == await page.locator('#upload-form input[name=hotwords]').evaluate('el=>el.offsetHeight') == 44
        assert await page.locator('#upload-form').evaluate("el=>new FormData(el).get('language')") == 'en'
        await choose_option(page, 'select[name=language]', 'ja')
        await language.focus()
        await language.press('e')
        await language.press('Enter')
        assert await page.locator('select[name=language]').input_value() == 'en'
        await language.click()
        await language.press('Tab')
        assert await page.locator('.custom-select-menu').count() == 0
        await page.locator('#close-upload').click()

        # The same component supports future dynamically inserted fields.
        await page.evaluate('''() => {
          const form=document.createElement('form');form.id='custom-select-fixture';
          form.innerHTML='<fieldset><label class="field"><span>검증용</span><select name="choice" required><option value="">선택하세요</option><option value="b">Beta</option></select></label></fieldset><label class="field"><span>빈 목록</span><select id="empty-select"></select></label>';
          document.querySelector('.summary-scroll').prepend(form);
        }''')
        field = page.locator('#custom-select-fixture select[name=choice]')
        await page.wait_for_function("document.querySelector('#custom-select-fixture .custom-select-trigger') !== null")
        await page.evaluate("document.querySelector('#custom-select-fixture').reportValidity()")
        assert await page.locator('.custom-select-menu').is_visible()
        await page.locator('#custom-select-fixture .custom-select-trigger').first.press('Escape')
        await choose_option(page, '#custom-select-fixture select[name=choice]', 'b')
        assert await page.locator('#custom-select-fixture').evaluate("el=>new FormData(el).get('choice')") == 'b'
        await page.evaluate("document.querySelector('#custom-select-fixture fieldset').disabled=true")
        await page.wait_for_function("document.querySelector('#custom-select-fixture .custom-select-trigger').disabled")
        await page.evaluate("document.querySelector('#custom-select-fixture fieldset').disabled=false;document.querySelector('#custom-select-fixture').reset()")
        await page.wait_for_function("document.querySelector('#custom-select-fixture .custom-select-value').textContent==='선택하세요'")
        empty = page.locator('#empty-select').locator('..').locator('button')
        await empty.click()
        await empty.press('Enter')
        await empty.press('Escape')
        assert not errors, errors
        await browser.close()
    print(f'PASS {engine}: custom menus, keyboard/typeahead, disabled options, form values/reset/validation, dynamic values/cards, modal bounds and matching heights')


if __name__ == '__main__': asyncio.run(main())
