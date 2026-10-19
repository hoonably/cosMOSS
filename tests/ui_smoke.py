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

        async def probe(route):
            nonlocal calls
            calls += 1
            gpu = {"index": 0, "uuid": GPU_UUID, "name": "NVIDIA GeForce RTX 3090", "memory_free_mib": 23000,
                   "memory_total_mib": 24576, "utilization_percent": min(calls, 4), "processes": [],
                   "process_info_known": True, "idle_candidate": True}
            await route.fulfill(json={
                "host": "lab-3090", "reachable": True, "gpu_supported": True,
                "checked_at": datetime.now(timezone.utc).isoformat(), "message": "현재 상태이며 GPU 예약이 아닙니다.",
                "gpus": [gpu, {**gpu, "index": 2, "uuid": GPU_OTHER, "idle_candidate": not busy,
                                "utilization_percent": 90 if busy else 0}],
            })

        async def uploads(route):
            await route.fulfill(status=202, json={"id": "c" * 32, "status": "ready", "duration": 1.0})

        async def note_list(route):
            nonlocal submitted
            if route.request.method == "POST":
                raw = b"Content-Type: " + route.request.headers["content-type"].encode() + b"\r\n\r\n" + route.request.post_data_buffer
                form = BytesParser(policy=default).parsebytes(raw)
                submitted = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode() for part in form.iter_parts()}
                note = {"id": "d" * 32, "title": "회의", "original_filename": "voice.wav", "status": "queued", "created_at": datetime.now(timezone.utc).isoformat(),
                        "device_id": "b" * 32, "device_name": "lab-3090 · GPU #2", "segments": [], "speaker_names": {}, "duration": 1,
                        "chunk_seconds": 1800, "progress_phase": "queued"}
                notes.append(note)
                await route.fulfill(status=202, json=note)
            else:
                await route.fulfill(json=notes)

        await page.route("**/api/devices/*/gpus", probe)
        await page.route("**/api/uploads", uploads)
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
        assert await page.locator('#review-path-note').text_content() == '기본 경로에 있는 전사 파일을 불러옵니다.'
        await page.click('#save-location')
        await page.wait_for_function("document.querySelector('#save-location-name').textContent === '노트'")
        assert '/Users/tester/Documents/노트' in await page.locator('#save-location').get_attribute('title')
        assert await page.locator('#review-path-note').text_content() == '기본 경로에 있는 전사 파일을 불러옵니다.'
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
        await choose_option(page, '#model-choice', 'cuda-8bit')
        await page.locator('#model-knobs > summary').click()
        assert await page.locator('[data-knob="precision"]').input_value() == 'int8'
        await page.locator('[data-knob="chunk_minutes"]').fill('15')
        await choose_option(page, '[data-knob="attention"]', 'eager')

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
        await page.click('#close-local-model')
        assert await page.locator("#device-dialog").is_hidden()
        assert await page.locator("#server-state").count() == 0
        assert await page.get_by_text("All connection attempts failed", exact=False).count() == 0
        assert "gpu_uuid" not in added and "gpu_index" not in added
        assert added["model_cache"] == "" and added["remote_port"] == 18400
        assert calls == 0, "Model preparation must not touch GPUs"
        if screenshot:
            await page.locator(".device-panel").screenshot(path=screenshot.replace(".png", "-devices.png"))
        await page.locator('[data-device-id="' + "b" * 32 + '"] .use-device').click()
        assert await page.evaluate('state.executionDeviceId') == 'b' * 32
        await choose_option(page, '#output-format', 'srt')
        await page.locator('input[name="selected-gpu"]').first.wait_for()
        data = io.BytesIO()
        with wave.open(data, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000)
        await page.set_input_files("#audio-file", {"name": "voice.wav", "mimeType": "audio/wav", "buffer": data.getvalue()})
        await page.get_by_text("전처리 완료 · 전사를 시작할 수 있어요", exact=True).wait_for()
        assert await page.locator(".submit-upload").is_disabled()
        await page.check(f'input[value="{GPU_OTHER}"]')
        assert await page.locator(".submit-upload").is_enabled()
        await page.wait_for_function("document.querySelector('#gpu-probe-result').textContent.includes('2%')", timeout=10000)
        assert await page.locator(f'input[value="{GPU_OTHER}"]').is_checked(), "Refresh lost job GPU selection"
        busy = True
        await page.click("#probe-ssh-host")
        await page.locator(f'input[value="{GPU_OTHER}"]:disabled').wait_for()
        assert await page.locator(".submit-upload").is_disabled(), "Busy GPU remained selectable"
        busy = False
        await page.click("#probe-ssh-host")
        await page.locator(f'input[value="{GPU_OTHER}"]:enabled').wait_for()
        await page.check(f'input[value="{GPU_OTHER}"]')
        if screenshot:
            await page.locator("#upload-dialog").screenshot(path=screenshot)
        await page.click(".submit-upload")
        await page.locator("#upload-dialog").wait_for(state="hidden")
        assert submitted["device_id"] == "b" * 32 and submitted["gpu_uuid"] == GPU_OTHER, submitted
        assert "selected-gpu" not in submitted
        assert submitted['output_format'] == 'srt'
        assert 'chunk_minutes' not in submitted, 'Upload overrode the device chunk setting'
        before = calls
        await asyncio.sleep(3.2)
        assert calls == before, "GPU polling continued after upload close"
        if await page.locator('#note-dialog').is_visible():
            await page.click('#close-note')
            await page.locator('#note-dialog').wait_for(state='hidden')
        await page.click('#show-devices')
        await page.locator('#welcome').wait_for(state='visible')
        await page.click('[data-device-id="local"] .use-device')
        assert await page.locator("#execution-gpu").is_hidden(), "Local target showed GPU selection"
        assert await page.locator("#device-privacy").count() == 0
        assert await page.locator("#output-format").input_value() == "srt"
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
    print(f"PASS {engine}: device panel and independent scrolling, SSH candidates, host-only registration, per-job GPU, submitted UUID, busy-GPU rejection, polling cleanup")


if __name__ == "__main__":
    asyncio.run(main())
