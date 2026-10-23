from enum import StrEnum


class LinkType(StrEnum):
    """Where a banner or notification leads when tapped.

    order: one of the recipient's orders (value = order id). admin_order / admin_product: the
    admin app's order / product screen (value = id); only ever sent to admins.
    Banners and admin campaigns use the catalog types only (see CATALOG_LINK_TYPES).
    """

    none = "none"
    category = "category"
    product = "product"
    brand = "brand"
    new = "new"
    sale = "sale"
    url = "url"
    order = "order"
    admin_order = "admin_order"
    admin_product = "admin_product"


CATALOG_LINK_TYPES = frozenset(
    {LinkType.none, LinkType.category, LinkType.product, LinkType.brand, LinkType.new, LinkType.sale, LinkType.url}
)
ADMIN_LINK_TYPES = frozenset({LinkType.admin_order, LinkType.admin_product})
