"""화면·음성의 개인정보 탐지.

놓치는 쪽이 잘못 가리는 쪽보다 훨씬 위험하므로 넉넉하게 잡는다. OCR 샘플 사이 시간도 가리고,
시각별 위치를 이어서 스크롤을 따라간다. 결과는 웹 화면에서 끄거나 직접 추가할 수 있다.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np

from . import claude
from .config import MODELS
from .media import LogFn, Runner, iter_frames

Rect = tuple[int, int, int, int]

# 문자열만 보고 판별할 수 있는 형식
PATTERNS: list[tuple[str, re.Pattern]] = [(kind, re.compile(pattern)) for kind, pattern in [
    ("이메일", r"[A-Za-z0-9._%+\-]+\s?@\s?[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"),
    ("휴대전화", r"(?<!\d)(?:\+?82[\s.\-]?)?0?1[016789][\s.\-]?\d{3,4}[\s.\-]?\d{4}(?!\d)"),
    ("전화번호", r"(?<!\d)0(?:2|[3-6][1-5]|70|80)[\s.\-)]\s?\d{3,4}[\s.\-]?\d{4}(?!\d)"),
    ("주민등록번호", r"(?<!\d)\d{6}\s?-\s?[1-8]\d{6}(?!\d)"),
    ("카드번호", r"(?<!\d)\d{4}[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{3,4}(?!\d)"),
    ("사업자등록번호", r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)"),
    ("API 키", r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9\-]{10,}"
              r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,})"),
    # OCR이 '서울'을 '서을'로 읽는 일이 있어 지명 목록이 아니라 '~시/도 ~구 ~로' 구조로 찾는다.
    ("주소", r"(?:(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)[가-힣]*"
            r"|[가-힣]{2,}(?:시|도))\s*[가-힣]{2,}(?:시|군|구)\s*(?:[가-힣]+(?:시|군|구)\s*)?"
            r"[가-힣0-9]+(?:로|길|동|읍|면|리)(?:\s*\d+(?:-\d+)?)?"),
]]
# 앞의 항목명이 있어야 판별되는 값. 그룹 1이 가릴 부분이다.
LABELED: list[tuple[str, re.Pattern]] = [(kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in [
    ("이름", r"(?:이름|성명|받는\s?분|보내는\s?분|예금주|수령인|수취인)\s*[:：]?\s*([가-힣]{2,4})(?![가-힣])"),
    ("주소", r"(?:주소|거주지|배송지|수령지)\s*[:：]\s*(\S.{3,})"),
    ("비밀 값", r"(?:비밀번호|패스워드|password|passwd|pwd|secret|token|api[_\s-]?key)\s*[:=：]\s*[\"']?(\S{3,})"),
    ("계좌번호", r"(?:계좌|은행|국민|신한|우리|하나|농협|기업|카카오뱅크|토스)[^\d]{0,12}(\d{2,6}-\d{2,6}-\d{2,7}(?:-\d{1,3})?)"),
]]
# '김철수 010-1234-5678'처럼 연락처 바로 앞에 성씨로 시작하는 한글이 붙으면 사람 이름으로 본다.
# 성씨 조건이 없으면 '제 번호는 010-...'의 '번호는'까지 이름이 된다.
SURNAMES = "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민나진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용"
NAME_BEFORE_CONTACT = re.compile(rf"(?<![가-힣])([{SURNAMES}][가-힣]{{1,3}})\s*(?=0?1[016789][\s.\-]?\d{{3,4}}|[A-Za-z0-9._%+\-]+\s?@)")
NOT_A_NAME = {"연락처", "전화", "전화번호", "휴대폰", "휴대전화", "이메일", "메일", "문의", "고객센터", "계정", "아이디", "메일주소",
              "주소", "이름"}
# OCR이 숫자를 글자로 잘못 읽는 경우. 글자 수가 같아서 위치 계산에 영향이 없다.
DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1"})

OCR_MIN_SCORE = 0.3
OCR_MAX_SIDE = 1920
LOW_RES_SIDE = 1600      # 긴 변이 이보다 작으면 2배로 키워서 읽는다
THUMB_WIDTH = 960        # 화면 변화 비교용 축소 크기
CHANGE_LEVEL = 12        # 화면 변화로 볼 밝기 차이
CHANGE_MIN_PIXELS = 8    # 이보다 적게 바뀌면 영상 압축 잡음으로 본다
FULL_OCR_AREA = 0.35     # 바뀐 영역이 이 비율을 넘으면 화면 전체를 읽는다
MAX_PATCHES = 12
PATCH_MARGIN = 12
RECT_PAD = 6
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")


def find_in_text(text: str) -> list[tuple[str, int, int]]:
    """문자열에서 (종류, 시작, 끝) 목록을 찾는다."""
    found: list[tuple[str, int, int]] = []
    fixed = re.sub(r"(?<=\d)[OolI|]|[OolI|](?=\d)", lambda m: m.group().translate(DIGIT_FIX), text)
    for kind, pattern in PATTERNS:
        source = text if kind in ("이메일", "API 키", "주소") else fixed
        found += [(kind, m.start(), m.end()) for m in pattern.finditer(source)]
    for kind, pattern in LABELED:
        source = text if kind != "계좌번호" else fixed
        found += [(kind, m.start(1), m.end(1)) for m in pattern.finditer(source)]
    found += [("이름", m.start(1), m.end(1)) for m in NAME_BEFORE_CONTACT.finditer(fixed) if m.group(1) not in NOT_A_NAME]
    return found


def _char_weight(ch: str) -> float:
    if "가" <= ch <= "힣":
        return 1.0
    if ch.isspace() or ch in ".,:;-_|'":
        return 0.35
    return 0.6 if ch.isupper() or ch.isdigit() else 0.52


def sub_rect(box: Rect, text: str, start: int, end: int) -> Rect:
    """줄 전체 상자에서 일치한 글자가 있는 부분을 글자 폭 비율로 추정한다."""
    x0, y0, x1, y1 = box
    weights = [_char_weight(c) for c in text]
    total = sum(weights) or 1.0
    # OCR 상자와 글자 위치가 정확히 맞지 않으므로 앞뒤로 한 글자씩 더 가린다.
    left = sum(weights[:max(0, start - 1)]) / total
    right = sum(weights[:min(len(text), end + 1)]) / total
    if right - left > 0.6:
        return box
    width = x1 - x0
    return int(x0 + width * left), y0, int(x0 + width * right), y1


def _pad(rect: Rect, width: int, height: int, pad: int = RECT_PAD) -> Rect:
    x0, y0, x1, y1 = rect
    return max(0, x0 - pad), max(0, y0 - pad), min(width, x1 + pad), min(height, y1 + pad)


def _iou(a: Rect, b: Rect) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


@dataclass
class Track:
    kind: str
    key: str
    text: str
    points: list[tuple[float, Rect]] = field(default_factory=list)


def _norm_key(text: str) -> str:
    return re.sub(r"[\s\-.]", "", text).lower()


def _distance(a: Rect, b: Rect) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _similar(a: str, b: str) -> bool:
    """OCR 오독 정도의 차이인가. 다른 사람의 값을 같은 추적으로 잇지 않기 위해 본다."""
    return SequenceMatcher(None, a, b).ratio() >= 0.7


def _velocity(frm: tuple[float, Rect], to: tuple[float, Rect]) -> tuple[float, float]:
    span = to[0] - frm[0]
    return (to[1][0] - frm[1][0]) / span, (to[1][1] - frm[1][1]) / span


def _moved(rect: Rect, dx: float, dy: float) -> Rect:
    return int(rect[0] + dx), int(rect[1] + dy), int(rect[2] + dx), int(rect[3] + dy)


def _shared_motion(track: Track, tracks: list[Track]) -> tuple[float, float]:
    """한 번만 잡힌 글자의 속도를 같은 시각의 다른 글자들에서 빌려 온다.

    목록을 스크롤하면 맨 위 줄은 다음 샘플 전에 화면 밖으로 나가 속도를 알 수 없지만, 함께
    보이는 줄들은 같이 움직인다.
    """
    time = track.points[0][0]
    motions = [_velocity(first, second)
               for other in tracks
               for first, second in zip(other.points, other.points[1:])
               if first[0] <= time <= second[0]]
    if not motions:
        return 0.0, 0.0
    return float(np.median([m[0] for m in motions])), float(np.median([m[1] for m in motions]))


def _with_margins(track: Track, tracks: list[Track], interval: float) -> list[list]:
    """앞뒤로 한 샘플 간격을 더 붙인다.

    샘플 사이에 나타나거나 사라졌을 수 있어서다. 움직이던 글자는 같은 속도로 이어 간다고 보고
    위치를 연장하므로, 화면 밖에서 들어오는 도중이라 아직 못 읽은 글자까지 덮인다.
    """
    points = track.points
    if len(points) > 1:
        start_speed, end_speed = _velocity(points[0], points[1]), _velocity(points[-2], points[-1])
    else:
        start_speed = end_speed = _shared_motion(track, tracks)
    head = _moved(points[0][1], -start_speed[0] * interval, -start_speed[1] * interval)
    tail = _moved(points[-1][1], end_speed[0] * interval, end_speed[1] * interval)
    full = [(max(0.0, points[0][0] - interval), head), *points, (points[-1][0] + interval, tail)]
    return [[round(t, 3), list(rect)] for t, rect in full]


def build_tracks(samples: list[tuple[float, list[dict]]], interval: float) -> list[dict]:
    """샘플별 검출을 이어서 시간 구간으로 만든다.

    같은 글자는 위치가 달라도 같은 대상이다(스크롤). 같은 값이 화면에 두 번 보일 수 있으므로
    한 샘플에서 한 추적에는 한 위치만 붙이고, 가장 가까운 추적으로 잇는다.
    """
    max_gap = max(interval * 2.5, 1.0)
    tracks: list[Track] = []
    for time, detections in samples:
        seen: set[tuple[str, Rect]] = set()
        for det in detections:
            if (det["kind"], det["rect"]) in seen:
                continue
            seen.add((det["kind"], det["rect"]))
            key = _norm_key(det["text"])
            alive = [t for t in tracks if t.kind == det["kind"] and 0 < time - t.points[-1][0] <= max_gap]
            same = [t for t in alive if t.key == key]
            if same:
                match = min(same, key=lambda t: _distance(t.points[-1][1], det["rect"]))
            else:
                # OCR이 조금 다르게 읽은 같은 값이면 위치가 겹칠 때 잇는다.
                near = [t for t in alive if _iou(t.points[-1][1], det["rect"]) >= 0.3 and _similar(t.key, key)]
                match = max(near, key=lambda t: _iou(t.points[-1][1], det["rect"]), default=None)
            if match is None:
                match = Track(det["kind"], key, det["text"])
                tracks.append(match)
            match.points.append((time, det["rect"]))
    results = []
    for index, track in enumerate(sorted(tracks, key=lambda t: t.points[0][0]), 1):
        points = _with_margins(track, tracks, interval)
        results.append({"id": index, "kind": track.kind, "text": track.text, "start": points[0][0], "end": points[-1][0],
                        "points": points, "enabled": True, "manual": False})
    return results


class OcrEngine:
    def __init__(self, frame_size: tuple[int, int] = (1920, 1080)) -> None:
        from rapidocr import EngineType, LangRec, ModelType, OCRVersion, RapidOCR

        # 저해상도 영상의 뭉개진 작은 글자는 사람은 읽어도 OCR은 놓친다. 확대해서 읽는다.
        self.upscale = 2 if max(frame_size) < LOW_RES_SIDE else 1
        self.engine = RapidOCR(params={
            "Global.log_level": "critical",
            "Global.text_score": OCR_MIN_SCORE,
            "Global.use_cls": False,  # 화면 글자는 기울어져 있지 않다
            # 기본값(736px)은 1080p 화면의 작은 글자를 놓친다.
            "Det.limit_type": "max",
            "Det.limit_side_len": OCR_MAX_SIDE,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            # 기본 PP-OCRv6에는 한국어 모델이 없다.
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "Rec.lang_type": LangRec.KOREAN,
        })

    def read(self, frame: np.ndarray) -> list[tuple[Rect, str]]:
        scale = self.upscale
        if scale > 1:
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        result = self.engine(frame)
        if result is None or result.boxes is None or result.txts is None:
            return []
        lines = []
        for quad, text in zip(result.boxes, result.txts):
            xs, ys = quad[:, 0] / scale, quad[:, 1] / scale
            lines.append(((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1), str(text)))
        return lines


def _thumb(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (THUMB_WIDTH, max(1, gray.shape[0] * THUMB_WIDTH // gray.shape[1])), interpolation=cv2.INTER_AREA)


def changed_boxes(before: np.ndarray, after: np.ndarray, width: int) -> list[Rect]:
    """두 프레임에서 달라진 영역을 원본 좌표 상자로 돌려준다."""
    mask = (cv2.absdiff(before, after) > CHANGE_LEVEL).astype(np.uint8)
    if np.count_nonzero(mask) < CHANGE_MIN_PIXELS:
        return []
    # 같은 줄의 글자들이 한 상자로 묶이도록 가로로 더 넓게 잇는다.
    mask = cv2.dilate(mask, np.ones((9, 41), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    scale = width / after.shape[1]
    return [(int(stats[i, 0] * scale), int(stats[i, 1] * scale),
             int((stats[i, 0] + stats[i, 2]) * scale), int((stats[i, 1] + stats[i, 3]) * scale))
            for i in range(1, count)]


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def update_lines(ocr: OcrEngine, frame: np.ndarray, lines: list[tuple[Rect, str]], boxes: list[Rect]) -> list[tuple[Rect, str]]:
    """바뀐 영역만 다시 읽는다.

    웹캠이나 마우스만 움직이는 화면에서 프레임 전체를 매번 읽으면 너무 느리다. 바뀐 영역에
    걸친 기존 줄까지 포함해 자르므로, 줄 중간이 잘려서 글자를 놓치는 일은 없다.
    """
    height, width = frame.shape[:2]
    if len(boxes) > MAX_PATCHES or sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes) > width * height * FULL_OCR_AREA:
        return ocr.read(frame)
    kept = list(lines)
    for box in boxes:
        touching = [line for line in kept if _overlaps(line[0], box)]
        kept = [line for line in kept if line not in touching]
        rects = [box] + [line[0] for line in touching]
        x0 = max(0, min(r[0] for r in rects) - PATCH_MARGIN)
        y0 = max(0, min(r[1] for r in rects) - PATCH_MARGIN)
        x1 = min(width, max(r[2] for r in rects) + PATCH_MARGIN)
        y1 = min(height, max(r[3] for r in rects) + PATCH_MARGIN)
        for rect, text in ocr.read(frame[y0:y1, x0:x1]):
            kept.append(((rect[0] + x0, rect[1] + y0, rect[2] + x0, rect[3] + y0), text))
    return kept


def _line_detections(lines: list[tuple[Rect, str]], width: int, height: int, extra: dict[str, list[dict]]) -> list[dict]:
    detections = []
    for box, text in lines:
        for kind, start, end in find_in_text(text):
            detections.append({"kind": kind, "text": text[start:end], "rect": _pad(sub_rect(box, text, start, end), width, height)})
        for item in extra.get(text, ()):
            start = text.find(item["text"])
            if start >= 0:
                rect = sub_rect(box, text, start, start + len(item["text"]))
                detections.append({"kind": item["kind"], "text": item["text"], "rect": _pad(rect, width, height)})
    # 항목명 칸과 값 칸이 나뉜 표: '이름' 상자 바로 오른쪽 상자가 값이다.
    for box, text in lines:
        if not re.fullmatch(r"\s*(이름|성명|예금주|수령인|수취인)\s*[:：]?\s*", text):
            continue
        for other, value in lines:
            same_row = abs((other[1] + other[3]) - (box[1] + box[3])) < (box[3] - box[1])
            if same_row and 0 <= other[0] - box[2] < width * 0.25 and re.fullmatch(r"[가-힣]{2,4}", value.strip()):
                detections.append({"kind": "이름", "text": value.strip(), "rect": _pad(other, width, height)})
    return detections


def _yunet_path(log: LogFn) -> Path:
    model = MODELS / "face_detection_yunet_2023mar.onnx"
    if not model.exists():
        log("얼굴 인식 모델 내려받는 중")
        temp = model.with_suffix(".part")
        urllib.request.urlretrieve(YUNET_URL, temp)
        temp.replace(model)
    # OpenCV는 윈도우에서 한글 경로의 파일을 못 여는 경우가 있어 임시 폴더로 복사해서 쓴다.
    target = Path(tempfile.gettempdir()) / model.name
    if not target.exists():
        shutil.copy(model, target)
    return target


class FaceFinder:
    def __init__(self, width: int, height: int, log: LogFn) -> None:
        self.detector = cv2.FaceDetectorYN.create(str(_yunet_path(log)), "", (width, height), 0.6, 0.3, 5000)
        self.width, self.height = width, height

    def find(self, frame: np.ndarray) -> list[dict]:
        _, faces = self.detector.detect(frame)
        found = []
        for face in faces if faces is not None else ():
            x, y, w, h = face[:4]
            rect = (int(x), int(y), int(x + w), int(y + h))
            found.append({"kind": "얼굴", "text": "", "rect": _pad(rect, self.width, self.height, int(h * 0.2))})
        return found


def scan_video(video: Path, info: dict, options: dict, log: LogFn, runner: Runner) -> list[dict]:
    interval = options["ocr_interval"]
    width, height = info["width"], info["height"]
    ocr = OcrEngine((width, height)) if options["mosaic"] else None
    faces = FaceFinder(width, height, log) if options["face_mosaic"] else None
    use_claude = options["pii_claude"] and ocr is not None and claude.available()

    samples: list[tuple[float, list[dict]]] = []
    frames_lines: list[tuple[float, list[tuple[Rect, str]], list[dict]]] = []
    prev, lines, face_dets = None, [], []
    scanned = total = 0
    for time, frame in iter_frames(video, info, interval, runner):
        total += 1
        thumb = _thumb(frame)
        boxes = None if prev is None else changed_boxes(prev, thumb, width)
        if boxes is None or boxes:
            prev = thumb
            scanned += 1
            if ocr:
                lines = ocr.read(frame) if boxes is None else update_lines(ocr, frame, lines, boxes)
            if faces:
                face_dets = faces.find(frame)
        frames_lines.append((time, lines, face_dets))
        if total % 40 == 0:
            log(f"개인정보 탐지 진행: {time:.0f}s / {info['duration']:.0f}s (OCR {scanned}장)")
    log(f"OCR 프레임 {scanned}장 처리 (샘플 {total}장 중, 나머지는 화면이 같아 재사용)")

    extra: dict[str, list[dict]] = {}
    if use_claude:
        unique = sorted({text for _, lines_at, _ in frames_lines for _, text in lines_at if re.search(r"[가-힣A-Za-z]{2}", text)})
        if unique:
            log(f"Claude 보조 판별: 화면 글자 {len(unique)}줄 전송")
            try:
                for item in claude.find_pii(unique):
                    if 0 <= item["id"] < len(unique):
                        extra.setdefault(unique[item["id"]], []).append({"text": item["text"], "kind": item["kind"] or "이름"})
            except claude.ClaudeError as error:
                log(f"Claude 보조 판별 실패, 규칙 탐지 결과만 씁니다: {error}")

    for time, lines_at, face_dets in frames_lines:
        samples.append((time, _line_detections(lines_at, width, height, extra) + list(face_dets)))
    return build_tracks(samples, interval)


def rect_at(track: dict, t: float) -> Rect:
    """시각 t에 가려야 할 영역: 앞뒤 샘플의 위치를 모두 덮는 상자.

    샘플 사이의 위치를 비율로 추정하면 마우스 휠처럼 불규칙한 스크롤에서 글자가 모자이크 밖으로
    나온다. 움직이는 동안 조금 넓게 가리는 쪽이 안전하다.
    """
    points = track["points"]
    if t <= points[0][0]:
        return tuple(points[0][1])
    for (t0, r0), (t1, r1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return min(r0[0], r1[0]), min(r0[1], r1[1]), max(r0[2], r1[2]), max(r0[3], r1[3])
    return tuple(points[-1][1])


def find_spoken_pii(segments: list[dict], use_claude: bool, log: LogFn) -> list[dict]:
    """음성으로 말한 개인정보의 구간. 시각은 컷 편집본 기준이다."""
    words = [w for s in segments for w in s["words"] if not w["removed"]]
    spans: list[dict] = []

    def add(first: int, last: int, kind: str, text: str) -> None:
        spans.append({"id": len(spans) + 1, "start": words[first]["start"], "end": words[last]["end"],
                      "kind": kind, "text": text, "enabled": True})

    joined, offsets = "", []
    for index, word in enumerate(words):
        offsets.append((len(joined), index))
        joined += word["word"] + " "

    def word_at(position: int) -> int:
        return max((i for start, i in offsets if start <= position), default=0)

    for kind, start, end in find_in_text(joined):
        add(word_at(start), word_at(max(start, end - 1)), kind, joined[start:end])
    # '공일공 일이삼사'처럼 숫자를 말로 읽은 경우
    for match in re.finditer(r"(?:[공영일이삼사오육칠팔구]{2,}\s*){3,}", joined):
        add(word_at(match.start()), word_at(match.end() - 2), "숫자 나열", match.group().strip())

    if use_claude and joined.strip():
        try:
            lines = [s["text"] for s in segments if s["text"]]
            for item in claude.find_pii(lines):
                position = joined.find(item["text"])
                if position >= 0:
                    add(word_at(position), word_at(position + len(item["text"]) - 1), item["kind"], item["text"])
        except claude.ClaudeError as error:
            log(f"Claude 음성 판별 실패, 규칙 탐지 결과만 씁니다: {error}")
    return spans
