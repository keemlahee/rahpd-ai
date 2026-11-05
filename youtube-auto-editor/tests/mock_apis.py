"""실제 호출 없이 Claude·OpenAI 경로를 시험하는 가짜 서버. 과금되지 않는다.

공식 SDK가 만든 요청을 그대로 받아 형식을 검사하고, 받은 내용을 received.json에 남긴다.
실제 모델의 품질은 검증하지 못하지만 요청 형식, 응답 처리, 후처리, 렌더까지는 확인된다.
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
TRUTH = Path(__file__).parent / "sample_truth.json"
log: list[dict] = []


def fake_plan(text: str) -> dict:
    """샘플 영상의 위험한 구간(스크롤되는 개인정보)에 일부러 확대와 정지 효과를 넣는다."""
    return {
        "illustrations": [
            {"start": 6.0, "end": 11.0, "concept": "무음을 찾아 자르는 과정", "label": "무음 자르기", "logo": "",
             "image_prompt": "A pair of scissors cutting a flat sound wave ribbon at its quiet gap."},
        ],
        "overlays": [
            {"start": 12.0, "end": 14.0, "kind": "icon", "name": "지우개", "logo": "",
             "image_prompt": "A single chunky eraser."},
            {"start": 27.0, "end": 29.0, "kind": "logo", "name": "없는 로고", "logo": "없는로고.png", "image_prompt": ""},
        ],
        "effects": [
            {"start": 1.0, "end": 3.0, "type": "impact_text", "text": "실화냐?!", "sfx": "pop"},
            {"start": 3.2, "end": 4.6, "type": "punch_zoom", "text": "", "sfx": "whoosh"},
            {"start": 4.8, "end": 5.6, "type": "shake", "text": "", "sfx": "boom"},
            {"start": 11.2, "end": 11.9, "type": "focus_lines", "text": "", "sfx": "whoosh"},
            {"start": 15.0, "end": 16.2, "type": "freeze", "text": "", "sfx": "ding"},
            {"start": 16.4, "end": 19.4, "type": "mono_tension", "text": "", "sfx": "tension"},
            {"start": 20.6, "end": 22.4, "type": "punch_zoom", "text": "", "sfx": "pop"},
            {"start": 22.6, "end": 24.0, "type": "freeze", "text": "", "sfx": "ding"},
            {"start": 24.2, "end": 26.4, "type": "title_card", "text": "운명의 순간", "sfx": "boom"},
            {"start": 29.5, "end": 33.0, "type": "slow_zoom", "text": "", "sfx": "none"},
        ],
    }


def fake_cut_review(text: str) -> dict:
    # 프로그램이 찾은 후보를 하나 되살리고, 범위를 벗어난 추가 컷도 보낸다(방어 코드 확인용).
    candidates = json.loads(text.split("candidates\n", 1)[1])
    keep = [candidates[-1]["id"]] if len(candidates) > 3 else []
    return {"keep_candidates": keep, "extra_cuts": [{"first_word": 1, "last_word": 30, "reason": "너무 긴 추가 컷"}]}


def fake_style(text: str) -> dict:
    return {"lines": [{"id": line["id"], "text": f"{line['text'].rstrip('.')} ㄹㅇ"} for line in json.loads(text)]}


def fake_transcription() -> dict:
    """정답지로 만든 whisper-1 응답. 실제처럼 단어에는 문장부호가 없다."""
    events = json.loads(TRUTH.read_text(encoding="utf-8"))["events"]
    segments, words = [], []
    for index, event in enumerate(events):
        tokens = event["text"].split()
        total = sum(len(token) for token in tokens)
        cursor = event["start"]
        length = event["end"] - event["start"]
        for token in tokens:
            span = length * len(token) / total
            words.append({"word": token.strip(".,?!"), "start": round(cursor, 3), "end": round(cursor + span, 3)})
            cursor += span
        segments.append({"id": index, "seek": 0, "start": event["start"], "end": event["end"], "text": " " + event["text"],
                         "tokens": [], "temperature": 0.0, "avg_logprob": -0.1, "compression_ratio": 1.0, "no_speech_prob": 0.0})
    return {"task": "transcribe", "language": "korean", "duration": events[-1]["end"],
            "text": " ".join(event["text"] for event in events), "segments": segments, "words": words}


def fake_image(width: int, height: int, transparent: bool) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    margin = min(width, height) // 6
    draw.ellipse((margin, margin, width - margin, height - margin), fill=(242, 182, 50, 255), outline=(36, 48, 74, 255), width=14)
    draw.rectangle((width // 2 - 90, height // 2 - 60, width // 2 + 90, height // 2 + 60), fill=(242, 118, 90, 255), outline=(36, 48, 74, 255), width=10)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def answer_for(system: str, user: str) -> dict:
    if "시각 자료 기획자" in system:
        return fake_plan(user)
    if "컷 편집자" in system:
        return fake_cut_review(user)
    if "자막 작가" in system:
        return fake_style(user)
    if "개인정보" in system:
        return {"found": []}
    return {"svg": ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
                    '<circle cx="256" cy="256" r="180" fill="#F2B632" stroke="#24304A" stroke-width="9"/></svg>')}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _send_json(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.startswith("/v1/audio/transcriptions"):
            head = raw[:4000].decode("utf-8", "replace")
            fields = {name: (f'name="{name}"' in head) for name in ("model", "language", "prompt", "response_format", "timestamp_granularities[]")}
            log.append({"api": "openai-transcribe", "bytes": len(raw), "fields": fields,
                        "whisper-1": "whisper-1" in head, "verbose_json": "verbose_json" in head})
            self._send_json(fake_transcription())
        elif self.path.startswith("/v1/chat/completions"):
            body = json.loads(raw)
            system, user = body["messages"][0]["content"], body["messages"][1]["content"]
            log.append({"api": "openai-chat", "model": body["model"], "format": body.get("response_format", {}).get("type"),
                        "strict": body.get("response_format", {}).get("json_schema", {}).get("strict"), "system": system[:30]})
            text = json.dumps(answer_for(system, user), ensure_ascii=False)
            self._send_json({"id": "chatcmpl-mock", "object": "chat.completion", "created": 1, "model": body["model"],
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": text, "refusal": None}}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        elif self.path.startswith("/v1/messages"):
            body = json.loads(raw)
            user = body["messages"][0]["content"]
            system = body.get("system", "")
            log.append({"api": "claude", "model": body["model"], "beta": self.headers.get("anthropic-beta"),
                        "fallbacks": body.get("fallbacks"), "stream": body.get("stream"),
                        "output_config": list(body.get("output_config", {})), "system": system[:30], "user": user[:200]})
            self._stream_claude(json.dumps(answer_for(system, user), ensure_ascii=False))
        elif self.path.startswith("/v1/images/generations"):
            body = json.loads(raw)
            log.append({"api": "openai-image", **{k: body.get(k) for k in ("model", "size", "quality", "background", "output_format")},
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
