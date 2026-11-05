"""샘플 프레임의 시각이 정확해야 모자이크가 글자를 따라간다."""
import subprocess
from pathlib import Path

import numpy as np

from app import media
from app.config import find_tool
from app.media import Runner

FPS = 30
W, H = 64, 36


def make_clock_video(path: Path, seconds: int = 3) -> None:
    """프레임 번호를 밝기로 새긴 영상을 만든다."""
    frames = seconds * FPS
    proc = subprocess.Popen(
        [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", str(path)],
        stdin=subprocess.PIPE)
    for index in range(frames):
        proc.stdin.write(bytes([index * 2 + 16]) * (W * H))
    proc.stdin.close()
    proc.wait()


def test_iter_frames_time_matches_the_frame_it_returns(tmp_path):
    video = tmp_path / "clock.mp4"
    make_clock_video(video)
    info = media.probe(video)
    seen = [(t, float(np.mean(frame))) for t, frame in media.iter_frames(video, info, 0.5, Runner())]
    assert [t for t, _ in seen][:4] == [0.0, 0.5, 1.0, 1.5]
    for time, brightness in seen:
        expected = round(time * FPS) * 2 + 16
        assert abs(brightness - expected) <= 3, f"t={time}: 밝기 {brightness}, 기대 {expected}"


def test_concat_audio_keeps_exact_length_of_kept_frames():
    rate = 48000
    samples = np.ones((rate * 4, 2), dtype=np.int16) * 1000
    keeps = [[0, 29], [60, 89]]  # 1초씩 두 조각
    out = media.concat_audio(samples, rate, keeps, 30.0)
    assert len(out) == rate * 2


def test_mute_silences_only_the_range():
    rate = 1000
    samples = np.ones((rate * 3, 1), dtype=np.int16) * 1000
    out = media.mute_ranges(samples, rate, [(1.0, 2.0)])
    assert out[1100:1900].max() == 0
    assert out[500, 0] == 1000 and out[2500, 0] == 1000
