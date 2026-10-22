from enum import StrEnum


class LinkType(StrEnum):
    """Where a banner or notification leads when tapped."""

    none = "none"
    category = "category"
    product = "product"
    brand = "brand"
    new = "new"
    sale = "sale"
    url = "url"
