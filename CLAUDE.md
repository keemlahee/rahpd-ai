# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 저장소 구성

프로젝트는 `youtube-auto-editor/` 하나입니다. 영상을 올리면 전사, 1차 컷 편집, MZ 말투 자막, 일러스트, 예능 효과, 개인정보 모자이크까지 처리하는 로컬 웹 앱(FastAPI + 순수 JS)입니다. 사용자 설명서는 `youtube-auto-editor/README.md`에 있습니다.

Windows용으로 만들었지만 리눅스에서도 돕니다. 윈도우에서는 `app/media.py`가 `msvcrt`와 `_winapi`로 큰 파이프를 만들고, `config.find_tool`은 winget 설치 경로에서 ffmpeg를 찾습니다. 리눅스에서는 `os.pipe`를 `F_SETPIPE_SZ`로 키워 쓰고 ffmpeg는 PATH에서 찾습니다. `install.bat`은 윈도우 전용이라 리눅스에서는 `.venv`(`pip install -r requirements.txt`)와 글꼴을 직접 준비합니다. 글꼴은 `config.find_font`가 윈도우 글꼴 폴더 다음으로 `assets/fonts`, `~/.local/share/fonts`, `/usr/share/fonts`에서 같은 파일 이름(`NotoSansKR-VF.ttf`, `NotoSerifKR-VF.ttf`)을 찾고, 맑은 고딕 굵게가 없으면 `NanumGothicBold.ttf`를 씁니다.

## 명령어

모두 `youtube-auto-editor/`에서 실행합니다. 린터와 빌드 단계는 없습니다.

```bash
install.bat                                    # Python, ffmpeg, VC++ 런타임, .venv 준비
.venv\Scripts\python -m app                    # 감시기와 서버 실행 (http://127.0.0.1:8766). start.bat은 브라우저도 연다
.venv\Scripts\python -m pytest tests           # 전체 테스트
.venv\Scripts\python -m pytest tests/test_cuts.py::test_time_map_follows_cuts   # 테스트 하나
.venv\Scripts\python -m tests.make_sample      # samples/test_video.mp4와 정답지 생성 (Windows 한국어 음성 필요)
.venv\Scripts\python -m tests.verify_sample    # 샘플 완성본을 다시 전사·OCR해 검사 (샘플을 먼저 전체 실행)
.venv\Scripts\python -m tests.mock_apis 9199   # 가짜 Claude·OpenAI 서버
```

가짜 서버를 쓰려면 앱을 띄울 때 `ANTHROPIC_BASE_URL=http://127.0.0.1:9199`와 `OPENAI_BASE_URL=http://127.0.0.1:9199/v1`를 지정합니다. 받은 요청은 `tests/received.json`에 남습니다.

코드를 고쳐도 서버를 다시 켤 필요가 없습니다. `app/*.py`를 저장하면 감시기가 재시작합니다. `requirements.txt`를 바꿨을 때만 `install.bat`이 필요합니다.

## 구조

### 단계와 캐시 (`app/pipeline.py`)

`STAGES`는 transcribe → cut → captions → plan → images → pii → render 순서입니다. 결과는 `workspace/<영상 id>/`에 쌓입니다.

각 단계의 해시는 `_params`(그 단계의 옵션)와 `depends`(앞 단계의 rev)로 만듭니다. 해시가 같고 `OUTPUTS` 파일이 있으면 재사용합니다. 그래서 새 옵션을 추가할 때는 `config.DEFAULT_OPTIONS`, `Project._params`, `web/index.html`의 `data-opt`을 함께 고쳐야 캐시가 무효화됩니다.

웹에서 결과를 고치는 경로는 두 가지입니다.

- `bump`: 그 단계의 rev만 바꿔 뒤 단계를 다시 돌립니다 (기획, 모자이크 수정).
- `invalidate`: 그 단계 자체를 다시 돌립니다 (컷 수정).

`plan.json`, `captions.json`, `cut_plan.json`에 `edited: true`가 있으면 강제 재실행 전까지 그 내용을 유지합니다.

실패 처리는 단계마다 다릅니다. OpenAI 전사, 자막 말투, 기획이 실패하면 대체 결과로 끝까지 진행하되 `retry_next`로 완료 기록을 남기지 않아 다음 실행에서 다시 시도합니다. 컷 문맥 검토 실패는 `review_failed`만 남기고 넘어갑니다. 컷을 미완료로 두면 뒤의 OCR이 매번 다시 돌기 때문입니다.

### 코드 자동 적용 (`app/__main__.py`)

`python -m app`은 감시기이고, 실제 서버는 그 자식 프로세스(uvicorn)입니다. uvicorn의 reload를 쓰지 않는 이유는 파일이 바뀌는 즉시 프로세스를 끊어 렌더 중인 작업까지 죽이기 때문입니다. 감시기는 `/api/job`으로 작업이 없음을 확인한 뒤에만 재시작하고, 서버가 답이 없으면 바쁜 것으로 봅니다.

화면은 `/api/version`을 폴링합니다. 값이 바뀌면 새로고침하되, `hasWorkInProgress`(수정 중인 기획, 업로드, 재생 중인 영상, 입력 중인 키)가 참이면 끝날 때까지 미룹니다.

### 두 개의 시간축

- 원본 시각: `transcript.json`, `cut_plan.json`의 `cuts`
- 컷 편집본 시각: `cut_transcript.json`, `captions.json`, `plan.json`, `pii.json`. 최종 영상과 같은 시각입니다.

변환은 `cuts.TimeMap`이 맡습니다. `cut_plan.json`의 `keeps`는 프레임 번호입니다. 영상(`media.frame_filter`)과 오디오(`media.concat_audio`)를 같은 프레임 경계로 잘라서, 컷이 수백 개여도 싱크가 어긋나지 않습니다. 가변 프레임 화면 녹화는 `fps` 필터로 먼저 고정합니다.

`media.iter_frames`는 `fps` 필터가 아니라 프레임 번호로 샘플을 뽑습니다. `fps` 필터는 시각이 최대 한 칸 밀려서 스크롤되는 글자와 모자이크가 어긋납니다.

### 렌더 (`app/render.py`)

`cut.mp4`는 미리보기와 OCR 전용입니다. 최종본은 화질 손실을 막으려고 원본에서 같은 프레임 필터로 다시 읽어 한 번만 인코딩합니다.

`render_final`은 ffmpeg 디코더 → numpy → ffmpeg 인코더 구조입니다. 윈도우 기본 파이프는 프레임 전송이 느려서 `media._big_pipe`의 16MB 파이프를 씁니다. 효과가 없는 프레임은 numpy를 거치지 않습니다.

프레임 합성 순서는 모자이크 → 화면 효과(`fx.FrameEffects`) → 일러스트·아이콘·큰 글자(`Layer`) → 자막(`CaptionTrack`)입니다. 모자이크가 먼저여야 확대와 정지 효과에서 가린 내용이 드러나지 않습니다. 자막은 줄 수가 많아 미리 그리지 않고 나올 때 그립니다.

### 자막과 효과

- `captions.py`: `build_items`가 남은 단어를 조각으로 나누고 `restyle`이 말투를 바꿉니다. 화면과 SRT로 나갈 때는 항상 `for_display`를 거칩니다. 자막에 개인정보가 찍히면 모자이크를 해도 소용없기 때문입니다.
- `layout.py`: `scan_people`이 얼굴에서 사람 영역을 추정해 `pii.json`의 `people`에 넣고, `place`가 그 영역과 자막 자리를 피해 위치를 고른 뒤 안 되면 크기를 줄입니다.
- `fx.py`: 스타일별 허용 효과는 `STYLE_TYPES`입니다. 효과 추가는 이 표, `MAX_SECONDS`, `llm.PLAN_SYSTEM`과 스키마의 enum, `web/app.js`의 `fxNames`를 함께 고칩니다. 효과음은 파형을 계산해 만듭니다.

### 개인정보 탐지 (`app/privacy.py`)

`scan_video`의 흐름입니다.

1. `changed_boxes`와 `update_lines`로 바뀐 영역만 다시 OCR합니다.
2. `find_in_text`가 정규식으로 찾고, 줄 안의 위치는 `sub_rect`가 추정합니다.
3. `build_tracks`가 샘플별 검출을 시간 구간으로 잇습니다. 같은 값이 두 번 보이면 추적을 나누고, 다른 값은 위치가 겹쳐도 잇지 않습니다.
4. `rect_at`은 앞뒤 샘플 위치를 모두 덮는 상자를 돌려줍니다. 위치를 보간하면 불규칙한 스크롤에서 글자가 새기 때문입니다.

### 컷 판단 (`app/cuts.py`)

`find_word_candidates`가 규칙으로 후보를 찾고, 키가 있으면 `llm.review_cuts`가 문맥을 검토합니다. `이 이미지를`처럼 정상적인 표현을 더듬음으로 보지 않도록 앞말이 두 글자 이상일 때만 말 시작 실수로 칩니다. `_silent_mask`는 작게 말한 단어를 무음에서 제외합니다.

### 외부 API

- 언어 모델 호출은 모두 `llm.ask_json`을 거칩니다. Claude를 먼저 쓰고(스트리밍, JSON 스키마 출력, `fallbacks="default"`), 키나 크레딧이 없으면 OpenAI 채팅의 strict JSON 스키마로 대신합니다. 스키마는 `additionalProperties: false`이고 모든 속성이 `required`여야 합니다.
- 전사는 `transcribe_openai`가 기본입니다. 단어별 시각은 `whisper-1`만 주므로 다른 모델로 바꾸면 컷 편집이 동작하지 않습니다. `whisper-1`은 2027-02-26 종료 예정이며 그 뒤에는 로컬 전사(`transcribe`)가 남는 경로입니다.
- 이미지 파일명에 프롬프트 해시가 들어가서, 같은 프롬프트는 다시 과금되지 않습니다 (`images._cache_name`).
- 그림은 최대 `images.WORKERS`장을 동시에 만듭니다. 요청 한도(429)에 걸리면 `_wait_out_rate_limit`이 기다렸다 재시도하고, 잔액 부족은 바로 실패로 알립니다. 중지하면 아직 시작하지 않은 그림은 취소합니다.
- 그림 안에는 글자를 넣지 않습니다. 라벨은 `render.build_card`가 맑은 고딕으로 직접 씁니다.
- `assets/logos/`에 없는 로고는 그림으로 지어내지 않습니다.

### 웹 (`app/server.py`, `app/jobs.py`, `web/`)

`only_local` 미들웨어가 다른 사이트의 요청을 막습니다. 작업은 `jobs.job` 하나만 동시에 돌고, 화면은 `/api/job`을 폴링해 로그를 받습니다. 옵션은 브라우저 `localStorage`에 저장되고 서버는 `config.clean_options`로 검증합니다.

## 환경 함정

- OpenCV는 윈도우에서 한글 경로의 파일을 못 여는 경우가 있습니다. 얼굴 모델은 임시 폴더로 복사해서 엽니다.
- faster-whisper에 파일 경로를 주면 PyAV 호환 문제로 실패합니다. 파형 배열을 넘깁니다.
- RapidOCR 기본값 PP-OCRv6에는 한국어 모델이 없어 PP-OCRv5를 지정합니다. 기본 `Det.limit_side_len`(736)은 1080p의 작은 글자를 놓칩니다.
- `anthropic` 2.x는 `httpx`가 아니라 `httpx2`를 씁니다.
- `gpt-image-2`는 이 계정에서 투명 배경을 거절합니다. `images._openai_image`가 불투명으로 받고 `remove_flat_background`가 배경을 지우며, 거절한 모델은 `_opaque_only`에 기억해 같은 요청을 반복하지 않습니다.
- `.env`에 실제 키가 있어서, 테스트에서 주소를 바꾸지 않으면 실제 과금 호출이 나갑니다. `tests/test_images.py`는 키와 주소를 모두 가짜로 바꿉니다. 이 PC의 셸에는 `ANTHROPIC_BASE_URL`이 실제 주소로 설정돼 있으니, 앱이나 스크립트를 시험할 때는 두 주소를 모두 명시합니다.
- Pillow의 `multiline_textbbox`는 소수를 돌려줍니다. `Image.new`에 넘기기 전에 정수로 바꿔야 합니다.
- 윈도우에 `NotoSansKR-VF.ttf`가 없는 PC에서는 `render._display_font`가 맑은 고딕 굵게로 대신합니다.

## 검증 상태

실제 OpenAI 호출은 그림 2장으로만 확인했습니다. OpenAI 전사, 채팅(말투 변환, 기획), 8장 동시 생성은 `tests/mock_apis.py`로만 시험해서 실제 응답의 품질을 모릅니다. 특히 MZ 말투의 자연스러움과 `gpt-5.4-mini`의 strict 스키마 수용 여부를 먼저 확인하세요. 이 계정의 요청 한도도 알 수 없습니다.

실제 Claude 호출은 API 크레딧이 없어 가짜 서버로만 확인했습니다. 크레딧 오류가 요청 형식 오류를 가렸을 수 있습니다. 실제 사람 목소리와 실제 얼굴로도 아직 시험하지 않았으므로, 얼굴 탐지에 기대는 아이콘 회피 배치는 탐지 이후의 배치 로직만 검증된 상태입니다.
