# cosMOSS

**녹음을 기록으로, 기록을 다음 할 일로.**

회의, 인터뷰, 강의 녹음을 텍스트로 옮기고, 화자를 정리하고, AI로 요약하는 Mac 앱입니다.
기본은 이 Mac에서 처리하며, 필요하면 다른 장치도 연결할 수 있습니다.

[![Mac용 다운로드](https://img.shields.io/badge/macOS-%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-18B978?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/hoonably/cosMOSS/releases)

Apple Silicon · macOS 13 이상

![cosMOSS 앱 화면](docs/images/cosmoss.png)

## 할 수 있는 일

- **녹음을 텍스트로** — 음성·영상 파일을 전사하고, 화자와 발언 시간을 구분합니다.
- **화자와 내용 정리** — 넓은 편집 창에서 본문을 수정하고, 화자 이름을 지정하거나 드래그로 병합합니다.
- **익숙한 파일로 저장** — TXT·SRT·VTT를 저장하고, 기존 전사 파일도 불러옵니다.
- **AI로 요약** — Codex·Claude를 선택해 정리한 내용을 Markdown 파일로 저장합니다.
- **다른 장치에서도 처리** — SSH로 NVIDIA GPU 장치를 연결하고 실행할 때 GPU를 고릅니다.

MP3, WAV, M4A, MP4 등 음성·영상 파일을 지원합니다. 마이크 녹음은 지원하지 않습니다.

## 설치

1. 위 **다운로드** 버튼에서 Mac 앱 ZIP을 받습니다.
2. 압축을 풀고 **cosMOSS.app**을 **응용 프로그램** 폴더로 옮깁니다.
3. 음성·영상 변환에 필요한 FFmpeg를 설치합니다.

   ```bash
   brew install ffmpeg
   ```

4. 앱을 열고 **이 Mac → 모델·설정**에서 모델을 준비합니다. 처음에는 인터넷 연결이 필요합니다.

현재는 프리릴리스입니다. macOS가 실행을 차단하면 **시스템 설정 → 개인정보 보호 및 보안 → 확인 없이 열기**에서 허용하세요.

## 사용하기

1. **처리 장치**에서 **노트 만들기**를 눌러 녹음과 저장 형식을 선택합니다.
2. **노트 정리**에서 파일을 열어 내용을 다듬고, **화자 설정**에서 이름을 지정합니다.
3. **AI 요약**에서 Codex 또는 Claude와 요청을 선택해 요약합니다. 해당 CLI 설치와 로그인이 필요합니다.

기본 경로는 **Downloads**입니다. 상단 **기본 경로**에서 바꿀 수 있습니다.
이 폴더의 TXT·SRT·VTT를 불러오고, 전사 파일과 AI 요약을 같은 곳에 저장합니다.

## 내 파일은 어디에 있나요?

음성과 노트는 이 Mac에 보관됩니다. 다른 처리 장치를 선택하면 음성을 해당 장치로 전송하며,
AI 요약을 실행하면 전사 내용이 선택한 AI 서비스로 전달됩니다.

모델은 한 번 준비하면 다시 사용할 수 있습니다. 이미 받은 모델 폴더도 지정할 수 있습니다.

[자세한 사용 안내](docs/USAGE.md) · [문제 제보](https://github.com/hoonably/cosMOSS/issues) · [개발 안내](docs/DEVELOPMENT.md)

## Credits

[ginjae/mac-moss](https://github.com/ginjae/mac-moss),
[JoNebula/moss-note](https://github.com/JoNebula/moss-note),
[jiseungjeong/moss-note](https://github.com/jiseungjeong/moss-note)의 작업을 바탕으로 만들었습니다.
전사와 화자 구분에는 OpenMOSS의 MOSS 모델을 사용합니다.

[MIT License](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
