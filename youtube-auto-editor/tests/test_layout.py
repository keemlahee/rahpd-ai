"""아이콘 자리 잡기: 사람을 가리지 않아야 한다."""
import numpy as np
from PIL import Image

from app import layout, render

W, H, MARGIN = 1920, 1080, 36
CHIP = 180


def rect_of(x: int, y: int, scale: float, size: int = CHIP) -> tuple:
    return x, y, x + int(size * scale), y + int(size * scale)


def test_free_screen_uses_the_top_right_at_full_size():
    x, y, scale = layout.place(CHIP, CHIP, W, H, [], MARGIN)
    assert (scale, y) == (1.0, MARGIN)
    assert x + CHIP == W - MARGIN


def test_moves_to_another_corner_when_a_person_is_there():
    person = (1200, 100, 1920, 1080)
    x, y, scale = layout.place(CHIP, CHIP, W, H, [person], MARGIN)
    assert scale == 1.0
    assert x == MARGIN
    assert layout.overlap_area(rect_of(x, y, scale), person) == 0


def test_shrinks_when_no_corner_is_free_at_full_size():
    # 위쪽 띠에 남은 틈이 작아서 큰 아이콘은 어디에도 안 들어간다.
    people = [(MARGIN + 110, 0, W - MARGIN - 110, H), (0, MARGIN + 110, W, H)]
    x, y, scale = layout.place(CHIP, CHIP, W, H, people, MARGIN)
    assert scale < 1.0
    assert all(layout.overlap_area(rect_of(x, y, scale), person) == 0 for person in people)


def test_person_filling_the_screen_still_gives_a_position():
    x, y, scale = layout.place(CHIP, CHIP, W, H, [(0, 0, W, H)], MARGIN)
    assert scale == layout.SCALES[-1]
    assert 0 <= x < W and 0 <= y < H


def test_subtitles_are_avoided_too():
    band = layout.subtitle_band(W, H)
    person = (0, 0, W, 300)
    x, y, scale = layout.place(CHIP, CHIP, W, H, [person, band], MARGIN)
    assert layout.overlap_area(rect_of(x, y, scale), band) == 0


def test_person_zone_reaches_the_bottom_and_widens_around_the_face():
    x0, y0, x1, y1 = layout.person_zone((900, 200, 1000, 320), W, H)
    assert x0 < 900 and x1 > 1000 and y0 < 200 and y1 == H


def test_zones_cover_the_seconds_around_the_item():
    people = [{"t": 3.0, "zones": [[1, 1, 2, 2]]}, {"t": 9.0, "zones": [[3, 3, 4, 4]]}]
    assert layout.zones_during(people, 3.5, 5.0) == [(1, 1, 2, 2)]
    assert layout.zones_during(None, 0, 10) == []


def icon_plan(tmp_path, *spans) -> tuple[dict, str]:
    Image.new("RGBA", (200, 200), (240, 100, 80, 255)).save(tmp_path / "icon.png")
    items = [{"start": start, "end": end, "image": "icon.png", "kind": "icon"} for start, end in spans]
    return {"illustrations": [], "overlays": items}, tmp_path


def boxes(layers) -> list[tuple]:
    return [(l.x, l.y, l.x + l.image.shape[1], l.y + l.image.shape[0]) for l in layers]


def test_rendered_icon_avoids_the_person(tmp_path):
    plan, folder = icon_plan(tmp_path, (5.0, 7.0))
    zone = [1100, 0, W, H]
    layers = render.build_layers(plan, folder, "center", W, H, lambda _m: None, [{"t": 5.0, "zones": [zone]}], True)
    assert layout.overlap_area(boxes(layers)[0], tuple(zone)) == 0


def test_icon_is_smaller_when_the_person_leaves_little_room(tmp_path):
    plan, folder = icon_plan(tmp_path, (5.0, 7.0))
    free = render.build_layers(plan, folder, "center", W, H, lambda _m: None, [], False)[0]
    people = [{"t": 5.0, "zones": [[120, 0, W - 120, H], [0, 150, W, H]]}]
    tight = render.build_layers(plan, folder, "center", W, H, lambda _m: None, people, False)[0]
    assert tight.image.shape[0] < free.image.shape[0]


def test_two_icons_at_the_same_time_do_not_cover_each_other(tmp_path):
    plan, folder = icon_plan(tmp_path, (5.0, 7.0), (6.0, 8.0))
    first, second = boxes(render.build_layers(plan, folder, "center", W, H, lambda _m: None, [], False))
    assert layout.overlap_area(first, second) == 0


def test_side_card_moves_to_the_side_without_the_person(tmp_path):
    Image.new("RGBA", (600, 400), (240, 100, 80, 255)).save(tmp_path / "art.png")
    plan = {"illustrations": [{"start": 5.0, "end": 8.0, "image": "art.png", "label": ""}], "overlays": []}
    people = [{"t": 5.0, "zones": [[1200, 0, W, H]]}]
    layer = render.build_layers(plan, tmp_path, "right", W, H, lambda _m: None, people, False)[0]
    assert layer.x + layer.image.shape[1] < 1200


def test_center_card_is_not_moved_by_people(tmp_path):
    Image.new("RGBA", (600, 400), (240, 100, 80, 255)).save(tmp_path / "art.png")
    plan = {"illustrations": [{"start": 5.0, "end": 8.0, "image": "art.png", "label": ""}], "overlays": []}
    plain = render.build_layers(plan, tmp_path, "center", W, H, lambda _m: None, [], False)[0]
    people = [{"t": 5.0, "zones": [[800, 0, 1100, H]]}]
    assert render.build_layers(plan, tmp_path, "center", W, H, lambda _m: None, people, False)[0].x == plain.x


def test_big_text_avoids_the_person_and_subtitles():
    items = [{"start": 1.0, "end": 2.0, "type": "impact_text", "text": "실화냐?!"}]
    zone = (500, 0, 1400, H)
    box = boxes(render._effect_layers(items, [{"t": 1.0, "zones": [list(zone)]}], W, H, 12))[0]
    assert layout.overlap_area(box, zone) == 0
    assert np.isclose(layout.overlap_area(box, layout.subtitle_band(W, H)), 0)
