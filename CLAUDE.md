# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 저장소 구성

프로젝트는 `youtube-auto-editor/` 하나입니다. 영상을 올리면 전사, 1차 컷 편집, 일러스트 합성, 개인정보 모자이크까지 처리하는 로컬 웹 앱(FastAPI + 순수 JS)입니다. 사용자 설명서는 `youtube-auto-editor/README.md`에 있습니다.

Windows 전용입니다. `app/media.py`가 `msvcrt`와 `_winapi`로 큰 파이프를 만들고, `config.find_tool`은 winget 설치 경로에서 ffmpeg를 찾습니다.

## 명령어

모두 `youtube-auto-editor/`에서 실행합니다. 린터와 빌드 단계는 없습니다.

```bash
install.bat                                    # Python, ffmpeg, VC++ 런타임, .venv 준비
.venv\Scripts\python -m app                    # 서버 실행 (http://127.0.0.1:8766). start.bat은 브라우저도 연다
.venv\Scripts\python -m pytest tests           # 전체 테스트
.venv\Scripts\python -m pytest tests/test_cuts.py::test_time_map_follows_cuts   # 테스트 하나
.venv\Scripts\python -m tests.make_sample      # samples/test_video.mp4와 정답지 생성 (Windows 한국어 음성 필요)
.venv\Scripts\python -m tests.verify_sample    # 샘플 완성본을 다시 전사·OCR해 검사 (샘플을 먼저 전체 실행)
.venv\Scripts\python -m tests.mock_apis 9199   # 가짜 Claude·OpenAI 서버
```

가짜 서버를 쓰려면 앱을 띄울 때 `ANTHROPIC_BASE_URL=http://127.0.0.1:9199`와 `OPENAI_BASE_URL=http://127.0.0.1:9199/v1`를 지정합니다. 받은 요청은 `tests/received.json`에 남습니다.

## 구조

### 단계와 캐시 (`app/pipeline.py`)

`STAGES`는 transcribe → cut → plan → images → pii → render 순서입니다. 결과는 `workspace/<영상 id>/`에 쌓입니다.

각 단계의 해시는 `_params`(그 단계의 옵션)와 `depends`(앞 단계의 rev)로 만듭니다. 해시가 같고 `OUTPUTS` 파일이 있으면 재사용합니다. 그래서 새 옵션을 추가할 때는 `config.DEFAULT_OPTIONS`, `Project._params`, `web/index.html`의 `data-opt`을 함께 고쳐야 캐시가 무효화됩니다.

웹에서 결과를 고치는 경로는 두 가지입니다.

- `bump`: 그 단계의 rev만 바꿔 뒤 단계를 다시 돌립니다 (기획, 모자이크 수정).
- `invalidate`: 그 단계 자체를 다시 돌립니다 (컷 수정).

`plan.json`과 `cut_plan.json`에 `edited: true`가 있으면 강제 재실행 전까지 그 내용을 유지합니다.

실패 처리는 단계마다 다릅니다. 기획이 실패하면 `retry_next`로 완료 기록을 남기지 않아 다음 실행에서 다시 시도합니다. 컷 문맥 검토 실패는 `review_failed`만 남기고 넘어갑니다. 컷을 미완료로 두면 뒤의 OCR이 매번 다시 돌기 때문입니다.

### 두 개의 시간축

- 원본 시각: `transcript.json`, `cut_plan.json`의 `cuts`
- 컷 편집본 시각: `cut_transcript.json`, `plan.json`, `pii.json`. 최종 영상과 같은 시각입니다.

변환은 `cuts.TimeMap`이 맡습니다. `cut_plan.json`의 `keeps`는 프레임 번호입니다. 영상(`media.frame_filter`)과 오디오(`media.concat_audio`)를 같은 프레임 경계로 잘라서, 컷이 수백 개여도 싱크가 어긋나지 않습니다. 가변 프레임 화면 녹화는 `fps` 필터로 먼저 고정합니다.

`media.iter_frames`는 `fps` 필터가 아니라 프레임 번호로 샘플을 뽑습니다. `fps` 필터는 시각이 최대 한 칸 밀려서 스크롤되는 글자와 모자이크가 어긋납니다.

### 렌더 (`app/render.py`)

`cut.mp4`는 미리보기와 OCR 전용입니다. 최종본은 화질 손실을 막으려고 원본에서 같은 프레임 필터로 다시 읽어 한 번만 인코딩합니다.

`render_final`은 ffmpeg 디코더 → numpy → ffmpeg 인코더 구조입니다. 윈도우 기본 파이프는 프레임 전송이 느려서 `media._big_pipe`의 16MB 파이프를 씁니다. 효과가 없는 프레임은 numpy를 거치지 않습니다. 모자이크를 먼저 입히고 그 위에 일러스트를 얹습니다.

### 개인정보 탐지 (`app/privacy.py`)

`scan_video`의 흐름입니다.

1. `changed_boxes`와 `update_lines`로 바뀐 영역만 다시 OCR합니다.
2. `find_in_text`가 정규식으로 찾고, 줄 안의 위치는 `sub_rect`가 추정합니다.
3. `build_tracks`가 샘플별 검출을 시간 구간으로 잇습니다. 같은 값이 두 번 보이면 추적을 나누고, 다른 값은 위치가 겹쳐도 잇지 않습니다.
4. `rect_at`은 앞뒤 샘플 위치를 모두 덮는 상자를 돌려줍니다. 위치를 보간하면 불규칙한 스크롤에서 글자가 새기 때문입니다.

### 컷 판단 (`app/cuts.py`)

`find_word_candidates`가 규칙으로 후보를 찾고, 키가 있으면 `claude.review_cuts`가 문맥을 검토합니다. `이 이미지를`처럼 정상적인 표현을 더듬음으로 보지 않도록 앞말이 두 글자 이상일 때만 말 시작 실수로 칩니다. `_silent_mask`는 작게 말한 단어를 무음에서 제외합니다.

### 외부 API

- Claude 호출은 모두 `claude.ask_json`을 거칩니다: 스트리밍, JSON 스키마 출력, `fallbacks="default"`. 모델은 환경 변수 `CLAUDE_MODEL`로 바꿉니다.
- 이미지 파일명에 프롬프트 해시가 들어가서, 같은 프롬프트는 다시 과금되지 않습니다 (`images._cache_name`).
- 그림 안에는 글자를 넣지 않습니다. 라벨은 `render.build_card`가 맑은 고딕으로 직접 씁니다.
- `assets/logos/`에 없는 로고는 그림으로 지어내지 않습니다.

### 웹 (`app/server.py`, `app/jobs.py`, `web/`)

`only_local` 미들웨어가 다른 사이트의 요청을 막습니다. 작업은 `jobs.job` 하나만 동시에 돌고, 화면은 `/api/job`을 폴링해 로그를 받습니다. 옵션은 브라우저 `localStorage`에 저장되고 서버는 `config.clean_options`로 검증합니다.

## 환경 함정

- OpenCV는 윈도우에서 한글 경로의 파일을 못 여는 경우가 있습니다. 얼굴 모델은 임시 폴더로 복사해서 엽니다.
- faster-whisper에 파일 경로를 주면 PyAV 호환 문제로 실패합니다. 파형 배열을 넘깁니다.
- RapidOCR 기본값 PP-OCRv6에는 한국어 모델이 없어 PP-OCRv5를 지정합니다. 기본 `Det.limit_side_len`(736)은 1080p의 작은 글자를 놓칩니다.
- `anthropic` 2.x는 `httpx`가 아니라 `httpx2`를 씁니다.
- `gpt-image-2`는 이 계정에서 투명 배경을 거절합니다. `images._openai_image`가 불투명으로 받고 `remove_flat_background`가 배경을 지웁니다.

## 검증 상태

실제 OpenAI 호출은 확인했습니다. 실제 Claude 호출은 API 크레딧이 없어 `tests/mock_apis.py`로만 확인했습니다. 크레딧 오류가 요청 형식 오류를 가렸을 수 있으니, 충전 후에는 기획과 컷 검토를 먼저 확인하세요. 실제 사람 목소리와 실제 얼굴로도 아직 시험하지 않았습니다.
