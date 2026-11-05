"""로컬 웹 서버. 이 PC에서만 접속되며 API 키를 다루므로 다른 사이트의 요청은 막는다."""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import media, pipeline
from .config import (CLAUDE_KEY, DEFAULT_OPTIONS, DEVICE, LOGOS, OPENAI_KEY, PORT, UPLOADS, VIDEO_EXTS, WEB, WORKSPACE,
                     clean_options, read_json, set_keys, write_json)
from .jobs import job

app = FastAPI(title="유튜브 자동 편집")
BOOT_ID = uuid.uuid4().hex
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
LOCAL_ORIGINS = {f"http://{host}" for host in LOCAL_HOSTS}


@app.middleware("http")
async def only_local(request: Request, call_next):
    """다른 웹사이트가 브라우저를 통해 이 서버를 조작하는 것(키 변경, 실행)을 막는다."""
    origin = request.headers.get("origin")
    if request.headers.get("host") not in LOCAL_HOSTS or (origin and origin not in LOCAL_ORIGINS):
        return JSONResponse({"detail": "로컬 접속만 허용됩니다."}, status_code=403)
    return await call_next(request)


def _video(vid: str) -> Path:
    try:
        return pipeline.find_video(vid)
    except KeyError as error:
        raise HTTPException(404, "영상을 찾을 수 없습니다.") from error


def _result_json(vid: str, name: str) -> dict:
    data = read_json(WORKSPACE / vid / name)
    if data is None:
        raise HTTPException(404, "아직 결과가 없습니다.")
    return data


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "video.mp4"


@app.get("/api/version")
def version():
    """화면이 코드 변경을 알아채는 값. 서버가 다시 켜지거나 웹 파일이 바뀌면 달라진다."""
    newest = max(path.stat().st_mtime_ns for path in WEB.iterdir() if path.is_file())
    return {"version": f"{BOOT_ID}-{newest}"}


@app.get("/api/status")
def status():
    return {"device": DEVICE, "keys": pipeline.key_status(), "defaults": DEFAULT_OPTIONS,
            "logos": pipeline.available_logos(), "logo_dir": str(LOGOS)}


@app.post("/api/videos")
def videos(body: dict | None = None):
    options = clean_options((body or {}).get("options"))
    items = []
    for path in pipeline.list_video_paths():
        vid = pipeline.video_id(path)
        items.append({"id": vid, "name": path.name, "folder": path.parent.name,
                      "size_mb": round(path.stat().st_size / 1_048_576, 1),
                      "stages": pipeline.stage_status(path, options)})
    return {"videos": items, "stages": pipeline.STAGE_LABELS}


@app.post("/api/upload")
def upload(file: UploadFile):
    name = _safe_name(file.filename or "")
    if Path(name).suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(400, "지원하지 않는 영상 형식입니다.")
    target = UPLOADS / name
    number = 1
    while target.exists():
        target = UPLOADS / f"{Path(name).stem} ({number}){Path(name).suffix}"
        number += 1
    with open(target, "wb") as out:
        shutil.copyfileobj(file.file, out, 1024 * 1024)
    return {"id": pipeline.video_id(target), "name": target.name}


@app.delete("/api/videos/{vid}/cache")
def delete_cache(vid: str):
    _video(vid)
    if job.running and job.video_id == vid:
        raise HTTPException(409, "실행 중인 영상의 캐시는 지울 수 없습니다.")
    pipeline.clear_cache(vid)
    return {"ok": True}


@app.post("/api/keys")
def save_keys(body: dict):
    set_keys({CLAUDE_KEY: body.get("claude", ""), OPENAI_KEY: body.get("openai", "")})
    return pipeline.key_status()


@app.post("/api/run")
def run(body: dict):
    stage = body.get("stage", "all")
    if stage != "all" and stage not in pipeline.STAGES:
        raise HTTPException(400, "알 수 없는 단계입니다.")
    path = _video(body.get("video_id", ""))
    try:
        job.start(path, stage, clean_options(body.get("options")), bool(body.get("force")))
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True}


@app.post("/api/stop")
def stop():
    job.stop()
    return {"ok": True}


@app.get("/api/job")
def job_state(since: int = 0):
    return job.snapshot(since)


@app.get("/api/videos/{vid}/result")
def result(vid: str):
    _video(vid)
    folder = WORKSPACE / vid
    return {
        "final": (folder / "final.mp4").exists(),
        "cut": (folder / "cut.mp4").exists(),
        "srt": (folder / "final.srt").exists(),
        "report": (folder / "report.md").exists(),
        "summary": read_json(folder / "summary.json"),
        "report_md": (folder / "report.md").read_text("utf-8") if (folder / "report.md").exists() else "",
    }


@app.get("/api/videos/{vid}/edit")
def edit_data(vid: str):
    """기획 수정 화면에 필요한 자료를 한 번에 돌려준다."""
    _video(vid)
    folder = WORKSPACE / vid
    return {
        "plan": read_json(folder / "plan.json", {"illustrations": [], "overlays": [], "effects": []}),
        "captions": read_json(folder / "captions.json", {"items": []}),
        "cuts": read_json(folder / "cut_plan.json", {"cuts": []}),
        "pii": read_json(folder / "pii.json", {"tracks": [], "speech": []}),
        "transcript": read_json(folder / "cut_transcript.json", {"segments": []}),
        "info": media.probe(folder / "cut.mp4") if (folder / "cut.mp4").exists() else None,
        "ready": (folder / "plan.json").exists() and (folder / "pii.json").exists(),
    }


def _require_idle() -> None:
    if job.running:
        raise HTTPException(409, "작업이 끝난 뒤에 저장해 주세요.")


@app.put("/api/videos/{vid}/plan")
def save_plan(vid: str, body: dict):
    _video(vid)
    _require_idle()
    plan = _result_json(vid, "plan.json")
    plan["illustrations"] = body["illustrations"]
    plan["overlays"] = body["overlays"]
    plan["effects"] = body.get("effects", [])
    plan["edited"] = True  # 다음 실행에서 기획 단계가 이 내용을 덮어쓰지 않게 한다
    plan["warnings"] = []
    write_json(WORKSPACE / vid / "plan.json", plan)
    # 프롬프트가 바뀐 항목만 새로 그린다(이미지 캐시는 프롬프트 해시로 찾는다).
    pipeline.bump(vid, "plan")
    return {"ok": True}


@app.put("/api/videos/{vid}/captions")
def save_captions(vid: str, body: dict):
    _video(vid)
    _require_idle()
    data = _result_json(vid, "captions.json")
    texts = {int(key): str(value) for key, value in body["texts"].items()}
    for item in data["items"]:
        if item["id"] in texts:
            item["text"] = texts[item["id"]]
    data["edited"] = True
    write_json(WORKSPACE / vid / "captions.json", data)
    pipeline.bump(vid, "captions")
    return {"ok": True}


@app.put("/api/videos/{vid}/cuts")
def save_cuts(vid: str, body: dict):
    _video(vid)
    _require_idle()
    plan = _result_json(vid, "cut_plan.json")
    enabled = {int(key): bool(value) for key, value in body["enabled"].items()}
    for cut in plan["cuts"]:
        if cut["id"] in enabled:
            cut["enabled"] = enabled[cut["id"]]
    plan["edited"] = True
    write_json(WORKSPACE / vid / "cut_plan.json", plan)
    pipeline.invalidate(vid, "cut")
    return {"ok": True}


@app.put("/api/videos/{vid}/pii")
def save_pii(vid: str, body: dict):
    _video(vid)
    _require_idle()
    data = _result_json(vid, "pii.json")
    enabled = {int(key): bool(value) for key, value in body.get("enabled", {}).items()}
    for track in data["tracks"]:
        if track["id"] in enabled:
            track["enabled"] = enabled[track["id"]]
    next_id = max((t["id"] for t in data["tracks"]), default=0) + 1
    for item in body.get("added", []):
        rect = [int(v) for v in item["rect"]]
        start, end = float(item["start"]), float(item["end"])
        data["tracks"].append({"id": next_id, "kind": "직접 추가", "text": "", "start": start, "end": end,
                               "points": [[start, rect], [end, rect]], "enabled": True, "manual": True})
        next_id += 1
    removed = set(body.get("removed", []))
    data["tracks"] = [t for t in data["tracks"] if not (t.get("manual") and t["id"] in removed)]
    write_json(WORKSPACE / vid / "pii.json", data)
    pipeline.bump(vid, "pii")
    return {"ok": True}


@app.post("/api/videos/{vid}/regen-image")
def regen_image(vid: str, body: dict):
    """그림 한 장만 지우고 이미지 단계를 다시 돌린다. 나머지는 캐시를 쓴다."""
    _video(vid)
    _require_idle()
    name = Path(body.get("image", "")).name
    if not name:
        raise HTTPException(400, "이미지가 없습니다.")
    (WORKSPACE / vid / "images" / name).unlink(missing_ok=True)
    pipeline.bump(vid, "plan")
    return {"ok": True}


@app.get("/files/{vid}/{name:path}")
def files(vid: str, name: str):
    """워크스페이스 안의 파일만 내보낸다. 파일 응답은 영상 탐색에 필요한 Range 요청을 지원한다."""
    base = (WORKSPACE / vid).resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "파일이 없습니다.")
    return FileResponse(target)


@app.get("/source/{vid}")
def source(vid: str):
    return FileResponse(_video(vid))


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB), name="static")
