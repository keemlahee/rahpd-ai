"""일러스트·아이콘 이미지 생성.

그림 안에는 글자를 넣지 않는다. 이미지 모델이 그린 한글은 깨지는 경우가 많아서, 필요한 짧은
라벨은 렌더 단계에서 프로그램이 맑은 고딕으로 직접 쓴다.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import claude
from .config import LOGOS, OPENAI_KEY, get_key
from .media import LogFn, Runner

STYLE = (
    "Style: flat vector illustration combined with a hand-drawn cartoon look. Bold, slightly wobbly dark navy outlines, "
    "solid flat color fills, a limited warm palette of mustard yellow, coral, teal, cream and navy, simple friendly shapes, "
    "no gradients, no photorealism, no 3D. One clear centered subject with generous empty margin. "
    "Absolutely no text, letters, numbers, captions, watermarks or real brand logos anywhere in the image."
)
OPENAI_MODELS = [m for m in [os.environ.get("OPENAI_IMAGE_MODEL"), "gpt-image-2", "gpt-image-1.5", "gpt-image-1"] if m]
ILLUST_SIZE, ICON_SIZE = "1536x1024", "1024x1024"
WORKERS = 4


def resolve_provider(choice: str) -> str:
    """'auto'는 OpenAI 키를 우선 쓰고, 없으면 Claude가 그린 SVG로 대신한다."""
    has_openai, has_claude = bool(get_key(OPENAI_KEY)), claude.available()
    if choice == "openai":
        return "openai" if has_openai else ""
    if choice == "claude":
        return "claude" if has_claude else ""
    return "openai" if has_openai else ("claude" if has_claude else "")


def remove_flat_background(image: Image.Image) -> Image.Image:
    """불투명하게 나온 그림에서 가장자리와 이어진 밝은 배경만 지운다. 그림 안쪽의 흰색은 남는다."""
    rgba = np.array(image.convert("RGBA"))
    if rgba[..., 3].min() < 250:
        return image.convert("RGBA")
    bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    height, width = bgr.shape[:2]
    mask = np.zeros((height + 2, width + 2), np.uint8)
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE | (255 << 8)
    for x, y in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        if mask[y + 1, x + 1] == 0:
            cv2.floodFill(bgr, mask, (x, y), (0, 0, 0), (12, 12, 12), (12, 12, 12), flags)
    background = cv2.GaussianBlur(mask[1:-1, 1:-1], (3, 3), 0)
    rgba[..., 3] = 255 - background
    return Image.fromarray(rgba, "RGBA")


def trim(image: Image.Image, margin: int = 16) -> Image.Image:
    box = image.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if not box:
        return image
    left, top, right, bottom = box
    return image.crop((max(0, left - margin), max(0, top - margin),
                       min(image.width, right + margin), min(image.height, bottom + margin)))


def _openai_image(prompt: str, size: str, quality: str) -> bytes:
    import openai

    client = openai.OpenAI(api_key=get_key(OPENAI_KEY))
    last_error: Exception | None = None
    for model in OPENAI_MODELS:
        for background in ("transparent", "opaque"):
            try:
                result = client.images.generate(model=model, prompt=prompt, size=size, quality=quality, n=1,
                                                background=background, output_format="png")
                return base64.b64decode(result.data[0].b64_json)
            except openai.AuthenticationError as error:
                raise RuntimeError("OpenAI API 키가 올바르지 않습니다.") from error
            except openai.RateLimitError as error:
                # 잔액 부족도 429로 온다. 모델을 바꿔도 소용없으니 바로 알린다.
                raise RuntimeError(f"OpenAI 사용 한도 또는 잔액 문제입니다: {error.message}") from error
            except (openai.NotFoundError, openai.PermissionDeniedError) as error:
                last_error = error
                break  # 이 키로 못 쓰는 모델(조직 인증 필요 등) → 다음 모델
            except openai.BadRequestError as error:
                last_error = error
                text = str(error).lower()
                if "safety" in text or "moderation" in text:
                    raise RuntimeError("OpenAI 안전 필터가 이 프롬프트를 거절했습니다. 프롬프트를 고쳐 주세요.") from error
                if "background" in text or "transparent" in text:
                    continue  # 투명 배경 미지원 → 불투명으로 받아 배경을 직접 지운다
                break
            except openai.APIError as error:
                raise RuntimeError(f"OpenAI 연결 또는 서버 오류: {error}") from error
    raise RuntimeError(f"OpenAI 이미지 생성 실패: {last_error}")


def _claude_image(prompt: str, width: int, height: int) -> bytes:
    import resvg_py

    svg = claude.draw_svg(prompt, width, height)
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, width=width * 2))


def _generate(item: dict, provider: str, target: Path) -> None:
    is_icon = item["kind"] == "icon"
    if provider == "openai":
        prompt = f"{item['image_prompt']}\n\n{STYLE}"
        data = _openai_image(prompt, ICON_SIZE if is_icon else ILLUST_SIZE, "low" if is_icon else "medium")
    else:
        width, height = (512, 512) if is_icon else (768, 512)
        data = _claude_image(item["image_prompt"], width, height)
    image = trim(remove_flat_background(Image.open(io.BytesIO(data))))
    image.save(target)


def _cache_name(index: int, item: dict, provider: str) -> str:
    digest = hashlib.sha1(f"{provider}|{item['kind']}|{item['image_prompt']}".encode("utf-8")).hexdigest()[:10]
    prefix = "illust" if item["kind"] == "illust" else "overlay"
    return f"{prefix}{index:02d}_{item['kind']}_{digest}.png"


def find_logo(name: str) -> Path | None:
    path = LOGOS / Path(name).name
    return path if name and path.is_file() else None


def generate_all(plan: dict, image_dir: Path, provider_choice: str, log: LogFn, runner: Runner) -> list[str]:
    """plan의 각 항목에 image 파일명을 채운다. 같은 프롬프트는 다시 과금되지 않도록 재사용한다."""
    image_dir.mkdir(exist_ok=True)
    provider = resolve_provider(provider_choice)
    jobs: list[tuple[dict, Path]] = []
    warnings: list[str] = []

    entries = [({**item, "kind": "illust"}, item, f"일러스트 {i}", i) for i, item in enumerate(plan["illustrations"], 1)]
    entries += [({**item}, item, f"오버레이 {i}", i) for i, item in enumerate(plan["overlays"], 1)]

    for view, item, title, index in entries:
        item["image"] = ""
        logo = find_logo(item.get("logo", ""))
        if view["kind"] == "logo":
            if logo:
                log(f"{title}: 로고 '{item.get('name', '')}' ← {logo.name}")
                continue
            # 로고 파일이 없으면 넣지 않는다. 비슷한 그림을 지어내면 틀린 로고가 영상에 들어간다.
            warnings.append(f"{title}: 로고 파일 '{item.get('logo', '')}'이(가) 없어 건너뜁니다.")
            continue
        if not view.get("image_prompt", "").strip():
            continue
        if not provider:
            continue
        name = _cache_name(index, view, provider)
        item["image"] = name
        target = image_dir / name
        label = item.get("label") or item.get("name") or item.get("concept", "")
        log(f"{title}: '{label}' → {name}" + (" (재사용)" if target.exists() else ""))
        if not target.exists():
            jobs.append((view, target))

    if not provider and any(e[0].get("image_prompt") for e in entries):
        warnings.append("그림을 만들 API 키가 없어 이미지 생성을 건너뜁니다. 설정에 키를 넣어 주세요.")

    if jobs:
        log(f"이미지 {len(jobs)}장 생성 중 ({'OpenAI' if provider == 'openai' else 'Claude SVG'})")
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(_generate, view, provider, target): (view, target) for view, target in jobs}
            for future in as_completed(futures):
                runner.check()
                view, target = futures[future]
                try:
                    future.result()
                except Exception as error:  # 그림 한 장이 실패해도 나머지 편집은 끝까지 진행한다.
                    warnings.append(f"그림 생성 실패 ({target.name}): {error}")
                    log(f"이미지 실패: {target.name}: {error}")

    for view, item, _title, _index in entries:
        if item.get("image") and not (image_dir / item["image"]).exists():
            item["image"] = ""
    return warnings
