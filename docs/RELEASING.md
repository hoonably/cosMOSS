# 릴리스 준비

공식 배포는 [서명된 릴리스 절차](SIGNED_RELEASES.md)의 중앙 워크플로를 사용합니다. 아래 로컬 패키징은 검토용이며, 다른 키나 Mac별 개발 인증서로 배포하지 않습니다.

[← cosMOSS](../README.md) · [개발 가이드](DEVELOPMENT.md)

## 배포 파일

Apple Silicon용 DMG를 기본 설치 파일로 제공하고, 서명된 앱 ZIP은 Sparkle 업데이트에 사용합니다. 앱은 macOS 13 이상을 지원합니다. DMG를 열고 **cosMOSS → 응용 프로그램**으로 드래그하면 설치할 수 있습니다.

```sh
./scripts/prepare-release.sh
```

`dist/cosMOSS-v<version>-macos-arm64.dmg`와 `.zip`이 생성됩니다. 기존 `build-mac-dmg.sh`는 앱 아이콘·Applications 바로가기·설치 배경을 구성합니다. 개인 노트·모델·인증정보는 포함하지 않습니다.

이 명령은 기존 업데이트 키를 먼저 확인하고 필요하면 로그인 Keychain에서 복원한 뒤, 테스트·설치 파일 생성·업데이트 피드 서명까지 수행합니다. 키를 찾거나 일치를 확인하지 못하면 중단합니다. 키를 새로 생성해 기존 배포에 덮어쓰지 마세요.

DMG·ZIP을 GitHub에 올려 다운로드와 체크섬을 확인한 **다음** 서명된 `appcast.xml`을 `main`에 푸시하세요. GitHub 릴리스 등록만으로 앱 내 업데이트가 연결되지는 않습니다. 피드·ZIP 서명 검증과 낮은 버전에서 새 버전 발견 테스트가 모두 통과해야 배포가 완료됩니다.

```sh
.venv/bin/python tests/future_update_smoke.py dist/cosMOSS-v<version>-macos-arm64.zip
```

실제 배포 ZIP을 임시 폴더에서 다음 버전으로 만들고 서명해, 다운로드·서명 검증·앱의 새 버전 발견·변조 거절을 확인합니다. 이 테스트는 사용자 앱을 교체하거나 가상 다음 버전을 공개하지 않습니다.

서명·공증은 기존 배포 설정을 따릅니다. 임시 서명으로 검증한 로컬 산출물을 Apple 공증 완료로 표시하지 않습니다. 실제 사용자용 버전 번호를 결정하고, 서명과 앱 실행을 확인한 뒤 appcast를 생성합니다. **검토 전에는 태그·릴리스·appcast를 공개하지 않습니다.**

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
