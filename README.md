# cosMOSS

**흩어진 목소리를, 정돈된 기록으로.**

Turn recordings into organized notes with transcription, speaker diarization, and AI summaries.

녹음·영상에서 누가 무슨 말을 했는지 구분하고, 핵심을 정리하는 앱을 만들고 있습니다.
현재는 전사·화자 구분·수동 편집이 가능한 기반과 macOS 개발용 앱을 제공합니다.
**AI 요약과 RTX 3090용 CUDA 추론 서버는 아직 구현하지 않았습니다.**

## 현재 기능

- 음성·영상 파일 업로드, FFmpeg 음성 정규화, 긴 녹음 분할
- MOSS 기반 전사, 화자 구분, 타임스탬프, 청크 경계 화자 연결
- 음성 재생, 본문·화자 이름 편집, 노트 검색과 자동 저장
- TXT, SRT, WebVTT, JSON 내보내기
- Apple Silicon MLX 추론 서버 또는 호환 전사 서버 연결
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

### 외부 추론 서버 연결

`.env`에서 다음 설정을 바꿉니다.

```dotenv
MOSS_TRANSCRIPTION_BACKEND=remote
MOSS_TRANSCRIPTION_URL=http://your-inference-host:8001/v1
MOSS_MODEL_NAME=your-served-model-id
MOSS_TRANSCRIPTION_API_KEY=
```

이 설정은 서버 연결 기능입니다. **CUDA 서버를 설치하거나 RTX 3090에서의 추론을 보장하는 기능은 아닙니다.**
서버는 `/v1/models`와 `/v1/audio/transcriptions`를 지원하고,
화자·타임스탬프가 포함된 MOSS 전사 문자열 또는 `segments` 배열을 JSON으로 반환해야 합니다.
일반 음성 전사 API의 호환성은 별도 확인이 필요합니다. 자세한 계약은 [개발 안내](docs/DEVELOPMENT.md)에 있습니다.

외부 서버를 지정하면 업로드한 음성이 해당 서버로 전송됩니다. 서버 토큰은 앱 백엔드에서만 사용합니다.

## 설정

전체 설정 예시는 [.env.example](.env.example)에 있습니다.
스크립트는 `.env`를 읽으며, 파일에 지정된 값이 셸의 같은 변수보다 우선합니다.
상대 모델·캐시 경로는 저장소 루트를 기준으로 합니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MOSS_TRANSCRIPTION_BACKEND` | `mlx` | `mlx` 또는 `remote`; 표시 문구와 스트리밍 기본값 |
| `MOSS_TRANSCRIPTION_URL` | `http://127.0.0.1:8001/v1` | 장치와 독립적인 추론 서버 주소 |
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

새 URL 변수가 없으면 기존 `MOSS_MLX_URL`을 사용합니다. 로컬 MLX 포트를 바꿀 때에는
`MOSS_MLX_PORT`와 앱의 `MOSS_TRANSCRIPTION_URL`을 함께 맞춥니다.

## 구조와 데이터

```text
SwiftUI / WebKit Mac 앱 또는 브라우저
                    ↓
             FastAPI 앱 서버
               ↙         ↘
  SQLite + 녹음 파일      전사 API
                       ↙       ↘
               로컬 MLX     호환 외부 서버
```

- `src/`: 앱 API, SQLite 저장소, 전사 파서, MLX 서버
- `static/`: 노트·전사 편집 UI
- `macos/`: SwiftUI / WebKit 앱
- `scripts/`: 설치, 실행, Mac 앱 빌드
- `data/`: 녹음·노트, `models/`: 모델, `logs/`: 실행 로그

녹음, 모델, 인증 정보, 런타임, 빌드 산출물은 Git에서 제외합니다.
앱을 종료한 뒤 `data/` 전체를 백업하면 녹음과 노트를 보관할 수 있습니다.
화자 연결은 인접 청크의 경계 발화를 사용하므로, 경계에서 말하지 않은 사람은 다른 화자로 남을 수 있습니다.
화자 구분은 발언을 묶는 기능이며 실제 인물의 신원을 식별하는 기능은 아닙니다.

## 다음 개발

- [ ] RTX 3090용 CUDA 추론 서버와 실제 장치 검증
- [ ] 요약·주요 논의·결정·할 일 생성과 편집
- [ ] 앱에서 추론 서버·모델·요약 제공자 설정
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
