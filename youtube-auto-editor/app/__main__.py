"""서버를 띄우고, 코드가 바뀌면 알아서 다시 띄운다.

uvicorn의 reload는 파일이 바뀌는 즉시 프로세스를 끊어서 렌더 중인 작업까지 죽인다. 그래서 작업이
끝나기를 기다렸다가 직접 다시 띄운다. 화면은 /api/version이 바뀐 것을 보고 스스로 새로 읽는다.
"""
import json
import subprocess
import sys
import time
import urllib.request

from .config import HOST, PORT, ROOT

CHECK_SECONDS = 1.0
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def code_stamp() -> dict[str, int]:
    stamp = {}
    for path in (ROOT / "app").glob("*.py"):
        try:
            stamp[path.name] = path.stat().st_mtime_ns
        except OSError:
            pass  # 편집기가 저장하는 순간 파일이 잠깐 없을 수 있다
    return stamp


def job_running() -> bool:
    try:
        with NO_PROXY.open(f"http://{HOST}:{PORT}/api/job", timeout=3) as response:
            return bool(json.load(response)["running"])
    except (OSError, ValueError, KeyError):
        return True  # 답이 없으면 바쁜 것으로 본다. 작업을 끊는 것보다 조금 늦는 편이 낫다


def start_server() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "app.server:app", "--host", HOST, "--port", str(PORT),
                             "--log-level", "warning"], cwd=ROOT)


def main() -> None:
    server = None
    try:
        while True:
            seen = code_stamp()
            server = start_server()
            while server.poll() is None:
                time.sleep(CHECK_SECONDS)
                if code_stamp() != seen and not job_running():
                    print("코드가 바뀌어 서버를 다시 시작합니다.", flush=True)
                    server.terminate()
                    server.wait()
            if code_stamp() == seen:
                print("서버가 멈췄습니다. 코드가 고쳐지면 다시 시작합니다.", flush=True)
            while code_stamp() == seen:
                time.sleep(CHECK_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None and server.poll() is None:
            server.terminate()


if __name__ == "__main__":
    main()
