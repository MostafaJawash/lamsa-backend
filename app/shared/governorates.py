from dataclasses import dataclass


@dataclass(frozen=True)
class Governorate:
    code: str
    name: str


GOVERNORATES = (
    Governorate("damascus", "دمشق"),
    Governorate("rif_dimashq", "ريف دمشق"),
    Governorate("aleppo", "حلب"),
    Governorate("homs", "حمص"),
    Governorate("hama", "حماة"),
    Governorate("latakia", "اللاذقية"),
    Governorate("tartus", "طرطوس"),
    Governorate("idlib", "إدلب"),
    Governorate("deir_ez_zor", "دير الزور"),
    Governorate("raqqa", "الرقة"),
    Governorate("hasakah", "الحسكة"),
    Governorate("daraa", "درعا"),
    Governorate("as_suwayda", "السويداء"),
    Governorate("quneitra", "القنيطرة"),
)

GOVERNORATE_CODES = frozenset(item.code for item in GOVERNORATES)


def is_governorate(value: str) -> bool:
    return value in GOVERNORATE_CODES
