"""Navigate long TXT/SRT speaker previews without modifying their files."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.transcript import plain_text, srt_text


async def main():
    base = sys.argv[1]
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    engine = sys.argv[3] if len(sys.argv) > 3 else 'webkit'
    segments = []
    for index in range(120):
        speaker = 'S01' if index < 20 else ('S02' if index % 2 == 0 else 'S01') if index < 70 else ['S01', 'S02', 'S03'][index % 3]
        segments.append({'id': index, 'start': index * 10, 'end': index * 10 + 5,
                         'speaker': speaker, 'text': f'{index + 1}번째 발화입니다. 긴 회의에서 화자별로 이동하는 내용을 확인합니다.'})
    names = {'S01': '민수', 'S02': '지민', 'S03': '수빈'}
    errors = []
    with tempfile.TemporaryDirectory(prefix='cosmoss-speaker-navigation-') as temporary:
        folder = Path(temporary).resolve()
        for fmt, render in [('txt', plain_text), ('srt', srt_text)]:
            (folder / f'긴 회의 {fmt.upper()}.{fmt}').write_text(render(segments, names))
        async with async_playwright() as pw:
            options = {'executable_path': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
            browser = await getattr(pw, engine).launch(headless=True, **options)
            page = await browser.new_page(viewport={'width': 1440, 'height': 950}, device_scale_factor=2)
            page.on('pageerror', lambda error: errors.append(str(error)))
            location = json.dumps({'path': str(folder), 'name': folder.name})
            await page.add_init_script('window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>(' + location + ')}}};')
            await page.route('**/api/summary/providers', lambda r: r.fulfill(json=[]))
            await page.route('**/api/devices', lambda r: r.fulfill(json=[{'id': 'local', 'name': '이 Mac', 'kind': 'local', 'backend': 'mlx', 'setup': {'phase': 'idle'}}]))
            await page.route('**/api/local-model', lambda r: r.fulfill(json={'phase': 'idle'}))
            await page.goto(base)
            await page.wait_for_function("document.querySelector('#note-count').textContent === '2'")

            async def position(expected):
                await page.wait_for_function('(index)=>document.querySelector("#speaker-preview .is-current")?.dataset.previewIndex===String(index)', arg=expected)
                try:
                    await page.wait_for_function('''() => {
                        const panel=document.querySelector('#speaker-preview').getBoundingClientRect();
                        const row=document.querySelector('#speaker-preview .is-current').getBoundingClientRect();
                        return row.top>=panel.top-1 && row.bottom<=panel.bottom+1;
                    }''', timeout=4000)
                except Exception as error:
                    geometry = await page.locator('#speaker-preview').evaluate('el=>({scroll:el.scrollTop, panel:el.getBoundingClientRect().toJSON(), row:el.querySelector(".is-current").getBoundingClientRect().toJSON()})')
                    raise AssertionError({'expected': expected, 'geometry': geometry}) from error

            for fmt in ('txt', 'srt'):
                source = folder / f'긴 회의 {fmt.upper()}.{fmt}'
                before = source.read_bytes()
                await page.get_by_role('button', name=f'긴 회의 {fmt.upper()} 편집', exact=True).click()
                await page.click('#edit-speakers')
                await page.locator('#speaker-dialog').wait_for(state='visible')
                await position(0)
                assert await page.evaluate('document.activeElement.id') == 'speaker-preview'
                assert '↑·↓' in await page.locator('#speaker-navigation-help').inner_text()
                await page.locator('#speaker-preview').evaluate('el=>el.scrollTop=el.scrollHeight')
                await page.click('[data-select-speaker="S03"]')
                expected = [index for index, segment in enumerate(segments) if segment['speaker'] == 'S03']
                await position(expected[0])
                assert await page.locator('.speaker-preview-row.highlighted').count() == len(expected)
                assert await page.locator('#speaker-preview').evaluate('el=>el.scrollTop>1000')
                assert await page.locator('#speaker-dialog').evaluate('el=>el.scrollTop') == 0
                for index in range(1, 4):
                    await page.keyboard.press('ArrowDown')
                    await position(expected[index])
                await page.keyboard.press('ArrowUp')
                await position(expected[2])
                await page.fill('#speaker-name', '수정 중인 이름')
                await page.keyboard.press('ArrowDown')
                assert await page.locator('#speaker-preview .is-current').get_attribute('data-preview-index') == str(expected[2])
                assert await page.locator('#speaker-name').input_value() == '수정 중인 이름'
                await page.click('[data-select-speaker="S02"]')
                await position(20)
                await page.click('[data-select-speaker="S03"]')
                await position(expected[0])
                assert await page.locator('#speaker-name').input_value() == '수정 중인 이름'
                await page.emulate_media(reduced_motion='reduce')
                await page.keyboard.press('ArrowUp')
                await position(expected[0])
                for _ in range(len(expected) + 2):
                    await page.keyboard.press('ArrowDown')
                await position(expected[-1])
                assert (await page.locator('#speaker-navigation-help').inner_text()).endswith(f'{len(expected)}/{len(expected)}')
                if output:
                    await page.locator('#speaker-dialog').screenshot(path=str(output.with_name(output.stem + '-' + fmt + '.png')))
                await page.locator('[data-select-speaker="S03"]').drag_to(page.locator('[data-select-speaker="S01"]'))
                await page.click('#confirm-speaker-merge')
                await position(0)
                assert await page.evaluate('document.activeElement.id') == 'speaker-name'
                await page.locator('#speaker-preview').focus()
                await page.keyboard.press('ArrowDown')
                await position(1)
                await page.click('#cancel-speakers')
                await page.click('#close-note')
                assert source.read_bytes() == before, 'Navigation or cancelled draft changed the source'
            assert not errors, errors
            await browser.close()
    print(f'PASS {engine}: TXT/SRT first utterance, skipped speakers, arrows, bounds, input isolation, reduced motion, merge reset and unchanged files')


if __name__ == '__main__':
    asyncio.run(main())
