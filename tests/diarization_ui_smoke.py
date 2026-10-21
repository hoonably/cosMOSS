"""UI fixtures with real subset approval/file persistence; no inference in this test."""
import asyncio
import json
from pathlib import Path
import sys
import uuid
import wave

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.diarization import MODELS, fingerprint, proposals
from src.store import NoteStore
from src.text_files import write_text
from ui_helpers import choose_option


async def main():
    base, data, png, engine = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
    assert 'cosmoss-qa' in data.parts
    store = NoteStore(data / 'moss-note.sqlite3')
    identity = uuid.uuid4().hex
    folder = store.upload_directory(identity); folder.mkdir(parents=True)
    audio = folder / 'fixture.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1,2,16000,0,'NONE','not compressed')); stream.writeframes(b'\0\1' * 16000 * 24)
    note = store.create(dict(id=identity,title='화자 모델 비교',original_filename=audio.name,status='done',source_path=str(audio),duration=24,device_id='remote'))
    segments = [dict(id=i,start=i*4,end=i*4+4,speaker=s,text='서로 다른 목소리가 포함된 테스트 발화입니다. ' + str(i))
                for i,s in enumerate(['S01','S02','S01','S02','S02','S02'])]
    turns = [dict(start=i*4,end=i*4+4,speaker=s) for i,s in enumerate(['a','b','a','c','c','b'])]
    note = store.update(identity,segments=segments,raw_segments=segments,speaker_names={'S01':'화자 1','S02':'화자 2'})
    output = data / 'output'; output.mkdir(exist_ok=True)
    note = store.update(identity,**write_text(note,str(output)))
    original = Path(note['text_path']).read_bytes()
    result = {**proposals(note,turns), 'proposal_id':uuid.uuid4().hex,'source_fingerprint':fingerprint(note),
              'inference_seconds':34,'total_seconds':40,'peak_reserved_gib':2.1}
    (data/'diarization').mkdir(exist_ok=True)
    (data/'diarization'/f'{identity}.json').write_text(json.dumps({'phase':'done','result':result}))
    running, prepared, errors, submitted = False, False, [], []
    async with async_playwright() as pw:
        args = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw,engine).launch(headless=True,**args)
        page = await browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=2)
        page.set_default_timeout(12000)
        page.on('pageerror',lambda error:errors.append(str(error)))
        await page.add_init_script('window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>(' + json.dumps({'path':str(output),'name':'output'}) + ')}}};')
        await page.route('**/api/devices',lambda r:r.fulfill(json=[{'id':'remote','name':'GPU 서버','host':'test-host','kind':'ssh','backend':'cuda'}]))
        await page.route('**/api/local-model',lambda r:r.fulfill(json={'phase':'idle'}))
        await page.route('**/api/setup',lambda r:r.fulfill(json={'ready':True,'phase':'ready'}))
        await page.route('**/api/health',lambda r:r.fulfill(json={'moss':{'ok':False},'max_upload_bytes':5*1024**3,'default_chunk_seconds':1800}))
        await page.route('**/api/summary/providers',lambda r:r.fulfill(json=[]))
        await page.route('**/api/text-library**',lambda r:r.fulfill(json=[{'text_path':note['text_path']}]))
        await page.route('**/api/diarization/models',lambda r:r.fulfill(json={'models':MODELS,'hosts':['@local','test-host'],'default_model_key':'nemotron-3'}))
        async def setup(route):
            nonlocal prepared
            if route.request.method == 'POST': prepared=True
            phase = 'installed' if prepared or 'nemotron-3' in route.request.url else 'auth_required'
            await route.fulfill(json={'phase':phase, **({'error':'이용 조건에 동의해 주세요.'} if phase=='auth_required' else {})})
        await page.route('**/api/diarization/setup**',setup)
        await page.route('**/api/ssh/probe',lambda r:r.fulfill(json={'gpus':[{'index':i,'uuid':'GPU-'+str(i),'name':'NVIDIA H200','memory_total_mib':143771,'memory_used_mib':0,'idle_candidate':True} for i in range(4)]}))
        async def jobs(route):
            nonlocal running
            if route.request.method=='POST':
                submitted.append(route.request.post_data_json);running=True
                return await route.fulfill(status=202,json={'phase':'running'})
            await route.fulfill(json={'phase':'done','result':result} if running else {'phase':'idle'})
        await page.route(f'**/api/notes/{identity}/diarization',jobs)
        await page.goto(base)
        await page.locator(f'.note-item[data-id="{identity}"]').click()
        await page.click('#open-diarization')
        await page.locator('.diarization-gpu-choice input').first.wait_for()
        assert await page.locator('#diarization-model').input_value()=='nemotron-3'
        assert await page.locator('#diarization-model option').count()==3
        assert await page.locator('#run-diarization').is_disabled()
        assert await page.locator('#diarization-count').is_disabled()
        assert await page.locator('#diarization-count').input_value()==''
        await choose_option(page,'#diarization-model','pyannote-community-1')
        assert await page.locator('#diarization-count').is_disabled()
        await choose_option(page,'#diarization-model','pyannote-community-1-fixed')
        await page.wait_for_function('document.querySelector("#diarization-auth-help").open')
        await page.wait_for_function('document.querySelector("#diarization-auth-help img").complete && document.querySelector("#diarization-auth-help img").naturalWidth>0')
        assert await page.locator('#diarization-count').is_enabled()
        await page.fill('#diarization-count','3')
        await page.click('#prepare-diarization')
        await page.locator('#diarization-setup-status').filter(has_text='모델 준비 완료').wait_for()
        await page.locator('.diarization-gpu-choice input').first.check()
        await page.click('#run-diarization')
        await page.locator('[data-diarization-change]').first.wait_for()
        assert submitted[0]['model_key']=='pyannote-community-1' and submitted[0]['num_speakers']==3
        assert await page.locator('[data-diarization-change]').count()==2
        assert await page.locator('#diarization-voices article').count()==3
        await page.locator('#diarization-voices [data-diarization-play]').first.click()
        await page.wait_for_function('document.querySelector("#diarization-player").readyState>=1')
        assert await page.locator('[data-diarization-change]:checked').count()==0
        assert await page.locator('#approve-diarization').is_disabled()
        assert Path(note['text_path']).read_bytes()==original
        await page.click('#close-diarization')
        await page.click('#open-diarization')
        await page.locator('[data-diarization-change]').first.wait_for()
        assert await page.locator('[data-diarization-change]:checked').count()==0
        await page.locator('[data-diarization-change]').first.check()
        for width in (1440,1100,390):
            await page.set_viewport_size({'width':width,'height':1000})
            assert await page.locator('#diarization-dialog').evaluate('el=>el.scrollWidth<=el.clientWidth+1')
            await page.locator('#diarization-dialog').screenshot(path=str(png.with_stem(png.stem+'-'+str(width))))
        await page.click('#approve-diarization')
        await page.locator('#diarization-dialog').wait_for(state='hidden')
        saved=store.get(identity)
        assert saved['raw_segments']==segments
        assert saved['segments'][3]['speaker']=='S03' and saved['segments'][4]['speaker']=='S02'
        assert [s['text'] for s in saved['segments']]==[s['text'] for s in segments]
        assert Path(note['text_path']).read_bytes()!=original
        await page.click('#open-diarization')
        await choose_option(page,'#diarization-host','@local')
        await page.wait_for_function('document.querySelector("#diarization-host").value==="@local" && document.querySelector("#diarization-model option[value=pyannote-community-1]").disabled')
        await page.wait_for_function('!document.querySelector("#run-diarization").disabled')
        assert await page.locator('#diarization-model').input_value()=='nemotron-3'
        assert await page.locator('#diarization-gpu-head').is_hidden()
        await page.click('#run-diarization')
        await page.locator('[data-diarization-change]').first.wait_for()
        assert submitted[-1]['host']=='@local' and submitted[-1]['gpu_uuid']=='local'
        assert submitted[-1]['model_key']=='nemotron-3' and submitted[-1]['num_speakers'] is None
        await page.click('#close-diarization')
        assert not errors,errors
        await browser.close()
    print('PASS',engine,'Nemotron default, 3 presets, Mac selection, auth guide, explicit preparation, GPU/count selection, unchecked preview, cancel, subset save, raw preservation, 1440/1100/390px')


if __name__=='__main__':asyncio.run(main())
