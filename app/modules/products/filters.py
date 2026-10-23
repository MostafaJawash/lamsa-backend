"""Product list filters: parsing (with whitelisting) and SQL conditions.

attr.<key> / opt.<key> params are parsed from the raw query string. Keys must exist in the
effective, filterable definitions; values are validated per type. JSON paths come from
json_path() (whitelisted keys) and every value is a bound parameter: no user input is ever
written into SQL text.

build_conditions(filters, exclude=...) is used by the list, the count and every facet, so
"each facet ignores its own filter" is implemented once.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import DECIMAL, JSON, ColumnElement, and_, bindparam, cast, exists, func, select
from sqlalchemy.dialects.mysql import match

from app.modules.brands.models import Brand
from app.modules.categories.definitions import AttributeDef, DefinitionRow, EffectiveDefinitions, VariantOptionDef
from app.modules.categories.enums import AttributeType, VariantOptionType
from app.modules.products.models import Product, ProductVariant
from app.modules.products.visibility import visible_product_condition
from app.shared.catalog import DEFINITION_KEY_PATTERN, json_path
from app.shared.exceptions import ErrorCode, ValidationFailed
from app.shared.money import MAX_MONEY

MAX_VALUES_PER_KEY = 20
MAX_FILTER_VALUE_LENGTH = 100
NUMBER = DECIMAL(12, 4)


def invalid_filter(param: str, reason: str) -> ValidationFailed:
    return ValidationFailed(ErrorCode.INVALID_FILTER, details={"param": param, "reason": reason})


@dataclass
class AttrFilter:
    definition: AttributeDef
    values: list[str] = field(default_factory=list)
    min: Decimal | None = None
    max: Decimal | None = None

    @property
    def key(self) -> str:
        return self.definition.key


@dataclass
class OptFilter:
    definition: VariantOptionDef
    values: list[str]

    @property
    def key(self) -> str:
        return self.definition.key


@dataclass
class ProductFilters:
    category_ids: list[int] | None = None
    brand_slugs: list[str] = field(default_factory=list)
    search: str | None = None  # boolean-mode query, already built and safe
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    in_stock: bool = False
    on_sale: bool = False
    is_new: bool = False
    attrs: list[AttrFilter] = field(default_factory=list)
    opts: list[OptFilter] = field(default_factory=list)


@dataclass(frozen=True)
class FilterDefinitions:
    """Definitions usable as filters: the selected category's effective ones, or (no category)
    every category's filterable ones, merged by key when their types agree."""

    attributes: dict[str, AttributeDef]
    options: dict[str, VariantOptionDef]
    conflicting: frozenset[str] = frozenset()


def definitions_for_category(definitions: EffectiveDefinitions) -> FilterDefinitions:
    return FilterDefinitions(
        attributes={d.key: d for d in definitions.filterable_attributes}, options=dict(definitions.variant_options)
    )


def definitions_for_all(rows: Sequence[DefinitionRow]) -> FilterDefinitions:
    """Without a category: a key is usable only if all its filterable definitions share one type.
    Options lists are merged so any category's values are accepted."""
    attributes: dict[str, AttributeDef] = {}
    options: dict[str, VariantOptionDef] = {}
    conflicting: set[str] = set()
    for row in rows:
        if row.kind == "attribute" and not row.is_filterable:
            continue
        target: dict[str, Any] = attributes if row.kind == "attribute" else options
        existing = target.get(row.key)
        merged_options = sorted(set((existing.options if existing else None) or []) | set(row.options or [])) or None
        if existing is not None and existing.type.value != row.type:
            conflicting.add(f"{row.kind}:{row.key}")
            continue
        if row.kind == "attribute":
            attributes[row.key] = AttributeDef(
                id=row.id, category_id=row.category_id, key=row.key, label=row.label, type=AttributeType(row.type),
                options=merged_options, unit=row.unit, is_required=False, is_filterable=True, sort_order=row.sort_order,
            )
        else:
            options[row.key] = VariantOptionDef(
                id=row.id, category_id=row.category_id, key=row.key, label=row.label,
                type=VariantOptionType(row.type), options=merged_options, sort_order=row.sort_order,
            )
    return FilterDefinitions(attributes=attributes, options=options, conflicting=frozenset(conflicting))


def has_definition_params(params: Sequence[tuple[str, str]]) -> bool:
    return any(name.startswith(("attr.", "opt.")) for name, _ in params)


def parse_definition_filters(
    params: Sequence[tuple[str, str]], definitions: FilterDefinitions
) -> tuple[list[AttrFilter], list[OptFilter]]:
    attrs: dict[str, AttrFilter] = {}
    opts: dict[str, OptFilter] = {}
    for name, raw_value in params:
        if not name.startswith(("attr.", "opt.")):
            continue
        value = raw_value.strip()
        if not value or len(value) > MAX_FILTER_VALUE_LENGTH:
            raise invalid_filter(name, "empty or too long value")
        prefix, _, rest = name.partition(".")
        key, _, bound = rest.partition(".")
        if not DEFINITION_KEY_PATTERN.match(key):
            raise invalid_filter(name, "invalid key")
        if prefix == "attr":
            _add_attr(attrs, definitions, name, key, bound, value)
        else:
            if bound:
                raise invalid_filter(name, "option filters take values only")
            _add_opt(opts, definitions, name, key, value)
    for f in [*attrs.values(), *opts.values()]:
        if len(f.values) > MAX_VALUES_PER_KEY:
            raise invalid_filter(f"{f.key}", "too many values")
    return list(attrs.values()), list(opts.values())


def _add_attr(
    attrs: dict[str, AttrFilter], definitions: FilterDefinitions, name: str, key: str, bound: str, value: str
) -> None:
    if f"attribute:{key}" in definitions.conflicting:
        raise invalid_filter(name, "this attribute differs between categories; choose a category first")
    definition = definitions.attributes.get(key)
    if definition is None:
        raise invalid_filter(name, "unknown or non-filterable attribute")
    current = attrs.setdefault(key, AttrFilter(definition))
    kind = definition.type
    if bound:
        if bound not in ("min", "max") or kind != AttributeType.number:
            raise invalid_filter(name, "min/max only apply to number attributes")
        number = _number(name, value)
        setattr(current, bound, number)
        if current.min is not None and current.max is not None and current.min > current.max:
            raise invalid_filter(name, "min is greater than max")
        return
    if kind == AttributeType.number:
        number = _number(name, value)
        current.min = current.max = number
    elif kind == AttributeType.boolean:
        if value not in ("true", "false"):
            raise invalid_filter(name, "expected true or false")
        current.values.append(value)
    elif kind in (AttributeType.select, AttributeType.multiselect):
        if value not in (definition.options or []):
            raise invalid_filter(name, "not one of the allowed values")
        current.values.append(value)
    elif kind == AttributeType.date:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise invalid_filter(name, "expected a date YYYY-MM-DD") from exc
        current.values.append(value)
    else:
        current.values.append(value)


def _add_opt(opts: dict[str, OptFilter], definitions: FilterDefinitions, name: str, key: str, value: str) -> None:
    if f"option:{key}" in definitions.conflicting:
        raise invalid_filter(name, "this option differs between categories; choose a category first")
    definition = definitions.options.get(key)
    if definition is None:
        raise invalid_filter(name, "unknown variant option")
    if definition.type == VariantOptionType.select and definition.options and value not in definition.options:
        raise invalid_filter(name, "not one of the allowed values")
    opts.setdefault(key, OptFilter(definition, [])).values.append(value)


def _number(name: str, value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise invalid_filter(name, "expected a number") from exc
    if not number.is_finite() or abs(number) > Decimal("99999999"):
        raise invalid_filter(name, "expected a number")
    return number


def validate_price_range(min_price: Decimal | None, max_price: Decimal | None) -> None:
    for name, value in (("min_price", min_price), ("max_price", max_price)):
        if value is not None and (value < 0 or value > MAX_MONEY):
            raise invalid_filter(name, "out of range")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise invalid_filter("min_price", "min_price is greater than max_price")


# ------------------------------------------------------------------------- conditions


def search_condition(boolean_query: str) -> ColumnElement[Any]:
    return match(Product.search_text, against=boolean_query).in_boolean_mode()


def attribute_condition(f: AttrFilter) -> ColumnElement[bool]:
    path = json_path(f.key)
    extracted = func.json_extract(Product.attributes, path)
    has_key = func.json_contains_path(Product.attributes, "one", path) == 1
    kind = f.definition.type
    if kind == AttributeType.number:
        number = cast(extracted, NUMBER)
        conditions = [has_key]
        if f.min is not None:
            conditions.append(number >= f.min)
        if f.max is not None:
            conditions.append(number <= f.max)
        return and_(*conditions)
    if kind == AttributeType.multiselect:
        # The list is bound once as JSON text, then parsed by CAST(... AS JSON) in MySQL.
        values = cast(bindparam(None, list(f.values), type_=JSON), JSON)
        return and_(has_key, func.json_overlaps(extracted, values) == 1)
    return and_(has_key, func.json_unquote(extracted).in_(f.values))


def option_condition(f: OptFilter) -> ColumnElement[bool]:
    return exists(
        select(ProductVariant.id).where(
            ProductVariant.product_id == Product.id,
            ProductVariant.is_active.is_(True),
            func.json_unquote(func.json_extract(ProductVariant.options, json_path(f.key))).in_(f.values),
        )
    )


def build_conditions(filters: ProductFilters, *, exclude: str | None = None) -> list[ColumnElement[Any]]:
    """Visibility + every filter, except the one named by `exclude`
    ("brand", "price", "attr:<key>", "opt:<key>")."""
    conditions: list[ColumnElement[Any]] = [visible_product_condition()]
    if filters.category_ids is not None:
        conditions.append(Product.category_id.in_(filters.category_ids))
    if filters.search:
        conditions.append(search_condition(filters.search))
    if filters.brand_slugs and exclude != "brand":
        conditions.append(Product.brand_id.in_(select(Brand.id).where(Brand.slug.in_(filters.brand_slugs))))
    if exclude != "price":
        if filters.min_price is not None:
            conditions.append(Product.price_from >= filters.min_price)
        if filters.max_price is not None:
            conditions.append(Product.price_from <= filters.max_price)
    if filters.in_stock:
        conditions.append(Product.in_stock.is_(True))
    if filters.on_sale:
        conditions.append(Product.is_on_sale.is_(True))
    if filters.is_new:
        conditions.append(Product.is_new.is_(True))
    for attr in filters.attrs:
        if exclude != f"attr:{attr.key}":
            conditions.append(attribute_condition(attr))
    for opt in filters.opts:
        if exclude != f"opt:{opt.key}":
            conditions.append(option_condition(opt))
    return conditions


def query_params(items: Mapping[str, str] | Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(items.items()) if isinstance(items, Mapping) else list(items)

