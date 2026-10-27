from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

HIDE_DOC = (
    "Out-of-stock products are left out of browsing (product lists, search, facets, category and brand "
    "product counts, card swatches). Product pages, `?ids=`, cart and favorites still show them."
)


class AdminSettingsOut(BaseModel):
    """The settings the admin API manages (registry: app.modules.settings.admin)."""

    catalog_hide_out_of_stock: bool = Field(description=HIDE_DOC)


class AdminSettingsUpdate(BaseModel):
    """Only the keys sent are changed; unknown keys and wrong types are a 422."""

    model_config = ConfigDict(extra="forbid")

    catalog_hide_out_of_stock: StrictBool | None = Field(default=None, description=HIDE_DOC)

    @model_validator(mode="after")
    def _values(self) -> "AdminSettingsUpdate":
        if not self.model_fields_set:
            raise ValueError("send at least one setting")
        for key in self.model_fields_set:
            if getattr(self, key) is None:
                raise ValueError(f"{key} must not be null")
        return self
