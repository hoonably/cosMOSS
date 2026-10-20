"""Create a drag-to-Applications installer without automating Finder."""

import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time

import dmgbuild


def main() -> None:
    started = time.perf_counter()
    root = Path(__file__).resolve().parent.parent
    app = Path(sys.argv[1]).resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if app.name != "cosMOSS.app" or "CosMOSSProjectRoot" in info:
        raise SystemExit("독립 실행 가능한 배포용 cosMOSS.app이 필요합니다.")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    version = info["CFBundleShortVersionString"]
    if not all(part.isdigit() for part in version.split(".")):
        raise SystemExit("잘못된 앱 버전입니다.")
    output = root / "dist" / f"cosMOSS-v{version}-macos-arm64.dmg"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the previous installer intact until the new image passes verification.
    with tempfile.TemporaryDirectory(prefix=".cosmoss-dmg-", dir=output.parent) as staging:
        temporary = Path(staging)
        background = temporary / "background.tiff"
        subprocess.run([
            "/usr/bin/xcrun", "swift", str(root / "scripts/build-dmg-background.swift"),
            str(background),
        ], check=True)
        image = temporary / output.name
        dmgbuild.build_dmg(str(image), "cosMOSS", settings={
            "format": "UDZO",
            "filesystem": "HFS+",
            "files": [str(app)],
            "symlinks": {"Applications": "/Applications"},
            "icon": str(app / "Contents/Resources/AppIcon.icns"),
            "background": str(background),
            "window_rect": ((240, 200), (640, 420)),
            "default_view": "icon-view",
            "include_icon_view_settings": True,
            "include_list_view_settings": False,
            "icon_locations": {"cosMOSS.app": (180, 218), "Applications": (460, 218)},
            "icon_size": 112,
            "text_size": 14,
            "grid_spacing": 90,
            "show_icon_preview": True,
        }, lookForHiDPI=False)
        subprocess.run(["/usr/bin/hdiutil", "verify", str(image)], check=True)
        mount = temporary / "mounted"
        mount.mkdir()
        subprocess.run([
            "/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
            "-mountpoint", str(mount), str(image),
        ], check=True, stdout=subprocess.DEVNULL)
        try:
            if not (mount / "Applications").is_symlink() or os.readlink(mount / "Applications") != "/Applications":
                raise RuntimeError("DMG의 응용 프로그램 폴더 연결이 올바르지 않습니다.")
            subprocess.run([
                "/usr/bin/codesign", "--verify", "--deep", "--strict", str(mount / app.name),
            ], check=True)
        finally:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=True, stdout=subprocess.DEVNULL)
        os.replace(image, output)
    print(f"릴리스 DMG: {output}")
    print(f"DMG 생성·검증: {time.perf_counter() - started:.1f}초")


if __name__ == "__main__":
    main()
