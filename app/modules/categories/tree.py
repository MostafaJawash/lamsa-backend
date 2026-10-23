"""All categories loaded once per request (a small table) and navigated in memory."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.categories.models import Category


@dataclass
class CategoryIndex:
    by_id: dict[int, Category]
    children: dict[int, list[Category]]

    @classmethod
    def build(cls, categories: Sequence[Category]) -> "CategoryIndex":
        by_id = {c.id: c for c in categories}
        children: dict[int, list[Category]] = {}
        for c in sorted(categories, key=lambda c: (c.sort_order, c.name, c.id)):
            if c.parent_id is not None:
                children.setdefault(c.parent_id, []).append(c)
        return cls(by_id=by_id, children=children)

    def by_slug(self, slug: str) -> Category | None:
        return next((c for c in self.by_id.values() if c.slug == slug), None)

    def parent(self, category: Category) -> Category | None:
        return self.by_id.get(category.parent_id) if category.parent_id is not None else None

    def is_visible(self, category: Category) -> bool:
        """Active, and (top-level or its parent is active)."""
        if not category.is_active:
            return False
        parent = self.parent(category)
        return parent is None or parent.is_active

    def roots(self) -> list[Category]:
        return sorted((c for c in self.by_id.values() if c.parent_id is None), key=lambda c: (c.sort_order, c.name, c.id))

    def subtree_ids(self, category: Category) -> list[int]:
        """The category and its children."""
        return [category.id, *(c.id for c in self.children.get(category.id, []))]

    def breadcrumb(self, category: Category) -> list[Category]:
        parent = self.parent(category)
        return [parent, category] if parent is not None else [category]


async def load_category_index(session: AsyncSession) -> CategoryIndex:
    return CategoryIndex.build(list((await session.scalars(select(Category))).all()))
