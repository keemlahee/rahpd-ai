"""예능 효과: 화면 효과와 직접 합성한 효과음.

방송의 로고, 글꼴, 음원은 쓰지 않고 한국 예능 특유의 편집 문법만 따른다. 효과음은 파형을 계산해서
만들기 때문에 저작권 문제가 없다.
"""
from __future__ import annotations

import math
from functools import cached_property

import cv2
import numpy as np

STYLE_TYPES = {
    "off": (),
    "variety": ("punch_zoom", "shake", "focus_lines", "freeze", "impact_text"),
    "survival": ("mono_tension", "slow_zoom", "title_card", "punch_zoom"),
}
STYLE_TYPES["mixed"] = tuple(dict.fromkeys(STYLE_TYPES["variety"] + STYLE_TYPES["survival"]))
TEXT_TYPES = ("impact_text", "title_card")
FRAME_TYPES = ("punch_zoom", "shake", "focus_lines", "freeze", "mono_tension", "slow_zoom")
MAX_SECONDS = {"punch_zoom": 2.5, "shake": 1.2, "focus_lines": 2.5, "freeze": 1.8, "impact_text": 2.5,
               "mono_tension": 5.0, "slow_zoom": 6.0, "title_card": 3.0}
ZOOM_RAMP = 0.15
ZOOM_AMOUNT = 0.22
SLOW_ZOOM_AMOUNT = 0.12
SHAKE_AMOUNT = 0.012     # 화면 너비 대비 흔들림 폭
MONO_FADE = 0.15
LETTERBOX = 0.085        # 흑백 긴장 효과의 위아래 검은 띠 비율
FLASH_SECONDS = 0.1
LINE_FRAMES = 2          # 집중선 무늬를 바꾸는 간격(프레임)


def _ease_out(value: float) -> float:
    return 1 - (1 - min(max(value, 0.0), 1.0)) ** 3


def _vignette(width: int, height: int) -> np.ndarray:
    """가운데는 밝고 가장자리는 어두운 곱셈용 무늬."""
    xs = np.linspace(-1, 1, width, dtype=np.float32)[None, :]
    ys = np.linspace(-1, 1, height, dtype=np.float32)[:, None]
    return (np.clip(1.08 - 0.5 * (xs ** 2 + ys ** 2), 0.25, 1.0) * 255).astype(np.uint8)


def _focus_lines(width: int, height: int, seed: int) -> np.ndarray:
    """가운데를 향해 모이는 만화 집중선. 선이 있는 곳이 0인 마스크를 돌려준다."""
    rng = np.random.default_rng(seed)
    mask = np.full((height, width), 255, np.uint8)
    cx, cy = width / 2, height / 2
    outer = math.hypot(width, height)
    for angle in rng.uniform(0, 2 * math.pi, 110):
        inner = min(width, height) * rng.uniform(0.34, 0.5)
        spread = rng.uniform(0.004, 0.016)
        tip = (cx + inner * math.cos(angle), cy + inner * math.sin(angle))
        left = (cx + outer * math.cos(angle - spread), cy + outer * math.sin(angle - spread))
        right = (cx + outer * math.cos(angle + spread), cy + outer * math.sin(angle + spread))
        cv2.fillPoly(mask, [np.array([tip, left, right], dtype=np.int32)], 0, lineType=cv2.LINE_AA)
    return mask


def _zoom(frame: np.ndarray, scale: float, cx: float, cy: float) -> None:
    height, width = frame.shape[:2]
    matrix = np.float32([[scale, 0, (1 - scale) * cx], [0, scale, (1 - scale) * cy]])
    frame[:] = cv2.warpAffine(frame, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


class FrameEffects:
    """프레임마다 그려지는 효과. 모자이크를 입힌 뒤에 적용해야 확대해도 개인정보가 드러나지 않는다."""

    def __init__(self, items: list[dict], width: int, height: int, fps: float, people: list[dict] | None = None) -> None:
        self.items = [item for item in items if item["type"] in FRAME_TYPES and item.get("enabled", True)]
        self.width, self.height, self.fps = width, height, fps
        self.people = people or []
        self.frozen: dict[int, np.ndarray] = {}

    @cached_property
    def vignette(self) -> np.ndarray:
        return _vignette(self.width, self.height)

    @cached_property
    def lines(self) -> list[np.ndarray]:
        return [_focus_lines(self.width, self.height, seed) for seed in range(3)]

    def active(self, t: float) -> list[dict]:
        return [item for item in self.items if item["start"] <= t <= item["end"]]

    def _focus(self, item: dict) -> tuple[float, float]:
        """확대의 중심. 사람이 있으면 얼굴 쪽으로 당긴다."""
        nearest = min(self.people, key=lambda sample: abs(sample["t"] - item["start"]), default=None)
        if nearest and abs(nearest["t"] - item["start"]) <= 2:
            x0, y0, x1, y1 = nearest["zones"][0]
            return (x0 + x1) / 2, y0 + (y1 - y0) * 0.2
        return self.width / 2, self.height / 2

    def _freeze(self, frame: np.ndarray, item: dict, t: float) -> None:
        stored = self.frozen.setdefault(item["start"], frame.copy())
        frame[:] = stored
        into = t - item["start"]
        if into < FLASH_SECONDS:
            strength = 1 - into / FLASH_SECONDS
            frame[:] = cv2.convertScaleAbs(frame, alpha=1 - strength * 0.7, beta=255 * strength * 0.7)

    def _mono(self, frame: np.ndarray, item: dict, t: float) -> None:
        strength = min(1.0, (t - item["start"]) / MONO_FADE, (item["end"] - t) / MONO_FADE)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.convertScaleAbs(gray, alpha=1.12, beta=-22)
        gray = cv2.multiply(gray, self.vignette, scale=1 / 255)
        mono = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        frame[:] = mono if strength >= 1 else cv2.addWeighted(mono, strength, frame, 1 - strength, 0)
        bar = int(self.height * LETTERBOX * strength)
        if bar:
            frame[:bar] = 0
            frame[-bar:] = 0

    def _shake(self, frame: np.ndarray, item: dict, t: float) -> None:
        decay = max(0.0, (item["end"] - t) / max(item["end"] - item["start"], 0.01))
        amount = self.width * SHAKE_AMOUNT * decay
        dx, dy = math.sin(t * 71) * amount, math.cos(t * 53) * amount
        matrix = np.float32([[1, 0, dx], [0, 1, dy]])
        frame[:] = cv2.warpAffine(frame, matrix, (self.width, self.height), borderMode=cv2.BORDER_REPLICATE)

    def apply(self, frame: np.ndarray, t: float, active: list[dict]) -> None:
        for item in active:
            kind = item["type"]
            if kind == "freeze":
                self._freeze(frame, item, t)
            elif kind == "mono_tension":
                self._mono(frame, item, t)
            elif kind == "punch_zoom":
                into, left = t - item["start"], item["end"] - t
                _zoom(frame, 1 + ZOOM_AMOUNT * _ease_out(min(into, left) / ZOOM_RAMP), *self._focus(item))
            elif kind == "slow_zoom":
                progress = (t - item["start"]) / max(item["end"] - item["start"], 0.01)
                _zoom(frame, 1 + SLOW_ZOOM_AMOUNT * progress, *self._focus(item))
            elif kind == "shake":
                self._shake(frame, item, t)
            elif kind == "focus_lines":
                pattern = self.lines[int(t * self.fps / LINE_FRAMES) % len(self.lines)]
                frame[:] = cv2.bitwise_and(frame, frame, mask=pattern)
        for start in [start for start in self.frozen if not any(i["start"] == start for i in active)]:
            del self.frozen[start]


# ---------------------------------------------------------------- 효과음
def _sweep(rate: int, seconds: float, start_hz: float, end_hz: float, decay: float) -> np.ndarray:
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    phase = 2 * np.pi * (start_hz * t + (end_hz - start_hz) * t ** 2 / (2 * seconds))
    return np.sin(phase) * np.exp(-decay * t)


def _noise(rate: int, seconds: float, smooth: int) -> np.ndarray:
    noise = np.random.default_rng(0).standard_normal(int(rate * seconds)).astype(np.float32)
    return np.convolve(noise, np.ones(smooth, dtype=np.float32) / smooth, mode="same")


def make_sfx(name: str, rate: int) -> np.ndarray:
    """-1~1 범위의 모노 파형."""
    if name == "pop":
        sound = _sweep(rate, 0.14, 480, 1500, 22)
    elif name == "boom":
        hit = _sweep(rate, 0.5, 95, 42, 7) + 0.3 * _noise(rate, 0.5, 40) * np.exp(-30 * np.arange(int(rate * 0.5)) / rate)
        sound = np.zeros(int(rate * 0.8), dtype=np.float32)
        second = int(rate * 0.28)
        sound[:len(hit)] += hit
        sound[second:second + len(hit)] += hit[:len(sound) - second] * 1.1
    elif name == "whoosh":
        length = int(rate * 0.5)
        sound = _noise(rate, 0.5, 18) * np.sin(np.linspace(0, np.pi, length, dtype=np.float32)) ** 2
    elif name == "ding":
        sound = _sweep(rate, 0.9, 1568, 1568, 5) + 0.5 * _sweep(rate, 0.9, 3136, 3136, 7)
    elif name == "tension":
        t = np.arange(int(rate * 1.8), dtype=np.float32) / rate
        sound = (np.sin(2 * np.pi * 55 * t) + np.sin(2 * np.pi * 58.5 * t)) * np.clip(t / 1.2, 0, 1) * np.clip((1.8 - t) / 0.2, 0, 1)
    else:
        return np.zeros(0, dtype=np.float32)
    return sound / max(float(np.max(np.abs(sound))), 1e-6)


def mix_sfx(samples: np.ndarray, rate: int, items: list[dict], gain: float = 0.45) -> np.ndarray:
    """효과음을 시작 시각에 더한다. 목소리가 깨지지 않게 넘치는 부분은 자른다."""
    cues = [item for item in items if item.get("sfx", "none") != "none" and item.get("enabled", True)]
    if not cues:
        return samples
    mixed = samples.astype(np.float32)
    channels = samples.shape[1]
    for item in cues:
        sound = make_sfx(item["sfx"], rate) * 32767 * gain
        first = int(item["start"] * rate)
        length = min(len(sound), len(mixed) - first)
        if length > 0:
            mixed[first:first + length] += sound[:length, None].repeat(channels, axis=1)
    return np.clip(mixed, -32768, 32767).astype(np.int16)
