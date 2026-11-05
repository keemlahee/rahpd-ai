"""개인정보 탐지와 위치 추적."""
import numpy as np
import pytest

from app import privacy


@pytest.mark.parametrize("text, kind, value", [
    ("연락처 010-1234-5678", "휴대전화", "010-1234-5678"),
    ("010 1234 5678로 전화", "휴대전화", "010 1234 5678"),
    ("O1O-1234-5678", "휴대전화", "010-1234-5678"),
    ("+82 10-1234-5678", "휴대전화", "+82 10-1234-5678"),
    ("대표번호 02-123-4567", "전화번호", "02-123-4567"),
    ("메일 hong.gildong+a@naver.co.kr 입니다", "이메일", "hong.gildong+a@naver.co.kr"),
    ("주민번호 900101-1234567", "주민등록번호", "900101-1234567"),
    ("카드 1234-5678-9012-3456", "카드번호", "1234-5678-9012-3456"),
    ("키 sk-ant-api03-abcDEF1234567890xyz", "API 키", "sk-ant-api03-abcDEF1234567890xyz"),
    ("ghp_abcdefghijklmnopqrstuvwxyz123456", "API 키", "ghp_abcdefghijklmnopqrstuvwxyz123456"),
    ("서울특별시 강남구 테헤란로 123", "주소", "서울특별시 강남구 테헤란로 123"),
    ("서을특별시 강남구 테헤란로 123", "주소", "서을특별시 강남구 테헤란로 123"),
    ("부산 해운대구 우동 1408", "주소", "부산 해운대구 우동 1408"),
    ("주소: 어딘가 알 수 없는 곳", "주소", "어딘가 알 수 없는 곳"),
    ("이름: 홍길동", "이름", "홍길동"),
    ("김철수010-2222-3333", "이름", "김철수"),
    ("박민수 minsu.park@example.org", "이름", "박민수"),
    ("예금주 김철수", "이름", "김철수"),
    ("password: hunter2!", "비밀 값", "hunter2!"),
    ("국민은행 123456-78-901234", "계좌번호", "123456-78-901234"),
])
def test_detects(text, kind, value):
    found = privacy.find_in_text(text)
    source = text.translate(privacy.DIGIT_FIX) if text.startswith("O1O") else text
    assert any(k == kind and source[start:end] == value for k, start, end in found), found


@pytest.mark.parametrize("text", [
    "회원 정보 수정", "버전 3.12.10", "2026-11-05 오후 3시", "조회수 1,234,567회", "1920x1080 30fps",
    "이름을 입력하세요", "gpt-image-2 모델", "오늘 날씨가 좋습니다", "전화번호를 입력해 주세요",
    "이 프로그램은 무음 구간을 찾아서 자동으로 잘라 줍니다", "대시보드 메뉴에서 설정을 고른 뒤 다음으로 이동",
])
def test_ignores_ordinary_text(text):
    assert privacy.find_in_text(text) == []


def _spoken(text: str) -> list[dict]:
    words = [{"word": token, "start": i * 0.5, "end": i * 0.5 + 0.4, "removed": False} for i, token in enumerate(text.split())]
    return [{"text": text, "words": words}]


def test_spoken_phone_number_is_found_with_its_time():
    spans = privacy.find_spoken_pii(_spoken("제 번호는 010-1234-5678 입니다"), False, lambda _m: None)
    assert [(s["kind"], s["start"], s["end"]) for s in spans] == [("휴대전화", 1.0, 1.4)]


def test_number_read_aloud_in_korean_is_found():
    spans = privacy.find_spoken_pii(_spoken("연락처는 공일공 일이삼사 오육칠팔 입니다"), False, lambda _m: None)
    assert any(s["kind"] == "숫자 나열" and s["start"] == 0.5 and s["end"] == 1.9 for s in spans)


def test_ordinary_speech_is_not_muted():
    assert privacy.find_spoken_pii(_spoken("오늘은 프로그램을 소개해 드리겠습니다"), False, lambda _m: None) == []


def test_claude_finding_is_placed_on_the_screen_line():
    lines = [((100, 10, 500, 40), "담당자는 김민지 대리입니다")]
    found = privacy._line_detections(lines, 1920, 1080, {lines[0][1]: [{"text": "김민지", "kind": "이름"}]})
    assert [d["kind"] for d in found] == ["이름"]
    assert 100 <= found[0]["rect"][0] < 300


def test_value_in_cell_next_to_label_cell_is_found():
    lines = [((100, 10, 160, 40), "성명"), ((200, 10, 280, 40), "이순신")]
    found = privacy._line_detections(lines, 1920, 1080, {})
    assert [(d["kind"], d["text"]) for d in found] == [("이름", "이순신")]


def test_contact_label_is_not_taken_for_a_name():
    kinds = {kind for kind, _, _ in privacy.find_in_text("연락처 010-1234-5678")}
    assert kinds == {"휴대전화"}


def test_sub_rect_covers_only_the_value():
    text = "고객센터 담당 직원의 연락처는 010-1234-5678"
    start = text.index("0")
    x0, _, x1, _ = privacy.sub_rect((100, 10, 900, 40), text, start, len(text))
    assert 400 < x0 < 640
    assert x1 == 900


def test_value_filling_most_of_the_line_masks_the_whole_box():
    text = "연락처 010-1234-5678"
    box = (100, 10, 500, 40)
    assert privacy.sub_rect(box, text, text.index("0"), len(text)) == box


def _scrolling(times, speed=100, top=500):
    return [(t, [{"kind": "휴대전화", "text": "010-1111-2222",
                  "rect": (100, top - int(t * speed), 400, top + 40 - int(t * speed))}]) for t in times]


def test_track_covers_the_whole_path_between_samples():
    tracks = privacy.build_tracks(_scrolling((1.0, 1.5, 2.0)), 0.5)
    assert len(tracks) == 1
    _, top, _, bottom = privacy.rect_at(tracks[0], 1.25)
    assert top <= 350 and bottom >= 440  # t=1.0 위치(400~440)와 t=1.5 위치(350~390)를 모두 덮는다


def test_track_extends_along_its_motion_before_first_and_after_last_sample():
    track = privacy.build_tracks(_scrolling((1.0, 1.5, 2.0)), 0.5)[0]
    assert track["start"] == 0.5 and track["end"] == 2.5
    _, top, _, bottom = privacy.rect_at(track, 0.7)
    assert bottom >= 490  # 스크롤로 올라오는 글자는 첫 샘플 전에 더 아래에 있었다
    _, top, _, bottom = privacy.rect_at(track, 2.3)
    assert top <= 250
    for _, rect in track["points"]:
        assert all(-1000 < value < 3000 for value in rect)


def test_static_text_keeps_its_box():
    hit = {"kind": "이메일", "text": "a@b.co", "rect": (10, 20, 110, 40)}
    track = privacy.build_tracks([(1.0, [hit]), (1.5, [hit])], 0.5)[0]
    assert privacy.rect_at(track, 0.6) == (10, 20, 110, 40)
    assert privacy.rect_at(track, 1.2) == (10, 20, 110, 40)


def test_different_value_at_same_place_is_a_new_track():
    """화면이 바뀌어 같은 자리에 다른 사람의 값이 나오면 이어 붙이면 안 된다."""
    old = {"kind": "이메일", "text": "gildong.hong@example.com", "rect": (100, 350, 600, 400)}
    new = {"kind": "이메일", "text": "minsu.park@example.org", "rect": (100, 360, 600, 410)}
    assert len(privacy.build_tracks([(1.0, [old]), (1.5, [new])], 0.5)) == 2


def test_ocr_variation_at_same_place_is_the_same_track():
    one = {"kind": "이메일", "text": "minsu.park@example.org", "rect": (100, 360, 600, 410)}
    two = {"kind": "이메일", "text": "minsu.park@example.ora", "rect": (100, 362, 600, 412)}
    assert len(privacy.build_tracks([(1.0, [one]), (1.5, [two])], 0.5)) == 1


def test_row_seen_once_borrows_motion_from_rows_moving_with_it():
    """맨 위 줄은 첫 샘플 뒤 바로 화면 밖으로 나가 한 번만 잡힌다."""
    def row(text, top):
        return {"kind": "휴대전화", "text": text, "rect": (100, top, 400, top + 40)}

    samples = [(1.0, [row("010-1111-1111", 10), row("010-2222-2222", 200), row("010-3333-3333", 400)]),
               (1.5, [row("010-2222-2222", 130), row("010-3333-3333", 330)])]
    tracks = privacy.build_tracks(samples, 0.5)
    once = next(t for t in tracks if t["text"] == "010-1111-1111")
    _, _, _, bottom = privacy.rect_at(once, 0.7)
    assert bottom >= 110  # 첫 샘플 전에는 70px 더 아래에 있었다


def test_same_value_shown_twice_stays_two_tracks():
    def both(t):
        shift = int(t * 100)
        return (t, [{"kind": "휴대전화", "text": "010-1111-2222", "rect": (100, 100 - shift, 400, 140 - shift)},
                    {"kind": "휴대전화", "text": "010-1111-2222", "rect": (100, 900 - shift, 400, 940 - shift)}])

    tracks = privacy.build_tracks([both(0.0), both(0.5), both(1.0)], 0.5)
    assert len(tracks) == 2
    top, bottom = sorted(tracks, key=lambda t: t["points"][1][1][1])
    assert privacy.rect_at(top, 0.25)[3] < 200  # 멀리 떨어진 두 위치가 한 상자로 합쳐지지 않는다
    assert privacy.rect_at(bottom, 0.25)[1] > 800


def test_track_bridges_a_missed_frame():
    hit = {"kind": "이메일", "text": "a@b.co", "rect": (0, 0, 100, 20)}
    tracks = privacy.build_tracks([(0.0, [hit]), (0.5, []), (1.0, [hit])], 0.5)
    assert len(tracks) == 1


def _blank() -> np.ndarray:
    return np.full((540, 960), 255, np.uint8)


def test_no_boxes_when_only_noise_changes():
    noisy = _blank()
    noisy[10, 10] = 0
    assert privacy.changed_boxes(_blank(), noisy, 1920) == []


def test_changed_box_is_in_source_coordinates():
    after = _blank()
    after[100:120, 200:260] = 0
    (x0, y0, x1, y1), = privacy.changed_boxes(_blank(), after, 1920)
    assert x0 <= 400 and x1 >= 520 and y0 <= 200 and y1 >= 240


class FakeOcr:
    """호출된 영역을 기록하고, 정해 둔 글자를 영역 안 좌표로 돌려준다."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.shapes: list[tuple[int, int]] = []

    def read(self, frame):
        self.shapes.append(frame.shape[:2])
        return [((5, 5, 50, 20), self.text)]


def test_update_reads_only_the_changed_patch_and_keeps_other_lines():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    far = ((100, 900, 400, 930), "그대로인 줄")
    near = ((1000, 100, 1500, 130), "바뀌기 전 줄")
    ocr = FakeOcr("바뀐 줄")
    result = privacy.update_lines(ocr, frame, [far, near], [(1200, 110, 1260, 125)])
    assert ocr.shapes[0][1] < 600  # 화면 전체가 아니라 조각만 읽었다
    assert far in result and near not in result
    rect, text = next(line for line in result if line[1] == "바뀐 줄")
    assert rect[0] > 900  # 조각 좌표가 화면 좌표로 옮겨졌다


def test_patch_contains_the_whole_line_it_touches():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    line = ((100, 500, 1500, 530), "연락처 010-1234-5678")
    ocr = FakeOcr("x")
    privacy.update_lines(ocr, frame, [line], [(800, 505, 820, 525)])
    assert ocr.shapes[0][1] >= 1400


def test_large_change_reads_the_full_frame():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    ocr = FakeOcr("x")
    privacy.update_lines(ocr, frame, [], [(0, 0, 1900, 1000)])
    assert ocr.shapes == [(1080, 1920)]


def test_different_values_stay_separate():
    one = {"kind": "휴대전화", "text": "010-1111-2222", "rect": (0, 0, 100, 20)}
    two = {"kind": "휴대전화", "text": "010-3333-4444", "rect": (0, 300, 100, 320)}
    assert len(privacy.build_tracks([(0.0, [one, two]), (0.5, [one, two])], 0.5)) == 2
