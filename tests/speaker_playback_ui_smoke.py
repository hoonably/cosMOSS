"""Click-only, bounded speaker playback using an actual WAV and media API.

Usage: URL QA_DATA_DIR PNG [webkit|chromium]. Synthetic audio, no ML models.
"""
import asyncio
import json
import sys
import uuid
import wave
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.store import NoteStore
from src.text_files import write_text


async def main():
    base, data, screenshot = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3])
    engine = sys.argv[4] if len(sys.argv) > 4 else 'webkit'
    assert 'cosmoss-qa' in data.parts
    store = NoteStore(data / 'moss-note.sqlite3')
    identity = uuid.uuid4().hex
    folder = store.upload_directory(identity)
    folder.mkdir(parents=True)
    source = folder / 'recording.m4a'
    source.write_bytes(b'original fixture; use the prepared WAV for playback')
    prepared = folder / 'normalized.wav'
    with wave.open(str(prepared), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x01' * 16000 * 12)
    segments = [dict(id=i, start=start, end=end, speaker=speaker, text=text) for i, (start, end, speaker, text) in enumerate([
        (.4, 1.1, 'S01', '첫 번째 화자의 짧은 발화입니다.'),
        (2, 3.8, 'S02', '다른 화자의 목소리를 비교합니다.'),
        (6, 7.5, 'S01', '재생 버튼으로 직접 확인하고 이름을 정합니다.'),
        (9, 9, 'S02', '길이가 없는 발화는 재생하지 않습니다.'),
    ])]
    store.create(dict(id=identity, status='done', title='대본 옆 구간 재생 테스트', duration=12,
        source_path=str(source), normalized_path=str(prepared), original_filename='recording.m4a', media_type='audio/mp4'))
    note = store.update(identity, segments=segments, raw_segments=segments, speaker_names={'S01':'민수', 'S02':'지민'})
    output = data / 'output' / identity
    store.update(identity, **write_text(note, str(output)))
    errors, requests, writes = [], [], []
    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width':1440, 'height':1000}, device_scale_factor=2)
        page.set_default_timeout(10000)
        page.on('pageerror', lambda error:errors.append(str(error)))
        page.on('request', lambda r: requests.append(r.url) if '/media' in r.url else None)
        page.on('request', lambda r: writes.append(r.url) if r.method == 'POST' and r.url.endswith('/speakers') else None)
        location = json.dumps({'path':str(output), 'name':'output'})
        await page.add_init_script('window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>(' + location + ')}}};')
        await page.route('**/api/devices', lambda r:r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_selected':False}]))
        await page.route('**/api/local-model', lambda r:r.fulfill(json={'phase':'idle'}))
        await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes', lambda r:r.fulfill(json=[store.get(identity)]))
        await page.goto(base)
        await page.locator(f'.note-item[data-id="{identity}"] .note-edit').click()
        await page.click('#edit-speakers')
        buttons = page.locator('[data-speaker-play]')
        player = page.locator('#speaker-preview-player')
        assert await buttons.count() == 4
        assert await buttons.nth(3).is_disabled()
        assert await player.get_attribute('src') is None
        assert not requests, 'Opening the dialog fetched or played audio'
        await page.click('[data-select-speaker="S02"]')
        await page.keyboard.press('Enter')
        await page.keyboard.press('ArrowDown')
        assert not requests, 'Speaker selection/navigation started playback'
        await buttons.first.click()
        await page.wait_for_function('''() => {const p=document.querySelector('#speaker-preview-player');return !p.paused && p.currentTime>=.4;}''')
        assert await buttons.first.get_attribute('aria-pressed') == 'true'
        await page.wait_for_function('document.querySelector("#speaker-preview-player").paused')
        assert await player.evaluate('p=>p.currentTime<=1.25')
        assert await buttons.first.get_attribute('aria-pressed') == 'false'
        assert requests and all('?playback=true' in url for url in requests)
        await buttons.nth(1).click()
        await page.wait_for_function('document.querySelector("#speaker-preview-player").currentTime>=2')
        await buttons.nth(1).click()
        assert await player.evaluate('p=>p.paused')
        await buttons.nth(1).click()
        await page.wait_for_function('!document.querySelector("#speaker-preview-player").paused')
        await buttons.nth(2).click()
        await page.wait_for_function('''() => {const p=document.querySelector('#speaker-preview-player');return !p.paused && p.currentTime>=6;}''')
        assert await buttons.nth(1).get_attribute('aria-pressed') == 'false'
        assert await buttons.nth(2).get_attribute('aria-pressed') == 'true'
        await page.click('[data-select-speaker="S01"]')
        assert await player.evaluate('p=>p.paused')
        await page.fill('#speaker-name', '확인 중인 이름')
        assert await page.locator('[data-preview-name]').first.inner_text() == '확인 중인 이름'
        assert await buttons.first.inner_text() == '재생'
        await page.keyboard.press('End')
        await page.keyboard.press('Space')
        assert (await page.locator('#speaker-name').input_value()).endswith(' ')
        assert await player.evaluate('p=>p.paused'), 'Typing a space started playback'
        await page.locator('#speaker-preview').focus()
        await page.keyboard.press('ArrowDown')
        assert await page.locator('#speaker-preview .is-current').get_attribute('data-preview-index') == '2'
        await page.keyboard.down('Space')
        await page.wait_for_function('''() => {const p=document.querySelector('#speaker-preview-player');return !p.paused && p.currentTime>=6;}''')
        await page.keyboard.down('Space')  # key repeat must not stop/start repeatedly
        assert await buttons.nth(2).get_attribute('aria-pressed') == 'true'
        await page.keyboard.up('Space')
        await page.keyboard.press('Space')
        assert await player.evaluate('p=>p.paused')
        await page.keyboard.press('Space')
        await page.wait_for_function('!document.querySelector("#speaker-preview-player").paused')
        await page.keyboard.press('ArrowUp')
        assert await player.evaluate('p=>p.paused'), 'Arrow navigation left the previous utterance playing'
        assert await page.locator('#speaker-preview .is-current').get_attribute('data-preview-index') == '0'
        await buttons.nth(2).focus()
        await page.keyboard.press('Space')
        await page.wait_for_function('''() => {const p=document.querySelector('#speaker-preview-player');return !p.paused && p.currentTime>=6;}''')
        await page.keyboard.press('Space')
        assert await player.evaluate('p=>p.paused'), 'Focused play button toggled twice on Space'
        for width in (1440, 1100, 390):
            await page.set_viewport_size({'width':width, 'height':1000})
            assert await page.locator('#speaker-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
            assert await page.locator('#speaker-preview').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
            await page.locator('#speaker-dialog').screenshot(path=str(screenshot.with_stem(screenshot.stem+'-'+str(width))))
        await page.set_viewport_size({'width':1440, 'height':1000})
        await buttons.nth(1).click()
        await page.wait_for_function('!document.querySelector("#speaker-preview-player").paused')
        await page.click('#cancel-speakers')
        await page.wait_for_function('''() => {const p=document.querySelector('#speaker-preview-player');return p.paused && !p.getAttribute('src');}''')
        assert await player.evaluate('p=>p.paused') and await player.get_attribute('src') is None
        assert store.get(identity)['speaker_names'] == {'S01':'민수', 'S02':'지민'}
        assert store.get(identity)['segments'] == segments and not writes
        await page.click('#edit-speakers')
        await page.route('**/api/notes/'+identity+'/media?*', lambda r:r.fulfill(status=404))
        await buttons.first.click()
        await page.wait_for_function('document.querySelector("#toast").textContent.includes("재생하지 못했습니다")')
        assert await player.evaluate('p=>p.paused')
        assert await buttons.first.get_attribute('aria-pressed') == 'false'
        assert source.read_bytes() == b'original fixture; use the prepared WAV for playback'
        assert not errors, errors
        await browser.close()
    print(engine, 'PASS: click/Space, actual WAV, seek/end/stop/switch/close, arrows, typing/repeat isolation, draft preservation, error recovery, responsive UI')


if __name__ == '__main__':
    asyncio.run(main())
