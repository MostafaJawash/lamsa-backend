class ImageError(Exception):
    """Base class for image upload errors."""


class InvalidImageError(ImageError):
    """The data is not a readable JPEG, PNG or WebP image."""


class ImageTooLargeError(ImageError):
    """The file exceeds the byte limit or the image exceeds the pixel limit."""
