"""Proposal/approval/save boundaries against isolated note and output-file APIs.

Usage: uv run --no-project --with playwright python tests/voice_review_ui_smoke.py
URL QA_DATA_DIR PNG [chromium|webkit]. Match scores and audio are format fixtures;
this exercises UI/persistence, not voice recognition accuracy.
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
    engine = sys.argv[4] if len(sys.argv) > 4 else 'chromium'
    assert 'cosmoss-qa' in data.parts, 'Use an isolated QA data directory'
    store = NoteStore(data / 'moss-note.sqlite3')
    identity = uuid.uuid4().hex
    folder = store.upload_directory(identity)
    folder.mkdir(parents=True)
    source = folder / 'format-fixture.wav'
    with wave.open(str(source), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x01' * (16000 * 20))
    segments = [dict(id=index, start=index * 5, end=index * 5 + 5, speaker=speaker, text=text)
                for index, (speaker, text) in enumerate([('S01', '첫 번째 화자의 발언입니다.'),
                    ('S02', '두 번째 화자의 발언입니다.'), ('S03', '첫 번째 화자와 같은 목소리 후보입니다.')])]
    result = {'matches': [dict(speaker=speaker, name=name, similarity=score, recommended=True,
                              samples=[{'start':index * 5, 'end':index * 5 + 5}])
              for index, (speaker, name, score) in enumerate([('S01', '민수', .94), ('S02', '지민', .88), ('S03', '민수', .93)])],
              'merge_candidates': [dict(source='S03', target='S01', similarity=.95, recommended=True,
                  source_samples=[{'start':10, 'end':15}], target_samples=[{'start':0, 'end':5}])]}
    note = store.create(dict(id=identity, title='목소리 추천 승인 테스트', status='done', source_path=str(source),
                             normalized_path=str(source), original_filename=source.name, duration=20))
    output = data / 'output'
    note = store.update(identity, segments=segments, raw_segments=segments,
                        speaker_names={speaker:speaker for speaker in ('S01', 'S02', 'S03')},
                        voice_matches=result['matches'], voice_merge_candidates=result['merge_candidates'])
    note = store.update(identity, **write_text(note, str(output)))
    original_text = Path(note['text_path']).read_bytes()
    saved, compared, errors = [], [], []
    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width':1440,'height':1000}, device_scale_factor=2)
        page.set_default_timeout(10000)
        page.on('pageerror', lambda error:errors.append(str(error)))
        page.on('request', lambda request:saved.append(request.post_data_json)
                if request.method == 'POST' and request.url.endswith('/speakers') else None)
        await page.add_init_script('window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>(' +
                                  json.dumps({'path':str(output),'name':'output'}) + ')}}};')
        await page.route('**/api/devices', lambda route:route.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','setup':{'phase':'idle'}}]))
        await page.route('**/api/local-model', lambda route:route.fulfill(json={'phase':'idle'}))
        await page.route('**/api/summary/providers', lambda route:route.fulfill(json=[]))
        await page.route('**/api/text-library**', lambda route:route.fulfill(json=[{'text_path':note['text_path']}]))
        async def identify(route):
            compared.append(route.request.post_data_json)
            await route.fulfill(json=result)
        await page.route('**/identify-speakers', identify)
        await page.goto(base)
        await page.locator(f'.note-item[data-id="{identity}"]').click()
        await page.click('#edit-speakers')
        await page.click('#identify-voice-speakers')
        await page.locator('#voice-review-dialog').wait_for(state='visible')
        assert not compared, 'Cached proposals unexpectedly ran identification again'
        assert await page.locator('[data-voice-proposal]:checked').count() == 0
        assert await page.locator('#approve-voice-review').is_disabled()
        assert await page.locator('#voice-review-before-count').inner_text() == '3명'
        assert await page.locator('#voice-review-after-count').inner_text() == '3명'
        assert await page.locator('#speaker-name').input_value() == 'S01'
        assert not saved
        await page.locator('[data-voice-sample]').first.click()
        await page.wait_for_function('document.querySelector("#voice-review-player").readyState >= 1')
        await page.keyboard.press('Escape')
        assert await page.locator('#speaker-dialog').is_visible()
        assert await page.locator('#voice-review-player').get_attribute('src') is None
        await page.click('#identify-voice-speakers')
        name_card = lambda speaker:page.locator('[data-voice-kind="name"]').filter(has_text=speaker)
        await name_card('S01').locator('input').check()
        await page.locator('[data-voice-kind="merge"] input').check()
        assert await page.locator('#voice-review-after-count').inner_text() == '2명'
        assert '민수' in await page.locator('#voice-review-after').inner_text()
        assert '지민' not in await page.locator('#voice-review-after').inner_text()
        for width in (1440, 1100, 390):
            await page.set_viewport_size({'width':width, 'height':1000})
            assert await page.locator('#voice-review-dialog').evaluate('el=>el.scrollWidth<=el.clientWidth+1')
            footer = await page.locator('#approve-voice-review').bounding_box()
            assert 0 <= footer['y'] and footer['y'] + footer['height'] <= 1000
            await page.locator('#voice-review-dialog').screenshot(path=str(screenshot.with_stem(screenshot.stem + '-' + str(width))))
        await page.set_viewport_size({'width':1440, 'height':1000})
        await page.click('#approve-voice-review')
        assert await page.locator('.speaker-choice').count() == 2
        assert await page.evaluate('state.speakerDraft.names.S01') == '민수'
        assert await page.evaluate('state.speakerDraft.names.S02') == 'S02'
        assert store.get(identity)['speaker_merges'] == {}
        assert Path(note['text_path']).read_bytes() == original_text
        assert not saved
        await page.click('#cancel-speakers')
        await page.click('#edit-speakers')
        assert await page.locator('.speaker-choice').count() == 3
        assert await page.locator('#speaker-name').input_value() == 'S01'
        await page.click('#identify-voice-speakers')
        assert await page.locator('[data-voice-proposal]:checked').count() == 0
        await name_card('S02').locator('input').check()
        await page.click('#approve-voice-review')
        await page.click('#save-speakers')
        await page.locator('#speaker-dialog').wait_for(state='hidden')
        persisted = store.get(identity)
        assert persisted['speaker_names'] == {'S01':'S01', 'S02':'지민', 'S03':'S03'}
        assert persisted['speaker_merges'] == {}
        assert '지민' in Path(note['text_path']).read_text() and '민수' not in Path(note['text_path']).read_text()
        await page.click('#edit-speakers')
        await page.click('#identify-voice-speakers')
        await page.locator('[data-voice-kind="merge"] input').check()
        await page.click('#approve-voice-review')
        await page.click('#save-speakers')
        await page.locator('#speaker-dialog').wait_for(state='hidden')
        persisted = store.get(identity)
        assert persisted['speaker_merges'] == {'S03':'S01'}
        assert persisted['raw_segments'] == segments
        assert [segment['speaker'] for segment in persisted['segments']] == ['S01','S02','S01']
        assert '민수' not in Path(note['text_path']).read_text()
        await page.click('#edit-speakers')
        await page.fill('#speaker-name', '직접 지정한 이름')
        await page.evaluate('compareSpeakerVoices(true)')
        await page.locator('#voice-review-dialog').wait_for(state='visible')
        assert compared == [{'speaker_merges':{}}]
        assert await page.locator('#speaker-name').input_value() == '직접 지정한 이름'
        assert '직접 지정한 이름' in await page.locator('#voice-review-before').inner_text()
        assert await page.locator('[data-voice-kind="merge"]').count() == 0
        await page.click('#cancel-voice-review')
        result.clear()
        await page.evaluate('compareSpeakerVoices(true)')
        await page.locator('#voice-review-dialog').wait_for(state='visible')
        assert await page.locator('.voice-review-empty').is_visible()
        assert await page.locator('#approve-voice-review').is_disabled()
        await page.click('#cancel-voice-review')
        await page.click('#cancel-speakers')
        assert store.get(identity)['speaker_names']['S01'] == 'S01'
        assert len(saved) == 2
        assert not errors, errors
        await browser.close()
    print(f'PASS {engine}: unchecked recommendations, before/after, sample playback, partial approval, cancel, explicit save, merge persistence, original preservation, 1440/1100/390px')


if __name__ == '__main__':
    asyncio.run(main())
