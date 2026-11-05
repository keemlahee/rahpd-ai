"""OpenAI 전사 응답 처리와 언어 모델 예비 경로. 네트워크에 나가지 않는다."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from app import llm, transcribe


def words(*pairs) -> list:
    return [NS(word=text, start=start, end=end) for text, start, end in pairs]


def test_response_gets_offset_and_grouped_by_segment():
    result = transcribe.merge_response(words(("안녕", 0.0, 0.4), ("하세요", 0.4, 0.8), ("시작", 2.0, 2.4)),
                                       [NS(text="안녕 하세요", end=1.0), NS(text="시작", end=3.0)], 100.0)
    assert [s["text"] for s in result] == ["안녕 하세요", "시작"]
    assert result[1]["start"] == 102.0 and result[1]["words"][0]["end"] == 102.4


def test_punctuation_comes_back_from_the_segment_text():
    result = transcribe.merge_response(words(("네", 0.0, 0.2), ("맞습니다", 0.3, 0.8)), [NS(text=" 네, 맞습니다.", end=1.0)], 0.0)
    assert [w["word"] for w in result[0]["words"]] == ["네,", "맞습니다."]


def test_word_list_is_kept_when_token_count_differs():
    result = transcribe.merge_response(words(("가나", 0.0, 0.2), ("다", 0.3, 0.8)), [NS(text="가나다", end=1.0)], 0.0)
    assert [w["word"] for w in result[0]["words"]] == ["가나", "다"]


def test_no_words_gives_no_segments():
    assert transcribe.merge_response([], [NS(text="", end=1.0)], 0.0) == []


def test_word_past_the_last_segment_end_is_not_lost():
    result = transcribe.merge_response(words(("끝", 5.0, 5.3)), [NS(text="끝", end=4.9)], 0.0)
    assert len(result) == 1


def test_short_audio_is_uploaded_whole():
    samples = np.zeros((16000 * 60, 1), dtype=np.int16)
    assert transcribe.split_points(samples, 16000) == [(0.0, 60.0)]


def test_long_audio_is_split_in_the_quiet_part():
    rate, limit = 16000, 100.0
    samples = (np.random.default_rng(0).normal(0, 3000, rate * 250).astype(np.int16))[:, None]
    quiet = int(rate * 90)
    samples[quiet:quiet + rate] = 0
    bounds = transcribe.split_points(samples, rate, limit)
    assert 89.9 <= bounds[0][1] <= 91.1
    assert bounds[0][0] == 0.0 and bounds[-1][1] == 250.0
    assert all(end - start <= limit for start, end in bounds)
    assert all(a[1] == b[0] for a, b in zip(bounds, bounds[1:]))


@pytest.fixture
def keys(monkeypatch):
    state = {"claude": "c", "openai": "o"}
    monkeypatch.setattr(llm, "get_key", lambda name: state["claude" if "ANTHROPIC" in name else "openai"])
    monkeypatch.setattr(llm, "_claude_paused_until", 0.0)
    return state


def test_claude_is_used_first(keys, monkeypatch):
    monkeypatch.setattr(llm, "_ask_claude", lambda *_a: {"from": "claude"})
    monkeypatch.setattr(llm, "_ask_openai", lambda *_a: pytest.fail("OpenAI를 부르면 안 된다"))
    assert llm.ask_json("s", "u", {}) == {"from": "claude"}


def test_openai_takes_over_when_claude_has_no_credit(keys, monkeypatch):
    calls = []

    def refuse(*_args):
        calls.append(1)
        raise llm.NoCreditError("크레딧 부족")

    monkeypatch.setattr(llm, "_ask_claude", refuse)
    monkeypatch.setattr(llm, "_ask_openai", lambda *_a: {"from": "openai"})
    assert llm.ask_json("s", "u", {}) == {"from": "openai"}
    assert llm.ask_json("s", "u", {}) == {"from": "openai"}
    assert len(calls) == 1  # 크레딧이 없다는 것을 안 뒤에는 Claude를 다시 부르지 않는다


def test_openai_only_setup_works(keys, monkeypatch):
    keys["claude"] = ""
    monkeypatch.setattr(llm, "_ask_openai", lambda *_a: {"from": "openai"})
    assert llm.ask_json("s", "u", {}) == {"from": "openai"}
    assert llm.available()


def test_claude_error_is_reported_when_there_is_no_openai_key(keys, monkeypatch):
    keys["openai"] = ""

    def refuse(*_args):
        raise llm.LLMError("Claude 오류")

    monkeypatch.setattr(llm, "_ask_claude", refuse)
    with pytest.raises(llm.LLMError, match="Claude 오류"):
        llm.ask_json("s", "u", {})


def test_no_keys_means_not_available(keys):
    keys["claude"] = keys["openai"] = ""
    assert not llm.available()
    with pytest.raises(llm.LLMError):
        llm.ask_json("s", "u", {})
