from enum import StrEnum


class AttributeType(StrEnum):
    text = "text"
    number = "number"
    select = "select"
    multiselect = "multiselect"
    boolean = "boolean"
    date = "date"


class VariantOptionType(StrEnum):
    color = "color"  # name + hex
    select = "select"
