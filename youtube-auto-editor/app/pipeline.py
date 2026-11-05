"""단계 실행과 캐시.

각 단계는 자기 옵션과 앞 단계의 결과 번호(rev)로 해시를 만든다. 해시가 같고 결과 파일이 있으면
다시 돌리지 않는다. 그래서 라벨만 고치면 최종 렌더만, 무음 기준을 바꾸면 컷 이후만 다시 돈다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import captions, cuts, fx, images, layout, llm, media, privacy, render, report
from .config import (CLAUDE_KEY, DEVICE, LOGOS, OPENAI_KEY, ROOT, SAMPLES, UPLOADS, VIDEO_EXTS, WORKSPACE, get_key, mask_key,
                     read_json, write_json)
from .media import LogFn, Runner
from .transcribe import OPENAI_MODEL, TranscribeError, transcribe, transcribe_openai

STAGES = ["transcribe", "cut", "captions", "plan", "images", "pii", "render"]
STAGE_LABELS = {"transcribe": "전사", "cut": "컷 편집", "captions": "자막", "plan": "기획", "images": "이미지 생성",
                "pii": "개인정보 탐지", "render": "최종 렌더"}
OUTPUTS = {
    "transcribe": ["transcript.json"],
    "cut": ["cut_plan.json", "cut.mp4", "cut_transcript.json"],
    "captions": ["captions.json"],
    "plan": ["plan.json"],
    "images": ["plan.json"],
    "pii": ["pii.json"],
    "render": ["final.mp4", "final.srt", "report.md", "summary.json"],
}


def video_id(path: Path) -> str:
    return hashlib.sha1(str(path.relative_to(ROOT)).encode("utf-8")).hexdigest()[:12]


def list_video_paths() -> list[Path]:
    found = []
    for folder in (UPLOADS, SAMPLES):
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        found += sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def find_video(vid: str) -> Path:
    for path in list_video_paths():
        if video_id(path) == vid:
            return path
    raise KeyError(vid)


def workdir(vid: str) -> Path:
    path = WORKSPACE / vid
    path.mkdir(parents=True, exist_ok=True)
    return path


def available_logos() -> list[str]:
    return sorted(p.name for p in LOGOS.iterdir() if p.suffix.lower() in (".png", ".webp", ".jpg", ".jpeg"))


class Project:
    def __init__(self, path: Path, options: dict, log: LogFn, runner: Runner) -> None:
        self.path = path
        self.options = options
        self.log = log
        self.runner = runner
        self.dir = workdir(video_id(path))
        self.state_file = self.dir / "state.json"
        self.state: dict = read_json(self.state_file, {}) or {}
        self.info = media.probe(path)
        self.retry_next: set[str] = set()  # 이번 실행에서 실패해 다음 실행 때 다시 할 단계

    # ---- 캐시 판단
    def _uses_openai_transcribe(self) -> bool:
        return self.options["transcribe_engine"] == "openai" and bool(get_key(OPENAI_KEY))

    def _params(self, stage: str) -> dict:
        o = self.options
        whisper = OPENAI_MODEL if self._uses_openai_transcribe() else (o["whisper_model"] or DEVICE["default_whisper"])
        fx_on = o["fx_style"] != "off"
        return {
            "transcribe": {"model": whisper},
            "cut": {"cut": o["cut"], "min": o["silence_min"], "keep": o["silence_keep"], "llm": llm.available()},
            "captions": {"mz": o["mz_subtitles"], "llm": llm.available()},
            "plan": {"i": o["illustrations"], "o": o["overlays"], "ir": o["illust_per_min"], "or": o["overlay_per_min"],
                     "fx": o["fx_style"], "fr": o["fx_per_min"], "llm": llm.available()},
            "images": {"provider": images.resolve_provider(o["image_provider"]) if (o["illustrations"] or o["overlays"]) else ""},
            "pii": {"m": o["mosaic"], "f": o["face_mosaic"], "a": o["audio_mute"], "ocr": o["ocr_interval"], "c": o["pii_claude"],
                    "people": o["overlays"] or fx_on},
            "render": {"pos": o["illust_position"], "crf": o["crf"], "burn": o["burn_subtitles"]},
        }[stage]

    def depends(self, stage: str) -> list[str]:
        return {"cut": ["transcribe"], "captions": ["cut"], "plan": ["cut"], "images": ["plan"], "pii": ["cut"],
                "render": ["cut", "captions", "images", "pii"]}.get(stage, [])

    def _hash(self, stage: str) -> str:
        upstream = {name: self.state.get(name, {}).get("rev") for name in self.depends(stage)}
        text = json.dumps({"p": self._params(stage), "u": upstream}, sort_keys=True)
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    def fresh(self, stage: str) -> bool:
        entry = self.state.get(stage)
        return bool(entry and entry.get("hash") == self._hash(stage)
                    and all((self.dir / name).exists() for name in OUTPUTS[stage]))

    def _finish(self, stage: str) -> None:
        self.state[stage] = {"hash": self._hash(stage), "rev": uuid.uuid4().hex[:8], "at": time.time()}
        write_json(self.state_file, self.state)

    def _forget(self, stage: str) -> None:
        """실패한 단계를 완료로 남기지 않는다. 크레딧을 충전한 뒤 다시 실행했을 때 재사용되면 안 된다."""
        self.state.pop(stage, None)
        write_json(self.state_file, self.state)

    @contextmanager
    def step(self, title: str):
        self.log(f"▶ {title}")
        started = time.time()
        yield
        self.log(f"✓ {title} ({time.time() - started:.1f}s)")

    # ---- 단계 진입점
    def run(self, target: str, force: bool) -> None:
        names = STAGES if target == "all" else STAGES[:STAGES.index(target) + 1]
        info = self.info
        self.log(f"── {self.path.name}  {info['width']}x{info['height']} {info['fps']}fps {info['duration']:.1f}s")
        self.log("   단계: " + " → ".join(STAGE_LABELS[n] for n in names))
        for name in names:
            self.runner.check()
            rerun = force and (target == "all" or name == target)
            if not rerun and self.fresh(name):
                self.log(f"· {STAGE_LABELS[name]}: 이전 결과 재사용")
                continue
            getattr(self, f"_stage_{name}")(rerun)
            if name in self.retry_next:
                self._forget(name)
            else:
                self._finish(name)
        if self.retry_next:
            failed = ", ".join(STAGE_LABELS[n] for n in STAGES if n in self.retry_next)
            self.log(f"⚠ {failed} 단계가 일부 실패한 채로 끝났습니다. 원인을 해결하고 전체 실행을 다시 누르면 이 단계부터 다시 합니다.")
        self.log("완료")

    def _stage_transcribe(self, _rerun: bool) -> None:
        audio = self.dir / "audio16k.wav"
        if not self.info["has_audio"]:
            self.log("오디오 트랙이 없어 전사를 건너뜁니다.")
            write_json(self.dir / "transcript.json", {"segments": []})
            return
        with self.step("오디오 추출"):
            media.extract_audio(self.path, audio, 16000, 1, self.runner)
        model = self.options["whisper_model"] or DEVICE["default_whisper"]
        data = None
        if self._uses_openai_transcribe():
            try:
                with self.step(f"전사 (OpenAI {OPENAI_MODEL})"):
                    data = transcribe_openai(audio, self.log, self.runner)
            except TranscribeError as error:
                self.log(f"OpenAI 전사 실패, 이 PC에서 전사합니다: {error}")
                self.retry_next.add("transcribe")
        if data is None:
            with self.step("전사 (이 PC의 Whisper)"):
                data = transcribe(audio, model, self.info["duration"], self.log, self.runner)
        write_json(self.dir / "transcript.json", data)

    def _stage_cut(self, rerun: bool) -> None:
        transcript = read_json(self.dir / "transcript.json")
        words = cuts.flatten_words(transcript)
        existing = read_json(self.dir / "cut_plan.json")
        edited = existing and existing.get("edited") and not rerun
        audio16 = self.dir / "audio16k.wav"
        with self.step("컷 편집 계획 (무음·추임새·말더듬)"):
            if edited:
                plan = existing
                self.log("웹에서 수정한 컷 목록을 사용합니다.")
            elif audio16.exists():
                plan = cuts.make_plan(transcript, audio16, self.info["duration"], self.options, self.log)
                self._review_cuts(plan, words, audio16)
            else:
                plan = {"threshold_db": 0, "cuts": [], "edited": False}

            fps = self.info["fps"]
            total_frames = int(self.info["duration"] * fps)
            keep_seconds = cuts.keeps_from_cuts(plan["cuts"], self.info["duration"], words)
            keeps = cuts.to_frames(keep_seconds, fps, total_frames)
            plan["keeps"] = keeps
            timemap = cuts.TimeMap(keeps, fps)
            removed = sum(1 for c in plan["cuts"] if c["enabled"])
            self.log(f"컷 {removed}개 → {self.info['duration']:.1f}s → {timemap.total:.1f}s")
            write_json(self.dir / "cut_plan.json", plan)
            write_json(self.dir / "cut_transcript.json", cuts.remap_transcript(transcript, timemap))

        with self.step("컷 편집본 렌더"):
            cut_audio = self._cut_audio(keeps)
            media.render_cut(self.path, self.info, keeps, cut_audio, self.dir / "cut.mp4", self.dir, self.runner)

    def _review_cuts(self, plan: dict, words: list[dict], audio16) -> None:
        if not plan["cuts"] or not words or not llm.available():
            return
        try:
            with self.step("컷 문맥 검토 (AI)"):
                review = llm.review_cuts(words, plan["cuts"])
                samples, rate = media.read_wav(audio16)
                cuts.apply_review(plan, words, cuts.energy_db(samples, rate), review, self.log)
        except llm.LLMError as error:
            # 문맥 검토는 보조 기능이다. 실패했다고 컷을 미완료로 두면 뒤의 OCR까지 매번 다시 돈다.
            self.log(f"문맥 검토를 건너뜁니다: {error}")
            plan["review_failed"] = True

    def _cut_audio(self, keeps: list[list[int]]) -> Path | None:
        if not self.info["has_audio"]:
            return None
        full = self.dir / "audio48k.wav"
        if not full.exists():
            media.extract_audio(self.path, full, 48000, 2, self.runner)
        samples, rate = media.read_wav(full)
        target = self.dir / "cut_audio.wav"
        media.write_wav(target, media.concat_audio(samples, rate, keeps, self.info["fps"]), rate)
        return target

    def _stage_captions(self, rerun: bool) -> None:
        existing = read_json(self.dir / "captions.json")
        if existing and existing.get("edited") and not rerun:
            self.log("웹에서 수정한 자막을 사용합니다.")
            return
        segments = read_json(self.dir / "cut_transcript.json")["segments"]
        items = captions.build_items(segments)
        for item in items:
            item["text"] = item["source"]
        warnings = []
        if self.options["mz_subtitles"] and items:
            if llm.available():
                try:
                    with self.step("자막 말투 변환 (MZ)"):
                        captions.restyle(items, self.log)
                except llm.LLMError as error:
                    self.log(f"자막 말투 변환 실패, 전사 내용 그대로 씁니다: {error}")
                    warnings.append(f"자막 말투 변환 실패: {error}")
                    self.retry_next.add("captions")
            else:
                warnings.append("언어 모델 API 키가 없어 자막 말투를 바꾸지 않았습니다.")
        write_json(self.dir / "captions.json", {"items": items, "warnings": warnings, "edited": False})

    def _stage_plan(self, rerun: bool) -> None:
        o = self.options
        existing = read_json(self.dir / "plan.json")
        if existing and existing.get("edited") and not rerun:
            self.log("웹에서 수정한 기획을 사용합니다.")
            return
        plan = {"illustrations": [], "overlays": [], "effects": [], "warnings": []}
        effect_types = fx.STYLE_TYPES[o["fx_style"]]
        if not o["illustrations"] and not o["overlays"] and not effect_types:
            write_json(self.dir / "plan.json", plan)
            return
        if not llm.available():
            self.log("언어 모델 API 키가 없어 시각 요소 기획을 건너뜁니다.")
            plan["warnings"].append("API 키가 없어 일러스트, 오버레이, 예능 효과를 넣지 않았습니다.")
            write_json(self.dir / "plan.json", plan)
            return
        transcript = read_json(self.dir / "cut_transcript.json")
        lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in cuts.sentences(transcript["segments"])]
        duration = media.probe(self.dir / "cut.mp4")["duration"]
        if not lines:
            write_json(self.dir / "plan.json", plan)
            return
        with self.step("시각 요소·예능 효과 기획 (AI)"):
            minutes = duration / 60

            def wanted(enabled: bool, per_minute: float) -> int:
                return max(round(minutes * per_minute), 1) if enabled else 0

            try:
                raw = llm.plan_visuals(lines, duration, wanted(o["illustrations"], o["illust_per_min"]),
                                       wanted(o["overlays"], o["overlay_per_min"]), available_logos(),
                                       wanted(bool(effect_types), o["fx_per_min"]), effect_types)
            except llm.LLMError as error:
                self.log(f"기획 실패: {error}")
                plan["warnings"].append(f"시각 요소 기획 실패: {error}")
                write_json(self.dir / "plan.json", plan)
                self.retry_next.add("plan")
                return
            plan = sanitize_plan(raw, duration, o)
            self.log(f"기획 완료: 일러스트 {len(plan['illustrations'])}개, 오버레이 {len(plan['overlays'])}개, "
                     f"예능 효과 {len(plan['effects'])}개")
        write_json(self.dir / "plan.json", plan)

    def _stage_images(self, _rerun: bool) -> None:
        plan = read_json(self.dir / "plan.json")
        with self.step("이미지 준비 (일러스트·아이콘·로고)"):
            plan["warnings"] = [w for w in plan.get("warnings", []) if "기획" in w or "API 키" in w]
            plan["warnings"] += images.generate_all(plan, self.dir / "images", self.options["image_provider"], self.log, self.runner)
        write_json(self.dir / "plan.json", plan)

    def _stage_pii(self, _rerun: bool) -> None:
        o = self.options
        result = {"tracks": [], "speech": [], "people": []}
        previous = read_json(self.dir / "pii.json", {}) or {}
        manual = [t for t in previous.get("tracks", []) if t.get("manual")]
        if o["mosaic"] or o["face_mosaic"]:
            with self.step("개인정보 탐지 (OCR)"):
                info = media.probe(self.dir / "cut.mp4")
                result["tracks"] = privacy.scan_video(self.dir / "cut.mp4", {**self.info, "duration": info["duration"]}, o, self.log, self.runner)
                kinds: dict[str, int] = {}
                for track in result["tracks"]:
                    kinds[track["kind"]] = kinds.get(track["kind"], 0) + 1
                found = ", ".join(f"{k} {v}곳" for k, v in kinds.items()) or "없음"
                self.log(f"화면 개인정보: {found}")
        if o["audio_mute"]:
            with self.step("음성 개인정보 탐지"):
                segments = read_json(self.dir / "cut_transcript.json")["segments"]
                result["speech"] = privacy.find_spoken_pii(segments, llm.available(), self.log)
                self.log(f"음성 개인정보: {len(result['speech'])}곳")
        if o["overlays"] or o["fx_style"] != "off":
            with self.step("사람 위치 찾기"):
                result["people"] = layout.scan_people(self.dir / "cut.mp4", self.info, self.log, self.runner)
                self.log(f"사람이 보이는 구간: {len(result['people'])}초")
        for index, track in enumerate(manual, len(result["tracks"]) + 1):
            result["tracks"].append({**track, "id": index})
        write_json(self.dir / "pii.json", result)

    def _stage_render(self, _rerun: bool) -> None:
        o = self.options
        cut_plan = read_json(self.dir / "cut_plan.json")
        plan = read_json(self.dir / "plan.json")
        pii = read_json(self.dir / "pii.json")
        caption_data = read_json(self.dir / "captions.json")
        keeps = cut_plan["keeps"]
        info = self.info
        width, height = info["width"], info["height"]
        people = pii.get("people", [])
        items = captions.for_display(caption_data["items"], pii["speech"])
        burn = o["burn_subtitles"] and bool(items)
        with self.step("최종 렌더 (모자이크+효과+합성)"):
            layers = render.build_layers(plan, self.dir / "images", o["illust_position"], width, height, self.log, people, burn)
            tracks = [t for t in pii["tracks"] if t["enabled"]]
            audio = self._final_audio(pii, plan.get("effects", []))
            effects = fx.FrameEffects(plan.get("effects", []), width, height, info["fps"], people)
            track = render.CaptionTrack(items, width, height) if burn else None
            render.render_final(self.path, info, keeps, layers, tracks, audio, self.dir / "final.mp4", self.dir,
                                o["crf"], self.log, self.runner, effects, track)
        with self.step("자막·리포트 작성"):
            (self.dir / "final.srt").write_text(report.build_srt(items), encoding="utf-8")
            warnings = list(plan.get("warnings", [])) + list(caption_data.get("warnings", []))
            if cut_plan.get("review_failed"):
                warnings.append("문맥 검토 없이 규칙으로만 잘랐습니다. 대본 탭에서 지워진 말을 확인해 주세요.")
            if not info["has_audio"]:
                warnings.append("오디오가 없는 영상이라 컷 편집을 하지 않았습니다.")
            state = {"cut_plan": cut_plan, "plan": plan, "pii": pii, "warnings": warnings, "subtitles": len(items),
                     "source_duration": info["duration"], "final_duration": media.probe(self.dir / "final.mp4")["duration"]}
            summary = report.summarize(state)
            write_json(self.dir / "summary.json", summary)
            (self.dir / "report.md").write_text(report.build_markdown(self.path.name, summary, state), encoding="utf-8")

    def _final_audio(self, pii: dict, effects: list[dict]) -> Path | None:
        source = self.dir / "cut_audio.wav"
        if not source.exists():
            return None
        ranges = [(s["start"], s["end"]) for s in pii["speech"] if s["enabled"]]
        if not ranges and not effects:
            return source
        samples, rate = media.read_wav(source)
        target = self.dir / "final_audio.wav"
        media.write_wav(target, fx.mix_sfx(media.mute_ranges(samples, rate, ranges), rate, effects), rate)
        return target


def sanitize_plan(raw: dict, duration: float, options: dict) -> dict:
    """모델 응답의 시간이 영상을 벗어나거나 겹쳐도 렌더가 깨지지 않게 다듬는다."""
    def spans(items: list[dict], low: float, high: float) -> list[dict]:
        clean = []
        for item in sorted(items, key=lambda i: i["start"]):
            start = max(0.0, float(item["start"]))
            end = min(duration, max(float(item["end"]), start + low), start + high)
            if end - start < low * 0.6:
                continue
            if clean and start < clean[-1]["end"]:
                continue
            clean.append({**item, "id": len(clean) + 1, "start": round(start, 2), "end": round(end, 2), "enabled": True})
        return clean

    illustrations = spans(raw.get("illustrations", []), 3.0, 9.0) if options["illustrations"] else []
    overlays = spans(raw.get("overlays", []), 1.2, 3.5) if options["overlays"] else []
    # 카드가 떠 있는 동안 모서리 아이콘이 나오면 눈이 분산된다.
    overlays = [o for o in overlays if not any(o["start"] < i["end"] and o["end"] > i["start"] for i in illustrations)]
    for item in illustrations:
        item["label"] = item.get("label", "")[:14]

    allowed = fx.STYLE_TYPES[options["fx_style"]]
    effects = []
    for item in spans([i for i in raw.get("effects", []) if i["type"] in allowed], 0.5, 6.0):
        # 일러스트 카드가 화면을 덮는 동안에는 효과가 보이지 않는다.
        if any(item["start"] < i["end"] and item["end"] > i["start"] for i in illustrations):
            continue
        item["end"] = min(item["end"], round(item["start"] + fx.MAX_SECONDS[item["type"]], 2))
        item["text"] = item.get("text", "")[:14] if item["type"] in fx.TEXT_TYPES else ""
        if item["type"] in fx.TEXT_TYPES and not item["text"].strip():
            continue
        effects.append({**item, "id": len(effects) + 1})
    return {"illustrations": illustrations, "overlays": overlays, "effects": effects, "warnings": []}


# ---- 웹 API가 쓰는 조회·수정
def stage_status(path: Path, options: dict) -> dict:
    """영상 목록의 칩 색. 결과 파일이 있고 현재 옵션과 맞으면 done, 옵션이 달라졌으면 stale."""
    vid = video_id(path)
    folder = WORKSPACE / vid
    state = read_json(folder / "state.json", {}) or {}
    status = {}
    if not state:
        return {name: "none" for name in STAGES}
    try:
        project = Project(path, options, lambda _m: None, Runner())
    except RuntimeError:
        return {name: "none" for name in STAGES}
    for name in STAGES:
        if name not in state:
            status[name] = "none"
        elif project.fresh(name) and all(status[dep] == "done" for dep in project.depends(name)):
            status[name] = "done"
        else:
            status[name] = "stale"  # 앞 단계가 다시 돌면 이 단계도 다시 돈다
    return status


def bump(vid: str, stage: str) -> None:
    """웹에서 결과를 고쳤을 때: 번호를 바꿔 뒤 단계가 다시 돌게 한다."""
    folder = workdir(vid)
    state = read_json(folder / "state.json", {}) or {}
    if stage in state:
        state[stage]["rev"] = uuid.uuid4().hex[:8]
        write_json(folder / "state.json", state)


def invalidate(vid: str, stage: str) -> None:
    """웹에서 컷을 고쳤을 때: 이 단계 자체를 다시 돌려야 한다."""
    folder = workdir(vid)
    state = read_json(folder / "state.json", {}) or {}
    if stage in state:
        state[stage]["hash"] = ""
        write_json(folder / "state.json", state)


def clear_cache(vid: str) -> None:
    shutil.rmtree(WORKSPACE / vid, ignore_errors=True)


def key_status() -> dict:
    return {"claude": mask_key(get_key(CLAUDE_KEY)), "openai": mask_key(get_key(OPENAI_KEY))}
