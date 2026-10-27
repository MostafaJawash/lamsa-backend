from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.modules.cart.schemas import (
    CartCountOut,
    CartItemAdd,
    CartItemUpdate,
    CartMerge,
    CartMergeOut,
    CartOut,
    CartPreview,
    CartPreviewOut,
)
from app.modules.cart.service import CartPreviewService, CartService
from app.shared.dependencies.auth import CurrentUser, DbSession

router = APIRouter(prefix="/cart", tags=["cart"])


def get_service(session: DbSession, user: CurrentUser) -> CartService:
    return CartService(session, user)


Service = Annotated[CartService, Depends(get_service)]


@router.get("", response_model=CartOut)
async def get_cart(service: Service) -> CartOut:
    """The cart with live prices, stock status, line issues and delivery quotes.
    The cart does not reserve stock; stock is taken only when an order is placed."""
    return await service.get()


@router.get("/count", response_model=CartCountOut)
async def cart_count(service: Service) -> CartCountOut:
    """The badge numbers only: items_count and lines_count exactly as in GET /cart."""
    return await service.count()


@router.post("/preview", response_model=CartPreviewOut)
async def preview_cart(data: CartPreview, session: DbSession) -> CartPreviewOut:
    """Public (an Authorization header is ignored): the cart for items kept on a device (guests),
    computed exactly like GET /cart without storing anything. Lines carry a `key`
    ("<product_id>:<variant_id or 0>") instead of an id; unknown or hidden products come back as
    UNAVAILABLE lines; the same product and variant twice become one line (notice LINES_MERGED)."""
    return await CartPreviewService(session).preview(data.items)


@router.post("/items", response_model=CartOut, status_code=status.HTTP_201_CREATED)
async def add_item(data: CartItemAdd, service: Service, response: Response) -> CartOut:
    """Add a product (201) or grow an existing line (200)."""
    cart, created = await service.add_item(data.product_id, data.variant_id, data.quantity)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return cart


@router.patch("/items/{item_id}", response_model=CartOut)
async def update_item(item_id: int, data: CartItemUpdate, service: Service) -> CartOut:
    """Change quantity and/or variant. Picking a variant already in the cart merges the lines."""
    return await service.update_item(item_id, data.quantity, data.variant_id)


@router.delete("/items/{item_id}", response_model=CartOut)
async def delete_item(item_id: int, service: Service) -> CartOut:
    return await service.delete_item(item_id)


@router.delete("", response_model=CartOut)
async def clear_cart(service: Service) -> CartOut:
    return await service.clear()


@router.post("/resolve-issues", response_model=CartOut)
async def resolve_issues(service: Service) -> CartOut:
    """The "update my cart" button: removes unavailable lines, caps quantities, reports notices."""
    return await service.resolve_issues()


@router.post("/merge", response_model=CartMergeOut)
async def merge_cart(data: CartMerge, service: Service) -> CartMergeOut:
    """Merge the guest cart kept on the device, right after login/register."""
    return await service.merge(data.items)
