"""1차 컷 편집 계획: 무음, 추임새, 말더듬.

원칙은 '확실한 것만 자른다'이다. 무음을 남겨서 생기는 손해는 작지만, 말을 잘라서 생기는 손해는
크다. 그래서 작게 말한 단어는 무음으로 보지 않고, '이 이미지'처럼 정상적인 표현은 더듬음으로
보지 않으며, Claude 키가 있으면 문맥을 보고 후보를 한 번 더 걸러낸다.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from pathlib import Path

import numpy as np

from .media import LogFn, read_wav

HOP = 0.01  # 에너지 분석 간격(초)
FILLER_RE = re.compile(r"(어|음|으|에|흠|엄|어음|으음)\1{0,3}")
EDGE_CHARS = " .,?!…~·-—\"'“”‘’"
# 강조를 위한 반복이라 지우면 말맛이 달라지는 단어
EMPHASIS_REPEATS = {"네", "예", "응", "하나", "천천히", "빨리", "점점", "자꾸", "정말", "진짜", "너무", "아주", "많이",
                    "매우", "계속", "더", "또", "차근차근", "조금", "좀"}
# 앞 단어가 뒷 단어의 첫머리와 같아도 '이 이미지'처럼 정상일 수 있어서 두 글자 이상만 더듬음으로 본다.
MIN_FALSE_START_LEN = 2
MAX_REPEAT_GAP = 0.8
BLIP_MAX = 0.7          # 전사되지 않은 짧은 소리의 최대 길이
BLIP_MARGIN = 0.15      # 그 소리 앞뒤로 있어야 하는 무음
MIN_CUT = 0.12          # 이보다 짧은 컷은 튀기만 하므로 하지 않는다
MIN_KEEP_NO_WORD = 0.25 # 컷 사이에 남은, 말이 없는 조각은 이 길이 미만이면 함께 지운다


def norm(word: str) -> str:
    return word.strip(EDGE_CHARS)


def flatten_words(transcript: dict) -> list[dict]:
    return [word for segment in transcript["segments"] for word in segment["words"]]


def energy_db(samples: np.ndarray, rate: int) -> np.ndarray:
    hop = int(rate * HOP)
    count = len(samples) // hop
    if count == 0:
        return np.zeros(0)
    mono = samples[: count * hop, 0].astype(np.float32).reshape(count, hop)
    rms = np.sqrt(np.mean(mono ** 2, axis=1)) + 1e-6
    return 20 * np.log10(rms / 32768.0)


def silence_threshold(db: np.ndarray) -> float:
    """영상마다 마이크와 잡음 수준이 달라서 기준을 소리 분포에서 계산한다."""
    noise = max(float(np.percentile(db, 10)), -70.0)
    loud = db[db > noise + 6]
    speech = float(np.percentile(loud, 80)) if loud.size else noise + 20
    return float(np.clip(noise + 0.25 * (speech - noise), -55.0, -28.0))


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """True가 이어지는 구간을 [시작, 끝) 인덱스로 돌려준다."""
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()))


def _silent_mask(db: np.ndarray, threshold: float, words: list[dict]) -> np.ndarray:
    silent = db < threshold
    # 마우스 클릭이나 키보드 소리 같은 아주 짧은 잡음은 무음으로 친다.
    for first, last in _runs(~silent):
        if last - first < 6:
            silent[first:last] = True
    # 작게 말한 단어 보호: 전사된 단어 안에서 기준보다 10dB 낮은 소리까지는 말로 본다.
    # 단어 시간이 무음까지 늘어나는 경우가 있어 단어 구간 전체를 보호하지는 않는다.
    soft = db > threshold - 10
    for word in words:
        first, last = int(word["start"] / HOP), int(word["end"] / HOP)
        voiced = np.flatnonzero(soft[first:last])
        if voiced.size:
            silent[first + voiced[0]: first + voiced[-1] + 1] = False
    return silent


def _valley(db: np.ndarray, center: float, before: float, after: float) -> float:
    """단어 경계 주변에서 가장 조용한 지점을 찾아 다음 말의 첫소리가 잘리지 않게 한다."""
    first = max(0, int((center - before) / HOP))
    last = min(len(db), int((center + after) / HOP))
    return (first + int(np.argmin(db[first:last]))) * HOP if last > first else center


def _word_cut(words: list[dict], index: int, db: np.ndarray) -> tuple[float, float]:
    word = words[index]
    start = _valley(db, word["start"], 0.06, 0.04)
    end = _valley(db, word["end"], 0.04, 0.08)
    if index > 0:
        start = max(start, words[index - 1]["end"] - 0.01)
    if index + 1 < len(words):
        end = min(end, words[index + 1]["start"] + 0.01)
    return start, max(end, start)


def find_word_candidates(words: list[dict]) -> list[dict]:
    found = []
    for index, word in enumerate(words):
        text = norm(word["word"])
        if not text:
            continue
        if FILLER_RE.fullmatch(text):
            found.append({"index": index, "type": "filler", "text": text, "reason": "추임새"})
            continue
        if index + 1 >= len(words):
            continue
        following = norm(words[index + 1]["word"])
        if words[index + 1]["start"] - word["end"] > MAX_REPEAT_GAP:
            continue
        if text == following and text not in EMPHASIS_REPEATS:
            found.append({"index": index, "type": "stutter", "text": text, "reason": "같은 단어 반복"})
        elif len(text) >= MIN_FALSE_START_LEN and len(following) > len(text) and following.startswith(text):
            found.append({"index": index, "type": "stutter", "text": text, "reason": "말을 시작하다 다시 함"})
    return found


def find_blips(db: np.ndarray, silent: np.ndarray, words: list[dict]) -> list[tuple[float, float]]:
    """전사에 안 잡힌 짧고 고립된 소리('음...'이나 숨소리)를 찾는다."""
    spans = sorted((w["start"], w["end"]) for w in words)
    blips = []
    for first, last in _runs(~silent):
        start, end = first * HOP, last * HOP
        if not 0.1 <= end - start <= BLIP_MAX:
            continue
        margin = int(BLIP_MARGIN / HOP)
        if not silent[max(0, first - margin):first].all() or not silent[last:last + margin].all():
            continue
        if first < margin or last + margin >= len(silent):
            continue  # 영상 맨 앞뒤의 소리는 건드리지 않는다
        if any(a < end + 0.1 and b > start - 0.1 for a, b in spans):
            continue
        blips.append((start, end))
    return blips


def _merge(ranges: list[tuple[float, float]], gap: float = 0.03) -> list[list[float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def keeps_from_cuts(cuts: list[dict], duration: float, words: list[dict]) -> list[list[float]]:
    """활성화된 컷의 여집합을 남길 구간으로 만든다."""
    ranges = [(c["start"], c["end"]) for c in cuts if c["enabled"] and c["end"] - c["start"] >= MIN_CUT]
    removed = _merge(ranges)
    keeps, cursor = [], 0.0
    for start, end in removed:
        if start > cursor:
            keeps.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < duration:
        keeps.append([cursor, duration])

    def has_word(keep: list[float]) -> bool:
        return any(w["start"] < keep[1] and w["end"] > keep[0] for w in words)

    inner = [k for i, k in enumerate(keeps) if 0 < i < len(keeps) - 1]
    dropped = {id(k) for k in inner if k[1] - k[0] < MIN_KEEP_NO_WORD and not has_word(k)}
    return [k for k in keeps if id(k) not in dropped] or [[0.0, duration]]


def to_frames(keeps: list[list[float]], fps: float, total_frames: int) -> list[list[int]]:
    frames: list[list[int]] = []
    for start, end in keeps:
        first, last = round(start * fps), min(round(end * fps), total_frames) - 1
        if last < first:
            continue
        if frames and first <= frames[-1][1] + 1:
            frames[-1][1] = max(frames[-1][1], last)
        else:
            frames.append([first, last])
    return frames


class TimeMap:
    """원본 시각과 컷 편집본 시각 사이의 변환."""

    def __init__(self, keeps: list[list[int]], fps: float) -> None:
        self.fps = fps
        self.starts = [first / fps for first, _ in keeps]
        self.ends = [(last + 1) / fps for _, last in keeps]
        offset, self.offsets = 0.0, []
        for start, end in zip(self.starts, self.ends):
            self.offsets.append(offset)
            offset += end - start
        self.total = offset

    def contains(self, t: float) -> bool:
        index = bisect_right(self.starts, t) - 1
        return index >= 0 and t < self.ends[index]

    def to_cut(self, t: float) -> float:
        index = bisect_right(self.starts, t) - 1
        if index < 0:
            return 0.0
        return self.offsets[index] + min(t, self.ends[index]) - self.starts[index]


def make_plan(transcript: dict, audio: Path, duration: float, options: dict, log: LogFn) -> dict:
    words = flatten_words(transcript)
    samples, rate = read_wav(audio)
    db = energy_db(samples, rate)
    threshold = silence_threshold(db) if db.size else -40.0
    plan = {"threshold_db": round(threshold, 1), "cuts": [], "edited": False}
    if not options["cut"] or db.size == 0:
        return plan

    silent = _silent_mask(db, threshold, words)
    pad = options["silence_keep"] / 2
    cuts: list[dict] = []

    def add(start: float, end: float, kind: str, text: str, reason: str, word_index: int | None = None) -> None:
        cuts.append({"id": len(cuts) + 1, "start": round(start, 3), "end": round(end, 3), "type": kind, "text": text,
                     "reason": reason, "word_index": word_index, "enabled": True})

    for first, last in _runs(silent):
        length = (last - first) * HOP
        if length < options["silence_min"]:
            continue
        start = first * HOP + (pad if first > 0 else 0.0)
        end = last * HOP - (pad if last < len(db) else 0.0)
        if end - start >= MIN_CUT:
            add(start, end, "silence", "", f"무음 {length:.1f}초")

    for candidate in find_word_candidates(words):
        start, end = _word_cut(words, candidate["index"], db)
        add(start, end, candidate["type"], candidate["text"], candidate["reason"], candidate["index"])

    for start, end in find_blips(db, silent, words):
        add(start, end, "sound", "", "전사되지 않은 짧은 소리")

    plan["cuts"] = sorted(cuts, key=lambda c: c["start"])
    counts = {kind: sum(c["type"] == kind for c in cuts) for kind in ("silence", "filler", "stutter", "sound")}
    log(f"컷 후보: 무음 {counts['silence']}곳, 추임새 {counts['filler']}곳, 말더듬 {counts['stutter']}곳, "
        f"짧은 소리 {counts['sound']}곳 (무음 기준 {threshold:.0f}dB)")
    return plan


def apply_review(plan: dict, words: list[dict], db: np.ndarray, review: dict, log: LogFn) -> None:
    """Claude의 판단을 반영한다. 추가 컷은 짧은 구간만 허용해 문장이 통째로 지워지지 않게 한다."""
    keep_ids = set(review.get("keep_candidates", []))
    restored = 0
    for cut in plan["cuts"]:
        if cut["id"] in keep_ids and cut["word_index"] is not None:
            cut["enabled"] = False
            cut["reason"] += " → 문맥상 유지"
            restored += 1

    next_id = max((c["id"] for c in plan["cuts"]), default=0) + 1
    added = 0
    for extra in review.get("extra_cuts", []):
        first, last = extra.get("first_word", -1), extra.get("last_word", -1)
        if not 0 <= first <= last < len(words) or last - first >= 5:
            continue
        start = _word_cut(words, first, db)[0]
        end = _word_cut(words, last, db)[1]
        if end - start > 3.0:
            continue
        text = " ".join(norm(w["word"]) for w in words[first:last + 1])
        plan["cuts"].append({"id": next_id, "start": round(start, 3), "end": round(end, 3), "type": "filler",
                             "text": text, "reason": extra.get("reason") or "문맥상 군말", "word_index": first,
                             "enabled": True})
        next_id += 1
        added += 1
    plan["cuts"].sort(key=lambda c: c["start"])
    log(f"문맥 검토: 후보 {restored}곳 유지, 군말 {added}곳 추가")


def sentences(segments: list[dict], max_chars: int = 70) -> list[dict]:
    """남은 단어를 문장 단위로 묶는다. Whisper 구간은 수십 초로 길어서 시각을 잡기에 너무 거칠다."""
    result: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if current:
            result.append({"start": current[0]["start"], "end": current[-1]["end"],
                           "text": " ".join(w["word"] for w in current)})
            current.clear()

    for segment in segments:
        for word in (w for w in segment["words"] if not w["removed"]):
            current.append(word)
            if word["word"][-1] in ".?!…" or sum(len(w["word"]) + 1 for w in current) >= max_chars:
                flush()
    flush()
    return result


def remap_transcript(transcript: dict, timemap: TimeMap) -> dict:
    """컷 편집본 시각의 대본을 만든다. removed는 대본 화면에서 지워진 말을 보여 줄 때 쓴다."""
    segments = []
    for segment in transcript["segments"]:
        words = []
        for word in segment["words"]:
            middle = (word["start"] + word["end"]) / 2
            kept = timemap.contains(middle)
            start = timemap.to_cut(word["start"])
            words.append({"word": word["word"], "start": round(start, 3),
                          "end": round(max(timemap.to_cut(word["end"]), start), 3), "removed": not kept})
        alive = [w for w in words if not w["removed"]]
        segments.append({
            "start": alive[0]["start"] if alive else None,
            "end": alive[-1]["end"] if alive else None,
            "text": " ".join(w["word"] for w in alive),
            "words": words,
        })
    return {"segments": segments}
