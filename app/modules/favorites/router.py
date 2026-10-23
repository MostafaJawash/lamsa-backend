from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.modules.cart.schemas import CartOut
from app.modules.favorites.schemas import FavoriteIds, FavoritesMerge, FavoritesMergeOut, MoveToCart
from app.modules.favorites.service import FavoritesService
from app.modules.products.schemas import ProductCardOut
from app.shared.dependencies.auth import CurrentUser, DbSession
from app.shared.dependencies.pagination import Page, PageParamsDep

router = APIRouter(prefix="/favorites", tags=["favorites"])


def get_service(session: DbSession, user: CurrentUser) -> FavoritesService:
    return FavoritesService(session, user)


Service = Annotated[FavoritesService, Depends(get_service)]


@router.get("", response_model=Page[ProductCardOut])
async def list_favorites(service: Service, params: PageParamsDep) -> Page[ProductCardOut]:
    """Visible favorites as product cards, newest favorite first."""
    return await service.list(params)


@router.get("/ids", response_model=FavoriteIds)
async def favorite_ids(service: Service) -> FavoriteIds:
    """All visible favorite product ids, to fill the hearts quickly."""
    return FavoriteIds(product_ids=await service.ids())


@router.post("/merge", response_model=FavoritesMergeOut)
async def merge_favorites(data: FavoritesMerge, service: Service) -> FavoritesMergeOut:
    """Merge the guest favorites kept on the device, right after login/register."""
    return await service.merge(data.product_ids)


@router.put("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def add_favorite(product_id: int, service: Service) -> Response:
    """Idempotent."""
    await service.add(product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favorite(product_id: int, service: Service) -> Response:
    """Idempotent; never 404."""
    await service.remove(product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{product_id}/move-to-cart", response_model=CartOut)
async def move_to_cart(product_id: int, data: MoveToCart, service: Service) -> CartOut:
    """Add to the cart and remove from favorites in one transaction."""
    return await service.move_to_cart(product_id, data.variant_id, data.quantity)
