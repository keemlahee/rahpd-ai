"""API 키 없이 Claude·OpenAI 경로를 시험하는 가짜 서버.

공식 SDK가 만든 요청을 그대로 받아 형식을 검사하고, 받은 내용을 received.json에 남긴다.
실제 모델의 품질은 검증하지 못하지만 요청 형식, 스트리밍 파싱, 후처리, 렌더까지는 확인된다.
    python -m tests.mock_apis 9199
"""
from __future__ import annotations

import base64
import io
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

RECEIVED = Path(__file__).parent / "received.json"
log: list[dict] = []


def fake_plan(text: str) -> dict:
    return {
        "illustrations": [
            {"start": 6.0, "end": 11.0, "concept": "무음을 찾아 자르는 과정", "label": "무음 자르기", "logo": "",
             "image_prompt": "A pair of scissors cutting a flat sound wave ribbon at its quiet gap."},
            {"start": 20.0, "end": 25.0, "concept": "개인정보를 가리는 모습", "label": "개인정보 보호", "logo": "",
             "image_prompt": "A shield covering an identity card with a blurred pixel pattern."},
        ],
        "overlays": [
            {"start": 13.0, "end": 15.0, "kind": "icon", "name": "지우개", "logo": "",
             "image_prompt": "A single chunky eraser."},
            {"start": 27.0, "end": 29.0, "kind": "logo", "name": "없는 로고", "logo": "없는로고.png", "image_prompt": ""},
        ],
    }


def fake_cut_review(text: str) -> dict:
    # 프로그램이 찾은 후보를 하나 되살리고, 범위를 벗어난 추가 컷도 보낸다(방어 코드 확인용).
    candidates = json.loads(text.split("candidates\n", 1)[1])
    keep = [candidates[-1]["id"]] if len(candidates) > 3 else []
    return {"keep_candidates": keep, "extra_cuts": [{"first_word": 1, "last_word": 30, "reason": "너무 긴 추가 컷"}]}


def fake_image(width: int, height: int, transparent: bool) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    margin = min(width, height) // 6
    draw.ellipse((margin, margin, width - margin, height - margin), fill=(242, 182, 50, 255), outline=(36, 48, 74, 255), width=14)
    draw.rectangle((width // 2 - 90, height // 2 - 60, width // 2 + 90, height // 2 + 60), fill=(242, 118, 90, 255), outline=(36, 48, 74, 255), width=10)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _send_json(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_claude(self, text: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def event(name: str, data: dict) -> None:
            self.wfile.write(f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n".encode("utf-8"))

        usage = {"input_tokens": 10, "output_tokens": 0}
        event("message_start", {"message": {"id": "msg_mock", "type": "message", "role": "assistant", "content": [],
                                            "model": "mock", "stop_reason": None, "stop_sequence": None, "usage": usage}})
        event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
        for start in range(0, len(text), 40):
            event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text[start:start + 40]}})
        event("content_block_stop", {"index": 0})
        event("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}})
        event("message_stop", {})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/v1/messages"):
            user = body["messages"][0]["content"]
            system = body.get("system", "")
            log.append({"api": "claude", "model": body["model"], "beta": self.headers.get("anthropic-beta"),
                        "fallbacks": body.get("fallbacks"), "stream": body.get("stream"),
                        "output_config": list(body.get("output_config", {})), "system": system[:30], "user": user[:200]})
            if "시각 자료 기획자" in system:
                self._stream_claude(json.dumps(fake_plan(user), ensure_ascii=False))
            elif "컷 편집자" in system:
                self._stream_claude(json.dumps(fake_cut_review(user), ensure_ascii=False))
            elif "개인정보" in system:
                self._stream_claude(json.dumps({"found": []}))
            else:
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
                       '<circle cx="256" cy="256" r="180" fill="#F2B632" stroke="#24304A" stroke-width="9"/></svg>')
                self._stream_claude(json.dumps({"svg": svg}))
        elif self.path.startswith("/v1/images/generations"):
            log.append({"api": "openai", **{k: body.get(k) for k in ("model", "size", "quality", "background", "output_format")},
                        "prompt": body["prompt"][:120]})
            if body.get("model") == "gpt-image-2" and body.get("background") == "transparent":
                self._send_json({"error": {"message": "background transparent is not supported", "type": "invalid_request_error"}}, 400)
            else:
                width, height = (int(v) for v in body["size"].split("x"))
                data = fake_image(width, height, body.get("background") == "transparent")
                self._send_json({"created": 1, "data": [{"b64_json": base64.b64encode(data).decode("ascii")}]})
        else:
            self._send_json({"error": {"message": f"unknown path {self.path}"}}, 404)
        RECEIVED.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9199
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
