"""캐시 판단: 실패한 단계가 완료로 남으면 원인을 해결해도 다시 실행되지 않는다."""
from pathlib import Path

import pytest

from app import pipeline
from app.config import clean_options, read_json, write_json
from app.media import Runner


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.media, "probe", lambda _p: {"width": 64, "height": 36, "fps": 30.0, "duration": 1.0,
                                                             "has_audio": True, "color_space": ""})
    monkeypatch.setattr(pipeline, "video_id", lambda _p: "test")
    monkeypatch.setattr(pipeline, "workdir", lambda _v: tmp_path)
    return pipeline.Project(Path("clip.mp4"), clean_options({}), lambda _m: None, Runner())


def run_stub(project, monkeypatch, fail: bool) -> list[str]:
    """각 단계를 결과 파일만 만드는 가짜로 바꿔, 실제로 실행된 단계 목록을 돌려준다."""
    ran: list[str] = []

    def stub(name: str):
        def work(_rerun: bool) -> None:
            ran.append(name)
            for output in pipeline.OUTPUTS[name]:
                (project.dir / output).touch()
            if fail and name == "plan":
                project.retry_next.add("plan")
        return work

    project.retry_next = set()
    for name in pipeline.STAGES:
        monkeypatch.setattr(project, f"_stage_{name}", stub(name))
    project.run("all", False)
    return ran


def test_second_run_reuses_everything(project, monkeypatch):
    run_stub(project, monkeypatch, fail=False)
    assert run_stub(project, monkeypatch, fail=False) == []


def test_failed_stage_runs_again_with_everything_after_it(project, monkeypatch):
    run_stub(project, monkeypatch, fail=True)
    assert "plan" not in project.state
    again = run_stub(project, monkeypatch, fail=False)
    assert again == ["plan", "images", "render"]


def test_failed_stage_does_not_rerun_stages_before_it(project, monkeypatch):
    run_stub(project, monkeypatch, fail=True)
    assert "transcribe" not in run_stub(project, monkeypatch, fail=False)


def test_repeated_plan_failure_reruns_nothing_else(project, monkeypatch):
    """크레딧 없이 쓸 때 실행할 때마다 OCR과 렌더가 다시 돌면 안 된다."""
    run_stub(project, monkeypatch, fail=True)
    assert run_stub(project, monkeypatch, fail=True) == ["plan"]


def test_cut_review_failure_does_not_leave_cut_unfinished(project, monkeypatch):
    def refuse(*_args):
        raise pipeline.llm.LLMError("크레딧 부족")

    monkeypatch.setattr(pipeline.llm, "available", lambda: True)
    monkeypatch.setattr(pipeline.llm, "review_cuts", refuse)
    plan = {"cuts": [{"id": 1}]}
    project._review_cuts(plan, [{"word": "음"}], None)
    assert plan["review_failed"] is True
    assert project.retry_next == set()


def test_edited_plan_is_kept_without_calling_claude(project, monkeypatch):
    plan = {"illustrations": [{"label": "직접 쓴 일러스트"}], "overlays": [], "warnings": [], "edited": True}
    write_json(project.dir / "plan.json", plan)
    monkeypatch.setattr(pipeline.llm, "plan_visuals", lambda *_a: pytest.fail("언어 모델을 부르면 안 된다"))
    project._stage_plan(False)
    assert read_json(project.dir / "plan.json") == plan


def test_forced_rerun_replaces_edited_plan(project, monkeypatch):
    write_json(project.dir / "plan.json", {"illustrations": [{"label": "직접"}], "overlays": [], "edited": True})
    monkeypatch.setattr(pipeline.llm, "available", lambda: False)
    project._stage_plan(True)
    assert read_json(project.dir / "plan.json")["illustrations"] == []
