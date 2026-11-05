"""최종 합성: 원본 프레임에 모자이크와 일러스트 카드, 로고·아이콘을 입혀 한 번만 인코딩한다."""
from __future__ import annotations

import time as clock
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import media
from .config import FONT_BOLD
from .images import find_logo
from .media import LogFn, Runner
from .privacy import rect_at

CREAM = (255, 249, 236, 255)
INK = (28, 32, 48, 255)
POP_IN = 0.25
FADE_OUT = 0.15
DIM = 0.55  # 중앙 카드가 뜰 때 뒤 화면 밝기


@dataclass
class Layer:
    start: float
    end: float
    image: np.ndarray  # BGRA
    x: int
    y: int
    dim: bool = False

    def __post_init__(self) -> None:
        # 매 프레임 합성하므로 색과 투명도를 미리 나눠 둔다.
        self.bgr = self.image[..., :3].copy()
        self.alpha = self.image[..., 3].astype(np.float32) / 255.0


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = min(width / image.width, height / image.height)
    return image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD), size)


def _label_font(label: str, size: int, max_width: int) -> ImageFont.FreeTypeFont:
    while size > 14:
        font = _font(size)
        if font.getlength(label) <= max_width:
            return font
        size -= 2
    return _font(size)


def _to_bgra(image: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(image.convert("RGBA")), cv2.COLOR_RGBA2BGRA)


def build_card(art: Image.Image, label: str, max_width: int, max_height: int, unit: int,
               logo: Image.Image | None = None) -> Image.Image:
    """크림색 카드에 그림과 짧은 라벨을 얹는다. 굵은 외곽선과 그림자로 손그림 분위기를 맞춘다."""
    border, shadow, radius, pad = unit // 2, unit, unit * 3, unit * 2
    label_height = int(unit * 6.5) if label else 0
    art = _fit(art, max_width - (pad + border) * 2 - shadow, max_height - (pad + border) * 2 - shadow - label_height)
    width = art.width + (pad + border) * 2
    height = art.height + (pad + border) * 2 + label_height
    card = Image.new("RGBA", (width + shadow, height + shadow), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((shadow, shadow, width + shadow - 1, height + shadow - 1), radius, fill=INK)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius, fill=CREAM, outline=INK, width=border)
    card.alpha_composite(art, (pad + border, pad + border))
    if logo is not None:
        mark = _fit(logo, unit * 11, unit * 11)
        card.alpha_composite(mark, (width - mark.width - pad - border, pad + border))
    if label:
        font = _label_font(label, int(unit * 3.4), width - pad * 4)
        draw.text((width // 2, pad + border + art.height + label_height // 2), label, font=font, fill=INK, anchor="mm")
    return card


def build_chip(art: Image.Image, size: int, unit: int) -> Image.Image:
    """작은 로고·아이콘 칩."""
    border, shadow, radius, pad = max(2, unit // 3), max(3, unit // 2), unit * 2, unit
    art = _fit(art, size - (pad + border) * 2 - shadow, size - (pad + border) * 2 - shadow)
    width, height = art.width + (pad + border) * 2, art.height + (pad + border) * 2
    chip = Image.new("RGBA", (width + shadow, height + shadow), (0, 0, 0, 0))
    draw = ImageDraw.Draw(chip)
    draw.rounded_rectangle((shadow, shadow, width + shadow - 1, height + shadow - 1), radius, fill=INK)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius, fill=CREAM, outline=INK, width=border)
    chip.alpha_composite(art, (pad + border, pad + border))
    return chip


def _place_illustration(art: Image.Image, label: str, logo: Image.Image | None, position: str, width: int, height: int,
                        unit: int):
    if position == "full":
        card = build_card(art, label, width - unit * 4, height - unit * 4, unit, logo)
        return card, (width - card.width) // 2, (height - card.height) // 2, True
    if position == "right":
        card = build_card(art, label, int(width * 0.42), int(height * 0.8), unit, logo)
        return card, width - card.width - unit * 3, (height - card.height) // 2, False
    if position == "pip":
        card = build_card(art, label, int(width * 0.3), int(height * 0.42), unit, logo)
        return card, width - card.width - unit * 3, height - card.height - unit * 3, False
    card = build_card(art, label, int(width * 0.7), int(height * 0.84), unit, logo)
    return card, (width - card.width) // 2, (height - card.height) // 2, True


def build_layers(plan: dict, image_dir: Path, position: str, width: int, height: int, log: LogFn) -> list[Layer]:
    unit = max(4, height // 90)
    layers: list[Layer] = []

    def open_image(path: Path | None) -> Image.Image | None:
        if not path or not path.exists():
            return None
        try:
            return Image.open(path).convert("RGBA")
        except OSError as error:
            log(f"이미지를 열 수 없습니다 ({path.name}): {error}")
            return None

    def generated(item: dict) -> Image.Image | None:
        return open_image(image_dir / item["image"]) if item.get("image") else None

    def logo_of(item: dict) -> Image.Image | None:
        return open_image(find_logo(item.get("logo", "")))

    for item in plan.get("illustrations", []):
        art, logo = generated(item), logo_of(item)
        if art is None or not item.get("enabled", True):
            continue
        card, x, y, dim = _place_illustration(art, item.get("label", "").strip(), logo, position, width, height, unit)
        layers.append(Layer(item["start"], item["end"], _to_bgra(card), x, y, dim))

    chip_size = int(height * 0.17)
    for item in plan.get("overlays", []):
        art = generated(item) or logo_of(item)
        if art is None or not item.get("enabled", True):
            continue
        chip = build_chip(art, chip_size, unit)
        layers.append(Layer(item["start"], item["end"], _to_bgra(chip), width - chip.width - unit * 3, unit * 3))
    return layers


def _blend(frame: np.ndarray, bgr: np.ndarray, alpha: np.ndarray, x: int, y: int, opacity: float) -> None:
    height, width = frame.shape[:2]
    left, top = max(0, x), max(0, y)
    right, bottom = min(width, x + bgr.shape[1]), min(height, y + bgr.shape[0])
    if right <= left or bottom <= top:
        return
    color = bgr[top - y:bottom - y, left - x:right - x]
    weight = alpha[top - y:bottom - y, left - x:right - x] * opacity
    region = frame[top:bottom, left:right]
    # 1080p 카드를 numpy로 합성하면 렌더 시간의 대부분을 차지한다. blendLinear는 약 7배 빠르다.
    region[:] = cv2.blendLinear(color, region, weight, 1.0 - weight)


def _draw_layer(frame: np.ndarray, layer: Layer, t: float) -> None:
    into, left = t - layer.start, layer.end - t
    opacity, scale = 1.0, 1.0
    if into < POP_IN:
        progress = max(0.0, into / POP_IN)
        eased = 1 - (1 - progress) ** 3
        opacity, scale = eased, 0.88 + 0.12 * eased
    elif left < FADE_OUT:
        opacity = max(0.0, left / FADE_OUT)
    if layer.dim:
        frame[:] = cv2.convertScaleAbs(frame, alpha=1.0 - (1.0 - DIM) * opacity)
    bgr, alpha, x, y = layer.bgr, layer.alpha, layer.x, layer.y
    if scale < 1.0:
        height, width = bgr.shape[:2]
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        bgr = cv2.resize(bgr, size, interpolation=cv2.INTER_AREA)
        alpha = cv2.resize(alpha, size, interpolation=cv2.INTER_AREA)
        x += (width - size[0]) // 2
        y += (height - size[1]) // 2
    _blend(frame, bgr, alpha, x, y, opacity)


def pixelate(frame: np.ndarray, rect: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = rect
    height, width = frame.shape[:2]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(width, x1), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        return
    box_h = y1 - y0
    # 글자 한 줄이 2~3칸에 들어갈 만큼 크게 뭉갠다. 칸이 작으면 흐린 글자가 읽힐 수 있다.
    block = max(10, box_h // 3)
    region = frame[y0:y1, x0:x1]
    small = cv2.resize(region, (max(1, (x1 - x0) // block), max(1, box_h // block)), interpolation=cv2.INTER_AREA)
    frame[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, box_h), interpolation=cv2.INTER_NEAREST)


def render_final(source: Path, info: dict, keeps: list[list[int]], layers: list[Layer], tracks: list[dict],
                 audio: Path | None, target: Path, workdir: Path, crf: int, log: LogFn, runner: Runner) -> None:
    width, height, fps = info["width"], info["height"], info["fps"]
    frame_size = width * height * 3
    total = sum(last - first + 1 for first, last in keeps)
    temp = target.with_name("final_rendering.mp4")
    decoder = media.open_decoder(source, info, keeps, workdir, runner)
    encoder = media.open_encoder(info, audio, temp, crf, runner, workdir / "encoder_log.txt")
    started = clock.time()
    written = 0
    try:
        for index in range(total):
            if index % 30 == 0:
                runner.check()
            buffer = decoder.stdout.read(frame_size)
            if len(buffer) < frame_size:
                break
            t = index / fps
            active_tracks = [track for track in tracks if track["start"] <= t <= track["end"]]
            active_layers = [layer for layer in layers if layer.start <= t <= layer.end]
            if active_tracks or active_layers:
                frame = np.frombuffer(buffer, np.uint8).reshape(height, width, 3).copy()
                for track in active_tracks:
                    pixelate(frame, rect_at(track, t))
                for layer in active_layers:
                    _draw_layer(frame, layer, t)
                encoder.stdin.write(frame.tobytes())
            else:
                encoder.stdin.write(buffer)
            written += 1
            if written % int(fps * 20) == 0:
                speed = (written / fps) / max(clock.time() - started, 0.01)
                log(f"렌더 진행: {written / fps:.0f}s / {total / fps:.0f}s (재생 속도의 {speed:.1f}배)")
    except (media.Cancelled, OSError) as error:
        # 중지를 누르면 인코더가 먼저 끊겨 쓰기 오류로 올 수도 있다. 어느 쪽이든 반쯤 쓴 파일은 지운다.
        encoder.kill()
        with suppress(OSError):
            encoder.stdin.close()
        encoder.wait()
        runner.untrack(encoder)
        temp.unlink(missing_ok=True)
        runner.check()
        detail = (workdir / "encoder_log.txt").read_text("utf-8", "replace")[-400:]
        raise RuntimeError(f"인코딩 실패: {detail or error}") from error
    finally:
        decoder.kill()
        decoder.stdout.close()
        decoder.wait()
        runner.untrack(decoder)
    encoder.stdin.close()
    code = encoder.wait()
    runner.untrack(encoder)
    runner.check()
    if written == 0:
        detail = (workdir / "decoder_log.txt").read_text("utf-8", "replace")[-400:]
        raise RuntimeError(f"원본 영상을 읽지 못했습니다: {detail}")
    if code != 0:
        raise RuntimeError(f"인코딩 실패: {(workdir / 'encoder_log.txt').read_text('utf-8', 'replace')[-400:]}")
    if written < total - 2:  # 영상 트랙이 오디오보다 한두 프레임 짧은 것은 흔하다
        log(f"참고: 예상 {total}프레임 중 {written}프레임까지만 읽었습니다.")
    temp.replace(target)
