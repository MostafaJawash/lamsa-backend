"""Seed base data: categories (with attributes and variant options), delivery methods, settings.

Idempotent: rows are matched by slug / code / (category, key) / setting key, updated in place
if they exist and inserted otherwise. Selecting first (instead of INSERT ... ON DUPLICATE KEY
UPDATE) avoids burning AUTO_INCREMENT values on every re-run.

Store settings are only inserted when missing, so values changed later in the admin survive.

Run with:  python -m scripts.seed_base
"""

import asyncio
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db.models  # noqa: F401  (registers every model)
from app.db.session import SessionLocal, engine
from app.modules.categories.enums import AttributeType, VariantOptionType
from app.modules.categories.models import Category, CategoryAttribute, CategoryVariantOption
from app.modules.delivery.models import DeliveryMethod
from app.modules.settings.models import StoreSetting

INGREDIENTS: dict[str, Any] = {"key": "ingredients", "label": "المكونات", "type": AttributeType.text}

CATEGORIES: list[dict[str, Any]] = [
    {
        "slug": "makeup",
        "name": "مكياج",
        "attributes": [
            INGREDIENTS,
            {
                "key": "finish",
                "label": "اللمسة",
                "type": AttributeType.select,
                "options": ["مطفي", "لامع", "ساتان"],
                "is_filterable": True,
            },
        ],
        "variant_options": [
            {"key": "color", "label": "اللون", "type": VariantOptionType.color},
        ],
        "children": [],
    },
    {
        "slug": "care",
        "name": "عناية وعطور",
        "attributes": [
            {
                "key": "skin_type",
                "label": "نوع البشرة",
                "type": AttributeType.select,
                "options": ["جافة", "دهنية", "مختلطة", "حساسة", "كل الأنواع"],
                "is_filterable": True,
            },
            INGREDIENTS,
        ],
        "variant_options": [
            {"key": "size", "label": "الحجم", "type": VariantOptionType.select},
        ],
        "children": [
            {
                "slug": "perfumes",
                "name": "عطور",
                "attributes": [
                    {"key": "notes", "label": "النفحات", "type": AttributeType.text},
                    {
                        "key": "concentration",
                        "label": "التركيز",
                        "type": AttributeType.select,
                        "options": ["EDP", "EDT", "Body Mist"],
                        "is_filterable": True,
                    },
                ],
                "variant_options": [],
                "children": [],
            },
        ],
    },
    {
        "slug": "hair",
        "name": "شعر",
        "attributes": [
            {
                "key": "hair_type",
                "label": "نوع الشعر",
                "type": AttributeType.select,
                "options": ["جاف", "دهني", "عادي", "مجعد", "مصبوغ"],
                "is_filterable": True,
            },
            INGREDIENTS,
        ],
        "variant_options": [
            {"key": "color", "label": "اللون", "type": VariantOptionType.color},
            {"key": "size", "label": "الحجم", "type": VariantOptionType.select},
        ],
        "children": [],
    },
]

DELIVERY_METHODS: list[dict[str, Any]] = [
    {
        "code": "home",
        "name": "توصيل للبيت",
        "description": "خلال ٢ لـ ٣ أيام عمل",
        "fee": Decimal("4.95"),
        "free_from": Decimal("49.00"),
        "requires_address": True,
    },
    {
        "code": "express",
        "name": "توصيل سريع",
        "description": "بنفس اليوم إذا طلبت قبل الساعة ٦",
        "fee": Decimal("7.95"),
        "free_from": None,
        "requires_address": True,
    },
    {
        "code": "pickup",
        "name": "استلام من المتجر",
        "description": "جاهز خلال ساعتين",
        "fee": Decimal("0.00"),
        "free_from": None,
        "requires_address": False,
    },
]

STORE_SETTINGS: dict[str, Any] = {
    "store_name": "لمسة",
    "currency": "EUR",
    "support_phone": None,
    "low_stock_threshold": 3,
    "cart_max_quantity_per_line": 10,
    "cart_max_lines": 50,
    "favorites_max": 200,
}


def _apply(obj: Any, values: dict[str, Any]) -> None:
    for field, value in values.items():
        setattr(obj, field, value)


async def _upsert_category(
    session: AsyncSession, data: dict[str, Any], parent_id: int | None, sort_order: int
) -> int:
    category = await session.scalar(select(Category).where(Category.slug == data["slug"]))
    if category is None:
        category = Category(slug=data["slug"])
        session.add(category)
    _apply(category, {"name": data["name"], "parent_id": parent_id, "sort_order": sort_order})
    await session.flush()

    for index, attr in enumerate(data["attributes"]):
        row = await session.scalar(
            select(CategoryAttribute).where(
                CategoryAttribute.category_id == category.id, CategoryAttribute.key == attr["key"]
            )
        )
        if row is None:
            row = CategoryAttribute(category_id=category.id, key=attr["key"])
            session.add(row)
        _apply(
            row,
            {
                "label": attr["label"],
                "type": attr["type"],
                "options": attr.get("options"),
                "unit": attr.get("unit"),
                "is_required": attr.get("is_required", False),
                "is_filterable": attr.get("is_filterable", False),
                "sort_order": index,
            },
        )

    for index, opt in enumerate(data["variant_options"]):
        row = await session.scalar(
            select(CategoryVariantOption).where(
                CategoryVariantOption.category_id == category.id, CategoryVariantOption.key == opt["key"]
            )
        )
        if row is None:
            row = CategoryVariantOption(category_id=category.id, key=opt["key"])
            session.add(row)
        _apply(
            row,
            {"label": opt["label"], "type": opt["type"], "options": opt.get("options"), "sort_order": index},
        )

    for index, child in enumerate(data["children"]):
        await _upsert_category(session, child, parent_id=category.id, sort_order=index)

    return category.id


async def seed_categories(session: AsyncSession) -> None:
    for index, data in enumerate(CATEGORIES):
        await _upsert_category(session, data, parent_id=None, sort_order=index)


async def seed_delivery_methods(session: AsyncSession) -> None:
    for index, data in enumerate(DELIVERY_METHODS):
        method = await session.scalar(select(DeliveryMethod).where(DeliveryMethod.code == data["code"]))
        if method is None:
            method = DeliveryMethod(code=data["code"])
            session.add(method)
        _apply(method, {**{k: v for k, v in data.items() if k != "code"}, "sort_order": index})


async def seed_settings(session: AsyncSession) -> None:
    for key, value in STORE_SETTINGS.items():
        if await session.get(StoreSetting, key) is None:
            session.add(StoreSetting(key=key, value=value))


async def main() -> None:
    engine.echo = False
    async with SessionLocal() as session, session.begin():
        await seed_categories(session)
        await seed_delivery_methods(session)
        await seed_settings(session)

    async with SessionLocal() as session:
        counts = {
            "categories": len((await session.scalars(select(Category.id))).all()),
            "category_attributes": len((await session.scalars(select(CategoryAttribute.id))).all()),
            "category_variant_options": len((await session.scalars(select(CategoryVariantOption.id))).all()),
            "delivery_methods": len((await session.scalars(select(DeliveryMethod.id))).all()),
            "store_settings": len((await session.scalars(select(StoreSetting.key))).all()),
        }
    await engine.dispose()
    print("Seed complete:", ", ".join(f"{name}={count}" for name, count in counts.items()))


if __name__ == "__main__":
    asyncio.run(main())
