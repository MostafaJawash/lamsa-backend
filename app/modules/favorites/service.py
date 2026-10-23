from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.cart.schemas import CartOut
from app.modules.cart.service import CartService
from app.modules.favorites import repository as repo
from app.modules.favorites.schemas import FavoritesMergeOut, SkippedFavorite
from app.modules.products.catalog_service import CatalogProductService
from app.modules.products.schemas import ProductCardOut
from app.modules.settings.service import load_limits
from app.modules.users.models import User
from app.shared.dependencies.pagination import Page, PageParams
from app.shared.exceptions import ErrorCode, NotFound, ValidationFailed


class FavoritesService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user
        # Kept as a plain int: a rollback (retry) expires ORM objects such as `user`.
        self.user_id = user.id

    async def list(self, params: PageParams) -> Page[ProductCardOut]:
        rows, total = await repo.favorite_cards(self.session, self.user_id, params.offset, params.page_size)
        if total is None:
            total = await repo.count_visible(self.session, self.user_id) if params.page > 1 else 0
        cards = await CatalogProductService(self.session).cards(rows, favorite_ids={r.id for r in rows})
        return Page[ProductCardOut](items=cards, page=params.page, page_size=params.page_size, total=total)

    async def ids(self) -> list[int]:
        return await repo.visible_favorite_ids(self.session, self.user_id)

    async def add(self, product_id: int) -> None:
        """Idempotent. 404 if not visible; 422 FAVORITES_LIMIT_REACHED over the limit."""
        limits = await load_limits(self.session)
        if not await repo.visible_product_ids(self.session, [product_id]):
            raise NotFound(ErrorCode.PRODUCT_NOT_FOUND)
        existing = await repo.all_favorite_ids(self.session, self.user_id)
        if product_id not in existing and len(existing) >= limits.favorites_max:
            raise ValidationFailed(ErrorCode.FAVORITES_LIMIT_REACHED, details={"max": limits.favorites_max})
        await repo.add_favorites(self.session, self.user_id, [product_id])
        await self.session.commit()

    async def remove(self, product_id: int) -> None:
        await repo.remove_favorite(self.session, self.user_id, product_id)
        await self.session.commit()

    async def merge(self, product_ids: Sequence[int]) -> FavoritesMergeOut:
        """Guest favorites after login: add visible ones, skip the rest and anything over the limit."""
        limits = await load_limits(self.session)
        wanted = list(dict.fromkeys(product_ids))
        visible = await repo.visible_product_ids(self.session, wanted)
        existing = await repo.all_favorite_ids(self.session, self.user_id)
        added: list[int] = []
        skipped: list[SkippedFavorite] = []
        count = len(existing)
        for product_id in wanted:
            if product_id not in visible:
                skipped.append(SkippedFavorite(product_id=product_id, reason=ErrorCode.PRODUCT_NOT_FOUND.value))
            elif product_id in existing:
                added.append(product_id)  # already a favorite: nothing to do, counts as kept
            elif count >= limits.favorites_max:
                skipped.append(SkippedFavorite(product_id=product_id, reason=ErrorCode.FAVORITES_LIMIT_REACHED.value))
            else:
                added.append(product_id)
                count += 1
        await repo.add_favorites(self.session, self.user_id, [p for p in added if p not in existing])
        await self.session.commit()
        return FavoritesMergeOut(added=added, skipped=skipped)

    async def move_to_cart(self, product_id: int, variant_id: int | None, quantity: int) -> CartOut:
        """ONE transaction: add to the cart (same rules as POST /cart/items), then unfavorite.
        If adding fails, nothing is committed and the favorite stays."""
        cart = CartService(self.session, self.user)

        async def operation() -> CartOut:
            limits, _ = await cart.add_in_transaction(product_id, variant_id, quantity, pick_variant=True)
            await repo.remove_favorite(self.session, self.user_id, product_id)
            await self.session.commit()
            return await cart.render(limits)

        return await cart.retrying(operation)
