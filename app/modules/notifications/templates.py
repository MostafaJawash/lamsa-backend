"""ALL notification texts (Arabic), in one place. Services never build texts themselves."""

from dataclasses import dataclass

TITLE_MAX = 150
BODY_MAX = 500


@dataclass(frozen=True)
class Text:
    title: str
    body: str

    def clipped(self) -> "Text":
        return Text(self.title[:TITLE_MAX], self.body[:BODY_MAX])


def _product(name: str, variant_label: str | None) -> str:
    return f"{name} ({variant_label})" if variant_label else name


# ------------------------------------------------------------- customer: order updates


def order_placed(order_id: int) -> Text:
    return Text(f"استلمنا طلبك #{order_id} ✅", "رح نبلّش نجهّزه بأقرب وقت.").clipped()


def order_preparing(order_id: int) -> Text:
    return Text(f"طلبك #{order_id} قيد التجهيز", "عم نجهّز طلبك هلّق.").clipped()


def order_shipped(order_id: int) -> Text:
    return Text(f"طلبك #{order_id} انشحن 🚚", "طلبك صار بالطريق إلك.").clipped()


def order_ready_for_pickup(order_id: int) -> Text:
    return Text(f"طلبك #{order_id} جاهز للاستلام 🛍️", "فيك تمرّ تستلمه من المتجر.").clipped()


def order_delivered(order_id: int, is_pickup: bool) -> Text:
    if is_pickup:
        return Text(f"تم استلام طلبك #{order_id}، شكراً إلك", "نتمنى تعجبك المنتجات.").clipped()
    return Text(f"وصل طلبك #{order_id}، نتمنى تعجبك المنتجات", "شكراً لأنك تسوّقت من لمسة.").clipped()


def order_cancelled_by_store(order_id: int, reason: str | None) -> Text:
    return Text(f"انلغى طلبك #{order_id}", reason or "تواصل معنا إذا عندك أي سؤال.").clipped()


# ------------------------------------------------------------------------- admin alerts


def admin_new_order(order_id: int, items_count: int, total: str, delivery_method: str) -> Text:
    return Text(f"طلب جديد #{order_id}", f"{items_count} منتجات · {total} € · {delivery_method}").clipped()


def admin_customer_cancelled(order_id: int, reason: str | None) -> Text:
    return Text(f"الزبون لغى الطلب #{order_id}", reason or "بدون سبب.").clipped()


def admin_stock_low(name: str, variant_label: str | None, stock: int) -> Text:
    return Text(f"{_product(name, variant_label)}: باقي {stock} قطع", "صار المخزون قليل.").clipped()


def admin_stock_out(name: str, variant_label: str | None) -> Text:
    return Text(f"{_product(name, variant_label)} خلص من المخزون", "المنتج ما عاد متوفر للبيع.").clipped()


# ------------------------------------------------------------------------- smart alerts


def favorite_back_in_stock(name: str) -> Text:
    return Text("رجع للمخزون 🎉", f"{name} اللي بالمفضلة رجع متوفر.").clipped()


def favorite_price_drop(name: str, price: str) -> Text:
    return Text("نزل السعر 🔥", f"{name} اللي بالمفضلة صار بـ {price} €.").clipped()


def cart_reminder(first_product: str, others: int) -> Text:
    if others > 0:
        body = f"{first_product} و{others} منتجات تانية ناطرينك بالسلة."
    else:
        body = f"{first_product} ناطرك بالسلة."
    return Text("سلتك ناطرتك 🛍️", body).clipped()
