"""컷 판단: '이해를 해치지 않는다'는 요구가 깨지면 바로 드러나도록 고정한다."""
from app import cuts


def words(text: str, gap: float = 0.1) -> list[dict]:
    result, cursor = [], 0.0
    for token in text.split():
        result.append({"word": token, "start": cursor, "end": cursor + 0.3})
        cursor += 0.3 + gap
    return result


def cut_texts(text: str) -> list[str]:
    tokens = words(text)
    return [tokens[c["index"]]["word"] for c in cuts.find_word_candidates(tokens)]


def test_standalone_fillers_are_found():
    assert cut_texts("음, 안녕하세요 어 그래서 으음 시작합니다") == ["음,", "어", "으음"]


def test_filler_inside_a_word_is_kept():
    assert cut_texts("음식을 어디서 에어컨 먹었어요") == []


def test_demonstrative_before_same_syllable_is_not_a_stutter():
    assert cut_texts("이 이미지를 그 그림은 저 저기") == []


def test_real_repetition_and_false_start_are_found():
    assert cut_texts("그 그 다음에") == ["그"]
    assert cut_texts("프로 프로그램이 끝나면") == ["프로"]


def test_emphasis_repetition_is_kept():
    assert cut_texts("네 네 맞습니다 천천히 천천히 해요") == []


def test_repeat_across_long_pause_is_kept():
    tokens = words("그래서 그래서", gap=2.0)
    assert cuts.find_word_candidates(tokens) == []


def test_short_wordless_piece_between_cuts_is_removed_but_word_is_kept():
    make = lambda start, end: {"start": start, "end": end, "enabled": True}
    duration = 10.0
    keeps = cuts.keeps_from_cuts([make(2.0, 3.0), make(3.1, 4.0)], duration, [])
    assert keeps == [[0.0, 2.0], [4.0, 10.0]]
    spoken = [{"start": 3.02, "end": 3.08, "word": "네"}]
    keeps = cuts.keeps_from_cuts([make(2.0, 3.0), make(3.1, 4.0)], duration, spoken)
    assert [3.0, 3.1] in keeps


def test_disabled_cut_stays_in_video():
    keeps = cuts.keeps_from_cuts([{"start": 2.0, "end": 3.0, "enabled": False}], 10.0, [])
    assert keeps == [[0.0, 10.0]]


def test_time_map_follows_cuts():
    timemap = cuts.TimeMap(cuts.to_frames([[0.0, 2.0], [3.0, 5.0]], 30.0, 300), 30.0)
    assert timemap.total == 4.0
    assert timemap.to_cut(1.0) == 1.0
    assert timemap.to_cut(3.5) == 2.5
    assert not timemap.contains(2.5)


def test_sentences_split_long_whisper_segment_and_skip_removed_words():
    tokens = [{**w, "removed": False} for w in words("안녕하세요. 음 오늘은 시작합니다. 감사합니다.")]
    tokens[1]["removed"] = True
    lines = cuts.sentences([{"words": tokens}])
    assert [line["text"] for line in lines] == ["안녕하세요.", "오늘은 시작합니다.", "감사합니다."]
    assert lines[1]["start"] == tokens[2]["start"]


def test_review_cannot_delete_a_whole_sentence():
    import numpy as np

    tokens = words("하나 둘 셋 넷 다섯 여섯 일곱 여덟")
    plan = {"cuts": []}
    db = np.full(1000, -20.0)
    cuts.apply_review(plan, tokens, db, {"extra_cuts": [{"first_word": 0, "last_word": 7, "reason": ""}]}, lambda _m: None)
    assert plan["cuts"] == []
    cuts.apply_review(plan, tokens, db, {"extra_cuts": [{"first_word": 1, "last_word": 2, "reason": "군말"}]}, lambda _m: None)
    assert len(plan["cuts"]) == 1
