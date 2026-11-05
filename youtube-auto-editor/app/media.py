"""ffmpeg/ffprobe 호출, 오디오 입출력, 프레임 단위 컷 필터.

컷은 항상 프레임 번호로 계산한다. 화면 녹화는 가변 프레임이 흔해서, 먼저 fps 필터로 고정
프레임으로 만든 뒤 프레임 번호로 고른다. 오디오도 같은 프레임 경계로 자르면 컷이 수백 개여도
싱크가 어긋나지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import wave
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np

from .config import find_tool

if sys.platform == "win32":
    import msvcrt
    import _winapi
else:
    import fcntl

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 윈도우가 아니면 플래그가 없다
PIPE_SIZE = 16 << 20
IO_BUFFER = 1 << 20


class Cancelled(Exception):
    """사용자가 중지를 눌렀을 때."""


class Runner:
    """실행 중인 외부 프로세스를 추적해 중지 버튼에서 바로 끊을 수 있게 한다."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def track(self, proc: subprocess.Popen) -> subprocess.Popen:
        with self._lock:
            self._procs.add(proc)
        return proc

    def untrack(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def stop(self) -> None:
        self.cancel.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            proc.kill()


def probe(path: Path) -> dict:
    result = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(f"영상을 읽을 수 없습니다: {result.stderr.decode('utf-8', 'replace')[-300:]}")
    data = json.loads(result.stdout.decode("utf-8", "replace"))
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("영상 트랙이 없는 파일입니다.")
    audio = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)

    def rate(text: str | None) -> float:
        top, _, bottom = (text or "0/1").partition("/")
        return float(top) / float(bottom or 1) if float(bottom or 1) else 0.0

    average, nominal = rate(video.get("avg_frame_rate")), rate(video.get("r_frame_rate"))
    fps = average or nominal or 30.0
    if nominal and abs(nominal - average) > nominal * 0.05:
        # 화면 녹화는 화면이 멈추면 프레임을 안 남겨서 평균이 낮다. 평균을 쓰면 움직이는 구간이 끊긴다.
        fps = nominal if nominal <= 60 else 30.0
    fps = max(10.0, min(60.0, round(fps, 3)))
    width, height = int(video["width"]) // 2 * 2, int(video["height"]) // 2 * 2
    duration = float(data["format"].get("duration") or video.get("duration") or 0.0)
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": audio is not None,
        "color_space": video.get("color_space") or "",
    }


@lru_cache(maxsize=1)
def _filter_file_option() -> str:
    """필터를 파일로 넘기는 옵션. ffmpeg 7부터는 '-/filter:v'이고, 리눅스 배포판에 많은 6.x는 '-filter_script:v'만 안다."""
    result = subprocess.run([find_tool("ffmpeg"), "-version"], capture_output=True, creationflags=NO_WINDOW)
    match = re.match(rb"ffmpeg version n?(\d+)\.", result.stdout)
    return "-filter_script:v" if match and int(match.group(1)) < 7 else "-/filter:v"


def _ffmpeg_args(*args: str) -> list[str]:
    return [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args]


def run_ffmpeg(args: list[str], runner: Runner) -> None:
    proc = runner.track(subprocess.Popen(_ffmpeg_args(*args), stderr=subprocess.PIPE, creationflags=NO_WINDOW))
    try:
        _, stderr = proc.communicate()
    finally:
        runner.untrack(proc)
    runner.check()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 오류: {stderr.decode('utf-8', 'replace')[-500:]}")


def extract_audio(source: Path, target: Path, rate: int, channels: int, runner: Runner) -> None:
    run_ffmpeg(["-i", str(source), "-vn", "-ac", str(channels), "-ar", str(rate), "-c:a", "pcm_s16le", str(target)], runner)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """(프레임 수, 채널 수) 형태의 int16 배열을 돌려준다."""
    with wave.open(str(path), "rb") as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    return data.reshape(-1, channels), rate


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(samples.shape[1])
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.astype(np.int16).tobytes())


def concat_audio(samples: np.ndarray, rate: int, keeps: list[list[int]], fps: float, fade: float = 0.008) -> np.ndarray:
    """프레임 구간대로 오디오를 잇는다. 이음새의 '틱' 소리를 막으려고 끝을 짧게 페이드한다."""
    fade_len = int(rate * fade)
    ramp = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)[:, None]
    pieces = []
    for first, last in keeps:
        start, end = round(first * rate / fps), round((last + 1) * rate / fps)
        piece = samples[start:end].astype(np.float32)
        if len(piece) > fade_len * 2:
            piece[:fade_len] *= ramp
            piece[-fade_len:] *= ramp[::-1]
        pieces.append(piece)
    if not pieces:
        return np.zeros((0, samples.shape[1]), dtype=np.int16)
    return np.concatenate(pieces).astype(np.int16)


def mute_ranges(samples: np.ndarray, rate: int, ranges: list[tuple[float, float]], fade: float = 0.015) -> np.ndarray:
    out = samples.copy()
    fade_len = int(rate * fade)
    ramp = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)[:, None]
    for start, end in ranges:
        first, last = max(0, round(start * rate)), min(len(out), round(end * rate))
        if last <= first:
            continue
        out[first:last] = 0
        head = out[max(0, first - fade_len):first]
        head[:] = (head.astype(np.float32) * ramp[-len(head):]).astype(np.int16)
        tail = out[last:last + fade_len]
        tail[:] = (tail.astype(np.float32) * ramp[::-1][:len(tail)]).astype(np.int16)
    return out


def frame_filter(info: dict, keeps: list[list[int]], extra: str = "") -> str:
    """가변 프레임을 고정한 뒤 남길 프레임만 고르는 ffmpeg 필터 문자열."""
    fps = info["fps"]
    terms = "+".join(f"between(n,{first},{last})" for first, last in keeps)
    # 색 공간 태그가 없는 HD 영상은 플레이어가 BT.709로 보여 주므로, 같은 기준으로 해석해야 색이 안 변한다.
    matrix = info["color_space"] if info["color_space"] in ("bt709", "smpte170m", "bt470bg") else (
        "bt709" if info["height"] >= 720 else "smpte170m")
    parts = [
        f"fps={fps}",
        f"select='{terms}'",
        f"setpts=N/({fps}*TB)",
        f"scale={info['width']}:{info['height']}:in_color_matrix={matrix}:out_color_matrix=bt709",
    ]
    if extra:
        parts.append(extra)
    return ",".join(parts)


def _write_filter(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def render_cut(source: Path, info: dict, keeps: list[list[int]], audio: Path | None, target: Path,
               workdir: Path, runner: Runner) -> None:
    """미리보기와 OCR에 쓰는 컷 편집본. 최종본은 이 파일이 아니라 원본에서 다시 렌더한다."""
    script = _write_filter(workdir / "cut_filter.txt", frame_filter(info, keeps, "format=yuv420p"))
    args = ["-i", str(source)]
    if audio:
        args += ["-i", str(audio)]
    args += [_filter_file_option(), str(script), "-map", "0:v:0"]
    if audio:
        args += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    args += ["-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
             "-movflags", "+faststart", str(target)]
    run_ffmpeg(args, runner)


def _big_pipe() -> tuple[int, int]:
    """(읽기, 쓰기) 파일 번호. 윈도우 기본 파이프는 작아서 프레임 하나를 보내는 데 왕복이 수백 번 든다.

    1080p 프레임 전송이 초당 약 100장에서 330장으로 빨라진다. 리눅스는 가능한 만큼 파이프를 키운다.
    """
    if sys.platform != "win32":
        read_fd, write_fd = os.pipe()
        with suppress(OSError, AttributeError):
            fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, PIPE_SIZE)
        return read_fd, write_fd
    read_handle, write_handle = _winapi.CreatePipe(None, PIPE_SIZE)
    return msvcrt.open_osfhandle(read_handle, os.O_RDONLY), msvcrt.open_osfhandle(write_handle, 0)


def _reader_process(args: list[str], stderr, runner: Runner) -> subprocess.Popen:
    read_fd, write_fd = _big_pipe()
    proc = subprocess.Popen(args, stdout=write_fd, stderr=stderr, creationflags=NO_WINDOW)
    os.close(write_fd)
    proc.stdout = os.fdopen(read_fd, "rb", buffering=IO_BUFFER)
    return runner.track(proc)


def open_decoder(source: Path, info: dict, keeps: list[list[int]], workdir: Path, runner: Runner) -> subprocess.Popen:
    script = _write_filter(workdir / "final_filter.txt", frame_filter(info, keeps, "format=bgr24"))
    args = _ffmpeg_args("-i", str(source), _filter_file_option(), str(script), "-map", "0:v:0", "-an",
                        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "bgr24", "-")
    with open(workdir / "decoder_log.txt", "wb") as stderr:
        return _reader_process(args, stderr, runner)


def open_encoder(info: dict, audio: Path | None, target: Path, crf: int, runner: Runner, log_file: Path) -> subprocess.Popen:
    size = f"{info['width']}x{info['height']}"
    args = ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", size, "-r", str(info["fps"]), "-i", "-"]
    if audio:
        args += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    args += ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
             "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
             "-movflags", "+faststart", str(target)]
    read_fd, write_fd = _big_pipe()
    with open(log_file, "wb") as stderr:
        proc = subprocess.Popen(_ffmpeg_args(*args), stdin=read_fd, stderr=stderr, creationflags=NO_WINDOW)
    os.close(read_fd)
    proc.stdin = os.fdopen(write_fd, "wb", buffering=IO_BUFFER)
    return runner.track(proc)


def iter_frames(path: Path, info: dict, interval: float, runner: Runner):
    """고정 프레임 영상에서 interval초마다 프레임 하나를 (시각, BGR 배열)로 돌려준다.

    fps 필터로 뽑으면 각 시간 칸의 마지막 프레임이 나와서 시각이 최대 한 칸 밀린다. 그러면
    스크롤되는 글자와 모자이크가 어긋나므로 프레임 번호로 정확히 고른다.
    """
    width, height = info["width"], info["height"]
    step = max(1, round(interval * info["fps"]))
    args = _ffmpeg_args("-i", str(path), "-vf", f"select='not(mod(n,{step}))',scale={width}:{height}",
                        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "bgr24", "-")
    proc = _reader_process(args, subprocess.DEVNULL, runner)
    size = width * height * 3
    index = 0
    try:
        while True:
            runner.check()
            buffer = proc.stdout.read(size)
            if len(buffer) < size:
                return
            yield index * step / info["fps"], np.frombuffer(buffer, np.uint8).reshape(height, width, 3)
            index += 1
    finally:
        proc.kill()
        proc.stdout.close()
        proc.wait()
        runner.untrack(proc)


LogFn = Callable[[str], None]
