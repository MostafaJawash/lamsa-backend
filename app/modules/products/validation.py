"""Validation of product attribute values and variant options against effective definitions."""

import re
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.modules.categories.definitions import AttributeDef, EffectiveDefinitions
from app.modules.categories.enums import AttributeType, VariantOptionType
from app.modules.products.utils import build_options_signature
from app.shared.exceptions import Conflict, ErrorCode, ValidationFailed

TEXT_MAX_LENGTH = 1000
OPTION_VALUE_MAX_LENGTH = 50
HEX_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
EXPECTED: dict[AttributeType, str] = {
    AttributeType.text: f"string of at most {TEXT_MAX_LENGTH} characters",
    AttributeType.number: "number",
    AttributeType.select: "one of the options",
    AttributeType.multiselect: "non-empty list of unique options",
    AttributeType.boolean: "true or false",
    AttributeType.date: "date YYYY-MM-DD",
}


def _invalid(key: str, expected: str) -> ValidationFailed:
    return ValidationFailed(ErrorCode.INVALID_ATTRIBUTE_VALUE, details={"key": key, "expected": expected})


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def to_json_number(value: Decimal) -> int | float:
    """JSON cannot store Decimal: whole numbers become int, others float."""
    return int(value) if value == value.to_integral_value() else float(value)


def validate_attribute_value(definition: AttributeDef, value: Any) -> Any:
    """Return the cleaned value to store, or raise INVALID_ATTRIBUTE_VALUE."""
    key, kind = definition.key, definition.type
    expected = EXPECTED[kind]
    if kind == AttributeType.text:
        if not isinstance(value, str) or len(value.strip()) > TEXT_MAX_LENGTH:
            raise _invalid(key, expected)
        return value.strip()
    if kind == AttributeType.number:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise _invalid(key, expected)
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise _invalid(key, expected) from exc
        if not number.is_finite():
            raise _invalid(key, expected)
        return to_json_number(number)
    if kind == AttributeType.select:
        if not isinstance(value, str) or value not in (definition.options or []):
            raise _invalid(key, expected)
        return value
    if kind == AttributeType.multiselect:
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, str) for v in value)
            or len(set(value)) != len(value)
            or not set(value) <= set(definition.options or [])
        ):
            raise _invalid(key, expected)
        return value
    if kind == AttributeType.boolean:
        if not isinstance(value, bool):
            raise _invalid(key, expected)
        return value
    # date
    try:
        if not isinstance(value, str):
            raise ValueError
        date.fromisoformat(value)
        if len(value) != 10:
            raise ValueError
    except ValueError as exc:
        raise _invalid(key, expected) from exc
    return value


def validate_attributes(definitions: EffectiveDefinitions, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Clean a product's attributes: drop null/empty values, check keys, types and required keys."""
    cleaned: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        definition = definitions.attributes.get(key)
        if definition is None:
            raise ValidationFailed(ErrorCode.UNKNOWN_ATTRIBUTE, details={"key": key})
        stored = validate_attribute_value(definition, value)
        if not is_empty(stored):
            cleaned[key] = stored
    missing = [key for key in definitions.required_keys if is_empty(cleaned.get(key))]
    if missing:
        raise ValidationFailed(ErrorCode.MISSING_REQUIRED_ATTRIBUTE, details={"keys": missing})
    return cleaned


def missing_required(definitions: EffectiveDefinitions, attributes: Mapping[str, Any]) -> list[str]:
    return [key for key in definitions.required_keys if is_empty(attributes.get(key))]


def _invalid_options(reason: str, **extra: Any) -> ValidationFailed:
    return ValidationFailed(ErrorCode.INVALID_VARIANT_OPTIONS, details={"reason": reason, **extra})


def validate_variant_options(definitions: EffectiveDefinitions, options: Mapping[str, Any]) -> dict[str, str]:
    """Clean one variant's options. Color options carry "<key>_hex" (#RRGGBB, stored uppercase)."""
    if not isinstance(options, Mapping) or not options:
        raise _invalid_options("options must be a non-empty object")
    defs = definitions.variant_options
    hex_keys = {d.hex_key: d for d in defs.values() if d.type == VariantOptionType.color}
    cleaned: dict[str, str] = {}
    for key, value in options.items():
        if key in hex_keys:
            continue
        definition = defs.get(key)
        if definition is None:
            raise _invalid_options("unknown option key", key=key, allowed=sorted(defs))
        if not isinstance(value, str) or not (1 <= len(value.strip()) <= OPTION_VALUE_MAX_LENGTH):
            raise _invalid_options(f"value must be a string of 1..{OPTION_VALUE_MAX_LENGTH} characters", key=key)
        value = value.strip()
        if definition.type == VariantOptionType.select and definition.options and value not in definition.options:
            raise _invalid_options("value is not one of the options", key=key, allowed=definition.options)
        cleaned[key] = value
        if definition.type == VariantOptionType.color:
            hex_value = options.get(definition.hex_key)
            if not isinstance(hex_value, str) or not HEX_PATTERN.match(hex_value):
                raise _invalid_options("color needs a hex value #RRGGBB", key=definition.hex_key)
            cleaned[definition.hex_key] = hex_value.upper()
    for hex_key, definition in hex_keys.items():
        if hex_key in options and definition.key not in options:
            raise _invalid_options("hex value without its color", key=hex_key)
    return cleaned


def option_keys(options: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(k for k in options if not k.endswith("_hex"))


def validate_variant_set(all_options: Sequence[Mapping[str, Any]]) -> None:
    """All variants of a product use the same option keys, and no two have the same options."""
    if not all_options:
        return
    key_sets = {option_keys(o) for o in all_options}
    if len(key_sets) > 1:
        raise _invalid_options("all variants must use the same option keys", key_sets=[sorted(k) for k in key_sets])
    signatures = [build_options_signature(dict(o)) for o in all_options]
    duplicates = {s for s in signatures if signatures.count(s) > 1}
    if duplicates:
        raise Conflict(ErrorCode.DUPLICATE_VARIANT, details={"options_signature": sorted(duplicates)[0]})


def variant_label(options: Mapping[str, Any]) -> str:
    """Human label of a variant: option values (without hex) joined with " / "."""
    return " / ".join(str(v) for k, v in options.items() if not k.endswith("_hex"))
