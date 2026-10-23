"""THE link resolver, shared by banners and notifications.

- Saving (admin): catalog links only (none, category, product, brand, new, sale, url).
  category/product/brand need an existing slug; url must be https and <= 255 chars; none/new/sale
  take no value. Anything else: 422 INVALID_LINK.
- Recipients: admin_order / admin_product only for admins; an order link only to the order's
  owner. Customer-facing payloads therefore never contain admin_* links.
- Reading (public): banners and notifications check the target's visibility in ONE query.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.brands.models import Brand
from app.modules.categories.models import Category
from app.modules.products.models import Product
from app.modules.products.visibility import visible_category_condition, visible_product_condition
from app.shared.enums import ADMIN_LINK_TYPES, CATALOG_LINK_TYPES, LinkType
from app.shared.exceptions import ErrorCode, ValidationFailed

URL_MAX = 255
SLUG_LINKS = {LinkType.category: Category, LinkType.product: Product, LinkType.brand: Brand}
VALUELESS = {LinkType.none, LinkType.new, LinkType.sale}


@dataclass(frozen=True)
class Link:
    type: LinkType
    value: str | None


def invalid_link(reason: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(ErrorCode.INVALID_LINK, details={"reason": reason, **details})


async def validate_catalog_link(session: AsyncSession, link_type: LinkType, value: str | None) -> Link:
    """For banners and admin campaigns (one query at most)."""
    value = value.strip() if isinstance(value, str) and value.strip() else None
    if link_type not in CATALOG_LINK_TYPES:
        raise invalid_link("this link type is not allowed here", link_type=link_type.value)
    if link_type in VALUELESS:
        if value is not None:
            raise invalid_link("this link type takes no value", link_type=link_type.value)
        return Link(link_type, None)
    if value is None:
        raise invalid_link("a value is required", link_type=link_type.value)
    if link_type == LinkType.url:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or len(value) > URL_MAX:
            raise invalid_link(f"url must be https and at most {URL_MAX} characters", link_type="url")
        return Link(link_type, value)
    model = SLUG_LINKS[link_type]
    if await session.scalar(select(model.id).where(model.slug == value).limit(1)) is None:
        raise invalid_link("no such slug", link_type=link_type.value, value=value)
    return Link(link_type, value)


def check_recipient_link(link: Link, *, recipient_is_admin: bool, order_owner_id: int | None, recipient_id: int | None) -> None:
    """Guard for per-user rows; a violation is a programming error, not user input."""
    if link.type in ADMIN_LINK_TYPES and not recipient_is_admin:
        raise ValueError(f"{link.type} links are only for admins")
    if link.type == LinkType.order and (order_owner_id is None or order_owner_id != recipient_id):
        raise ValueError("an order link must point to an order of the recipient")


async def visible_link_targets(session: AsyncSession, links: Iterable[Link]) -> set[tuple[LinkType, str]]:
    """Which (type, slug) catalog targets are visible: ONE statement (UNION ALL)."""
    wanted: dict[LinkType, set[str]] = {}
    for link in links:
        if link.type in SLUG_LINKS and link.value:
            wanted.setdefault(link.type, set()).add(link.value)
    if not wanted:
        return set()
    parts: list[Any] = []
    if LinkType.product in wanted:
        parts.append(select(literal("product").label("link_kind"), Product.slug.label("slug"))
                     .where(Product.slug.in_(wanted[LinkType.product]), visible_product_condition()))
    if LinkType.category in wanted:
        parts.append(select(literal("category").label("link_kind"), Category.slug.label("slug"))
                     .where(Category.slug.in_(wanted[LinkType.category]), visible_category_condition(Category)))
    if LinkType.brand in wanted:
        parts.append(select(literal("brand").label("link_kind"), Brand.slug.label("slug"))
                     .where(Brand.slug.in_(wanted[LinkType.brand]), Brand.is_active.is_(True)))
    rows = await session.execute(union_all(*parts) if len(parts) > 1 else parts[0])
    return {(LinkType(kind), slug) for kind, slug in rows.all()}


def is_visible(link: Link, visible: set[tuple[LinkType, str]]) -> bool:
    if link.type in SLUG_LINKS:
        return link.value is not None and (link.type, link.value) in visible
    return True


__all__: Sequence[str] = ["Link", "check_recipient_link", "is_visible", "validate_catalog_link", "visible_link_targets"]
