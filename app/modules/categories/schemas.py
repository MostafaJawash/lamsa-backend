from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.modules.categories.enums import AttributeType, VariantOptionType
from app.shared.schemas import ImageUrls

CategoryName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Slug = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
DefinitionKey = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{1,49}$")]
OptionValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


def _unique_options(values: list[str] | None) -> list[str] | None:
    if values is not None and len(set(values)) != len(values):
        raise ValueError("options must be unique")
    return values


# ------------------------------------------------------------------------------- public


class CategoryRef(BaseModel):
    id: int
    name: str
    slug: str


class CategoryNode(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    image: ImageUrls | None
    product_count: int
    children: list["CategoryNode"] = []


class AttributeDefinitionOut(BaseModel):
    key: str
    label: str
    type: AttributeType
    unit: str | None
    options: list[str] | None


class VariantOptionDefinitionOut(BaseModel):
    key: str
    label: str
    type: VariantOptionType
    options: list[str] | None


class CategoryDetailOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    image: ImageUrls | None
    product_count: int
    breadcrumb: list[CategoryRef]
    children: list[CategoryNode]
    attributes: list[AttributeDefinitionOut] = Field(description="Effective filterable attributes")
    variant_options: list[VariantOptionDefinitionOut] = Field(description="Effective variant options")


# -------------------------------------------------------------------------------- admin


class CategoryCreate(BaseModel):
    name: CategoryName
    slug: Slug | None = None
    parent_id: int | None = None
    description: str | None = Field(default=None, max_length=5000)
    sort_order: int = Field(default=0, ge=-1_000_000, le=1_000_000)
    is_active: bool = True


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: CategoryName | None = None
    slug: Slug | None = None
    parent_id: int | None = None
    description: str | None = Field(default=None, max_length=5000)
    sort_order: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    is_active: bool | None = None


class AdminCategoryNode(BaseModel):
    id: int
    name: str
    slug: str
    parent_id: int | None
    description: str | None
    image: ImageUrls | None
    sort_order: int
    is_active: bool
    product_count: int
    created_at: datetime
    updated_at: datetime
    children: list["AdminCategoryNode"] = []


class AdminAttributeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: int
    key: str
    label: str
    type: AttributeType
    options: list[str] | None
    unit: str | None
    is_required: bool
    is_filterable: bool
    sort_order: int
    inherited: bool = False


class AdminVariantOptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: int
    key: str
    label: str
    type: VariantOptionType
    options: list[str] | None
    sort_order: int
    inherited: bool = False


class AdminCategoryDetail(AdminCategoryNode):
    own_attributes: list[AdminAttributeOut]
    own_variant_options: list[AdminVariantOptionOut]
    effective_attributes: list[AdminAttributeOut]
    effective_variant_options: list[AdminVariantOptionOut]


class AttributeCreate(BaseModel):
    key: DefinitionKey
    label: Label
    type: AttributeType
    options: list[OptionValue] | None = None
    unit: Unit | None = None
    is_required: bool = False
    is_filterable: bool = False
    sort_order: int = 0

    _unique = field_validator("options")(_unique_options)


class AttributeUpdate(BaseModel):
    """key cannot be changed after creation."""

    model_config = ConfigDict(extra="forbid")

    label: Label | None = None
    type: AttributeType | None = None
    options: list[OptionValue] | None = None
    unit: Unit | None = None
    is_required: bool | None = None
    is_filterable: bool | None = None
    sort_order: int | None = None

    _unique = field_validator("options")(_unique_options)


class VariantOptionCreate(BaseModel):
    key: DefinitionKey
    label: Label
    type: VariantOptionType
    options: list[OptionValue] | None = None
    sort_order: int = 0

    _unique = field_validator("options")(_unique_options)

    @model_validator(mode="after")
    def _color_has_no_options(self) -> "VariantOptionCreate":
        if self.type == VariantOptionType.color and self.options:
            raise ValueError("color options take free color names (with hex); options must be null")
        return self


class VariantOptionUpdate(BaseModel):
    """key cannot be changed after creation."""

    model_config = ConfigDict(extra="forbid")

    label: Label | None = None
    type: VariantOptionType | None = None
    options: list[OptionValue] | None = None
    sort_order: int | None = None

    _unique = field_validator("options")(_unique_options)


def dump_set(model: BaseModel) -> dict[str, Any]:
    """Only the fields the client actually sent (PATCH semantics)."""
    return model.model_dump(exclude_unset=True)
