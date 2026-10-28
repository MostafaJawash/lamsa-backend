from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.modules.brands.schemas import BrandRef
from app.modules.categories.enums import AttributeType, VariantOptionType
from app.modules.categories.schemas import CategoryRef
from app.modules.products.enums import ProductType
from app.shared.money import MoneyIn, MoneyOut
from app.shared.schemas import ImageUrls
from app.shared.enums import Currency

ProductName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Slug = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Stock = Annotated[int, Field(ge=0, le=1_000_000)]
AltText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]


class StockStatus(StrEnum):
    in_stock = "in_stock"
    low = "low"
    out = "out"


class ProductSort(StrEnum):
    newest = "newest"
    price_asc = "price_asc"
    price_desc = "price_desc"
    relevance = "relevance"


class AdminProductSort(StrEnum):
    newest = "newest"
    name = "name"
    price_asc = "price_asc"
    price_desc = "price_desc"
    stock_asc = "stock_asc"


# ================================================================================ public


class Swatch(BaseModel):
    name: str
    hex: str


class ProductCardOut(BaseModel):
    id: int
    slug: str
    name: str
    type: ProductType
    currency: Currency
    brand: BrandRef | None
    category: CategoryRef
    image: ImageUrls | None = Field(description="Main image (lowest sort_order)")
    price: MoneyOut = Field(description="Lowest current price (sale price when on sale)")
    compare_at_price: MoneyOut | None = Field(description="Regular price matching `price` when it is a sale price")
    discount_percent: int | None
    is_new: bool
    is_on_sale: bool
    in_stock: bool
    swatches: list[Swatch]
    is_favorite: bool = Field(default=False, description="Always false for anonymous users")


class AttributeValueOut(BaseModel):
    key: str
    label: str
    type: AttributeType
    unit: str | None
    value: Any
    display: str


class ProductImageOut(BaseModel):
    id: int
    alt_text: str | None
    variant_id: int | None
    color: str | None = Field(description="Color value this image belongs to; null for general images")
    width: int
    height: int
    urls: ImageUrls


class OptionValueOut(BaseModel):
    value: str
    hex: str | None = None


class VariantOptionOut(BaseModel):
    key: str
    label: str
    type: VariantOptionType
    values: list[OptionValueOut]


class VariantOut(BaseModel):
    id: int
    currency: Currency
    options: dict[str, str]
    price: MoneyOut
    sale_price: MoneyOut | None
    current_price: MoneyOut
    discount_percent: int | None
    stock_status: StockStatus
    stock_left: int | None = Field(description="Only when stock_status is low")
    image_ids: list[int] = Field(description="Images of this variant's color in order, then general images")


class BundleItemProductOut(BaseModel):
    id: int
    slug: str
    name: str
    image: ImageUrls | None


class BundleItemOut(BaseModel):
    product: BundleItemProductOut
    variant_label: str | None
    quantity: int


class ProductDetailOut(ProductCardOut):
    description: str | None
    breadcrumb: list[CategoryRef]
    attributes: list[AttributeValueOut]
    images: list[ProductImageOut]
    # simple / bundle
    regular_price: MoneyOut | None = None
    sale_price: MoneyOut | None = None
    current_price: MoneyOut | None = None
    stock_status: StockStatus | None = None
    stock_left: int | None = None
    # variable
    variant_options: list[VariantOptionOut] = []
    variants: list[VariantOut] = []
    # bundle
    items: list[BundleItemOut] = []


class BrandFacet(BaseModel):
    slug: str
    name: str
    count: int


class PriceRange(BaseModel):
    currency: Currency
    min: MoneyOut | None
    max: MoneyOut | None


class FacetValue(BaseModel):
    value: str
    count: int
    hex: str | None = None


class AttributeFacet(BaseModel):
    key: str
    label: str
    type: AttributeType
    unit: str | None = None
    values: list[FacetValue] | None = None
    min: float | None = None
    max: float | None = None


class OptionFacet(BaseModel):
    key: str
    label: str
    type: VariantOptionType
    values: list[FacetValue]


class FacetsOut(BaseModel):
    brands: list[BrandFacet]
    prices: list[PriceRange]
    attributes: list[AttributeFacet] = []
    options: list[OptionFacet] = []


# ================================================================================= admin


class VariantIn(BaseModel):
    options: dict[str, Any]
    sku: Sku | None = None
    price: MoneyIn
    sale_price: MoneyIn | None = None
    stock: Stock = 0
    is_active: bool = True
    sort_order: int = 0


class VariantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: dict[str, Any] | None = None
    sku: Sku | None = None
    price: MoneyIn | None = None
    sale_price: MoneyIn | None = None
    stock: Stock | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class BundleItemIn(BaseModel):
    product_id: int
    variant_id: int | None = None
    quantity: int = Field(default=1, ge=1, le=99)


class BundleItemsIn(BaseModel):
    items: list[BundleItemIn] = Field(max_length=50)


class ProductCreate(BaseModel):
    type: ProductType
    currency: Currency = Currency.syp
    name: ProductName
    slug: Slug | None = None
    description: str | None = Field(default=None, max_length=20_000)
    category_id: int
    brand_id: int | None = None
    sku: Sku | None = None
    price: MoneyIn | None = None
    sale_price: MoneyIn | None = None
    stock: Stock | None = None
    attributes: dict[str, Any] | None = None
    is_new: bool = False
    is_active: bool = Field(default=False, description="Ignored: new products are always inactive")
    variants: list[VariantIn] | None = Field(default=None, max_length=100)
    bundle_items: list[BundleItemIn] | None = Field(default=None, max_length=50)


class ProductUpdate(BaseModel):
    """PATCH: only sent fields change. `attributes`, when sent, REPLACES the whole object
    (keys with null values are dropped). `type` cannot change."""

    model_config = ConfigDict(extra="forbid")

    type: ProductType | None = None
    currency: Currency | None = None
    name: ProductName | None = None
    slug: Slug | None = None
    description: str | None = Field(default=None, max_length=20_000)
    category_id: int | None = None
    brand_id: int | None = None
    sku: Sku | None = None
    price: MoneyIn | None = None
    sale_price: MoneyIn | None = None
    stock: Stock | None = None
    attributes: dict[str, Any] | None = None
    is_new: bool | None = None
    is_active: bool | None = None


class StockAdjust(BaseModel):
    delta: int = Field(ge=-1_000_000, le=1_000_000)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None

    @model_validator(mode="after")
    def _non_zero(self) -> "StockAdjust":
        if self.delta == 0:
            raise ValueError("delta must not be 0")
        return self


class StockOut(BaseModel):
    stock: int


class ImageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alt_text: AltText | None = None
    variant_id: int | None = None


class ImageOrder(BaseModel):
    image_ids: list[int] = Field(max_length=100)


class AdminVariantOut(BaseModel):
    id: int
    currency: Currency
    sku: str
    options: dict[str, str]
    options_signature: str
    color: str | None
    price: MoneyOut
    sale_price: MoneyOut | None
    current_price: MoneyOut
    stock: int
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class AdminImageOut(BaseModel):
    id: int
    image_key: str
    variant_id: int | None
    color: str | None
    alt_text: str | None
    width: int
    height: int
    sort_order: int
    urls: ImageUrls


class AdminBundleItemOut(BaseModel):
    id: int
    product_id: int
    product_name: str
    product_slug: str
    variant_id: int | None
    variant_label: str | None
    quantity: int
    component_stock: int
    component_active: bool


class Publishable(BaseModel):
    ok: bool
    reasons: list[str]


class ProductWarning(BaseModel):
    code: str
    colors: list[str] = []


class AdminProductOut(BaseModel):
    id: int
    type: ProductType
    currency: Currency
    name: str
    slug: str
    description: str | None
    category: CategoryRef
    brand: BrandRef | None
    sku: str | None
    price: MoneyOut | None
    sale_price: MoneyOut | None
    stock: int | None
    bundle_stock: int | None = Field(description="Derived stock of a bundle")
    attributes: dict[str, Any]
    price_from: MoneyOut
    in_stock: bool
    is_on_sale: bool
    is_new: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    variants: list[AdminVariantOut]
    images: list[AdminImageOut]
    bundle_items: list[AdminBundleItemOut]
    publishable: Publishable
    warnings: list[ProductWarning]


class AdminProductListItem(BaseModel):
    id: int
    type: ProductType
    currency: Currency
    name: str
    slug: str
    sku: str | None
    category: CategoryRef
    brand: BrandRef | None
    image: ImageUrls | None
    price_from: MoneyOut
    stock: int | None = Field(description="Simple: stock; variable: sum of variant stock; bundle: null")
    in_stock: bool
    is_on_sale: bool
    is_new: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class LowStockItem(BaseModel):
    product_id: int
    product_name: str
    product_slug: str
    variant_id: int | None
    variant_label: str | None
    sku: str | None
    stock: int


class ImagesUploadOut(BaseModel):
    images: list[AdminImageOut]
    color: str | None = Field(description="Color the uploaded images were linked to, if any")


class ImageDeleteOut(BaseModel):
    deleted: bool
    warning: str | None = Field(description="LAST_IMAGE_OF_ACTIVE_PRODUCT when an active product has no images left")
