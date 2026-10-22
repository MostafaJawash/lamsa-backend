from enum import StrEnum


class PaymentMethod(StrEnum):
    cod = "cod"


class PaymentStatus(StrEnum):
    pending = "pending"
    paid = "paid"
    refunded = "refunded"


class OrderStatus(StrEnum):
    pending = "pending"
    preparing = "preparing"
    shipped = "shipped"
    ready_for_pickup = "ready_for_pickup"
    delivered = "delivered"
    cancelled = "cancelled"
