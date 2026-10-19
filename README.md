# cosMOSS

**흩어진 목소리를, 정돈된 기록으로.**

Turn recordings into organized notes with transcription, speaker diarization, and AI summaries.

녹음·영상에서 누가 무슨 말을 했는지 구분하고, 핵심을 정리하는 앱을 만들고 있습니다.
전사·화자 구분·수동 편집이 가능한 macOS 개발용 앱입니다.
기본 장치는 이 Mac이며, SSH로 연결할 서버와 GPU를 여러 개 등록할 수 있습니다.
**AI 요약은 아직 구현하지 않았습니다. CUDA 워커는 구현했으며 실제 모델 추론 검증은 별도로 필요합니다.**

## 현재 기능

- 음성·영상 파일 업로드, FFmpeg 음성 정규화, 긴 녹음 분할
- MOSS 기반 전사, 화자 구분, 타임스탬프, 청크 경계 화자 연결
- 음성 재생, 본문·화자 이름 편집, 노트 검색과 자동 저장
- TXT, SRT, WebVTT, JSON 내보내기
- Apple Silicon MLX 추론 서버, SSH로 연결한 GPU별 CUDA 워커, 호환 전사 서버 연결
- SSH config 호스트 선택, nvidia-smi GPU 상태 자동 갱신, GPU 번호 선택
- 녹음마다 처리 장치 지정, 선택한 GPU UUID를 작업에 고정
- SwiftUI + WebKit macOS 앱: 로컬 앱 서버 자동 시작, 파일 선택·저장 대화상자

현재 ‘녹음’은 녹음된 파일 업로드를 뜻합니다. 앱 내 마이크 녹음과 실시간 전사는 아직 지원하지 않습니다.

## 빠른 시작

Python 3.12, `uv`, `ffmpeg`가 필요합니다. macOS에서는 다음과 같이 준비합니다.

```bash
brew install uv ffmpeg
cp .env.example .env
./scripts/setup-app.sh
```

### Mac 앱 실행

macOS 13 이상과 Xcode Command Line Tools가 필요합니다. 빌드한 CPU 아키텍처에서 실행됩니다.

```bash
./scripts/build-mac-app.sh
open dist/cosMOSS.app
```

앱은 `127.0.0.1:18760`에서 Python 앱 서버를 시작하고, 마지막 창을 닫으면 자신이 시작한 서버를 종료합니다.
이미 실행 중인 cosMOSS 서버에 연결한 경우 그 서버는 종료하지 않습니다.

**현재 `.app`은 이 체크아웃의 코드와 `.venv`를 사용하는 개발용 빌드입니다.**
폴더를 옮기면 다시 빌드해야 합니다. 독립 실행 배포본, 서명·공증, 자동 업데이트는 이후 작업입니다.

앱 창을 열기 위해 전사 모델을 다운로드할 필요는 없습니다. 실제 전사에는 아래의 추론 서버가 필요합니다.

### Apple Silicon에서 로컬 전사

```bash
./scripts/setup-mlx.sh  # MLX 런타임과 모델 다운로드
./scripts/run-mlx.sh   # 앱과 별도 터미널에서 실행
```

이후 Mac 앱에서 파일을 올립니다. MLX 추론은 Apple Silicon 전용입니다.

브라우저에서 사용하려면:

```bash
./scripts/run-app.sh   # http://127.0.0.1:8000
```

Apple Silicon에서 웹 앱과 MLX 서버를 함께 실행하는 기존 방식도 유지합니다.

```bash
./scripts/start-mac.sh
```

### SSH 서버와 GPU 선택

1. 앱의 **처리 장치 관리**를 엽니다. 처음에는 **이 Mac**만 등록돼 있습니다.
2. `~/.ssh/config`의 호스트를 선택합니다. `Include` 파일의 별칭도 표시합니다.
3. GPU별 사용률, 여유/전체 VRAM, 프로세스를 약 3초 간격으로 확인합니다.
4. GPU 번호를 하나 고르고 **선택 GPU 추가**를 누릅니다. 내부적으로 GPU UUID를 저장합니다.
5. 등록된 장치에서 **전사 준비**를 누릅니다. 최초에는 원격 사용자 폴더에 실행 환경·모델을 설치합니다.
6. 준비가 끝나면 새 녹음의 **처리 장치**에서 해당 서버/GPU를 선택합니다.
7. 사용을 마치면 **워커 종료**를 누릅니다. 원격 워커는 앱을 닫아도 유지됩니다.

장치 이름은 예를 들어 `연구실 · GPU 2`로 붙일 수 있습니다. 포트는 기본적으로 `18400 + GPU 번호`이며
모델 ID·서버 종류와 함께 고급 연결 설정에서 바꿀 수 있습니다.

**원격 CUDA 워커 요구사항:** Linux, Python 3.10 이상, `venv` 또는 `uv`, 호환 NVIDIA 드라이버,
PyPI·PyTorch·GitHub·Hugging Face에 접근할 인터넷 연결과 모델 저장 공간.
워커는 PyTorch 2.8.0 / CUDA 12.8 빌드와 고정한 OpenMOSS 추론 코드를 사용합니다.
지원 여부는 해당 서버의 드라이버·운영체제에서 확인해야 합니다.

SSH 키·ProxyJump 등은 기존 SSH 설정을 사용합니다. 암호 입력과 새 호스트 키 승인은 앱에서 처리하지 않으므로,
필요하면 터미널에서 해당 별칭으로 먼저 접속하고 키를 SSH agent에 추가합니다.
앱은 선택한 호스트에만 접속하며 GPU 상태 조회 단계에서 모델을 설치하지 않습니다.

‘여유로 보임’은 현재 사용률 5% 이하, VRAM 80% 이상 여유, 확인 가능한 연산 프로세스 없음이라는
관측 기준입니다. **GPU 예약이나 작업 실행 가능 보장은 아닙니다.**
워커 준비 직전에도 GPU UUID와 연산 프로세스를 다시 확인하며, 다른 작업을 종료하지 않습니다.
같은 사용자의 cosMOSS 워커끼리는 GPU별 파일 잠금을 사용합니다.

이미 실행 중인 서버를 쓰려면 고급 설정의 **직접 실행한 호환 JSON 서버** 또는 **cosMOSS MLX 서버**를 선택합니다.
이 경우 실행 GPU는 기존 서버 설정을 따릅니다. 서버는 `/v1/models`와 `/v1/audio/transcriptions`를 지원하고,
화자·시간이 포함된 `text` 또는 `segments`를 반환해야 합니다.

원격 전사 시 음성이 해당 서버로 전송됩니다. 원격 워커는 loopback에만 바인딩하고,
앱이 소유하는 SSH 터널과 장치별 토큰을 통해 통신합니다.

## 설정

전체 설정 예시는 [.env.example](.env.example)에 있습니다.
스크립트는 `.env`를 읽으며, 파일에 지정된 값이 셸의 같은 변수보다 우선합니다.
상대 모델·캐시 경로는 저장소 루트를 기준으로 합니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MOSS_TRANSCRIPTION_BACKEND` | `mlx` | 장치 설정 도입 전 노트의 호환용 기본값 |
| `MOSS_TRANSCRIPTION_URL` | `http://127.0.0.1:8001/v1` | 이전 노트의 호환용 전사 서버 주소 |
| `MOSS_TRANSCRIPTION_API_KEY` | 빈 값 | 추론 서버 Bearer 토큰 |
| `MOSS_TRANSCRIPTION_STREAM` | MLX: `true`, remote: `false` | 포함된 서버의 NDJSON 진행·취소 프로토콜 사용 |
| `MOSS_MODEL_NAME` | `moss-mtd` | 서버가 제공하는 모델 ID |
| `MOSS_APP_HOST` / `MOSS_APP_PORT` | `127.0.0.1` / `8000` | 웹 모드 앱 주소 |
| `COSMOSS_DESKTOP_PORT` | `18760` | Mac 앱 전용 포트; 앱 실행 환경에서 지정 |
| `MOSS_CHUNK_SECONDS` | `1800` | 기본 전사 청크 길이; 업로드 시 변경 가능 |
| `MOSS_BRIDGE_SECONDS` | `30` | 청크 경계 화자 연결 길이 |
| `MOSS_MAX_UPLOAD_GB` | `5` | 파일당 업로드 제한 |
| `MLX_KV_BITS` | `8` | MLX KV 캐시 양자화; `0`으로 비활성화 |
| `MLX_PREFILL_STEP_SIZE` | `512` | MLX 음성 입력 처리 배치 크기 |

새 작업은 앱에서 선택한 장치 설정을 사용합니다. 로컬 MLX 서버와 ‘이 Mac’ 장치는 `MOSS_MLX_PORT`를 사용합니다.
장치 정보가 없는 이전 노트만 기존 전역 URL 설정을 사용하며, 새 URL 변수가 없으면 `MOSS_MLX_URL`을 사용합니다.

## 구조와 데이터

```text
SwiftUI / WebKit Mac 앱 또는 브라우저
                    ↓
             FastAPI 앱 서버
               ↙         ↘
  SQLite + 녹음 파일      작업별 장치 선택
                       ↙          ↘
               로컬 MLX        SSH 터널
                                  ↓
                          서버별 GPU 워커
```

- `src/`: 앱 API, 노트·장치 저장소, SSH/GPU 조회, 터널, MLX·CUDA 서버
- `static/`: 노트·전사 편집 UI
- `macos/`: SwiftUI / WebKit 앱
- `scripts/`: 설치, 실행, Mac 앱 빌드
- `data/`: 녹음·노트·장치 설정과 워커 토큰, `models/`: 로컬 모델, `logs/`: 실행 로그
- 원격 `~/.local/share/cosmoss/`: GPU별 워커 환경, 로그, 모델 캐시

녹음, 모델, 인증 정보, 런타임, 빌드 산출물은 Git에서 제외합니다.
앱을 종료한 뒤 `data/` 전체를 백업하면 녹음과 노트를 보관할 수 있습니다.
화자 연결은 인접 청크의 경계 발화를 사용하므로, 경계에서 말하지 않은 사람은 다른 화자로 남을 수 있습니다.
화자 구분은 발언을 묶는 기능이며 실제 인물의 신원을 식별하는 기능은 아닙니다.

## 다음 개발

- [x] SSH config 선택, GPU 상태 갱신, GPU별 장치 등록과 작업 라우팅
- [x] GPU UUID에 고정된 CUDA 워커의 준비·종료 구현
- [ ] CUDA 모델 설치와 실제 녹음 전사의 장치별 검증
- [ ] 장치별 동시 작업과 자동 배정; 현재 전사 큐는 한 작업씩 처리
- [ ] 요약·주요 논의·결정·할 일 생성과 편집
- [x] 앱에서 SSH 추론 서버·GPU·모델 설정
- [ ] 앱에서 요약 제공자 설정
- [ ] Mac 앱에서 MLX 서버 시작·종료와 모델 설치 관리
- [ ] 독립 실행 Mac 앱 패키징과 배포

현재 앱 창은 기존 웹 편집기를 재사용합니다. 요약 모델은 MOSS 전사 모델과 별도로 연결할 예정입니다.

## 개발과 확인

```bash
./scripts/setup-app.sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
./scripts/build-mac-app.sh
```

## 출처와 라이선스

이 프로젝트는 친구의 [ginjae/mac-moss](https://github.com/ginjae/mac-moss)를 기반으로 시작했습니다.
가져온 스냅샷은 `d69b3dbc227e9cee5c9beb8523e5b26b311b2b40`입니다.

| 단계 | 프로젝트 | 기반 |
|---|---|---|
| 원본 앱 | [JoNebula/moss-note](https://github.com/JoNebula/moss-note) | 전사·화자 구분·노트 편집 |
| 기반 포크 | [jiseungjeong/moss-note](https://github.com/jiseungjeong/moss-note) | Apple Silicon / MLX 적용 |
| 직접 기반 | [ginjae/mac-moss](https://github.com/ginjae/mac-moss) | 로컬 실행, 진행 표시, 작업 제어, UI 개선 |
| 이 프로젝트 | [hoonably/cosMOSS](https://github.com/hoonably/cosMOSS) | Mac 앱, 추론 연결 분리, 기록·정리 워크플로 개발 |

원본 [MIT License](LICENSE)와 저작권 표기를 보존합니다.
모델 및 외부 구성 요소 출처는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고하세요.
