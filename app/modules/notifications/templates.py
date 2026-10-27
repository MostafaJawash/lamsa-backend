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


def _products_count(count: int) -> str:
    if count == 1:
        return "منتج واحد"
    if count == 2:
        return "منتجان"
    if 3 <= count % 100 <= 10:
        return f"{count} منتجات"
    if 11 <= count % 100 <= 99:
        return f"{count} منتجًا"
    return f"{count} منتج"


def _pieces_count(count: int) -> str:
    if count == 1:
        return "قطعة واحدة"
    if count == 2:
        return "قطعتان"
    if 3 <= count % 100 <= 10:
        return f"{count} قطع"
    return f"{count} قطعة"


def _other_products_count(count: int) -> str:
    if count == 1:
        return "منتج آخر"
    if count == 2:
        return "منتجان آخران"
    if 3 <= count % 100 <= 10:
        return f"{count} منتجات أخرى"
    if 11 <= count % 100 <= 99:
        return f"{count} منتجًا آخر"
    return f"{count} منتج آخر"


# ------------------------------------------------------------- customer: order updates


def order_placed(order_id: int) -> Text:
    return Text(f"تم استلام طلبك #{order_id} ✅", "سنبدأ تجهيزه في أقرب وقت.").clipped()


def order_preparing(order_id: int) -> Text:
    return Text(f"طلبك #{order_id} قيد التجهيز", "نعمل على تجهيز طلبك الآن.").clipped()


def order_shipped(order_id: int) -> Text:
    return Text(f"تم شحن طلبك #{order_id} 🚚", "طلبك في الطريق إليك.").clipped()


def order_ready_for_pickup(order_id: int) -> Text:
    return Text(f"طلبك #{order_id} جاهز للاستلام 🛍️", "يمكنك استلامه من المتجر.").clipped()


def order_delivered(order_id: int, is_pickup: bool) -> Text:
    if is_pickup:
        return Text(f"تم استلام طلبك #{order_id}، شكرًا لك", "نتمنى أن تنال المنتجات إعجابك.").clipped()
    return Text(f"تم توصيل طلبك #{order_id}، نتمنى أن تنال المنتجات إعجابك", "شكرًا لتسوّقك من لمسة.").clipped()


def order_cancelled_by_store(order_id: int, reason: str | None) -> Text:
    return Text(f"أُلغي طلبك #{order_id}", reason or "تواصل معنا إذا كان لديك أي سؤال.").clipped()


# ------------------------------------------------------------------------- admin alerts


def admin_new_order(order_id: int, items_count: int, total: str, delivery_method: str) -> Text:
    return Text(f"طلب جديد #{order_id}", f"{_products_count(items_count)} · {total} € · {delivery_method}").clipped()


def admin_customer_cancelled(order_id: int, reason: str | None) -> Text:
    return Text(f"ألغى العميل الطلب #{order_id}", reason or "من دون سبب.").clipped()


def admin_stock_low(name: str, variant_label: str | None, stock: int) -> Text:
    return Text(f"{_product(name, variant_label)}: تبقّى {_pieces_count(stock)}", "أصبح المخزون منخفضًا.").clipped()


def admin_stock_out(name: str, variant_label: str | None) -> Text:
    return Text(f"نفد مخزون {_product(name, variant_label)}", "لم يعد المنتج متوفرًا للبيع.").clipped()


# ------------------------------------------------------------------------- smart alerts


def favorite_back_in_stock(name: str) -> Text:
    return Text("عاد إلى المخزون 🎉", f"يتوفر {name} الموجود في المفضلة من جديد.").clipped()


def favorite_price_drop(name: str, price: str) -> Text:
    return Text("انخفض السعر 🔥", f"أصبح سعر {name} الموجود في المفضلة {price} €.").clipped()


def cart_reminder(first_product: str, others: int) -> Text:
    if others > 0:
        body = f"{first_product} و{_other_products_count(others)} بانتظارك في السلة."
    else:
        body = f"{first_product} بانتظارك في السلة."
    return Text("سلتك بانتظارك 🛍️", body).clipped()
