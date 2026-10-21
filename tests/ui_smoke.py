"""Isolated browser smoke: register a host, prefetch, choose a GPU per job.

uv run --no-project --with playwright python tests/ui_smoke.py URL [PNG] [chromium|webkit]
SSH, local/remote model installation, and note submission use browser fixtures.
"""

import asyncio
import io
import sys
import time
import wave
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default

from playwright.async_api import async_playwright

from ui_helpers import choose_option


GPU_UUID = "GPU-11111111-2222-3333-4444-555555555555"
GPU_OTHER = "GPU-aaaaaaaa-2222-3333-4444-555555555555"


async def main():
    base = sys.argv[1]
    screenshot = sys.argv[2] if len(sys.argv) > 2 else None
    engine = sys.argv[3] if len(sys.argv) > 3 else "chromium"
    calls = 0
    busy = False
    busy_revision = 42
    worker_polls = 0
    remote_prepared = []
    remote_active = False
    remote_poll_times = []
    local_polls = 0
    targets = [{"id": "local", "name": "이 Mac", "kind": "local", "backend": "mlx", "model": "moss-mtd", "remote_port": 8001}]
    added = None
    submitted = None
    notes = []
    host_requests = 0
    local_setup = {"phase": "idle", "source": "cache", "model_id": "vanch007/mlx-MOSS-Transcribe-Diarize-8bit", "cache_dir": "/Users/tester/.cache/huggingface/hub", "suggested_path": ""}
    local_prepared = []
    memo_imports = []
    memo_lists = 0
    memo = {'id': 'e' * 64, 'version': 'f' * 64, 'title': '음성 메모 연구 미팅',
            'created_at': '2026-10-16T01:53:50+00:00', 'duration': 65, 'size': 1000, 'available': True}
    async with async_playwright() as playwright:
        kwargs = {"executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"} if engine == "chromium" else {}
        browser = await getattr(playwright, engine).launch(headless=True, **kwargs)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=2)
        catalogue = await (await page.request.get(base + '/api/model-catalog')).json()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        async def hosts(route):
            nonlocal host_requests
            host_requests += 1
            await route.fulfill(json={"hosts": ["lab-3090"]})

        await page.route("**/api/ssh/hosts", hosts)

        async def devices(route):
            nonlocal added, remote_active, worker_polls
            if route.request.method == "POST":
                added = route.request.post_data_json
                target = {**added, "id": "b" * 32, "kind": "ssh", "setup": {"phase": "idle"}}
                remote_active = True
                worker_polls = 0
                targets.append(target)
                await route.fulfill(status=201, json=target)
            else:
                await route.fulfill(json=targets)

        def progress(percent):
            return {"phase": "downloading", "download": {"percent": percent, "downloaded_bytes": percent * 1024 ** 2, "total_bytes": 100 * 1024 ** 2}}

        async def worker(route):
            nonlocal worker_polls
            if not remote_active:
                return await route.fulfill(json={"phase": "idle"})
            remote_poll_times.append(time.monotonic())
            worker_polls += 1
            result = progress(25 if worker_polls == 1 else 50) if worker_polls < 3 else {
                "phase": "installed", "model_cache": "/home/tester/.cache/huggingface/hub", "reused": False,
            }
            targets[-1]["setup"] = result
            await route.fulfill(json=result)

        async def remote_model(route):
            nonlocal remote_active, worker_polls
            if route.request.method == 'POST':
                values = route.request.post_data_json
                remote_prepared.append(values)
                remote_active = True
                worker_polls = 0
                targets[-1]["setup"] = progress(25)
                targets[-1].update(model_key=values['model_key'], options=values['options'])
                await route.fulfill(status=202, json=targets[-1]["setup"])
            else:
                saved = remote_prepared[-1] if remote_prepared else {"source": "cache", "path": ""}
                await route.fulfill(json={**catalogue, "recommendation": {"model_key": "cuda-original", "options": {**catalogue['recommendation']['options'], "chunk_minutes":30}},
                                         "profiles": [{"name":"RTX 3090", "index":0,"memory_gib":24,"model_key":"cuda-original","options":{**catalogue['recommendation']['options'],"chunk_minutes":30}}],
                                         **targets[-1]['setup'], "source": saved['source'], "suggested_path": saved['path'],
                                         "cache_dir": "/home/tester/.cache/huggingface/hub", "model_key": targets[-1].get('model_key', 'cuda-original'),
                                         "options": targets[-1].get('options', {}),
                                         "hardware": {"gpus": [{"index":0,"name":"RTX 3090","memory_gib":24}]}})

        await page.route('**/api/devices/*/model', remote_model)
        await page.route('**/api/devices/*/model/prepare', remote_model)

        await page.route("**/api/devices", devices)
        await page.route("**/api/devices/*/worker", worker)
        await page.route("**/api/devices/*/status", lambda route: route.fulfill(json={"ok": True, "label": "모델 준비 완료"}))

        async def local_model(route):
            nonlocal local_polls
            if route.request.method == "POST":
                values = route.request.post_data_json
                local_prepared.append(values)
                local_polls = 0
                local_setup.update(phase="loading", source=values["source"], suggested_path=values["path"], model_key=values['model_key'], options=values['options'])
                targets[0].update(model_key=values['model_key'], options=values['options'])
                if values['source'] == 'cache':
                    local_setup.update(progress(25))
                await route.fulfill(status=202, json=local_setup)
            else:
                if local_setup['phase'] == 'downloading':
                    local_polls += 1
                    local_setup.update(progress(50))
                    if local_polls >= 2:
                        local_setup['phase'] = 'ready'
                elif local_setup["phase"] == "loading":
                    local_setup["phase"] = "ready"
                await route.fulfill(json={**catalogue, **local_setup})

        await page.route("**/api/local-model", local_model)
        await page.route("**/api/local-model/prepare", local_model)
        await page.add_init_script("""
            const readLocation = () => JSON.parse(localStorage.getItem('test-save-location') || '{"path":"/Users/tester/Downloads","name":"Downloads"}');
            window.webkit = {messageHandlers: {
                openVoiceMemos: {postMessage: async () => true},
                openVoiceMemoPrivacy: {postMessage: async () => { window.__openedMemoPrivacy = true; return true; }},
                requestVoiceMemosAccess: {postMessage: async () => { window.__memoAccessChecks = (window.__memoAccessChecks || 0) + 1; return Boolean(window.__memoAccessAllowed); }},
                chooseModelFolder: {postMessage: async () => '/Users/tester/Models/moss mlx'},
                getSaveLocation: {postMessage: async () => readLocation()},
                chooseSaveLocation: {postMessage: async () => {
                    if (window.__cancelSaveLocation) return null;
                    const location = {path: '/Users/tester/Documents/노트', name: '노트'};
                    localStorage.setItem('test-save-location', JSON.stringify(location));
                    return location;
                }}
            }};
        """)

        def occupied_gpu():
            return {"index": 2, "uuid": GPU_OTHER, "name": "NVIDIA GeForce RTX 3090", "idle_candidate": False,
                    "memory_used_mib": 12000, "memory_total_mib": 24576, "memory_free_mib": 12576,
                    "utilization_percent": 90, "process_info_known": True, "busy_confirmation": f"busy-{busy_revision}",
                    "processes": [{"pid": str(busy_revision), "name": "gpu_dummy.py", "type": "C", "memory_mib": 12000}]}

        async def probe(route):
            nonlocal calls
            calls += 1
            gpu = {"index": 0, "uuid": GPU_UUID, "name": "NVIDIA GeForce RTX 3090", "memory_free_mib": 23000,
                   "memory_used_mib": 4, "memory_total_mib": 24576, "utilization_percent": min(calls, 4), "processes": [],
                   "process_info_known": True, "idle_candidate": True}
            await route.fulfill(json={
                "host": "lab-3090", "reachable": True, "gpu_supported": True,
                "checked_at": datetime.now(timezone.utc).isoformat(), "message": "현재 상태이며 GPU 예약이 아닙니다.",
                "gpus": [gpu, occupied_gpu() if busy else {**gpu, "index": 2, "uuid": GPU_OTHER,
                                                         "memory_used_mib": 0, "utilization_percent": 0}],
            })

        async def uploads(route):
            await route.fulfill(status=202, json={"id": "c" * 32, "status": "ready", "duration": 1.0})

        async def note_list(route):
            nonlocal submitted
            if route.request.method == "POST":
                raw = b"Content-Type: " + route.request.headers["content-type"].encode() + b"\r\n\r\n" + route.request.post_data_buffer
                form = BytesParser(policy=default).parsebytes(raw)
                values = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode() for part in form.iter_parts()}
                if busy and values.get('gpu_uuid') == GPU_OTHER and values.get('gpu_confirmation') != f'busy-{busy_revision}':
                    return await route.fulfill(status=409, json={'detail': {'code': 'gpu_confirmation_required', 'message': 'GPU 사용 확인 필요', 'gpu': occupied_gpu()}})
                submitted = values
                note = {"id": "d" * 32, "title": "회의", "original_filename": "voice.wav", "status": "queued", "created_at": datetime.now(timezone.utc).isoformat(),
                        "device_id": "b" * 32, "device_name": "lab-3090 · GPU #2", "segments": [], "speaker_names": {}, "duration": 1,
                        "chunk_seconds": 1800, "progress_phase": "queued"}
                notes.append(note)
                await route.fulfill(status=202, json=note)
            else:
                await route.fulfill(json=notes)

        await page.route("**/api/devices/*/gpus", probe)
        await page.route("**/api/uploads", uploads)
        async def list_memos(route):
            nonlocal memo_lists
            memo_lists += 1
            allowed = await page.evaluate('Boolean(window.__memoAccessAllowed)')
            await route.fulfill(json={
                'recordings': [memo, {**memo, 'id': 'd' * 64, 'title': 'iCloud 녹음', 'available': False}] if allowed else [],
                'permission_required': not allowed, 'metadata_unavailable': False,
            })
        await page.route('**/api/voice-memos', list_memos)
        async def import_memo(route):
            memo_imports.append(route.request.post_data_json)
            await uploads(route)
        await page.route('**/api/voice-memos/import', import_memo)
        await page.route("**/api/uploads/*", lambda route: route.fulfill(status=204))
        await page.route("**/api/notes", note_list)
        await page.route("**/api/notes/" + "d" * 32, lambda route: route.fulfill(json=notes[0]))
        await page.route("**/api/summary/providers", lambda route: route.fulfill(json=[
            {"id": "codex", "name": "Codex", "installed": True, "ready": True, "message": "로그인됨"},
            {"id": "claude", "name": "Claude", "installed": True, "ready": True, "message": "로그인됨"},
        ]))
        await page.route("**/api/notes/*/summary", lambda route: route.fulfill(json={"phase": "idle", "default_prompt": "노트를 요약해 주세요."}))
        await page.route('**/api/text-library**', lambda route: route.fulfill(json=[]))
        await page.goto(base)
        await page.locator('[data-device-id="local"] .default-device').wait_for()
        assert not await page.locator("#device-dialog").evaluate("el => el.open")
        assert await page.locator("#device-connections").count() == 0
        assert '새로운 장치 연결' in await page.locator('#stage-device .sidebar-settings #manage-devices').inner_text()
        assert await page.locator('.topbar-actions #save-location').count() == 1
        assert await page.locator('#device-panel-summary').is_hidden()
        assert await page.get_by_text('첫 노트를 만들어 보세요', exact=True).count() == 0
        assert await page.locator('.summary-intro, .device-panel-footnote').count() == 0
        assert await page.locator("#device-select, #chunk-minutes, #manage-upload-devices").count() == 0
        assert host_requests == 0, "Default Mac flow loaded optional SSH configuration"
        assert await page.locator('#open-upload, #welcome-upload').count() == 0
        assert (await page.locator('[data-device-id="local"] .use-device').inner_text()).strip() == '노트 만들기'
        await page.wait_for_function('document.querySelector(\'[data-device-id="local"] .use-device\')?.offsetHeight === 44')
        default_copy = await page.locator("body").inner_text()
        assert not any(word in default_copy for word in ["서버", "SSH", "GPU"]), default_copy
        assert await page.locator('#save-location-name').inner_text() == 'Downloads'
        assert await page.locator('#review-path-note').text_content() == '기본 경로에 있는 txt, srt, vtt 파일을 불러옵니다.'
        await page.click('#save-location')
        await page.wait_for_function("document.querySelector('#save-location-name').textContent === '노트'")
        assert '/Users/tester/Documents/노트' in await page.locator('#save-location').get_attribute('title')
        assert await page.locator('#review-path-note').text_content() == '기본 경로에 있는 txt, srt, vtt 파일을 불러옵니다.'
        assert await page.locator('#summary-path-note').text_content() == '기본 경로에 AI 요약 md파일을 저장합니다.'
        await page.evaluate('window.__cancelSaveLocation = true')
        await page.click('#save-location')
        assert await page.locator('#save-location-name').inner_text() == '노트'
        await page.reload()
        await page.wait_for_function("document.querySelector('#save-location-name').textContent === '노트'")
        assert await page.locator('#save-location').is_enabled()

        assert await page.locator('[data-device-id="local"] [data-device-action="status"]').count() == 0
        await page.click('[data-device-id="local"] [data-device-action="local-model"]')
        await page.locator('#prepare-local-model:not([disabled])').wait_for()
        assert await page.locator('#model-choice').input_value() == 'mlx-8bit'
        assert await page.locator('#model-choice option[value="cuda-original"]').evaluate('el => el.disabled')
        await choose_option(page, '#model-choice', 'mlx-4bit')
        await page.locator('#model-knobs > summary').click()
        await page.locator('[data-knob="chunk_minutes"]').fill('10')
        await choose_option(page, '[data-knob="kv_bits"]', '0')
        await choose_option(page, '[data-knob="prefill_step_size"]', '256')

        assert await page.locator('#model-sources input[value="cache"]').is_checked()
        assert await page.locator('#model-cache-path').inner_text() == local_setup['cache_dir']
        await page.locator('.model-source:has(input[value="folder"])').click()
        await page.click('#choose-model-folder')
        assert await page.locator('#model-folder-path').input_value() == '/Users/tester/Models/moss mlx'
        assert await page.locator('#model-folder-path').evaluate('el => el.offsetHeight') == 44
        await page.click('#prepare-local-model')
        await page.wait_for_function("state.localModel?.phase === 'ready' && !state.localModelSubmitting")
        assert await page.locator('#model-preparation').is_hidden()
        assert local_prepared[-1]['source'] == 'folder' and local_prepared[-1]['path'] == '/Users/tester/Models/moss mlx'
        assert local_prepared[-1]['model_key'] == 'mlx-4bit'
        assert local_prepared[-1]['options']['kv_bits'] == 0
        assert local_prepared[-1]['options']['prefill_step_size'] == 256

        await page.click('#close-local-model')
        await page.click('[data-device-id="local"] [data-device-action="local-model"]')
        await page.wait_for_function("document.querySelector('#model-sources input[value=folder]').checked")
        await page.locator('.model-source:has(input[value="cache"])').click()
        await page.click('#prepare-local-model')
        await page.locator('#model-download-progress strong').wait_for()
        assert await page.locator('#model-download-progress strong').inner_text() == '25.0%'
        await page.wait_for_function("document.querySelector('#model-download-progress strong')?.textContent === '50.0%'")
        await page.wait_for_function("state.localModel?.phase === 'ready' && !state.localModelSubmitting")
        assert await page.locator('#model-preparation').is_hidden()
        assert local_prepared[-1]['source'] == 'cache' and local_prepared[-1]['path'] == ''
        await page.click('#close-local-model')
        await page.locator("#manage-devices").click()
        await page.locator('[data-ssh-host="lab-3090"]').wait_for()
        assert await page.locator('#device-dialog #ssh-host-candidates').is_visible()
        if screenshot:
            await page.screenshot(path=screenshot.replace('.png', '-connect.png'))
        await page.click('[data-ssh-host="lab-3090"]')
        await page.wait_for_function("document.querySelector('#ssh-host').value === 'lab-3090' && document.querySelector('#device-name').value === 'lab-3090'")
        assert await page.locator("#ssh-host").input_value() == "lab-3090"
        assert await page.locator("#device-name").input_value() == "lab-3090"
        assert await page.locator("#add-device").is_enabled()
        assert await page.locator('#device-dialog input[name="selected-gpu"]').count() == 0
        assert calls == 0, "Registration must not probe/select GPUs"
        await page.click("#add-device")
        await page.locator('#device-dialog').wait_for(state='hidden')
        await page.wait_for_function("document.querySelector('[data-device-id=\"'+'b'.repeat(32)+'\"] .device-card-state').textContent.includes('모델 준비됨')")
        assert not await page.locator('#local-model-dialog').evaluate('el=>el.open'), 'Registration should prepare automatically'
        assert calls == 0, 'Automatic preparation must not probe or load a GPU'
        await page.locator('[data-device-id="' + 'b' * 32 + '"] [data-device-action="install"]').click()
        await page.locator('#local-model-dialog[open] #prepare-local-model:not([disabled])').wait_for()
        assert remote_prepared == [], 'Automatic setup should not submit the manual model form'
        assert await page.locator('#model-choice').input_value() == 'cuda-original'
        assert await page.locator('#model-choice option[value="mlx-8bit"]').evaluate('el => el.disabled')
        assert await page.locator('#model-choice option[value="cuda-8bit"], #model-choice option[value="cuda-4bit"]').count() == 0
        await page.locator('#model-knobs > summary').click()
        await choose_option(page, '[data-knob="precision"]', 'bf16')
        assert await page.locator('[data-knob="precision"] option[value="int8"], [data-knob="precision"] option[value="int4"]').count() == 0
        await page.locator('[data-knob="chunk_minutes"]').fill('15')
        await choose_option(page, '[data-knob="attention"]', 'eager')
        assert await page.locator('[data-knob="cuda_kv_bits"]').count() == 0

        assert await page.locator('#model-sources input[value="cache"]').is_checked()
        assert await page.locator('#model-cache-path').inner_text() == '/home/tester/.cache/huggingface/hub'
        await page.locator('.model-source:has(input[value="folder"])').click()
        assert await page.locator('#choose-model-folder').is_hidden(), 'Remote folders must never open the Mac picker'
        assert 'lab-3090' in await page.locator('#model-folder-help').inner_text()
        await page.locator('#model-folder-path').fill('~/models/moss')
        await page.locator('.model-source:has(input[value="cache"])').click()
        await page.click('#prepare-local-model')
        await page.locator('#model-download-progress strong').wait_for()
        assert await page.locator('#model-download-progress strong').inner_text() == '25.0%'
        if screenshot:
            await page.screenshot(path=screenshot.replace('.png', '-model-progress.png'))
        await page.click('#close-local-model')
        await page.wait_for_function("document.querySelector('[data-device-id=\"" + 'b' * 32 + "\"] .model-download strong')?.textContent === '50.0%'")
        await page.locator('[data-device-id="' + "b" * 32 + '"] .device-card-state.ready').wait_for()
        assert remote_prepared[-1]['source'] == 'cache' and remote_prepared[-1]['path'] == ''
        intervals = [b - a for a, b in zip(remote_poll_times, remote_poll_times[1:])]
        assert all(.75 <= gap <= 1.4 for gap in intervals[:2]), intervals
        await page.click('[data-device-id="' + 'b' * 32 + '"] [data-device-action="install"]')
        await page.locator('#prepare-local-model:not([disabled])').wait_for()
        await page.locator('.model-source:has(input[value="folder"])').click()
        await page.locator('#model-folder-path').fill('~/models/moss')
        await page.click('#prepare-local-model')
        await page.wait_for_function("state.workerStates['b'.repeat(32)]?.phase === 'installed' && !state.localModelSubmitting")
        assert await page.locator('#model-preparation').is_hidden()
        assert 'GPU는 전사할 때' not in await page.locator('#local-model-dialog').inner_text()
        assert remote_prepared[-1]['source'] == 'folder' and remote_prepared[-1]['path'] == '~/models/moss'
        assert 'cuda_kv_bits' not in remote_prepared[-1]['options']
        assert remote_prepared[-1]['options']['precision'] == 'bf16'
        await page.click('#close-local-model')
        assert await page.locator("#device-dialog").is_hidden()
        assert await page.locator("#server-state").count() == 0
        assert await page.get_by_text("All connection attempts failed", exact=False).count() == 0
        assert "gpu_uuid" not in added and "gpu_index" not in added
        assert added["model_cache"] == "" and added["remote_port"] == 18400
        assert calls == 0, "Model preparation must not touch GPUs"
        presentations = await page.evaluate('''() => {
            const id = 'b'.repeat(32);
            const previous = state.workerStates[id];
            const result = [];
            for (const worker of [
                {phase:'error', action:'install'},
                {phase:'interrupted', action:'install'},
                {phase:'error', action:'prepare', worker_cleanup_pending:true},
                {phase:'ready', action:'prepare', active_gpu_uuid:'GPU-test'},
            ]) {
                state.workerStates[id] = worker;
                renderDevices();
                result.push(document.querySelector(`[data-device-id="${id}"] [data-device-action="stop"]`)?.textContent || '');
            }
            state.workerStates[id] = previous;
            renderDevices();
            return result;
        }''')
        assert presentations == ['', '', '작업 정리', 'GPU 해제'], presentations
        if screenshot:
            await page.locator(".device-panel").screenshot(path=screenshot.replace(".png", "-devices.png"))
        await page.locator('[data-device-id="' + "b" * 32 + '"] .use-device').click()
        assert await page.evaluate('state.executionDeviceId') == 'b' * 32
        await choose_option(page, '#output-format', 'srt')
        await page.locator('input[name="selected-gpu"]').first.wait_for()
        gpu_panel = await page.locator('#execution-gpu').bounding_box()
        details = await page.locator('.upload-details').bounding_box()
        dialog = await page.locator('#upload-dialog').bounding_box()
        assert dialog['width'] > 1000
        assert gpu_panel['x'] + gpu_panel['width'] <= details['x']
        assert abs(gpu_panel['y'] - details['y']) < 1
        submit = await page.locator('.submit-upload').bounding_box()
        assert abs(submit['x'] - details['x']) < 1
        assert 0 <= submit['y'] - details['y'] - details['height'] <= 20
        assert await page.locator('#probe-ssh-host').count() == 0
        assert '3초마다 갱신' in await page.locator('#execution-gpu-meta').inner_text()
        assert await page.locator('.gpu-table th').nth(1).inner_text() == '사용 / 전체 VRAM'
        assert await page.locator('.gpu-table tbody tr').nth(0).locator('td').nth(1).inner_text() == '4 MiB / 24576 MiB'
        assert await page.locator('.gpu-table tbody tr').nth(1).locator('td').nth(1).inner_text() == '0 MiB / 24576 MiB'
        data = io.BytesIO()
        with wave.open(data, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000)
        left = await page.locator('#drop-zone').bounding_box()
        right = await page.locator('#choose-voice-memo').bounding_box()
        assert right['x'] > left['x'] and abs(left['y'] - right['y']) < 1
        assert abs(left['width'] - right['width']) < 1 and abs(left['height'] - right['height']) < 1
        await page.click('#choose-voice-memo')
        await page.locator('#voice-memo-access').wait_for(state='visible')
        assert not memo_imports
        await page.click('#voice-memo-privacy')
        assert await page.evaluate('window.__openedMemoPrivacy')
        assert await page.locator('#reveal-current-app').count() == 0
        assert await page.evaluate('window.__memoAccessChecks >= 1')
        assert await page.locator('.voice-memo-empty').count() == 0
        await page.click('#refresh-voice-memos')
        await page.wait_for_function("document.querySelector('#voice-memo-status').textContent.includes('접근을 차단')")
        assert memo_lists == 2 and not memo_imports
        await page.evaluate("window.__memoAccessAllowed = true; window.dispatchEvent(new Event('cosmoss-voice-memo-permission-return'));")
        await page.locator('.voice-memo-item').first.wait_for()
        assert not memo_imports, 'Opening the picker imported a recording'
        assert await page.locator('#import-voice-memo').is_disabled()
        assert await page.locator('.voice-memo-item input:disabled').count() == 1
        await page.fill('#voice-memo-search', '없는 녹음')
        assert await page.locator('.voice-memo-empty').inner_text() == '검색 결과가 없습니다.'
        await page.fill('#voice-memo-search', '연구')
        await page.locator('.voice-memo-item').click()
        assert not memo_imports, 'Selecting a row imported before confirmation'
        if screenshot:
            await page.locator('#voice-memo-dialog').screenshot(path=screenshot.replace('.png', '-memo-picker.png'))
        await page.click('#close-voice-memo')
        assert await page.evaluate('state.uploadPreparation') is None, 'Cancelling the picker started preparation'
        await page.click('#choose-voice-memo')
        await page.locator('.voice-memo-item').first.click()
        await page.click('#import-voice-memo')
        await page.get_by_text("전처리 완료 · 전사를 시작할 수 있어요", exact=True).wait_for()
        assert memo_imports == [{'id': memo['id'], 'version': memo['version']}]
        assert await page.locator('#audio-file').evaluate('el => !el.required')
        assert await page.locator(".submit-upload").is_disabled()
        assert await page.locator('#upload-chunk-count').is_disabled()
        assert await page.locator('#upload-estimate-memory').inner_text() == '-'
        assert await page.locator('#upload-estimate-time').inner_text() == '-'
        await page.check(f'input[value="{GPU_OTHER}"]')
        assert await page.locator(".submit-upload").is_enabled()
        assert await page.locator('#upload-chunk-count').is_enabled()
        async with page.expect_response('**/api/devices/*/gpus'):
            pass  # The automatic monitor refreshes without a button.
        assert await page.locator(f'input[value="{GPU_OTHER}"]').is_checked(), "Refresh lost job GPU selection"
        busy = True
        await page.wait_for_function("document.querySelector('#gpu-probe-result').textContent.includes('사용 중')")
        assert await page.locator(f'input[value="{GPU_OTHER}"]').is_enabled()
        assert await page.locator(f'input[value="{GPU_OTHER}"]').is_checked()
        await page.click('.submit-upload')
        await page.locator('#gpu-warning-dialog').wait_for(state='visible')
        assert 'gpu_dummy.py' in await page.locator('#gpu-warning-processes').inner_text()
        assert '12000 MiB' in await page.locator('#gpu-warning-description').inner_text()
        assert await page.locator('#cancel-gpu-warning').evaluate('el => document.activeElement === el')
        await page.click('#cancel-gpu-warning')
        await page.locator('.submit-upload:enabled').wait_for()
        assert not notes and not submitted, 'Cancelling the warning created a note'
        await page.check(f'input[value="{GPU_UUID}"]')
        await page.check(f'input[value="{GPU_OTHER}"]')
        await page.locator('#gpu-warning-dialog').wait_for(state='visible')
        await page.keyboard.press('Escape')
        await page.locator(f'input[value="{GPU_UUID}"]:checked').wait_for()
        await page.check(f'input[value="{GPU_OTHER}"]')
        await page.locator('#gpu-warning-dialog').wait_for(state='visible')
        if screenshot:
            await page.locator('#gpu-warning-dialog').screenshot(path=screenshot.replace('.png', '-gpu-warning.png'))
        await page.click('#confirm-gpu-warning')
        await page.locator(f'input[value="{GPU_OTHER}"]:checked').wait_for()
        if screenshot:
            await page.locator("#upload-dialog").screenshot(path=screenshot)
        await page.set_viewport_size({'width':390, 'height':950})
        gpu_panel = await page.locator('#execution-gpu').bounding_box()
        details = await page.locator('.upload-details').bounding_box()
        assert gpu_panel['y'] + gpu_panel['height'] <= details['y']
        assert gpu_panel['x'] >= 0 and details['x'] + details['width'] <= 390
        if screenshot:
            await page.locator('#upload-dialog').screenshot(path=screenshot.replace('.png','-mobile.png'))
        await page.set_viewport_size({'width':1440, 'height':1100})
        busy_revision = 43
        await page.click(".submit-upload")
        await page.locator('#gpu-warning-dialog').wait_for(state='visible')
        assert 'PID 43' in await page.locator('#gpu-warning-processes').inner_text(), 'Changed processes were not reconfirmed'
        await page.click('#confirm-gpu-warning')
        await page.locator("#upload-dialog").wait_for(state="hidden")
        assert submitted["device_id"] == "b" * 32 and submitted["gpu_uuid"] == GPU_OTHER, submitted
        assert submitted['gpu_confirmation'] == 'busy-43'
        assert "selected-gpu" not in submitted
        assert submitted['output_format'] == 'srt'
        assert submitted['chunk_count'] == '1' and 'chunk_minutes' not in submitted, 'Notes should submit only an equal-sized chunk count'
        before = calls
        await asyncio.sleep(3.2)
        assert calls == before, "GPU polling continued after upload close"
        if await page.locator('#note-dialog').is_visible():
            await page.click('#close-note')
            await page.locator('#note-dialog').wait_for(state='hidden')
        await page.click('#go-home')
        await page.locator('#welcome').wait_for(state='visible')
        await page.click('[data-device-id="local"] .use-device')
        assert await page.locator("#execution-gpu").is_hidden(), "Local target showed GPU selection"
        assert await page.locator("#device-privacy").count() == 0
        assert await page.locator("#output-format").input_value() == "srt"
        await page.set_viewport_size({'width': 380, 'height': 850})
        left = await page.locator('#drop-zone').bounding_box()
        right = await page.locator('#choose-voice-memo').bounding_box()
        assert left['x'] >= 0 and right['x'] + right['width'] <= 380
        assert right['x'] > left['x'] and abs(left['height'] - right['height']) < 1
        if screenshot:
            await page.locator('#upload-dialog').screenshot(path=screenshot.replace('.png', '-sources-mobile.png'))
        await page.set_viewport_size({'width': 1440, 'height': 1100})
        if screenshot:
            await page.locator('#upload-dialog').screenshot(path=screenshot.replace('.png', '-sources.png'))
        async with page.expect_file_chooser() as chooser:
            await page.locator('#audio-file').click()
        await (await chooser.value).set_files([])
        assert await page.evaluate('state.uploadPreparation') is None, 'Cancel started an upload'
        assert await page.locator('#audio-file').evaluate('el => el.required')
        await page.set_input_files('#audio-file', {'name': 'voice.wav', 'mimeType': 'audio/wav', 'buffer': data.getvalue()})
        await page.get_by_text('전처리 완료 · 전사를 시작할 수 있어요', exact=True).wait_for()
        await page.click("#close-upload")
        await page.locator('#welcome').wait_for(state='visible')
        for index in range(12):
            targets.append({"id": f"extra-{index}", "name": f"Server {index}", "kind": "ssh", "host": f"extra-{index}", "backend": "remote", "model": "moss", "remote_port": 18400})
        await page.click("#refresh-devices")
        await page.wait_for_function("document.querySelector('#device-count').textContent === '14'")
        assert await page.locator("#device-panel-scroll").evaluate("el => el.scrollHeight > el.clientHeight")
        await page.locator('[data-device-id="extra-11"]').evaluate('el => el.scrollIntoView({block: \"nearest\"})')
        assert await page.evaluate("scrollY") == 0, "Device list scrolled the whole page"
        assert (await page.locator(".device-panel-head").bounding_box())["y"] >= 0
        assert not errors, errors
        await browser.close()
    print(f"PASS {engine}: device panel, SSH registration, per-job GPU, busy-GPU confirmation/cancel, changed-process warning, polling cleanup")


if __name__ == "__main__":
    asyncio.run(main())
