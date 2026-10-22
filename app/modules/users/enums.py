from enum import StrEnum


class AuthProvider(StrEnum):
    phone = "phone"
    google = "google"


class UserRole(StrEnum):
    customer = "customer"
    admin = "admin"


class DevicePlatform(StrEnum):
    android = "android"
    ios = "ios"
    web = "web"
