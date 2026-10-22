from enum import StrEnum


class RevokeReason(StrEnum):
    """Why a refresh token was revoked. Only `rotated` qualifies for the reuse grace period."""

    rotated = "rotated"
    logout = "logout"
    logout_all = "logout_all"
    password_change = "password_change"
    admin_reset = "admin_reset"
    deactivated = "deactivated"
    reuse_detected = "reuse_detected"
