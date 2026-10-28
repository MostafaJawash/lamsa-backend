"""Pure helpers that turn loaded rows into API shapes (no queries)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.modules.brands.schemas import BrandRef
from app.modules.categories.definitions import AttributeDef, EffectiveDefinitions
from app.modules.categories.enums import AttributeType
from app.modules.categories.schemas import CategoryRef
from app.modules.products.enums import ProductType
from app.modules.products.schemas import (
    AttributeValueOut,
    OptionValueOut,
    ProductCardOut,
    StockStatus,
    Swatch,
    VariantOptionOut,
)
from app.modules.products.summary import VariantRow, pick_variant_for_price
from app.shared.money import discount_percent
from app.shared.schemas import image_urls
from app.shared.enums import Currency

MAX_SWATCHES = 8
LIST_SEPARATOR = "، "


# --------------------------------------------------------------------------------- stock


def stock_status(stock: int, threshold: int) -> tuple[StockStatus, int | None]:
    """in_stock / low (1..threshold, with the exact count) / out. Exact stock is never shown otherwise."""
    if stock <= 0:
        return StockStatus.out, None
    if stock <= threshold:
        return StockStatus.low, stock
    return StockStatus.in_stock, None


# ------------------------------------------------------------------------------ variants


@dataclass(frozen=True)
class LoadedVariant:
    id: int
    product_id: int
    options: dict[str, str]
    price: Decimal
    sale_price: Decimal | None
    stock: int
    is_active: bool
    sort_order: int

    @property
    def current(self) -> Decimal:
        return self.sale_price if self.sale_price is not None else self.price

    def row(self) -> VariantRow:
        return VariantRow(self.id, self.price, self.sale_price, self.stock, self.is_active, self.sort_order)


def color_key_of(options: Mapping[str, Any]) -> str | None:
    """A color option is recognised by its "<key>_hex" companion."""
    return next((k for k in options if not k.endswith("_hex") and f"{k}_hex" in options), None)


def swatches(variants: Sequence[LoadedVariant], *, in_stock_only: bool = False) -> list[Swatch]:
    """Card color dots: active variants (with in_stock_only, only those with stock > 0)."""
    result: list[Swatch] = []
    seen: set[str] = set()
    shown = (v for v in variants if v.is_active and (not in_stock_only or v.stock > 0))
    for v in sorted(shown, key=lambda v: (v.sort_order, v.id)):
        key = color_key_of(v.options)
        if key is None:
            continue
        name = v.options[key]
        if name not in seen:
            seen.add(name)
            result.append(Swatch(name=name, hex=v.options[f"{key}_hex"]))
        if len(result) == MAX_SWATCHES:
            break
    return result


@dataclass(frozen=True)
class PriceInfo:
    """THE pricing rule: what the customer pays, the regular price, and the crossed-out price."""

    current: Decimal
    regular: Decimal
    compare_at: Decimal | None  # the regular price, when current is a sale price
    discount_percent: int | None


def price_info(price: Decimal | None, sale_price: Decimal | None) -> PriceInfo:
    regular = price if price is not None else Decimal("0.00")
    if sale_price is None:
        return PriceInfo(current=regular, regular=regular, compare_at=None, discount_percent=None)
    return PriceInfo(
        current=sale_price, regular=regular, compare_at=regular, discount_percent=discount_percent(regular, sale_price)
    )


def compare_at(
    product_type: ProductType, price: Decimal | None, sale_price: Decimal | None, variants: Sequence[LoadedVariant]
) -> Decimal | None:
    """Regular price that matches the shown (lowest) price when that price is a sale price."""
    if product_type == ProductType.variable:
        chosen = pick_variant_for_price([v.row() for v in variants])
        return price_info(chosen.price, chosen.sale_price).compare_at if chosen is not None else None
    return price_info(price, sale_price).compare_at


@dataclass(frozen=True)
class CardRow:
    id: int
    slug: str
    name: str
    type: ProductType
    currency: Currency
    price: Decimal | None
    sale_price: Decimal | None
    price_from: Decimal
    is_new: bool
    is_on_sale: bool
    in_stock: bool
    brand: BrandRef | None
    category: CategoryRef


def build_card(
    row: CardRow, image_key: str | None, variants: Sequence[LoadedVariant], *, in_stock_swatches: bool = False
) -> ProductCardOut:
    compare = compare_at(row.type, row.price, row.sale_price, variants)
    return ProductCardOut(
        id=row.id, slug=row.slug, name=row.name, type=row.type, currency=row.currency,
        brand=row.brand, category=row.category,
        image=image_urls(image_key), price=row.price_from, compare_at_price=compare,
        discount_percent=discount_percent(compare, row.price_from) if compare is not None else None,
        is_new=row.is_new, is_on_sale=row.is_on_sale, in_stock=row.in_stock,
        swatches=swatches(variants, in_stock_only=in_stock_swatches) if row.type == ProductType.variable else [],
    )


# -------------------------------------------------------------------------------- images


@dataclass(frozen=True)
class LoadedImage:
    id: int
    variant_id: int | None
    image_key: str
    alt_text: str | None
    width: int
    height: int
    sort_order: int


# Image color rules, in one place. `color_key` is the key of the category's color option
# (e.g. "color"), or None when there is none.


def image_colors(
    images: Sequence[LoadedImage], variants: Sequence[LoadedVariant], color_key: str | None
) -> dict[int, str | None]:
    """Color value each image belongs to (via its variant), or None for general images."""
    if color_key is None:
        return {i.id: None for i in images}
    by_id = {v.id: v for v in variants}
    result: dict[int, str | None] = {}
    for image in images:
        variant = by_id.get(image.variant_id) if image.variant_id is not None else None
        result[image.id] = variant.options.get(color_key) if variant is not None else None
    return result


def color_image_ids(
    variant: LoadedVariant,
    images: Sequence[LoadedImage],
    colors: Mapping[int, str | None],
    color_key: str | None,
) -> list[int]:
    """Images of the variant's color, in sort_order (an image linked to one variant serves every
    variant of that color). Without a color option: images linked to this exact variant."""
    ordered = sorted(images, key=lambda i: (i.sort_order, i.id))
    if color_key is not None:
        own_color = variant.options.get(color_key)
        return [i.id for i in ordered if own_color is not None and colors.get(i.id) == own_color]
    return [i.id for i in ordered if i.variant_id == variant.id]


def variant_image_ids(
    variant: LoadedVariant,
    images: Sequence[LoadedImage],
    colors: Mapping[int, str | None],
    color_key: str | None,
) -> list[int]:
    """The variant's color images in sort_order, followed by the general images."""
    specific = color_image_ids(variant, images, colors, color_key)
    general = [i.id for i in sorted(images, key=lambda i: (i.sort_order, i.id)) if i.variant_id is None]
    return specific + [i for i in general if i not in specific]


def main_image(images: Sequence[LoadedImage]) -> LoadedImage | None:
    """The product's main image: lowest sort_order."""
    return min(images, key=lambda i: (i.sort_order, i.id), default=None)


def colors_without_images(
    images: Sequence[LoadedImage], variants: Sequence[LoadedVariant], color_key: str | None
) -> list[str]:
    if color_key is None:
        return []
    colors = image_colors(images, variants, color_key)
    with_images = {c for c in colors.values() if c is not None}
    active_colors: list[str] = []
    for v in sorted((v for v in variants if v.is_active), key=lambda v: (v.sort_order, v.id)):
        value = v.options.get(color_key)
        if value is not None and value not in active_colors:
            active_colors.append(value)
    return [c for c in active_colors if c not in with_images]


# ---------------------------------------------------------------------------- attributes


def display_value(definition: AttributeDef, value: Any) -> str:
    if definition.type == AttributeType.multiselect and isinstance(value, list):
        return LIST_SEPARATOR.join(str(v) for v in value)
    if definition.type == AttributeType.boolean:
        return "نعم" if value else "لا"
    text = format_number(value) if definition.type == AttributeType.number else str(value)
    return f"{text} {definition.unit}" if definition.unit else text


def format_number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def attribute_values(definitions: EffectiveDefinitions, attributes: Mapping[str, Any]) -> list[AttributeValueOut]:
    return [
        AttributeValueOut(
            key=d.key, label=d.label, type=d.type, unit=d.unit, value=attributes[d.key],
            display=display_value(d, attributes[d.key]),
        )
        for d in definitions.attributes.values()
        if d.key in attributes
    ]


def variant_option_values(definitions: EffectiveDefinitions, active: Sequence[LoadedVariant]) -> list[VariantOptionOut]:
    """Option axes limited to values present in active variants (in variant order)."""
    result: list[VariantOptionOut] = []
    ordered = sorted(active, key=lambda v: (v.sort_order, v.id))
    for definition in definitions.variant_options.values():
        values: list[OptionValueOut] = []
        seen: set[str] = set()
        for v in ordered:
            value = v.options.get(definition.key)
            if value is None or value in seen:
                continue
            seen.add(value)
            values.append(OptionValueOut(value=value, hex=v.options.get(definition.hex_key)))
        if values:
            result.append(VariantOptionOut(key=definition.key, label=definition.label, type=definition.type, values=values))
    return result
