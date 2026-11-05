"""경로, 기본 옵션, API 키(.env), 외부 도구 위치."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = ROOT / "uploads"
SAMPLES = ROOT / "samples"
WORKSPACE = ROOT / "workspace"
LOGOS = ROOT / "assets" / "logos"
MODELS = ROOT / "models"
WEB = ROOT / "web"
ENV_FILE = ROOT / ".env"

HOST = "127.0.0.1"
PORT = 8766

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
LANGUAGE = "ko"
FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
# 윈도우가 아닌 PC에서 같은 글꼴이나 대체 글꼴을 찾을 폴더
FONT_DIRS = [FONTS, ROOT / "assets" / "fonts", Path.home() / ".local" / "share" / "fonts", Path("/usr/share/fonts")]


def find_font(*names: str) -> Path:
    """윈도우 글꼴 폴더를 먼저 보고, 없으면 다른 글꼴 폴더에서 같은 이름이나 대체 글꼴을 찾는다."""
    for folder in FONT_DIRS:
        for name in names:
            if (folder / name).exists():
                return folder / name
    for folder in FONT_DIRS[1:]:
        for name in names:
            found = next(folder.rglob(name), None) if folder.is_dir() else None
            if found:
                return found
    return FONTS / names[0]


FONT_BOLD = find_font("malgunbd.ttf", "NanumGothicBold.ttf")
FONT_SANS_VF = find_font("NotoSansKR-VF.ttf")    # 예능 자막용. 굵기를 조절할 수 있는 글꼴이다
FONT_SERIF_VF = find_font("NotoSerifKR-VF.ttf")  # 긴장감 있는 타이틀용

CLAUDE_KEY = "ANTHROPIC_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"

for folder in (UPLOADS, SAMPLES, WORKSPACE, LOGOS, MODELS):
    folder.mkdir(parents=True, exist_ok=True)

# bool 옵션과 (최소, 최대) 범위가 있는 숫자 옵션. 웹 UI의 입력값은 여기서 걸러진다.
DEFAULT_OPTIONS: dict = {
    "cut": True,
    "illustrations": True,
    "overlays": True,
    "mosaic": True,
    "face_mosaic": False,
    "audio_mute": False,
    "pii_claude": False,
    "mz_subtitles": True,
    "burn_subtitles": True,
    "illust_position": "center",
    "transcribe_engine": "openai",
    "speech_language": "ko",  # 영상에서 말하는 언어. 한국어가 아니면 원어로 받아쓰고 자막을 한국어로 번역한다
    "whisper_model": "",  # 비어 있으면 장치에 맞는 기본 모델
    "image_provider": "auto",
    "fx_style": "mixed",
    "fx_per_min": 4.0,
    "silence_min": 0.6,
    "silence_keep": 0.3,
    "illust_per_min": 1.5,
    "overlay_per_min": 4.0,
    "ocr_interval": 0.5,
    "crf": 19,
}
RANGES = {
    "silence_min": (0.2, 5.0),
    "silence_keep": (0.0, 2.0),
    "illust_per_min": (0.0, 6.0),
    "overlay_per_min": (0.0, 12.0),
    "fx_per_min": (0.0, 15.0),
    "ocr_interval": (0.1, 5.0),
    "crf": (14, 30),
}
CHOICES = {
    "illust_position": ("center", "right", "pip", "full"),
    "image_provider": ("auto", "openai", "claude"),
    "transcribe_engine": ("openai", "local"),
    "speech_language": ("ko", "en", "auto"),
    "fx_style": ("off", "variety", "survival", "mixed"),
    "whisper_model": ("", "small", "medium", "large-v3-turbo", "large-v3"),
}


def clean_options(raw: dict | None) -> dict:
    """웹에서 받은 옵션을 기본값에 합치고 타입과 범위를 맞춘다."""
    opts = dict(DEFAULT_OPTIONS)
    for key, value in (raw or {}).items():
        if key not in opts:
            continue
        if isinstance(opts[key], bool):
            opts[key] = bool(value)
        elif key in RANGES:
            low, high = RANGES[key]
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            opts[key] = max(low, min(high, number))
        elif value in CHOICES.get(key, ()):
            opts[key] = value
    opts["crf"] = int(opts["crf"])
    return opts


def get_key(name: str) -> str:
    """요청마다 .env를 다시 읽어, 저장한 키가 재시작 없이 적용되게 한다."""
    value = dotenv_values(ENV_FILE).get(name) if ENV_FILE.exists() else None
    return (value or os.environ.get(name) or "").strip()


def set_keys(values: dict[str, str]) -> None:
    ENV_FILE.touch(exist_ok=True)
    for name, value in values.items():
        if value.strip():
            set_key(str(ENV_FILE), name, value.strip(), quote_mode="never")


def mask_key(value: str) -> str:
    return f"{value[:4]}...{value[-4:]}" if len(value) > 10 else ("설정됨" if value else "")


def find_tool(name: str) -> str:
    """PATH → winget 링크 → winget 패키지 폴더 순으로 ffmpeg/ffprobe를 찾는다.

    winget 설치 직후에는 현재 콘솔의 PATH에 반영되지 않기 때문에 뒤의 두 경로가 필요하다.
    """
    found = shutil.which(name)
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    link = local / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
    if link.exists():
        return str(link)
    for candidate in (local / "Microsoft" / "WinGet" / "Packages").glob(f"Gyan.FFmpeg*/**/bin/{name}.exe"):
        return str(candidate)
    raise RuntimeError(f"{name}을(를) 찾을 수 없습니다. install.bat을 먼저 실행해 주세요.")


def _cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except (OSError, ImportError):  # 윈도우가 아니면 winreg가 없다
        return "CPU"


def _nvidia_name() -> str:
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return output.stdout.strip().splitlines()[0] if output.returncode == 0 and output.stdout.strip() else ""


def detect_device() -> dict:
    gpu = _nvidia_name()
    if gpu:
        return {"kind": "cuda", "name": gpu, "default_whisper": "large-v3"}
    return {"kind": "cpu", "name": _cpu_name(), "default_whisper": "large-v3-turbo"}


DEVICE = detect_device()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """쓰기 도중 꺼져도 기존 파일이 깨지지 않도록 임시 파일을 거쳐 교체한다."""
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 윈도우에서는 웹 요청이 같은 파일을 읽는 순간 교체가 실패할 수 있어 잠깐 기다렸다 다시 한다.
    for _ in range(40):
        try:
            temp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05)
    temp.replace(path)
