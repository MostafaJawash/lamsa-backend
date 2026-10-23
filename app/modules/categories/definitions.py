"""Effective attribute and variant-option definitions of a category.

A child category inherits its parent's definitions; when both define the same key, the
child's definition wins. `merge_definitions` is the ONLY place this rule is implemented:
product validation, product detail, filters and facets all use it.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import Integer, String, Text, cast, literal, null, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.categories.enums import AttributeType, VariantOptionType
from app.modules.categories.models import CategoryAttribute, CategoryVariantOption

DefinitionKind = Literal["attribute", "option"]


@dataclass(frozen=True)
class AttributeDef:
    id: int
    category_id: int
    key: str
    label: str
    type: AttributeType
    options: list[str] | None
    unit: str | None
    is_required: bool
    is_filterable: bool
    sort_order: int
    inherited: bool = False


@dataclass(frozen=True)
class VariantOptionDef:
    id: int
    category_id: int
    key: str
    label: str
    type: VariantOptionType
    options: list[str] | None
    sort_order: int
    inherited: bool = False

    @property
    def hex_key(self) -> str:
        return f"{self.key}_hex"


@dataclass(frozen=True)
class EffectiveDefinitions:
    """Ordered: inherited (parent) definitions first, then the category's own, each by sort_order."""

    attributes: dict[str, AttributeDef] = field(default_factory=dict)
    variant_options: dict[str, VariantOptionDef] = field(default_factory=dict)

    @property
    def filterable_attributes(self) -> list[AttributeDef]:
        return [d for d in self.attributes.values() if d.is_filterable]

    @property
    def required_keys(self) -> list[str]:
        return [d.key for d in self.attributes.values() if d.is_required]

    @property
    def color_option(self) -> VariantOptionDef | None:
        return next((d for d in self.variant_options.values() if d.type == VariantOptionType.color), None)


@dataclass(frozen=True)
class DefinitionRow:
    kind: DefinitionKind
    id: int
    category_id: int
    key: str
    label: str
    type: str
    options: list[str] | None
    unit: str | None
    is_required: bool
    is_filterable: bool
    sort_order: int


def merge_definitions(
    category_id: int, parent_id: int | None, rows: Iterable[DefinitionRow]
) -> EffectiveDefinitions:
    """Merge parent and own definitions; the child's definition wins on the same key."""
    own = [r for r in rows if r.category_id == category_id]
    inherited = [r for r in rows if parent_id is not None and r.category_id == parent_id]
    own_keys = {(r.kind, r.key) for r in own}

    ordered = sorted(
        (r for r in inherited if (r.kind, r.key) not in own_keys), key=lambda r: (r.sort_order, r.id)
    ) + sorted(own, key=lambda r: (r.sort_order, r.id))

    attributes: dict[str, AttributeDef] = {}
    options: dict[str, VariantOptionDef] = {}
    for r in ordered:
        is_inherited = r.category_id != category_id
        if r.kind == "attribute":
            attributes[r.key] = AttributeDef(
                id=r.id, category_id=r.category_id, key=r.key, label=r.label, type=AttributeType(r.type),
                options=r.options, unit=r.unit, is_required=r.is_required, is_filterable=r.is_filterable,
                sort_order=r.sort_order, inherited=is_inherited,
            )
        else:
            options[r.key] = VariantOptionDef(
                id=r.id, category_id=r.category_id, key=r.key, label=r.label, type=VariantOptionType(r.type),
                options=r.options, sort_order=r.sort_order, inherited=is_inherited,
            )
    return EffectiveDefinitions(attributes=attributes, variant_options=options)


async def load_definition_rows(session: AsyncSession, category_ids: Sequence[int] | None) -> list[DefinitionRow]:
    """Attribute and variant-option definitions for the given categories (all if None), in ONE query."""
    attrs = select(
        literal("attribute").label("kind"),
        CategoryAttribute.id,
        CategoryAttribute.category_id,
        CategoryAttribute.key,
        CategoryAttribute.label,
        cast(CategoryAttribute.type, String(30)).label("type"),
        cast(CategoryAttribute.options, Text).label("options"),
        CategoryAttribute.unit,
        cast(CategoryAttribute.is_required, Integer).label("is_required"),
        cast(CategoryAttribute.is_filterable, Integer).label("is_filterable"),
        CategoryAttribute.sort_order,
    )
    opts = select(
        literal("option").label("kind"),
        CategoryVariantOption.id,
        CategoryVariantOption.category_id,
        CategoryVariantOption.key,
        CategoryVariantOption.label,
        cast(CategoryVariantOption.type, String(30)).label("type"),
        cast(CategoryVariantOption.options, Text).label("options"),
        null().label("unit"),
        literal(0).label("is_required"),
        literal(1).label("is_filterable"),
        CategoryVariantOption.sort_order,
    )
    if category_ids is not None:
        if not category_ids:
            return []
        attrs = attrs.where(CategoryAttribute.category_id.in_(category_ids))
        opts = opts.where(CategoryVariantOption.category_id.in_(category_ids))
    result = await session.execute(union_all(attrs, opts))
    return [_row(m) for m in result.mappings()]


def _row(m: Any) -> DefinitionRow:
    raw_options = m["options"]
    options = json.loads(raw_options) if raw_options not in (None, "null") else None
    return DefinitionRow(
        kind=m["kind"], id=m["id"], category_id=m["category_id"], key=m["key"], label=m["label"],
        type=m["type"], options=options, unit=m["unit"], is_required=bool(m["is_required"]),
        is_filterable=bool(m["is_filterable"]), sort_order=m["sort_order"],
    )


async def load_effective_definitions(
    session: AsyncSession, category_id: int, parent_id: int | None
) -> EffectiveDefinitions:
    ids = [category_id] if parent_id is None else [category_id, parent_id]
    return merge_definitions(category_id, parent_id, await load_definition_rows(session, ids))
