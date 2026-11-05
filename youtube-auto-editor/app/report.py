"""자막(SRT)과 편집 리포트."""
from __future__ import annotations

from .images import find_logo

MAX_CHARS = 36
MAX_SECONDS = 6.0
TYPE_NAMES = {"silence": "무음", "filler": "추임새", "stutter": "말더듬", "sound": "짧은 소리"}


def _srt_time(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def build_srt(segments: list[dict]) -> str:
    """컷 편집 후 남은 단어로 자막을 나눈다. 시각은 최종 영상 기준이다."""
    blocks, chunk = [], []

    def flush() -> None:
        if chunk:
            text = " ".join(w["word"] for w in chunk)
            blocks.append(f"{len(blocks) + 1}\n{_srt_time(chunk[0]['start'])} --> {_srt_time(chunk[-1]['end'])}\n{text}\n")
            chunk.clear()

    for segment in segments:
        for word in (w for w in segment["words"] if not w["removed"]):
            length = sum(len(w["word"]) + 1 for w in chunk) + len(word["word"])
            if chunk and (length > MAX_CHARS or word["end"] - chunk[0]["start"] > MAX_SECONDS):
                flush()
            chunk.append(word)
        flush()
    return "\n".join(blocks)


def summarize(state: dict) -> dict:
    """리포트 화면과 MD 파일이 함께 쓰는 수치."""
    cut_plan, plan, pii = state["cut_plan"], state["plan"], state["pii"]
    enabled = [c for c in cut_plan["cuts"] if c["enabled"]]
    return {
        "source_duration": round(state["source_duration"], 1),
        "final_duration": round(state["final_duration"], 1),
        "saved": round(state["source_duration"] - state["final_duration"], 1),
        "cuts": {name: sum(c["type"] == kind for c in enabled) for kind, name in TYPE_NAMES.items()},
        "illustrations": sum(1 for i in plan["illustrations"] if i.get("image") and i.get("enabled", True)),
        "overlays": sum(1 for i in plan["overlays"] if (i.get("image") or find_logo(i.get("logo", ""))) and i.get("enabled", True)),
        "mosaics": sum(1 for t in pii["tracks"] if t["enabled"]),
        "muted": sum(1 for s in pii["speech"] if s["enabled"]),
        "warnings": state["warnings"],
    }


def build_markdown(name: str, summary: dict, state: dict) -> str:
    lines = [
        f"# 편집 리포트: {name}", "",
        f"- 원본 길이: {summary['source_duration']}초",
        f"- 편집 후 길이: {summary['final_duration']}초 ({summary['saved']}초 단축)",
        "- 컷: " + ", ".join(f"{k} {v}곳" for k, v in summary["cuts"].items()),
        f"- 설명 일러스트 {summary['illustrations']}개, 로고·아이콘 {summary['overlays']}개",
        f"- 화면 모자이크 {summary['mosaics']}곳, 음성 음소거 {summary['muted']}곳", "",
    ]
    if summary["warnings"]:
        lines += ["## 확인이 필요한 내용", ""] + [f"- {w}" for w in summary["warnings"]] + [""]

    tracks = [t for t in state["pii"]["tracks"] if t["enabled"]]
    if tracks:
        lines += ["## 모자이크 (최종 영상 기준)", "", "| 시각 | 종류 |", "|---|---|"]
        lines += [f"| {clock(t['start'])}~{clock(t['end'])} | {t['kind']} |" for t in tracks] + [""]
    illustrations = state["plan"]["illustrations"]
    if illustrations:
        lines += ["## 설명 일러스트", "", "| 시각 | 라벨 | 개념 |", "|---|---|---|"]
        lines += [f"| {clock(i['start'])}~{clock(i['end'])} | {i.get('label', '')} | {i.get('concept', '')} |"
                  for i in illustrations] + [""]
    word_cuts = [c for c in state["cut_plan"]["cuts"] if c["enabled"] and c["type"] in ("filler", "stutter")]
    if word_cuts:
        lines += ["## 지운 말 (원본 기준)", "", "| 시각 | 내용 | 이유 |", "|---|---|---|"]
        lines += [f"| {clock(c['start'])} | {c['text']} | {c['reason']} |" for c in word_cuts] + [""]
    return "\n".join(lines)
