from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.modules.products.enums import ProductType
from app.modules.products.schemas import StockStatus
from app.shared.exceptions import CartIssue, CartNotice
from app.shared.money import MoneyOut
from app.shared.schemas import ImageUrls

# ---------------------------------------------------------------------------- requests


class CartItemAdd(BaseModel):
    product_id: int
    variant_id: int | None = None
    quantity: int = Field(default=1, ge=1, le=1000)


class CartItemUpdate(BaseModel):
    quantity: int | None = Field(default=None, ge=1, le=1000)
    variant_id: int | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "CartItemUpdate":
        if self.quantity is None and self.variant_id is None:
            raise ValueError("send quantity and/or variant_id")
        return self


class CartMergeItem(BaseModel):
    product_id: int
    variant_id: int | None = None
    quantity: int = Field(default=1, ge=1, le=1000)


class CartMerge(BaseModel):
    items: list[CartMergeItem] = Field(max_length=100)


class CartPreviewItem(BaseModel):
    product_id: int = Field(ge=1)
    variant_id: int | None = Field(default=None, ge=1)
    quantity: int = Field(ge=1, le=99)


class CartPreview(BaseModel):
    items: list[CartPreviewItem] = Field(max_length=50)


# --------------------------------------------------------------------------- responses


class CartBrand(BaseModel):
    name: str
    slug: str


class CartProduct(BaseModel):
    id: int
    slug: str
    name: str
    type: ProductType
    brand: CartBrand | None


class CartVariant(BaseModel):
    id: int
    options: dict[str, str]
    label: str


class CartBundleItem(BaseModel):
    name: str
    variant_label: str | None
    quantity: int


class LineIssue(BaseModel):
    code: CartIssue
    message: str
    available: int | None = None
    max: int | None = None


class CartLineFields(BaseModel):
    """Everything a cart line shows; CartLineOut adds the database id, CartPreviewLineOut a key."""

    quantity: int
    product: CartProduct
    variant: CartVariant | None
    image: ImageUrls | None
    unit_price: MoneyOut
    regular_price: MoneyOut
    compare_at_price: MoneyOut | None
    discount_percent: int | None
    line_total: MoneyOut = Field(description="unit_price x quantity; 0 for lines with an issue")
    stock_status: StockStatus
    stock_left: int | None
    items: list[CartBundleItem] = []
    issue: LineIssue | None


class CartLineOut(CartLineFields):
    id: int


class CartPreviewLineOut(CartLineFields):
    key: str = Field(description='"<product_id>:<variant_id or 0>", stable for the same product and variant')


class DeliveryQuote(BaseModel):
    code: str
    name: str
    description: str | None
    fee: MoneyOut
    is_free: bool
    free_from: MoneyOut | None
    amount_to_free_shipping: MoneyOut | None
    requires_address: bool


class CartNoticeOut(BaseModel):
    code: CartNotice
    message: str
    item_id: int | None = None
    product_id: int | None = None
    from_quantity: int | None = None
    to_quantity: int | None = None
    reason: CartIssue | None = None


class CartOut(BaseModel):
    lines: list[CartLineOut]
    items_count: int = Field(description="Sum of quantities of lines without issues")
    lines_count: int
    subtotal: MoneyOut = Field(description="Lines without issues only")
    savings: MoneyOut
    delivery_quotes: list[DeliveryQuote]
    can_checkout: bool
    issues_count: int
    notices: list[CartNoticeOut] = Field(description="What THIS request changed (empty on plain reads)")


MergeStatus = Literal["added", "merged", "capped", "skipped"]


class MergeReportItem(BaseModel):
    index: int
    product_id: int | None
    variant_id: int | None
    status: MergeStatus
    reason: str | None
    quantity: int = Field(description="Quantity of the cart line after the merge (0 when skipped)")


class CartMergeOut(CartOut):
    merge_report: list[MergeReportItem]


class CartPreviewOut(CartOut):
    """GET /cart's response for items kept on a device (nothing is stored)."""

    lines: list[CartPreviewLineOut]  # type: ignore[assignment]


class CartCountOut(BaseModel):
    items_count: int = Field(description="Sum of quantities of lines without issues, as in the cart")
    lines_count: int
