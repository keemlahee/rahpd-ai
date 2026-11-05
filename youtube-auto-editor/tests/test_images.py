"""그림 동시 생성.

테스트 안에서 띄운 가짜 서버로만 요청한다. .env에 실제 키가 있어도 키와 주소를 모두 바꾸므로
OpenAI로는 아무것도 나가지 않고 과금되지 않는다.
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import images
from app.media import Cancelled, Runner
from tests.mock_apis import fake_image

PNG = base64.b64encode(fake_image(64, 64, True)).decode("ascii")


class FakeOpenAI:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.delay = 0.0
        self.calls: list[dict] = []
        self.active = self.peak = 0
        self.rate_limited = 0       # 앞의 몇 개 요청을 한도 초과로 돌려보낼지
        self.out_of_credit = False
        self.reject_transparent = False

    def handle(self, body: dict) -> tuple[int, dict, dict]:
        with self.lock:
            self.calls.append(body)
            self.active += 1
            self.peak = max(self.peak, self.active)
            limited = self.rate_limited > 0
            self.rate_limited -= 1 if limited else 0
        try:
            if self.out_of_credit:
                return 429, {"error": {"message": "quota", "code": "insufficient_quota"}}, {"x-should-retry": "false"}
            if limited:
                return 429, {"error": {"message": "slow down", "code": "rate_limit_exceeded"}}, {"retry-after": "0"}
            if self.reject_transparent and body.get("background") == "transparent":
                return 400, {"error": {"message": "Transparent background is not supported for this model."}}, {}
            time.sleep(self.delay)
            return 200, {"created": 1, "data": [{"b64_json": PNG}]}, {}
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def fake(monkeypatch):
    state = FakeOpenAI()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, reply, headers = state.handle(body)
            data = json.dumps(reply).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(images, "get_key", lambda _name: "sk-test-not-real")
    monkeypatch.setattr(images, "OPENAI_MODELS", ["gpt-image-2"])
    monkeypatch.setattr(images, "_opaque_only", set())
    yield state
    server.shutdown()


def plan_of(count: int, tag: str = "") -> dict:
    return {"illustrations": [], "overlays": [{"kind": "icon", "name": f"그림{i}", "logo": "", "image_prompt": f"object {tag}{i}"}
                                              for i in range(count)]}


def generate(plan: dict, folder, log=lambda _m: None, runner: Runner | None = None) -> list[str]:
    return images.generate_all(plan, folder, "openai", log, runner or Runner())


def made(plan: dict, folder) -> int:
    return sum(1 for item in plan["overlays"] if item["image"] and (folder / item["image"]).exists())


def test_many_images_are_made_at_the_same_time(fake, tmp_path):
    fake.delay = 0.3
    plan = plan_of(16)
    started = time.time()
    assert generate(plan, tmp_path) == []
    elapsed = time.time() - started
    assert made(plan, tmp_path) == 16
    assert fake.peak >= images.WORKERS - 1
    assert elapsed < 16 * 0.3 / 3


def test_rate_limit_is_waited_out_and_no_image_is_lost(fake, tmp_path):
    fake.rate_limited = 5
    plan = plan_of(6)
    assert generate(plan, tmp_path) == []
    assert made(plan, tmp_path) == 6


def test_out_of_credit_is_reported_instead_of_waiting(fake, tmp_path):
    fake.out_of_credit = True
    plan = plan_of(2)
    started = time.time()
    warnings = generate(plan, tmp_path)
    assert len(warnings) == 2 and "크레딧" in warnings[0]
    assert time.time() - started < 5
    assert made(plan, tmp_path) == 0


def test_transparent_rejection_is_not_repeated_for_later_images(fake, tmp_path):
    fake.reject_transparent = True
    generate(plan_of(1, "first"), tmp_path)
    assert [call["background"] for call in fake.calls] == ["transparent", "opaque"]
    fake.calls.clear()
    plan = plan_of(5, "second")
    assert generate(plan, tmp_path) == []
    assert {call["background"] for call in fake.calls} == {"opaque"}
    assert made(plan, tmp_path) == 5


def test_stop_does_not_start_the_images_still_waiting(fake, tmp_path):
    fake.delay = 0.2
    runner = Runner()

    def stop_after_first(message: str) -> None:
        if "완료" in message:
            runner.cancel.set()

    with pytest.raises(Cancelled):
        generate(plan_of(40), tmp_path, stop_after_first, runner)
    time.sleep(0.5)
    assert len(fake.calls) < 40 / 2


def test_prompt_already_made_is_not_requested_again(fake, tmp_path):
    generate(plan_of(3), tmp_path)
    fake.calls.clear()
    plan = plan_of(3)
    assert generate(plan, tmp_path) == []
    assert fake.calls == []
    assert made(plan, tmp_path) == 3
