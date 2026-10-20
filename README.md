# cosMOSS

**녹음을 텍스트로, 대화를 기록으로.**

회의·인터뷰·강의 녹음을 전사하고, 화자를 구분하고, AI로 요약하는 Mac 앱입니다.

[![Mac용 다운로드](https://img.shields.io/badge/macOS-%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-18B978?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/hoonably/cosMOSS/releases)

Apple Silicon · macOS 13 이상

![cosMOSS 앱 화면](docs/images/cosmoss.png)

## 주요 기능

- **전사와 화자 구분** — 음성·영상에서 발언 내용, 시간, 화자를 정리합니다.
- **편집과 저장** — 본문과 화자 이름을 수정하고 TXT·SRT·VTT로 저장합니다. 기존 파일도 불러올 수 있습니다.
- **AI 요약** — Codex·Claude로 내용을 요약하고 Markdown으로 저장합니다.
- **처리 장치 선택** — 이 Mac에서 전사하거나 SSH로 연결한 NVIDIA GPU를 사용합니다.

MP3·WAV·M4A·MP4 등 녹음된 파일을 지원합니다. 마이크 녹음 기능은 없습니다.

## 시작하기

1. [릴리스 페이지](https://github.com/hoonably/cosMOSS/releases)에서 Mac 앱 ZIP을 받습니다.
2. 압축을 풀고 **cosMOSS.app**을 **응용 프로그램**에 옮깁니다.
3. 앱을 열고 상단 **설정 → 준비하기**를 누릅니다. 파일 처리에 필요한 도구를 자동으로 준비합니다.
4. 이 Mac에서 전사하려면 **이 Mac → 모델·설정**에서 모델을 준비합니다. 원격 장치만 사용한다면 Mac용 모델 설치 없이 **새로운 장치 연결**로 추가하세요.

도구나 모델을 처음 다운로드할 때는 인터넷 연결이 필요합니다.

macOS가 실행을 차단하면 **시스템 설정 → 개인정보 보호 및 보안 → 확인 없이 열기**에서 허용하세요.

## 사용하기

1. 사용할 장치에서 **노트 만들기**를 눌러 녹음 파일을 선택합니다.
2. 전사된 내용을 확인하고 본문과 화자 이름을 다듬습니다.
3. 원하는 형식으로 저장하거나 **AI 요약**으로 정리합니다.

AI 요약에는 이 Mac에 설치하고 로그인한 Codex 또는 Claude CLI가 필요합니다.
파일 저장 위치와 모델·장치 설정은 [사용 안내](docs/USAGE.md)를 참고하세요.

## 데이터 처리

음성과 노트는 이 Mac에 보관합니다.
다른 장치에서 전사하면 음성을 해당 장치로 전송합니다.
AI 요약을 실행하면 전사 내용을 선택한 AI 서비스로 전송합니다.

[사용 안내](docs/USAGE.md) · [개발 안내](docs/DEVELOPMENT.md) · [문제 제보](https://github.com/hoonably/cosMOSS/issues)

## Credits

[JoNebula/moss-note](https://github.com/JoNebula/moss-note), [jiseungjeong/moss-note](https://github.com/jiseungjeong/moss-note), [ginjae/mac-moss](https://github.com/ginjae/mac-moss)를 바탕으로 만들었으며, 전사와 화자 구분에는 OpenMOSS의 [MOSS-Transcribe-Diarize](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize) 모델을 사용합니다.

[MIT License](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
