from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from app.modules.orders.enums import OrderStatus, PaymentMethod, PaymentStatus
from app.shared.money import MoneyIn, MoneyOut
from app.shared.schemas import ImageUrls

Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class OrderSort(StrEnum):
    newest = "newest"
    oldest = "oldest"
    total_desc = "total_desc"


# ----------------------------------------------------------------------------- requests


class OrderCreate(BaseModel):
    delivery_method_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
    address_id: int | None = None
    customer_notes: Note | None = None
    gift_message: Note | None = None
    expected_total: MoneyIn = Field(description="The total shown on the checkout screen")


class OrderCancel(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None


class AdminOrderCancel(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=255)]
    restore_stock: bool = True


class StatusChange(BaseModel):
    to_status: OrderStatus
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None


# ---------------------------------------------------------------------------- responses


class DeliveryOut(BaseModel):
    code: str
    name: str


class OrderItemOut(BaseModel):
    product_id: int | None
    product_slug: str | None = Field(description="Only while the product is still visible")
    name: str
    variant_label: str | None
    options: dict[str, Any] | None
    image: ImageUrls | None
    unit_price: MoneyOut
    quantity: int
    line_total: MoneyOut
    bundle_contents: list[dict[str, Any]] | None


class TimelineStepOut(BaseModel):
    status: OrderStatus
    label: str
    reached_at: datetime | None
    is_current: bool
    cancel_reason: str | None = None


class OrderSummaryOut(BaseModel):
    id: int = Field(description="The order number")
    created_at: datetime
    status: OrderStatus
    status_label: str
    items_count: int
    total: MoneyOut
    image: ImageUrls | None = Field(description="First item's image")
    delivery_method_name: str


class AdminOrderSummaryOut(OrderSummaryOut):
    user_id: int
    customer_name: str
    customer_phone: str


class OrderDetailOut(BaseModel):
    id: int
    created_at: datetime
    status: OrderStatus
    status_label: str
    payment_method: PaymentMethod
    payment_status: PaymentStatus
    delivery: DeliveryOut
    shipping_address: dict[str, Any] | None
    customer_notes: str | None
    gift_message: str | None
    items: list[OrderItemOut]
    subtotal: MoneyOut
    discount: MoneyOut
    shipping_fee: MoneyOut
    total: MoneyOut
    timeline: list[TimelineStepOut]
    cancel_reason: str | None
    can_cancel: bool


class OrderCustomer(BaseModel):
    user_id: int
    name: str
    phone: str
    orders_count: int


class HistoryOut(BaseModel):
    from_status: OrderStatus | None
    to_status: OrderStatus
    changed_by_user_id: int | None
    changed_by_name: str | None
    note: str | None
    created_at: datetime


class AdminOrderDetailOut(OrderDetailOut):
    customer: OrderCustomer
    history: list[HistoryOut]
    allowed_next_statuses: list[OrderStatus]


class TopProduct(BaseModel):
    product_id: int | None
    name: str
    quantity: int


class OrderStats(BaseModel):
    date_from: date | None
    date_to: date | None
    orders_count: int
    counts: dict[str, int]
    delivered_revenue: MoneyOut
    average_order_value: MoneyOut
    cancelled_rate: float
    top_products: list[TopProduct]
