# 릴리스 준비

공식 배포는 [서명된 릴리스 절차](SIGNED_RELEASES.md)의 중앙 워크플로를 사용합니다. 아래 로컬 패키징은 검토용이며, 다른 키나 Mac별 개발 인증서로 배포하지 않습니다.

[← cosMOSS](../README.md) · [개발 가이드](DEVELOPMENT.md)

## 배포 파일

Apple Silicon용 DMG를 기본 설치 파일로 제공하고, 서명된 앱 ZIP은 Sparkle 업데이트에 사용합니다. 앱은 macOS 13 이상을 지원합니다. DMG를 열고 **cosMOSS → 응용 프로그램**으로 드래그하면 설치할 수 있습니다.

## 다음 버전 배포

`release.json`의 version과 build를 올리고 main에 푸시합니다. build는 표시 버전과 별개로 이전 배포보다 항상 커야 합니다. 중앙 빌드가 성공한 뒤 검증된 파일을 받아 공개합니다.

```sh
python3 scripts/release.py prepare
python3 scripts/release.py status
python3 scripts/release.py download
python3 scripts/release.py stage
python3 scripts/release.py publish
```

고정 키 일치, DMG·ZIP 앱 일치, 실제 다음 버전 교체와 변조 거절을 통과해야 배포할 수 있습니다. publish는 설치 파일의 공개 다운로드·서명을 확인한 뒤 appcast를 마지막에 게시합니다. 기존 버전이나 태그는 덮어쓰지 않습니다.

## 로컬 패키징 확인

```sh
./scripts/prepare-release.sh
```

검토용 `dist/cosMOSS-v<version>-macos-arm64.dmg`와 ZIP·피드를 생성합니다. 앱 아이콘·Applications 바로가기·설치 배경을 포함하며 개인 노트·모델·인증정보는 넣지 않습니다. 고정 공개키와 일치하는 키가 없으면 중단하며 새 키로 대체하지 않습니다. 로컬 생성물은 공식 중앙 배포를 대신하지 않습니다.

현재 중앙 배포는 ad-hoc 코드 서명과 고정된 Ed25519 업데이트 서명을 사용합니다. Apple Developer ID 서명·공증은 별개이며, 공증되지 않은 산출물을 공증 완료로 표시하지 않습니다.

## 업데이트 정책

새 설치는 **새 버전 자동 알림**만 기본으로 켭니다. 자동 다운로드·설치는 별도 선택이며, 기존 사용자의 저장된 선택을 유지합니다. 업데이트 전 서명 검증, 작업 종료 대기, 개발용 앱의 교체 방지는 그대로 유지합니다.

## 릴리스 글

**한 문장 요약 → 사용자에게 보이는 주요 변경 3~5개 → 필요한 설치·업데이트 안내** 순서로 씁니다. 과장된 표현이나 커밋 목록은 넣지 않고, 자세한 개발 정보는 별도 문서로 연결합니다.

[다음 릴리스 초안](RELEASE_NOTES.md)을 사용하되, 발행 전 실제 파일·버전·공증 상태에 맞춰 설치 안내를 확인하세요.

## 검증

- 앱: `./scripts/build-mac-app.sh`
- 업데이트: `python3 tests/update_preferences_smoke.py`
- 첫 실행: `python3 tests/startup_access_smoke.py`
- 권한 안내: `python3 tests/full_disk_access_guide_smoke.py`
- 배포 파일: `python3 tests/release_smoke.py` (실제 release 빌드 필요)

새 버전 없음·새 버전 있음·확인 실패를 구분하고, 알림 체크를 바꿔도 자동 설치 동의가 함께 켜지지 않는지 확인합니다.
