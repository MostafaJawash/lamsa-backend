from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.shared.enums import Currency
from app.shared.governorates import GOVERNORATE_CODES
from app.shared.money import MoneyIn, MoneyOut


class GovernorateOut(BaseModel):
    code: str
    name: str


class AdminDeliveryMethodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None
    fee: MoneyOut
    free_from: MoneyOut | None
    currency: Currency
    allowed_governorates: list[str] | None
    fee_paid_to_carrier: bool
    requires_address: bool
    is_active: bool
    sort_order: int


class AdminDeliveryMethodUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    fee: MoneyIn | None = None
    free_from: MoneyIn | None = None
    currency: Currency | None = None
    allowed_governorates: list[str] | None = None
    fee_paid_to_carrier: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None

    @field_validator("allowed_governorates")
    @classmethod
    def _governorates(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unique = list(dict.fromkeys(value))
        if any(code not in GOVERNORATE_CODES for code in unique):
            raise ValueError("unsupported governorate")
        return unique

    @model_validator(mode="after")
    def _not_empty(self) -> "AdminDeliveryMethodUpdate":
        if not self.model_fields_set:
            raise ValueError("send at least one field")
        for field in (
            "name",
            "fee",
            "currency",
            "fee_paid_to_carrier",
            "is_active",
            "sort_order",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self
