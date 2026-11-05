"""화면 위 요소의 자리 잡기: 사람을 가리지 않는 위치와 크기를 고른다."""
from __future__ import annotations

from typing import Iterable

from . import media
from .media import LogFn, Runner
from .privacy import FaceFinder

Rect = tuple[int, int, int, int]

SAMPLE_SECONDS = 1.0
SCALES = (1.0, 0.8, 0.62, 0.5)
SUBTITLE_BAND = 0.2      # 화면 아래쪽에서 자막이 차지하는 비율
# (가로, 세로) 빈 공간에서의 비율. 위쪽 모서리부터 시도해 시선을 덜 방해한다.
CORNERS = ((1, 0), (0, 0), (1, 0.45), (0, 0.45), (1, 0.75), (0, 0.75))
TEXT_SPOTS = ((0.5, 0.1), (0.5, 0.35), (0, 0.15), (1, 0.15), (0, 0.5), (1, 0.5))


def person_zone(face: Rect, width: int, height: int) -> Rect:
    """얼굴 상자에서 사람 전체가 차지할 영역을 추정한다. 어깨 폭만큼 넓히고 화면 아래까지 잇는다."""
    x0, y0, x1, y1 = face
    side = int((x1 - x0) * 1.2)
    return max(0, x0 - side), max(0, y0 - (y1 - y0) // 2), min(width, x1 + side), height


def scan_people(video, info: dict, log: LogFn, runner: Runner) -> list[dict]:
    """1초마다 얼굴을 찾아 [{t, zones}]를 돌려준다. 얼굴이 없는 샘플은 뺀다."""
    width, height = info["width"], info["height"]
    try:
        finder = FaceFinder(width, height, log)
    except OSError as error:
        log(f"사람 위치를 찾지 못해 기본 위치를 씁니다: {error}")
        return []
    people = []
    for time, frame in media.iter_frames(video, info, SAMPLE_SECONDS, runner):
        zones = [list(person_zone(found["rect"], width, height)) for found in finder.find(frame)]
        if zones:
            people.append({"t": round(time, 2), "zones": zones})
    return people


def zones_during(people: list[dict] | None, start: float, end: float) -> list[Rect]:
    """그 시간 동안 사람이 있었던 영역 모두. 움직이는 사람도 피하도록 앞뒤 샘플까지 본다."""
    return [tuple(zone) for sample in people or ()
            if start - SAMPLE_SECONDS <= sample["t"] <= end + SAMPLE_SECONDS for zone in sample["zones"]]


def overlap_area(a: Rect, b: Rect) -> int:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return width * height if width > 0 and height > 0 else 0


def subtitle_band(width: int, height: int) -> Rect:
    return 0, int(height * (1 - SUBTITLE_BAND)), width, height


def place(box_width: int, box_height: int, width: int, height: int, avoid: Iterable[Rect], margin: int,
          spots: tuple = CORNERS, scales: tuple = SCALES) -> tuple[int, int, float]:
    """피할 영역과 겹치지 않는 (x, y, 크기 배율)을 고른다.

    같은 크기로 자리를 먼저 찾고, 없으면 줄인다. 어디에도 못 들어가면 가장 덜 겹치는 자리를 쓴다.
    """
    avoid = list(avoid)
    best: tuple[int, int, int, float] | None = None
    for scale in scales:
        w, h = int(box_width * scale), int(box_height * scale)
        for fx, fy in spots:
            x = int(margin + fx * max(0, width - w - margin * 2))
            y = int(margin + fy * max(0, height - h - margin * 2))
            area = sum(overlap_area((x, y, x + w, y + h), rect) for rect in avoid)
            if area == 0:
                return x, y, scale
            if best is None or area < best[0]:
                best = (area, x, y, scale)
    return best[1], best[2], best[3]
