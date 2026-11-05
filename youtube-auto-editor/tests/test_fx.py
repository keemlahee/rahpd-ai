"""예능 효과와 효과음."""
import numpy as np
import pytest

from app import fx, pipeline, render
from app.config import clean_options

W, H = 320, 180


def frame_with_marker() -> np.ndarray:
    frame = np.full((H, W, 3), 120, np.uint8)
    frame[85:95, 155:165] = (0, 0, 255)
    return frame


def effect(kind: str, start: float = 1.0, end: float = 3.0, **extra) -> dict:
    return {"type": kind, "start": start, "end": end, "sfx": "none", "text": "", **extra}


def apply(item: dict, t: float, frame=None, people=None) -> np.ndarray:
    frame = frame_with_marker() if frame is None else frame
    effects = fx.FrameEffects([item], W, H, 30.0, people)
    effects.apply(frame, t, effects.active(t))
    return frame


def red_pixels(frame: np.ndarray) -> int:
    return int(np.count_nonzero((frame[..., 2] > 200) & (frame[..., 0] < 80)))


def test_punch_zoom_enlarges_the_center():
    assert red_pixels(apply(effect("punch_zoom"), 2.0)) > red_pixels(frame_with_marker()) * 1.3


def test_zoom_pulls_toward_the_person():
    people = [{"t": 1.0, "zones": [[200, 20, 300, 180]]}]
    frame = frame_with_marker()
    frame[:] = 120
    frame[50:54, 250:254] = (0, 0, 255)
    zoomed = apply(effect("punch_zoom"), 2.0, frame, people)
    ys, xs = np.nonzero(zoomed[..., 2] > 200)
    assert 240 <= xs.mean() <= 260


def test_mono_removes_color_and_adds_bars():
    frame = apply(effect("mono_tension"), 2.0)
    assert np.array_equal(frame[..., 0], frame[..., 2])
    assert frame[0, 160].tolist() == [0, 0, 0]


def test_focus_lines_draw_black_strokes():
    frame = apply(effect("focus_lines"), 2.0)
    assert np.count_nonzero(frame.sum(axis=2) == 0) > 500
    assert red_pixels(frame) == red_pixels(frame_with_marker())


def test_shake_moves_the_picture_and_settles():
    item = effect("shake", 1.0, 2.0)
    moved = apply(item, 1.1)
    assert not np.array_equal(moved, frame_with_marker())
    assert np.array_equal(apply(item, 2.0), frame_with_marker())


def test_freeze_holds_the_first_frame():
    effects = fx.FrameEffects([effect("freeze")], W, H, 30.0)
    first = frame_with_marker()
    effects.apply(first, 1.5, effects.active(1.5))
    later = np.zeros((H, W, 3), np.uint8)
    effects.apply(later, 2.0, effects.active(2.0))
    assert np.array_equal(later, first)


def test_disabled_effect_is_ignored():
    assert fx.FrameEffects([effect("shake", enabled=False)], W, H, 30.0).active(2.0) == []


def test_every_style_only_uses_known_effects():
    for kinds in fx.STYLE_TYPES.values():
        assert set(kinds) <= set(fx.MAX_SECONDS)


@pytest.mark.parametrize("name", ["pop", "boom", "whoosh", "ding", "tension"])
def test_sound_effect_is_audible_and_not_clipped(name):
    sound = fx.make_sfx(name, 48000)
    assert 0.9 <= np.max(np.abs(sound)) <= 1.0
    assert len(sound) < 48000 * 3


def test_no_sound_for_none():
    assert len(fx.make_sfx("none", 48000)) == 0


def test_sound_is_mixed_at_its_time_and_voice_does_not_wrap_around():
    samples = np.full((48000 * 2, 2), 30000, dtype=np.int16)
    mixed = fx.mix_sfx(samples, 48000, [effect("punch_zoom", 1.0, 2.0, sfx="pop")])
    assert np.array_equal(mixed[:48000], samples[:48000])
    assert mixed[48000:].min() > -32768 and mixed.max() == 32767


def test_sound_near_the_end_of_the_video_is_cut_off_safely():
    samples = np.zeros((48000, 2), dtype=np.int16)
    assert len(fx.mix_sfx(samples, 48000, [effect("x", 0.95, 1.0, sfx="ding")])) == 48000


def raw_plan(**fields) -> dict:
    return {"illustrations": [], "overlays": [], "effects": [], **fields}


def options(style: str) -> dict:
    return clean_options({"fx_style": style})


def test_plan_drops_effects_the_chosen_style_does_not_allow():
    raw = raw_plan(effects=[effect("mono_tension", 1, 3), effect("shake", 4, 5)])
    kinds = [item["type"] for item in pipeline.sanitize_plan(raw, 60, options("variety"))["effects"]]
    assert kinds == ["shake"]


def test_plan_keeps_no_effects_when_turned_off():
    raw = raw_plan(effects=[effect("shake", 1, 2)])
    assert pipeline.sanitize_plan(raw, 60, options("off"))["effects"] == []


def test_plan_limits_effect_length():
    raw = raw_plan(effects=[effect("freeze", 1, 9)])
    item = pipeline.sanitize_plan(raw, 60, options("mixed"))["effects"][0]
    assert item["end"] - item["start"] <= fx.MAX_SECONDS["freeze"]


def test_plan_removes_effects_hidden_by_an_illustration_card():
    card = {"start": 10, "end": 15, "concept": "", "label": "", "logo": "", "image_prompt": "x"}
    raw = raw_plan(illustrations=[card], effects=[effect("shake", 11, 12), effect("shake", 20, 21)])
    assert [i["start"] for i in pipeline.sanitize_plan(raw, 60, options("mixed"))["effects"]] == [20]


def test_plan_drops_text_effect_without_text():
    raw = raw_plan(effects=[effect("impact_text", 1, 2, text="  ")])
    assert pipeline.sanitize_plan(raw, 60, options("mixed"))["effects"] == []


def test_caption_track_shows_the_right_line_and_nothing_between():
    items = [{"id": 1, "start": 1.0, "end": 2.0, "text": "첫 줄"}, {"id": 2, "start": 3.0, "end": 4.0, "text": "둘째 줄"}]
    track = render.CaptionTrack(items, 1280, 720)
    assert track.layer_at(0.5) is None
    first = track.layer_at(1.5)
    assert first is not None and track.layer_at(1.6) is first
    assert track.layer_at(2.5) is None
    assert track.layer_at(3.5) is not first
    assert track.layer_at(5.0) is None


def test_long_caption_fits_inside_the_screen():
    items = [{"id": 1, "start": 0.0, "end": 1.0, "text": "아주 길게 말한 자막이라도 화면 밖으로 나가면 안 되니까 두 줄로 나눠야 합니다"}]
    layer = render.CaptionTrack(items, 640, 360).layer_at(0.5)
    assert layer.x >= 0 and layer.x + layer.image.shape[1] <= 640
    assert layer.y >= 0 and layer.y + layer.image.shape[0] <= 360


def test_heavy_font_is_really_heavier_than_regular():
    from PIL import Image, ImageDraw, ImageFont

    from app.config import FONT_SANS_VF
    if not FONT_SANS_VF.exists():
        pytest.skip("이 PC에 Noto Sans KR이 없음")

    def ink(font) -> int:
        image = Image.new("L", (300, 100), 0)
        ImageDraw.Draw(image).text((10, 10), "자막", font=font, fill=255)
        return int(np.count_nonzero(np.array(image) > 128))

    light = ImageFont.truetype(str(FONT_SANS_VF), 60)
    light.set_variation_by_axes([300])
    assert ink(render._display_font(FONT_SANS_VF, 60)) > ink(light) * 1.3
