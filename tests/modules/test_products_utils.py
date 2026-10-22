from app.modules.products.utils import build_options_signature


def test_signature_is_order_independent() -> None:
    a = build_options_signature({"size": "50 ml", "color": "أحمر كلاسيكي"})
    b = build_options_signature({"color": "أحمر كلاسيكي", "size": "50 ml"})
    assert a == b == "color=أحمر كلاسيكي|size=50 ml"


def test_signature_ignores_hex_keys() -> None:
    with_hex = build_options_signature({"color": "أحمر كلاسيكي", "color_hex": "#B3202C"})
    without_hex = build_options_signature({"color": "أحمر كلاسيكي"})
    assert with_hex == without_hex == "color=أحمر كلاسيكي"


def test_signature_of_single_option() -> None:
    assert build_options_signature({"size": "50 ml"}) == "size=50 ml"
