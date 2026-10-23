"""The ONE SQL condition deciding whether customers can see a product."""

from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.orm import aliased

from app.modules.brands.models import Brand
from app.modules.categories.models import Category
from app.modules.products.models import Product


def visible_category_condition(category: Any = Category) -> ColumnElement[bool]:
    """Category (or an alias of it) is active and (top-level or its parent is active)."""
    parent = aliased(Category)
    return and_(
        category.is_active.is_(True),
        or_(
            category.parent_id.is_(None),
            exists(select(parent.id).where(parent.id == category.parent_id, parent.is_active.is_(True))),
        ),
    )


def visible_product_condition() -> ColumnElement[bool]:
    """Product active AND its category visible AND (no brand OR brand active).

    Uses private aliases so it never auto-correlates with Category/Brand joined by the caller."""
    category = aliased(Category)
    brand = aliased(Brand)
    return and_(
        Product.is_active.is_(True),
        exists(select(category.id).where(category.id == Product.category_id, visible_category_condition(category))),
        or_(
            Product.brand_id.is_(None),
            exists(select(brand.id).where(brand.id == Product.brand_id, brand.is_active.is_(True))),
        ),
    )
