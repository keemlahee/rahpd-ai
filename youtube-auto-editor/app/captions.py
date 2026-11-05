"""화면 자막: 대본을 읽기 좋은 조각으로 나누고, MZ 말투로 바꾸고, 개인정보를 가린다."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import llm
from .media import LogFn
from .privacy import find_in_text

MAX_CHARS = 22
MAX_SECONDS = 5.0
MAX_GAP = 1.0        # 말이 이만큼 끊기면 자막을 나눈다
LEFTOVER_CHARS = 5   # 이보다 짧은 끝말은 앞줄에 붙인다
MAX_JOINED = 30      # 붙인 뒤의 최대 길이. 화면에서는 두 줄로 나뉜다
MIN_SECONDS = 0.8    # 읽을 시간이 없는 짧은 자막은 다음 자막 전까지 늘린다
BATCH = 120          # 긴 영상의 응답이 잘리지 않게 나눠서 보낸다
BATCH_WORKERS = 3
MASK = "●●●"
MUTED = "(삐-)"
SENTENCE_END = ".?!…"


def build_items(segments: list[dict]) -> list[dict]:
    """컷 편집 후 남은 단어로 자막 조각을 만든다. 시각은 최종 영상 기준이다."""
    items: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if current:
            items.append({"id": len(items) + 1, "start": current[0]["start"], "end": current[-1]["end"],
                          "source": " ".join(w["word"] for w in current)})
            current.clear()

    for segment in segments:
        for word in (w for w in segment["words"] if not w["removed"]):
            if current:
                length = sum(len(w["word"]) + 1 for w in current) + len(word["word"])
                if (length > MAX_CHARS or word["end"] - current[0]["start"] > MAX_SECONDS
                        or word["start"] - current[-1]["end"] > MAX_GAP):
                    flush()
            current.append(word)
            if word["word"][-1] in SENTENCE_END:
                flush()
    flush()
    items = _join_leftovers(items)
    for item, following in zip(items, items[1:] + [None]):
        limit = following["start"] if following else item["end"] + MIN_SECONDS
        item["end"] = round(min(max(item["end"], item["start"] + MIN_SECONDS), limit), 3)
    return items


def _join_leftovers(items: list[dict]) -> list[dict]:
    """글자 수로 자르다 끝말만 혼자 남은 줄('줍니다')을 앞줄에 붙인다. 말투를 바꿀 때도 줄 단위라 어색해진다."""
    joined: list[dict] = []
    for item in items:
        previous = joined[-1] if joined else None
        if (previous and len(item["source"]) <= LEFTOVER_CHARS and previous["source"][-1] not in SENTENCE_END
                and item["start"] - previous["end"] <= MAX_GAP and len(previous["source"]) + len(item["source"]) < MAX_JOINED):
            previous["source"] += " " + item["source"]
            previous["end"] = item["end"]
        else:
            joined.append(item)
    for index, item in enumerate(joined, 1):
        item["id"] = index
    return joined


def restyle(items: list[dict], log: LogFn) -> None:
    """각 조각의 text를 MZ 말투로 채운다. 실패하면 llm.LLMError를 낸다."""
    lines = [{"id": item["id"], "text": item["source"]} for item in items]
    batches = [lines[start:start + BATCH] for start in range(0, len(lines), BATCH)]
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        results = list(pool.map(llm.restyle_lines, batches))
    styled = {key: value for result in results for key, value in result.items()}
    changed = 0
    for item in items:
        text = styled.get(item["id"], "").strip()
        # 말하는 시간 안에 못 읽을 만큼 길어진 줄은 원문을 쓴다.
        if text and len(text) <= max(len(item["source"]) * 2, 30):
            item["text"] = text
            changed += text != item["source"]
    log(f"자막 말투 변환: {len(items)}줄 중 {changed}줄 변경")


def mask_pii(text: str) -> str:
    """자막에 찍히면 모자이크를 해도 소용없으므로 글자 자체를 가린다."""
    spans = sorted((start, end) for _, start, end in find_in_text(text))
    pieces, cursor = [], 0
    for start, end in spans:
        if start < cursor:
            cursor = max(cursor, end)
            continue
        pieces += [text[cursor:start], MASK]
        cursor = end
    return "".join(pieces) + text[cursor:]


def for_display(items: list[dict], speech: list[dict]) -> list[dict]:
    """화면과 SRT에 내보낼 자막. 음소거한 구간의 자막은 내용을 지운다."""
    muted = [(s["start"], s["end"]) for s in speech if s["enabled"]]
    output = []
    for item in items:
        hidden = any(start < item["end"] and end > item["start"] for start, end in muted)
        output.append({**item, "text": MUTED if hidden else mask_pii(item["text"])})
    return output
