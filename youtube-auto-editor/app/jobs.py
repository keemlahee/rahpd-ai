"""백그라운드 작업 하나를 돌리고 로그를 모은다. 전사와 렌더가 무거워 동시에 하나만 실행한다."""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from .media import Cancelled, Runner
from .pipeline import Project, video_id


class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.runner = Runner()
        self.logs: list[str] = []
        self.video_id = ""
        self.result = "idle"

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def log(self, message: str) -> None:
        self.logs.append(f"{time.strftime('%H:%M:%S')}  {message}")

    def start(self, path: Path, stage: str, options: dict, force: bool) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("이미 실행 중인 작업이 있습니다.")
            self.logs = []
            self.runner = Runner()
            self.video_id = video_id(path)
            self.result = "running"
            self.thread = threading.Thread(target=self._work, args=(path, stage, options, force), daemon=True)
            self.thread.start()

    def _work(self, path: Path, stage: str, options: dict, force: bool) -> None:
        try:
            Project(path, options, self.log, self.runner).run(stage, force)
            self.result = "done"
        except Cancelled:
            self.log("중지했습니다.")
            self.result = "stopped"
        except Exception as error:  # 실패 원인을 웹 로그에 보여 주고 서버는 계속 살아 있어야 한다.
            self.log(f"오류: {error}")
            traceback.print_exc()
            self.result = "error"

    def stop(self) -> None:
        self.runner.stop()

    def snapshot(self, since: int) -> dict:
        return {"running": self.running, "result": self.result, "video_id": self.video_id,
                "logs": self.logs[since:], "next": len(self.logs)}


job = Job()
