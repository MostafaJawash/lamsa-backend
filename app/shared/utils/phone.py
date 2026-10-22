import phonenumbers

from app.shared.exceptions import ErrorCode, ValidationFailed

MAX_RAW_PHONE_LENGTH = 32


def normalize_phone(raw: str, region: str | None = None) -> str:
    """Return the phone number in E.164 (e.g. +4915123456789) or raise INVALID_PHONE.

    Accepts local formats for the default region (e.g. 0151 2345 6789 in DE) and international
    formats (+49..., 0049...).
    """
    if region is None:
        from app.core.config import settings

        region = settings.DEFAULT_PHONE_REGION

    candidate = (raw or "").strip()
    if not candidate or len(candidate) > MAX_RAW_PHONE_LENGTH:
        raise _invalid()
    if candidate.startswith("00"):
        candidate = "+" + candidate[2:]
    try:
        number = phonenumbers.parse(candidate, region)
    except phonenumbers.NumberParseException as exc:
        raise _invalid() from exc
    if not phonenumbers.is_valid_number(number):
        raise _invalid()
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _invalid() -> ValidationFailed:
    return ValidationFailed(
        ErrorCode.INVALID_PHONE,
        details=[{"field": "phone", "message": "invalid phone number"}],
    )
