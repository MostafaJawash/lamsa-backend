from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.modules.cart.schemas import CartMergeOut
from app.modules.orders.enums import OrderStatus
from app.modules.orders.schemas import (
    AdminOrderCancel,
    AdminOrderDetailOut,
    AdminOrderSummaryOut,
    OrderCancel,
    OrderCreate,
    OrderDetailOut,
    OrderSort,
    OrderStatusGroup,
    OrderStats,
    OrderSummaryOut,
    StatusChange,
)
from app.modules.orders.service import AdminOrderService, OrderService, validate_idempotency_key
from app.shared.dependencies.auth import AdminUser, CurrentUser, DbSession
from app.shared.dependencies.pagination import Page, PageParamsDep

router = APIRouter(prefix="/orders", tags=["orders"])
admin_router = APIRouter(prefix="/admin/orders", tags=["admin-orders"])


def get_service(session: DbSession, user: CurrentUser) -> OrderService:
    return OrderService(session, user)


def get_admin_service(session: DbSession, admin: AdminUser) -> AdminOrderService:
    return AdminOrderService(session, admin)


Service = Annotated[OrderService, Depends(get_service)]
Admin = Annotated[AdminOrderService, Depends(get_admin_service)]

# ----------------------------------------------------------------------------- customer


@router.post("", response_model=OrderDetailOut, status_code=status.HTTP_201_CREATED)
async def place_order(
    data: OrderCreate,
    service: Service,
    response: Response,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", description="A fresh UUID per checkout attempt")
    ] = None,
) -> OrderDetailOut:
    """Place an order from the whole cart (cash on delivery). 201 for a new order; 200 with the
    existing order when the same Idempotency-Key was used before."""
    placed = await service.place(data, validate_idempotency_key(idempotency_key))
    response.status_code = status.HTTP_201_CREATED if placed.created else status.HTTP_200_OK
    return placed.detail


@router.get("", response_model=Page[OrderSummaryOut])
async def list_orders(
    service: Service,
    params: PageParamsDep,
    status_filter: Annotated[OrderStatus | None, Query(alias="status")] = None,
    status_group: Annotated[OrderStatusGroup | None, Query()] = None,
) -> Page[OrderSummaryOut]:
    """The customer's orders, newest first."""
    return await service.list(params, status_filter, status_group)


@router.get("/{order_id}", response_model=OrderDetailOut)
async def get_order(order_id: int, service: Service) -> OrderDetailOut:
    return await service.get(order_id)


@router.post("/{order_id}/cancel", response_model=OrderDetailOut)
async def cancel_order(order_id: int, service: Service, data: OrderCancel | None = None) -> OrderDetailOut:
    """Only while the order is pending. Restores the stock."""
    return await service.cancel(order_id, data.reason if data else None)


@router.post("/{order_id}/reorder", response_model=CartMergeOut)
async def reorder(order_id: int, service: Service) -> CartMergeOut:
    """Add the order's items back to the cart (merged, capped or skipped like a cart merge)."""
    return await service.reorder(order_id)


# -------------------------------------------------------------------------------- admin


@admin_router.get("", response_model=Page[AdminOrderSummaryOut])
async def admin_list_orders(
    service: Admin,
    params: PageParamsDep,
    status_filter: Annotated[OrderStatus | None, Query(alias="status")] = None,
    delivery_method: Annotated[str | None, Query(max_length=30, description="Delivery method code")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Order number, or part of name/phone")] = None,
    date_from: Annotated[date | None, Query(description="UTC date, inclusive")] = None,
    date_to: Annotated[date | None, Query(description="UTC date, inclusive")] = None,
    sort: OrderSort = OrderSort.newest,
) -> Page[AdminOrderSummaryOut]:
    return await service.list(params, status=status_filter, delivery_method=delivery_method, q=q,
                              date_from=date_from, date_to=date_to, sort=sort)


@admin_router.get("/stats", response_model=OrderStats)
async def order_stats(
    service: Admin,
    date_from: Annotated[date | None, Query(description="UTC date, inclusive")] = None,
    date_to: Annotated[date | None, Query(description="UTC date, inclusive")] = None,
) -> OrderStats:
    return await service.stats(date_from, date_to)


@admin_router.get("/{order_id}", response_model=AdminOrderDetailOut)
async def admin_get_order(order_id: int, service: Admin) -> AdminOrderDetailOut:
    return await service.get(order_id)


@admin_router.post("/{order_id}/status", response_model=AdminOrderDetailOut)
async def change_status(order_id: int, data: StatusChange, service: Admin) -> AdminOrderDetailOut:
    """Move the order one step forward (see allowed_next_statuses). Use /cancel to cancel."""
    return await service.change_status(order_id, data.to_status, data.note)


@admin_router.post("/{order_id}/cancel", response_model=AdminOrderDetailOut)
async def admin_cancel(order_id: int, data: AdminOrderCancel, service: Admin) -> AdminOrderDetailOut:
    """restore_stock=false only for shipped orders (goods lost / not returned)."""
    return await service.cancel(order_id, data.reason, data.restore_stock)
