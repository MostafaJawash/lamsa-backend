"""Pure image processing: validate an upload and produce WebP sizes. No network, no disk."""

import warnings
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.shared.exceptions import ImageTooLargeError, InvalidImageError


class ImageSize(StrEnum):
    thumb = "thumb"
    medium = "medium"
    large = "large"

    @property
    def max_side(self) -> int:
        return IMAGE_SIZES[self]


# Longest side in pixels for each stored size. Images are never upscaled.
IMAGE_SIZES: dict[ImageSize, int] = {
    ImageSize.thumb: 300,
    ImageSize.medium: 800,
    ImageSize.large: 1600,
}

ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
MAX_PIXELS = 40_000_000  # ~40 megapixels; larger images are rejected as decompression bombs
WEBP_QUALITY = 82
WEBP_METHOD = 6

# Pillow raises DecompressionBombError above 2x this and warns above 1x; the warning is
# turned into an error below, and MAX_PIXELS is also checked explicitly.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


@dataclass(frozen=True)
class ProcessedImage:
    variants: dict[ImageSize, bytes]  # WebP bytes per size
    width: int  # of the large output
    height: int


def process_image(data: bytes, max_bytes: int) -> ProcessedImage:
    """Validate an uploaded image and return WebP thumb/medium/large versions.

    The real format is detected from the bytes (the filename and client content type are
    ignored). EXIF orientation is applied, then all metadata is dropped. Alpha is kept when
    present; otherwise the image is converted to RGB.
    """
    if len(data) > max_bytes:
        raise ImageTooLargeError(f"File is {len(data)} bytes; the limit is {max_bytes} bytes")
    if not data:
        raise InvalidImageError("Empty file")

    image = _open_verified(data)
    try:
        image = ImageOps.exif_transpose(image)
        image = _normalize_mode(image)
        image.info.clear()  # drop EXIF, ICC profile, XMP, comments

        variants: dict[ImageSize, bytes] = {}
        width = height = 0
        for size in ImageSize:
            resized = image.copy()
            resized.thumbnail((size.max_side, size.max_side), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            resized.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
            variants[size] = buffer.getvalue()
            if size is ImageSize.large:
                width, height = resized.size
        return ProcessedImage(variants=variants, width=width, height=height)
    finally:
        image.close()


def _open_verified(data: bytes) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(BytesIO(data)) as probe:
                if probe.format not in ALLOWED_FORMATS:
                    raise InvalidImageError(f"Unsupported image format: {probe.format}")
                if probe.width * probe.height > MAX_PIXELS:
                    raise ImageTooLargeError(
                        f"Image is {probe.width}x{probe.height}; the limit is {MAX_PIXELS} pixels"
                    )
                probe.verify()
            # verify() leaves the image unusable, so open it again to decode.
            image = Image.open(BytesIO(data))
            image.load()
        except (ImageTooLargeError, InvalidImageError):
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageTooLargeError(str(exc)) from exc
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise InvalidImageError("File is not a valid image") from exc
    return image


def _normalize_mode(image: Image.Image) -> Image.Image:
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    target = "RGBA" if has_alpha else "RGB"
    return image if image.mode == target else image.convert(target)
