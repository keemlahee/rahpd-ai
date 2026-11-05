"""전사. 추임새가 문장에서 사라지지 않게 받아쓰도록 유도한다.

컷 편집은 단어별 시각이 있어야 동작한다. OpenAI 모델 중 이것을 주는 것은 whisper-1뿐이다.
whisper-1은 2027-02-26 종료 예정이므로, 그 뒤에는 local 전사가 남는 경로다.
"""
from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import openai

from . import media
from .config import DEVICE, LANGUAGE, MODELS, OPENAI_KEY, get_key
from .cuts import HOP, energy_db
from .media import LogFn, Runner, read_wav

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

OPENAI_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
CHUNK_SECONDS = 20 * 60  # 업로드 한도 25MB 안에 들도록 64kbps 기준 약 10MB씩 나눈다
CHUNK_SEARCH = 20.0      # 나눌 지점을 찾는 범위. 말 중간에서 잘리지 않게 가장 조용한 곳을 고른다
CHUNK_WORKERS = 4

# Whisper는 '음', '어'를 지워서 깔끔한 문장을 만드는 습성이 있다. 추임새가 그대로 적힌 문장을
# 초기 프롬프트로 주면 그 문체를 따라가므로, 컷 편집에 필요한 추임새가 전사에 남는다.
VERBATIM_PROMPT = "음, 어, 그러니까요, 어... 음... 말씀드린 대로 어 이렇게 음 진행하겠습니다."


class TranscribeError(RuntimeError):
    pass


def _load_model(name: str, log: LogFn):
    from faster_whisper import WhisperModel

    if DEVICE["kind"] == "cuda":
        try:
            log(f"whisper 모델 로드: {name} (cuda/float16)")
            return WhisperModel(name, device="cuda", compute_type="float16", download_root=str(MODELS))
        except (RuntimeError, ValueError, OSError) as error:
            log(f"GPU 사용 실패, CPU로 전환합니다: {error}")
    log(f"whisper 모델 로드: {name} (cpu/int8) — 처음 한 번은 모델을 내려받습니다")
    return WhisperModel(name, device="cpu", compute_type="int8", download_root=str(MODELS))


def transcribe(audio: Path, model_name: str, duration: float, log: LogFn, runner: Runner) -> dict:
    model = _load_model(model_name, log)
    samples, _ = read_wav(audio)
    segments, _ = model.transcribe(
        # 파일 경로를 주면 PyAV로 다시 디코딩하는데, 이미 추출한 16kHz WAV가 있으니 파형을 바로 넘긴다.
        samples[:, 0].astype(np.float32) / 32768.0,
        language=LANGUAGE,
        beam_size=5,
        word_timestamps=True,
        initial_prompt=VERBATIM_PROMPT,
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters={"threshold": 0.3, "min_silence_duration_ms": 400, "speech_pad_ms": 250},
    )
    result, word_count, next_report = [], 0, 30.0
    for segment in segments:
        runner.check()
        words = [
            {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip(), "prob": round(w.probability, 3)}
            for w in segment.words or []
            if w.word.strip()
        ]
        if not words:
            continue
        result.append({"start": words[0]["start"], "end": words[-1]["end"], "text": segment.text.strip(), "words": words})
        word_count += len(words)
        if segment.end >= next_report:
            log(f"전사 진행: {segment.end:.0f}s / {duration:.0f}s")
            next_report = segment.end + 30.0
    log(f"전사 완료: 문장 {len(result)}개, 단어 {word_count}개")
    return {"segments": result}


def split_points(samples: np.ndarray, rate: int, limit: float = CHUNK_SECONDS) -> list[tuple[float, float]]:
    """오디오를 limit초 안쪽 조각으로 나눈다. 나누는 지점은 주변에서 가장 조용한 곳이다."""
    duration = len(samples) / rate
    if duration <= limit:
        return [(0.0, duration)]
    db = energy_db(samples, rate)
    bounds, start = [], 0.0
    while duration - start > limit:
        first = int((start + limit - CHUNK_SEARCH) / HOP)
        last = int((start + limit) / HOP)
        end = (first + int(np.argmin(db[first:last]))) * HOP
        bounds.append((start, end))
        start = end
    bounds.append((start, duration))
    return bounds


def merge_response(words: list, segments: list, offset: float) -> list[dict]:
    """whisper-1 응답을 로컬 전사와 같은 모양으로 바꾼다.

    단어 목록에는 문장부호가 없어서, 어절 수가 맞으면 구간 텍스트의 어절로 바꿔 넣는다. 문장을 나누는
    cuts.sentences와 자막이 문장부호를 쓴다.
    """
    pending = [{"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "word": w.word.strip(), "prob": 1.0}
               for w in words if w.word.strip()]
    result, index = [], 0
    for position, segment in enumerate(segments):
        limit = float("inf") if position == len(segments) - 1 else segment.end + offset
        group = []
        while index < len(pending) and pending[index]["start"] < limit:
            group.append(pending[index])
            index += 1
        if not group:
            continue
        tokens = segment.text.split()
        if len(tokens) == len(group):
            for word, token in zip(group, tokens):
                word["word"] = token
        result.append({"start": group[0]["start"], "end": group[-1]["end"], "text": segment.text.strip(), "words": group})
    return result


def _request(client: openai.OpenAI, part: Path):
    try:
        with open(part, "rb") as file:
            return client.audio.transcriptions.create(
                model=OPENAI_MODEL, file=file, language=LANGUAGE, prompt=VERBATIM_PROMPT,
                response_format="verbose_json", timestamp_granularities=["word", "segment"])
    except openai.AuthenticationError as error:
        raise TranscribeError("OpenAI API 키가 올바르지 않습니다.") from error
    except openai.RateLimitError as error:
        if error.code == "insufficient_quota":
            raise TranscribeError("OpenAI 크레딧이 부족합니다.") from error
        raise TranscribeError("OpenAI 사용 한도에 걸렸습니다.") from error
    except openai.APIError as error:
        raise TranscribeError(f"OpenAI 전사 오류: {error}") from error


def transcribe_openai(audio: Path, log: LogFn, runner: Runner) -> dict:
    client = openai.OpenAI(api_key=get_key(OPENAI_KEY))
    samples, rate = read_wav(audio)
    bounds = split_points(samples, rate)
    folder = audio.parent / "upload_parts"
    folder.mkdir(exist_ok=True)

    def work(index: int) -> list[dict]:
        start, end = bounds[index]
        part = folder / f"part{index}.mp3"
        media.run_ffmpeg(["-ss", str(start), "-to", str(end), "-i", str(audio), "-ac", "1", "-c:a", "libmp3lame",
                          "-b:a", "64k", str(part)], runner)
        response = _request(client, part)
        log(f"전사 진행: 조각 {index + 1}/{len(bounds)} 완료")
        return merge_response(response.words or [], response.segments or [], start)

    try:
        with ThreadPoolExecutor(max_workers=min(CHUNK_WORKERS, len(bounds))) as pool:
            parts = list(pool.map(work, range(len(bounds))))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    result = [segment for part in parts for segment in part]
    log(f"전사 완료: 문장 {len(result)}개, 단어 {sum(len(s['words']) for s in result)}개")
    return {"segments": result}
