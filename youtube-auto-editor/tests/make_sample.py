"""검증용 샘플 영상을 만든다: samples/test_video.mp4 와 정답지 tests/sample_truth.json

Windows 한국어 음성으로 말을 합성해, 일부러 넣은 무음·추임새·말더듬의 위치와 화면 속
개인정보가 보이는 시간을 정확히 알고 있는 영상을 만든다.
    python -m tests.make_sample
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import SAMPLES, find_tool  # noqa: E402

W, H, FPS, RATE = 1920, 1080, 30, 22050
FONT = r"C:\Windows\Fonts\malgun.ttf"
FONT_B = r"C:\Windows\Fonts\malgunbd.ttf"

# (문장 또는 None, 뒤에 붙일 무음 초, 종류). 종류는 정답지 표시용이다.
SCRIPT = [
    ("안녕하세요. 오늘은 유튜브 영상을 자동으로 편집하는 프로그램을 소개해 드리겠습니다.", 1.6, "speech"),
    ("음", 0.25, "filler"),
    ("먼저 이 프로그램은 무음 구간을 찾아서 자동으로 잘라 줍니다.", 0.2, "speech"),
    ("어", 0.2, "filler"),
    ("그리고 쓸데없는 추임새도 함께 지워 줍니다.", 1.3, "speech"),
    ("화면에 보이는 전화번호와 이메일 같은 개인정보는 모자이크로 가려집니다.", 1.0, "speech"),
    ("아래로 내려가는 목록에 나오는 연락처도 끝까지 따라가면서 가려 줍니다.", 0.4, "speech"),
    ("이 이미지를 보시면 설명 그림이 들어가는 것을 확인할 수 있습니다.", 0.9, "speech"),
    ("프로 프로그램이 끝나면 자막 파일과 리포트도 함께 만들어집니다.", 1.2, "speech"),
    ("감사합니다.", 0.8, "speech"),
]

PROFILE = [("이름", "홍길동"), ("연락처", "010-1234-5678"), ("이메일", "gildong.hong@example.com"),
           ("주소", "서울특별시 강남구 테헤란로 123"), ("API 키", "sk-ant-api03-abcDEF1234567890xyz")]
SCROLL_ROWS = ["김철수 010-2222-3333", "이영희 010-4444-5555", "박민수 minsu.park@example.org",
               "최지우 010-6666-7777", "정하늘 sky.jung@example.net"]


def tts(text: str, target: Path) -> None:
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.SelectVoice('Microsoft Heami Desktop');"
        f"$s.SetOutputToWaveFile('{target}', "
        f"(New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({RATE}, 16, 1)));"
        f"$s.Speak([IO.File]::ReadAllText('{target}.txt', [Text.Encoding]::UTF8));$s.Dispose()"
    )
    target.with_suffix(target.suffix + ".txt").write_text(text, encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True)


def build_audio(folder: Path) -> tuple[np.ndarray, list[dict]]:
    tracks, events, cursor = [np.zeros(int(RATE * 0.5), np.int16)], [], 0.5
    for index, (text, pause, kind) in enumerate(SCRIPT):
        clip = folder / f"line{index}.wav"
        tts(text, clip)
        with wave.open(str(clip), "rb") as wav:
            data = np.frombuffer(wav.readframes(wav.getnframes()), np.int16)
        # 합성음 앞뒤의 무음을 잘라야 일부러 넣은 무음의 길이가 정확하다.
        loud = np.flatnonzero(np.abs(data) > 300)
        data = data[max(0, loud[0] - 200): loud[-1] + 200]
        events.append({"text": text, "kind": kind, "start": round(cursor, 3), "end": round(cursor + len(data) / RATE, 3)})
        tracks += [data, np.zeros(int(RATE * pause), np.int16)]
        cursor += len(data) / RATE + pause
    return np.concatenate(tracks), events


def slide(title: str, rows: list[tuple[str, str]] | None = None, note: str = "") -> Image.Image:
    image = Image.new("RGB", (W, H), (247, 248, 252))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, W, 110), fill=(36, 48, 74))
    draw.text((70, 28), title, font=ImageFont.truetype(FONT_B, 44), fill="white")
    if note:
        draw.text((90, 180), note, font=ImageFont.truetype(FONT, 40), fill=(60, 70, 90))
    row_font = ImageFont.truetype(FONT_B, 28)
    for index, (label, value) in enumerate(rows or []):
        draw.text((110, 180 + index * 90), f"{label}: {value}", font=row_font, fill=(30, 30, 40))
    return image


def scroll_strip() -> Image.Image:
    strip = Image.new("RGB", (W, H * 2), (255, 255, 255))
    draw = ImageDraw.Draw(strip)
    font = ImageFont.truetype(FONT, 30)
    for index, row in enumerate(SCROLL_ROWS * 2):
        y = 40 + index * 180
        draw.rounded_rectangle((90, y, W - 90, y + 130), 16, outline=(190, 195, 210), width=3)
        draw.text((130, y + 45), row, font=font, fill=(30, 30, 40))
    return strip


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        audio, events = build_audio(folder)
        duration = len(audio) / RATE
        wav_path = folder / "voice.wav"
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(audio.tobytes())

        pii_line = next(e for e in events if e["text"].startswith("화면에"))
        scroll_line = next(e for e in events if e["text"].startswith("아래로"))
        scene_static = pii_line["start"] - 0.3
        scene_scroll = scroll_line["start"] - 0.2
        scene_end = scroll_line["end"] + 0.3

        title = np.array(slide("자동 편집 프로그램 소개", note="무음, 추임새, 설명 그림, 개인정보 보호를 한 번에"))[:, :, ::-1]
        profile = np.array(slide("회원 정보", PROFILE))[:, :, ::-1]
        closing = np.array(slide("정리", note="이제 편집 시간을 아껴 보세요"))[:, :, ::-1]
        strip = np.array(scroll_strip())[:, :, ::-1]
        speed = 140  # 초당 스크롤 픽셀

        args = [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                "-i", str(wav_path), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", "aac", "-b:a", "192k", "-shortest", str(SAMPLES / "test_video.mp4")]
        proc = subprocess.Popen(args, stdin=subprocess.PIPE)
        for index in range(int(duration * FPS)):
            t = index / FPS
            if t < scene_static:
                frame = title
            elif t < scene_scroll:
                frame = profile
            elif t < scene_end:
                offset = int((t - scene_scroll) * speed)
                frame = strip[offset:offset + H]
            else:
                frame = closing
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        proc.wait()

    truth = {"duration": duration, "events": events,
             "pii_static": [scene_static, scene_scroll], "pii_scroll": [scene_scroll, scene_end],
             "profile": PROFILE, "scroll_rows": SCROLL_ROWS}
    (ROOT / "tests" / "sample_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"완료: {duration:.1f}초, 정적 개인정보 {scene_static:.1f}~{scene_scroll:.1f}s, 스크롤 {scene_scroll:.1f}~{scene_end:.1f}s")


if __name__ == "__main__":
    main()
