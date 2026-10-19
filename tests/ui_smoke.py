"""Run against an isolated app server, using browser fixtures for SSH/GPU I/O.

uv run --no-project --with playwright python tests/ui_smoke.py http://127.0.0.1:18961
"""

import asyncio
import json
import sys
from datetime import datetime, timezone

from playwright.async_api import async_playwright


GPU_UUID = "GPU-11111111-2222-3333-4444-555555555555"


async def main():
    base = sys.argv[1]
    screenshot = sys.argv[2] if len(sys.argv) > 2 else None
    calls = 0
    targets = [{"id": "local", "name": "이 Mac", "kind": "local", "backend": "mlx", "model": "moss-mtd", "remote_port": 8001}]
    added = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/ssh/hosts", lambda route: route.fulfill(json={"hosts": ["lab-3090"]}))

        async def devices(route):
            nonlocal added
            if route.request.method == "POST":
                added = route.request.post_data_json
                target = {**added, "id": "b" * 32, "kind": "ssh"}
                targets.append(target)
                await route.fulfill(status=201, json=target)
            else:
                await route.fulfill(json=targets)

        await page.route("**/api/devices", devices)
        await page.route("**/api/devices/*/worker/prepare", lambda route: route.fulfill(status=202, json={"phase": "connecting"}))
        await page.route("**/api/devices/*/worker", lambda route: route.fulfill(json={"phase": "ready"}))
        await page.route("**/api/devices/*/status", lambda route: route.fulfill(json={"ok": True, "label": "Lab GPU 0"}))

        async def probe(route):
            nonlocal calls
            calls += 1
            await route.fulfill(json={
                "host": "lab-3090", "reachable": True, "gpu_supported": True,
                "checked_at": datetime.now(timezone.utc).isoformat(), "message": "현재 상태이며 GPU 예약이 아닙니다.",
                "gpus": [{"index": 0, "uuid": GPU_UUID, "name": "NVIDIA GeForce RTX 3090", "memory_free_mib": 23000,
                          "memory_total_mib": 24576, "utilization_percent": calls, "processes": [],
                          "process_info_known": True, "idle_candidate": True}],
            })

        await page.route("**/api/ssh/probe", probe)
        await page.goto(base)
        await page.click("#manage-devices")
        await page.locator('#ssh-host option[value="lab-3090"]').wait_for(state="attached")
        await page.select_option("#ssh-host", "lab-3090")
        await page.locator('input[name="selected-gpu"]').wait_for()
        assert await page.locator("#add-device").is_disabled()
        await page.check(f'input[value="{GPU_UUID}"]')
        assert await page.locator("#device-name").input_value() == "lab-3090 · GPU 0"
        assert await page.locator("#device-port").input_value() == "18400"
        assert await page.locator("#add-device").is_enabled()
        await page.wait_for_function("document.querySelector('#gpu-probe-result').textContent.includes('2%')", timeout=10000)
        assert calls >= 2, "GPU metrics did not refresh automatically"
        assert await page.locator(f'input[value="{GPU_UUID}"]').is_checked(), "Refresh lost the GPU selection"
        await page.click("#add-device")
        await page.locator('[data-device-id="' + "b" * 32 + '"]').wait_for()
        assert added["gpu_uuid"] == GPU_UUID and added["gpu_index"] == 0
        assert added["remote_port"] == 18400 and added["backend"] == "cuda"
        await page.click('[data-device-action="prepare"]')
        await page.get_by_text("GPU 워커 준비 완료", exact=True).wait_for()
        if screenshot:
            await page.screenshot(path=screenshot, full_page=True)
        calls_before_close = calls
        await page.click("#close-device-dialog")
        await asyncio.sleep(3.5)
        assert calls == calls_before_close, "GPU polling continued after closing the dialog"
        await page.click("#open-upload")
        await page.select_option("#device-select", "b" * 32)
        assert await page.locator("#device-select").input_value() == "b" * 32
        assert not errors, errors
        await browser.close()
    print("PASS: GPU auto refresh, UUID selection persistence, registration payload, prepare progress, upload target selection, polling cleanup, no browser errors")


if __name__ == "__main__":
    asyncio.run(main())
