from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from app.modules.categories.definitions import DefinitionRow, merge_definitions
from app.modules.products.presenters import stock_status
from app.modules.products.schemas import StockStatus
from app.modules.products.summary import BundleComponent, VariantRow, bundle_stock, summarize_variable
from app.shared.catalog import json_path, normalize_sku
from app.shared.exceptions import MESSAGES, AppError, ErrorCode
from app.shared.money import MoneyIn, MoneyOut, discount_percent
from app.shared.utils.arabic import normalize_arabic
from app.shared.utils.slug import is_valid_slug, slug_from_name, with_suffix


def test_normalize_arabic() -> None:
    assert normalize_arabic("أإآٱ") == "اااا"
    assert normalize_arabic("ليلى عناية") == "ليلي عنايه"
    assert normalize_arabic("مُرَطِّبٌ") == "مرطب"
    assert normalize_arabic("عــطــر") == "عطر"
    assert normalize_arabic("٠١٢٣٤٥٦٧٨٩ ۵۰") == "0123456789 50"
    assert normalize_arabic("  Velvet   LIPSTICK ") == "velvet lipstick"
    assert normalize_arabic(None) == ""


class Price(BaseModel):
    value: MoneyIn


class PriceOut(BaseModel):
    value: MoneyOut


@pytest.mark.parametrize("raw", ["8.95", 8.95, 8, "0", "99999999.99", "8.950"])
def test_money_accepts(raw: object) -> None:
    value = Price(value=raw).value  # type: ignore[arg-type]
    assert value == value.quantize(Decimal("0.01"))


@pytest.mark.parametrize("raw", ["8.951", "-0.01", "100000000", "NaN", "abc"])
def test_money_rejects(raw: object) -> None:
    with pytest.raises(ValidationError):
        Price(value=raw)  # type: ignore[arg-type]


def test_money_serializes_as_string() -> None:
    assert PriceOut(value=Decimal("8.9")).model_dump_json() == '{"value":"8.90"}'
    assert discount_percent(Decimal("20"), Decimal("15")) == 25
    assert discount_percent(Decimal("10"), Decimal("7.95")) == 21
    assert discount_percent(Decimal("10"), Decimal("10")) is None


def test_slugs_and_skus() -> None:
    assert slug_from_name("Bella Lipstick 50 ml", "product") == "bella-lipstick-50-ml"
    assert slug_from_name("؟!", "category").startswith("category-")
    assert with_suffix("abc", 1) == "abc" and with_suffix("abc", 3) == "abc-3"
    assert len(with_suffix("x" * 120, 12)) == 120
    assert is_valid_slug("ok-slug-2") and not is_valid_slug("Bad") and not is_valid_slug("a") and not is_valid_slug("a--b")
    assert normalize_sku(" lm-12-a ") == "LM-12-A"
    with pytest.raises(AppError):
        normalize_sku("x")


def test_json_path_whitelist() -> None:
    assert json_path("skin_type") == '$."skin_type"'
    assert json_path("color_hex") == '$."color_hex"'
    for bad in ('a"]', "Skin", "a.b", "1x", "$", "x' OR 1=1"):
        with pytest.raises(ValueError):
            json_path(bad)


def row(kind: str, category_id: int, key: str, label: str, sort_order: int = 0, type_: str = "text") -> DefinitionRow:
    return DefinitionRow(kind=kind, id=hash((category_id, key)) % 10_000, category_id=category_id, key=key, label=label,  # type: ignore[arg-type]
                         type=type_, options=None, unit=None, is_required=False, is_filterable=True, sort_order=sort_order)


def test_merge_definitions_child_wins() -> None:
    rows = [
        row("attribute", 1, "ingredients", "parent ingredients", 1),
        row("attribute", 1, "skin_type", "parent skin", 0),
        row("attribute", 2, "skin_type", "child skin", 5),
        row("attribute", 2, "notes", "notes", 0),
        row("option", 1, "size", "size", type_="select"),
        row("attribute", 3, "unrelated", "x"),
    ]
    merged = merge_definitions(2, 1, rows)
    assert list(merged.attributes) == ["ingredients", "notes", "skin_type"]
    assert merged.attributes["skin_type"].label == "child skin" and not merged.attributes["skin_type"].inherited
    assert merged.attributes["ingredients"].inherited
    assert list(merged.variant_options) == ["size"] and merged.variant_options["size"].inherited
    top = merge_definitions(1, None, rows)
    assert list(top.attributes) == ["skin_type", "ingredients"]


def test_variable_summary_and_bundle_stock() -> None:
    v = [VariantRow(1, Decimal("12"), None, 0, True), VariantRow(2, Decimal("15"), Decimal("11"), 2, True),
         VariantRow(3, Decimal("5"), None, 9, False)]
    s = summarize_variable(v)
    assert (s.price_from, s.in_stock, s.is_on_sale) == (Decimal("11"), True, True)
    assert summarize_variable([VariantRow(1, Decimal("12"), None, 0, True)]).price_from == Decimal("12")
    assert bundle_stock([BundleComponent(2, 5, True), BundleComponent(1, 3, True)]) == 2
    assert bundle_stock([BundleComponent(1, 9, True), BundleComponent(1, 9, False)]) == 0
    assert bundle_stock([]) == 0


def test_stock_status() -> None:
    assert stock_status(0, 3) == (StockStatus.out, None)
    assert stock_status(3, 3) == (StockStatus.low, 3)
    assert stock_status(4, 3) == (StockStatus.in_stock, None)


def test_every_error_code_has_an_arabic_message() -> None:
    for code in ErrorCode:
        message = MESSAGES[code]
        assert message and any("؀" <= ch <= "ۿ" for ch in message), code
