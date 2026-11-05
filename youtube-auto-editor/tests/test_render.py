"""합성 결과가 맞는지 확인한다."""
import numpy as np
from PIL import Image

from app import render


def blank(value: int = 100) -> np.ndarray:
    return np.full((200, 300, 3), value, np.uint8)


def layer_of(alpha: int, color=(10, 20, 30), size=(50, 40), **kwargs) -> render.Layer:
    image = np.zeros((*size, 4), np.uint8)
    image[..., :3] = color
    image[..., 3] = alpha
    return render.Layer(start=0.0, end=10.0, image=image, x=kwargs.pop("x", 20), y=kwargs.pop("y", 30), **kwargs)


def test_opaque_layer_replaces_pixels_and_leaves_the_rest():
    frame = blank()
    render._draw_layer(frame, layer_of(255), 5.0)
    assert frame[50, 40].tolist() == [10, 20, 30]
    assert frame[0, 0].tolist() == [100, 100, 100]


def test_half_transparent_layer_mixes():
    frame = blank()
    render._draw_layer(frame, layer_of(128), 5.0)
    assert abs(int(frame[50, 40, 0]) - 55) <= 2


def test_fully_transparent_area_changes_nothing():
    frame = blank()
    render._draw_layer(frame, layer_of(0), 5.0)
    assert np.array_equal(frame, blank())


def test_layer_partly_outside_the_frame_is_clipped():
    frame = blank()
    render._draw_layer(frame, layer_of(255, x=280, y=180), 5.0)
    assert frame[199, 299].tolist() == [10, 20, 30]


def test_dim_darkens_background():
    frame = blank()
    render._draw_layer(frame, layer_of(255, dim=True), 5.0)
    assert abs(int(frame[0, 0, 0]) - 100 * render.DIM) <= 1


def test_pop_in_starts_transparent():
    frame = blank()
    render._draw_layer(frame, layer_of(255), 0.0)
    assert np.array_equal(frame, blank())


def test_pixelate_destroys_detail_only_inside_rect():
    frame = np.random.default_rng(0).integers(0, 255, (200, 300, 3), dtype=np.uint8)
    before = frame.copy()
    render.pixelate(frame, (50, 50, 200, 110))
    inside = frame[50:110, 50:200]
    assert len(np.unique(inside.reshape(-1, 3), axis=0)) < 40
    assert np.array_equal(frame[:50], before[:50])


def test_pixelate_handles_rect_outside_the_frame():
    frame = blank()
    render.pixelate(frame, (-50, -50, -10, -10))
    render.pixelate(frame, (290, 190, 400, 400))
    assert frame.shape == (200, 300, 3)


def test_card_contains_art_and_stays_inside_limits():
    art = Image.new("RGBA", (600, 400), (240, 100, 80, 255))
    card = render.build_card(art, "무음 자르기", 500, 400, 8)
    assert card.width <= 500 and card.height <= 400
    assert card.getpixel((card.width // 2, card.height // 3))[:3] == (240, 100, 80)
