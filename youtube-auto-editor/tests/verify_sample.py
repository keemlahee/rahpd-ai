"""완성된 final.mp4를 파이프라인과 독립적으로 다시 분석해 요구 사항을 검사한다.

    python -m tests.verify_sample
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import media, pipeline, privacy  # noqa: E402
from app.config import SAMPLES, WORKSPACE, find_tool, read_json  # noqa: E402
from app.media import Runner  # noqa: E402
from app.transcribe import transcribe  # noqa: E402

DENSE_INTERVAL = 0.2


def squash(text: str) -> str:
    return re.sub(r"[\s.,?!…]", "", text)


def ratio(found: str, expected: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, found, expected).ratio()


def main() -> int:
    truth = json.loads((ROOT / "tests" / "sample_truth.json").read_text(encoding="utf-8"))
    source = SAMPLES / "test_video.mp4"
    folder = WORKSPACE / pipeline.video_id(source)
    final = folder / "final.mp4"
    failures: list[str] = []

    def check(ok: bool, message: str) -> None:
        print(("  통과 " if ok else "  실패 ") + message)
        if not ok:
            failures.append(message)

    print("[1] 길이와 싱크")
    info = media.probe(final)
    output = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,duration,nb_frames", "-of", "json", str(final)],
        capture_output=True, text=True).stdout
    streams = {s["codec_type"]: s for s in json.loads(output)["streams"]}
    video_len, audio_len = float(streams["video"]["duration"]), float(streams["audio"]["duration"])
    plan = read_json(folder / "cut_plan.json")
    expected_frames = sum(last - first + 1 for first, last in plan["keeps"])
    check(int(streams["video"]["nb_frames"]) == expected_frames, f"프레임 수 {streams['video']['nb_frames']} = 계획 {expected_frames}")
    check(abs(video_len - audio_len) < 0.08, f"영상 {video_len:.3f}s / 음성 {audio_len:.3f}s")
    print(f"     원본 {truth['duration']:.1f}s → 최종 {video_len:.1f}s")

    print("[2] 말이 사라지지 않았는가 (최종 영상을 다시 전사)")
    wav = folder / "verify16k.wav"
    media.extract_audio(final, wav, 16000, 1, Runner())
    again = transcribe(wav, "large-v3-turbo", video_len, lambda _m: None, Runner())
    heard = squash(" ".join(s["text"] for s in again["segments"]))
    spoken = [e["text"] for e in truth["events"] if e["kind"] == "speech"]
    print("     다시 들은 대본:", " ".join(s["text"] for s in again["segments"]))
    for sentence in spoken:
        expected = squash(sentence).replace("프로프로그램", "프로그램")
        best = max((ratio(heard[i:i + len(expected)], expected) for i in range(0, max(1, len(heard) - len(expected) + 1), 2)), default=0)
        check(best >= 0.8, f"{best:.2f} {sentence}")
    check("이이미지" in heard or "이미지" in heard, "'이 이미지를'의 이미지가 유지됨")

    print("[3] 추임새·말더듬이 지워졌는가")
    words = [s["text"] for s in again["segments"]]
    joined = " ".join(words)
    leftovers = re.findall(r"(?:^|\s)(음|어)[,.\s]", joined)
    check(not leftovers, f"최종 대본의 단독 추임새: {leftovers}")
    check("프로 프로그램" not in joined, "'프로 프로그램' 더듬음 제거")

    print("[4] 개인정보가 보이지 않는가 (최종 영상을 %.1f초마다 OCR)" % DENSE_INTERVAL)
    ocr = privacy.OcrEngine()
    secrets = [value for _, value in truth["profile"]] + [token for row in truth["scroll_rows"] for token in row.split()]
    fragments = {squash(s).lower() for s in secrets}
    leaks = []
    frames = 0
    for time, frame in media.iter_frames(final, info, DENSE_INTERVAL, Runner()):
        frames += 1
        for _, text in ocr.read(frame):
            flat = squash(text).lower()
            hit = [f for f in fragments if f in flat] or [kind for kind, _, _ in privacy.find_in_text(text)]
            if hit:
                leaks.append((round(time, 1), text, hit))
    check(not leaks, f"{frames}프레임 검사, 노출 {len(leaks)}건")
    for leak in leaks[:12]:
        print("     노출:", leak)

    print("[5] 색이 유지되는가")
    timemap_frame = next(iter_one(source, info))
    final_frame = next(iter_one(final, info))
    probe_box = (slice(20, 90), slice(1500, 1900))
    difference = np.abs(timemap_frame[probe_box].astype(int).mean((0, 1)) - final_frame[probe_box].astype(int).mean((0, 1))).max()
    check(difference <= 3, f"머리글 영역 색 차이 {difference:.1f}/255")

    print("\n결과:", "모두 통과" if not failures else f"실패 {len(failures)}건")
    return 1 if failures else 0


def iter_one(path: Path, info: dict):
    for _, frame in media.iter_frames(path, info, 1.0, Runner()):
        yield frame.copy()


if __name__ == "__main__":
    sys.exit(main())
