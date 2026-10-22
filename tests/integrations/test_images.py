from io import BytesIO

import pytest
from PIL import Image

from app.integrations.storage import images
from app.integrations.storage.images import IMAGE_SIZES, ImageSize, process_image
from app.shared.exceptions import ImageTooLargeError, InvalidImageError

MAX_BYTES = 5 * 1024 * 1024
EXIF_ORIENTATION = 0x0112
EXIF_DESCRIPTION = 0x010E


def make_image(fmt: str, size: tuple[int, int], mode: str = "RGB", **save_kwargs: object) -> bytes:
    color: tuple[int, ...] = (200, 30, 40, 255) if mode == "RGBA" else (200, 30, 40)
    image = Image.new(mode, size, color)
    if mode == "RGBA":
        image.putpixel((0, 0), (0, 0, 0, 0))  # one fully transparent pixel
    buffer = BytesIO()
    image.save(buffer, format=fmt, **save_kwargs)
    return buffer.getvalue()


def open_webp(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    assert image.format == "WEBP"
    return image


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_allowed_formats_produce_all_webp_sizes(fmt: str) -> None:
    result = process_image(make_image(fmt, (2000, 1000)), MAX_BYTES)

    assert set(result.variants) == set(ImageSize)
    for size, data in result.variants.items():
        with open_webp(data) as out:
            assert max(out.size) == IMAGE_SIZES[size]
    assert (result.width, result.height) == (1600, 800)


def test_tall_image_is_bounded_by_longest_side() -> None:
    result = process_image(make_image("JPEG", (1000, 4000)), MAX_BYTES)

    expected = {ImageSize.thumb: (75, 300), ImageSize.medium: (200, 800), ImageSize.large: (400, 1600)}
    for size, dims in expected.items():
        with open_webp(result.variants[size]) as out:
            assert out.size == dims
    assert (result.width, result.height) == (400, 1600)


def test_tiny_image_is_never_upscaled() -> None:
    result = process_image(make_image("PNG", (50, 40)), MAX_BYTES)

    for data in result.variants.values():
        with open_webp(data) as out:
            assert out.size == (50, 40)
    assert (result.width, result.height) == (50, 40)


def test_png_alpha_is_preserved() -> None:
    result = process_image(make_image("PNG", (400, 400), mode="RGBA"), MAX_BYTES)

    with open_webp(result.variants[ImageSize.large]) as out:
        assert out.mode == "RGBA"
        assert out.getpixel((0, 0))[3] == 0
        assert out.getpixel((200, 200))[3] == 255


def test_opaque_image_is_rgb() -> None:
    result = process_image(make_image("JPEG", (400, 400)), MAX_BYTES)

    with open_webp(result.variants[ImageSize.large]) as out:
        assert out.mode == "RGB"


def test_metadata_is_stripped_and_orientation_applied() -> None:
    exif = Image.Exif()
    exif[EXIF_ORIENTATION] = 6  # rotate 90° clockwise when displayed
    exif[EXIF_DESCRIPTION] = "secret camera info"
    data = make_image("JPEG", (400, 200), exif=exif.tobytes())

    result = process_image(data, MAX_BYTES)

    with open_webp(result.variants[ImageSize.large]) as out:
        assert out.size == (200, 400)  # orientation baked into the pixels
        assert not out.info.get("exif")
        assert not out.info.get("icc_profile")
        assert not out.info.get("xmp")
        assert len(out.getexif()) == 0


def test_non_image_is_rejected() -> None:
    with pytest.raises(InvalidImageError):
        process_image(b"this is not an image, just text", MAX_BYTES)


def test_empty_file_is_rejected() -> None:
    with pytest.raises(InvalidImageError):
        process_image(b"", MAX_BYTES)


def test_truncated_image_is_rejected() -> None:
    data = make_image("PNG", (300, 300))
    with pytest.raises(InvalidImageError):
        process_image(data[: len(data) // 2], MAX_BYTES)


def test_disallowed_format_is_rejected() -> None:
    with pytest.raises(InvalidImageError):
        process_image(make_image("GIF", (100, 100)), MAX_BYTES)


def test_file_over_byte_limit_is_rejected() -> None:
    data = make_image("PNG", (100, 100))
    with pytest.raises(ImageTooLargeError):
        process_image(data, max_bytes=len(data) - 1)


def test_image_over_pixel_limit_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "MAX_PIXELS", 100 * 100 - 1)
    with pytest.raises(ImageTooLargeError):
        process_image(make_image("PNG", (100, 100)), MAX_BYTES)
