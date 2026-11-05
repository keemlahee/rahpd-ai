"""faster-whisper 전사. 추임새가 문장에서 사라지지 않게 받아쓰도록 유도한다."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .config import DEVICE, LANGUAGE, MODELS
from .media import LogFn, Runner, read_wav

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Whisper는 '음', '어'를 지워서 깔끔한 문장을 만드는 습성이 있다. 추임새가 그대로 적힌 문장을
# 초기 프롬프트로 주면 그 문체를 따라가므로, 컷 편집에 필요한 추임새가 전사에 남는다.
VERBATIM_PROMPT = "음, 어, 그러니까요, 어... 음... 말씀드린 대로 어 이렇게 음 진행하겠습니다."


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
