"""화면 자막: 나누기, 말투 변환, 개인정보 가리기."""
import pytest

from app import captions, llm


def segment(text: str, gap: float = 0.1, removed: tuple = ()) -> dict:
    words, cursor = [], 0.0
    for index, token in enumerate(text.split()):
        words.append({"word": token, "start": cursor, "end": cursor + 0.3, "removed": index in removed})
        cursor += 0.3 + gap
    return {"words": words}


def test_long_speech_is_split_into_readable_pieces():
    items = captions.build_items([segment("오늘은 유튜브 영상을 자동으로 편집하는 프로그램을 소개해 드리겠습니다")])
    assert len(items) >= 2
    assert all(len(item["source"]) <= captions.MAX_CHARS for item in items)


def test_word_left_alone_at_the_end_joins_the_line_before():
    items = captions.build_items([segment("그리고 쓸데없는 추임새도 함께 지워 줍니다.")])
    assert [item["source"] for item in items] == ["그리고 쓸데없는 추임새도 함께 지워 줍니다."]
    assert [item["id"] for item in items] == [1]


def test_finished_sentence_is_not_joined_to_the_next():
    items = captions.build_items([segment("이건 정말 긴 문장이라서 여기서 끝납니다. 네.")])
    assert items[-1]["source"] == "네."


def test_removed_words_do_not_appear_in_captions():
    items = captions.build_items([segment("음 안녕하세요", removed=(0,))])
    assert [item["source"] for item in items] == ["안녕하세요"]


def test_sentence_end_starts_a_new_caption():
    items = captions.build_items([segment("네. 시작합니다.")])
    assert [item["source"] for item in items] == ["네.", "시작합니다."]


def test_long_pause_starts_a_new_caption():
    items = captions.build_items([segment("앞말 뒷말", gap=2.0)])
    assert len(items) == 2


def test_short_caption_stays_long_enough_to_read_but_not_past_the_next():
    items = captions.build_items([segment("네. 다음", gap=0.3)])
    assert items[0]["end"] <= items[1]["start"]
    alone = captions.build_items([segment("네.")])
    assert alone[0]["end"] - alone[0]["start"] >= captions.MIN_SECONDS - 0.01


def items_for(*texts: str) -> list[dict]:
    return [{"id": i, "start": float(i), "end": i + 1.0, "source": text, "text": text} for i, text in enumerate(texts, 1)]


def test_restyle_replaces_text_and_keeps_original(monkeypatch):
    monkeypatch.setattr(llm, "restyle_lines", lambda lines: {line["id"]: "진짜 대박임" for line in lines})
    items = items_for("정말 놀랍습니다")
    captions.restyle(items, lambda _m: None)
    assert items[0]["text"] == "진짜 대박임"
    assert items[0]["source"] == "정말 놀랍습니다"


def test_line_the_model_skipped_keeps_the_original(monkeypatch):
    monkeypatch.setattr(llm, "restyle_lines", lambda lines: {1: "바뀐 줄"})
    items = items_for("첫 줄", "둘째 줄")
    captions.restyle(items, lambda _m: None)
    assert [item["text"] for item in items] == ["바뀐 줄", "둘째 줄"]


def test_line_too_long_to_read_keeps_the_original(monkeypatch):
    monkeypatch.setattr(llm, "restyle_lines", lambda lines: {1: "아" * 200})
    items = items_for("짧은 말")
    captions.restyle(items, lambda _m: None)
    assert items[0]["text"] == "짧은 말"


def test_long_video_is_sent_in_batches(monkeypatch):
    sizes = []

    def record(lines):
        sizes.append(len(lines))
        return {}

    monkeypatch.setattr(llm, "restyle_lines", record)
    captions.restyle(items_for(*["말"] * (captions.BATCH * 2 + 5)), lambda _m: None)
    assert sorted(sizes) == [5, captions.BATCH, captions.BATCH]


@pytest.mark.parametrize("text, secret", [
    ("제 번호 010-1234-5678 ㄹㅇ임", "010-1234-5678"),
    ("메일은 hong@example.com 임", "hong@example.com"),
    ("키는 sk-ant-api03-abcDEF1234567890xyz 임", "abcDEF1234567890"),
])
def test_personal_info_never_reaches_the_screen(text, secret):
    shown = captions.for_display(items_for(text), [])[0]["text"]
    assert secret not in shown
    assert captions.MASK in shown


def test_ordinary_caption_is_unchanged():
    assert captions.mask_pii("이거 ㄹㅇ 개꿀임") == "이거 ㄹㅇ 개꿀임"


def test_caption_over_muted_speech_is_hidden():
    items = items_for("공일공 일이삼사")
    speech = [{"start": 1.2, "end": 1.8, "enabled": True}]
    assert captions.for_display(items, speech)[0]["text"] == captions.MUTED
    speech[0]["enabled"] = False
    assert captions.for_display(items, speech)[0]["text"] == "공일공 일이삼사"


def test_foreign_speech_is_cut_into_longer_pieces_for_translation():
    speech = [segment("What is the most impressive part when you met Korean guys like a specific thing")]
    assert len(captions.build_items(speech, captions.FOREIGN_CHARS)) < len(captions.build_items(speech))


def test_translation_replaces_text_and_skips_korean_restyle(monkeypatch):
    calls = []

    def translate(lines, mz):
        calls.append(mz)
        return {line["id"]: "한국 남자 스타일 좋아함" for line in lines}

    monkeypatch.setattr(llm, "translate_lines", translate)
    monkeypatch.setattr(llm, "restyle_lines", lambda lines: pytest.fail("번역할 때는 한국어 말투 변환을 부르지 않는다"))
    items = items_for("I like Korean men's style")
    captions.restyle(items, lambda _m: None, translate=True, mz=False)
    assert items[0]["text"] == "한국 남자 스타일 좋아함"
    assert items[0]["source"] == "I like Korean men's style"
    assert calls == [False]
