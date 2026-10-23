"""Load-test data for the DEDICATED load database (DB_NAME must end in "_load").

  python -m scripts.seed_load_data                  # 3,000 products, 10,000 users (idempotent)
  python -m scripts.seed_load_data --reset          # remove all load data first, then seed again
  python -m scripts.seed_load_data --flash-stock 50 # reset the flash-sale product (stock, its orders)
  python -m scripts.seed_load_data --verify-flash 50  # after a run: stock never negative, sold == stock
  python -m scripts.seed_load_data --broadcast-report # duration and counts of the latest campaign

Everything is marked: product/brand slugs "load-...", phones +4915123400000..+4915123409999 and the
admin +4915123500000. All users share the password LOAD_PASSWORD (one precomputed hash, so seeding
takes seconds). Products share 12 placeholder image files. Run `python -m scripts.seed_base` first.
"""

import argparse
import asyncio
import io
import random
import sys
from decimal import Decimal
from typing import Any

from PIL import Image
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock, security
from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.integrations.storage.transaction import get_image_service
from app.modules.brands.models import Brand
from app.modules.cart.models import CartItem
from app.modules.categories.models import Category, CategoryAttribute, CategoryVariantOption
from app.modules.favorites.models import Favorite
from app.modules.notifications.models import Notification
from app.modules.orders.models import Order, OrderItem, OrderStatusHistory
from app.modules.products.models import BundleItem, Product, ProductImage, ProductVariant
from app.modules.products.summary import refresh_products
from app.modules.products.utils import build_options_signature
from app.modules.users.models import User, UserAddress, UserDevice

PRODUCTS = 3000
USERS = 10_000
BRANDS = 20
LOAD_PASSWORD = "LoadTest-pass1"
PHONE_BASE = 4915123400000
ADMIN_PHONE = "+4915123500000"
FLASH_SLUG = "load-flash-sale"
CHUNK = 500
RNG = random.Random(20261023)

WORDS = ["أحمر شفاه", "كريم مرطب", "عطر", "شامبو", "بلسم", "ماسكارا", "كحل", "سيروم", "زيت شعر", "بودرة",
         "ظل عيون", "غسول", "واقي شمس", "مزيل مكياج", "بخاخ", "صبغة شعر", "ملمع شفاه", "كونسيلر"]
COLORS = [("أحمر", "#C0392B"), ("وردي", "#F1948A"), ("نود", "#D7B49E"), ("بني", "#6E2C00"), ("أسود", "#1C1C1C"), ("ذهبي", "#D4AC0D")]
SIZES = ["50 ml", "100 ml", "200 ml"]


def phone(i: int) -> str:
    return f"+{PHONE_BASE + i}"


def refuse_unless_load_database() -> None:
    if not settings.DB_NAME.endswith("_load"):
        sys.exit(f"Refusing to run: DB_NAME={settings.DB_NAME!r} must end with '_load' (never lamsa or lamsa_test).")


def placeholder_png(n: int) -> bytes:
    hue = [(200, 120, 140), (120, 160, 200), (230, 200, 150), (150, 200, 160), (190, 150, 210), (240, 170, 120)][n % 6]
    shade = 0.75 + 0.25 * (n // 6)
    image = Image.new("RGB", (600, 600), tuple(int(c * shade) for c in hue))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


async def chunked_insert(session: AsyncSession, model: Any, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), CHUNK):
        await session.execute(insert(model).values(rows[start : start + CHUNK]))


# --------------------------------------------------------------------------- reset


async def reset(session: AsyncSession) -> None:
    load_users = select(User.id).where(User.phone.like("+491512340%") | (User.phone == ADMIN_PHONE)).scalar_subquery()
    load_products = select(Product.id).where(Product.slug.like("load-%")).scalar_subquery()
    load_orders = select(Order.id).where(Order.user_id.in_(load_users)).scalar_subquery()
    for statement in (
        delete(OrderStatusHistory).where(OrderStatusHistory.order_id.in_(load_orders)),
        delete(OrderItem).where(OrderItem.order_id.in_(load_orders)),
        delete(Order).where(Order.user_id.in_(load_users)),
        delete(Notification),  # the load database only
        delete(CartItem).where(CartItem.user_id.in_(load_users)),
        delete(Favorite).where(Favorite.user_id.in_(load_users)),
        delete(User).where(User.phone.like("+491512340%") | (User.phone == ADMIN_PHONE)),
        delete(BundleItem).where(BundleItem.bundle_product_id.in_(load_products)),
        delete(ProductImage).where(ProductImage.product_id.in_(load_products)),
        delete(ProductVariant).where(ProductVariant.product_id.in_(load_products)),
        delete(Product).where(Product.slug.like("load-%")),
        delete(Brand).where(Brand.slug.like("load-%")),
        text("DELETE FROM login_attempts"),
    ):
        await session.execute(statement)
    await session.commit()
    print("Load data removed.")


# ------------------------------------------------------------------------ products


async def seed_catalog(session: AsyncSession) -> None:
    existing = await session.scalar(select(func.count()).select_from(Product).where(Product.slug.like("load-%")))
    if existing:
        print(f"Products: {existing} load products already there, skipped.")
        return
    categories = list((await session.scalars(select(Category).order_by(Category.id))).all())
    if not categories:
        sys.exit("No categories: run python -m scripts.seed_base first.")
    attributes: dict[int, list[CategoryAttribute]] = {}
    for a in await session.scalars(select(CategoryAttribute)):
        attributes.setdefault(a.category_id, []).append(a)
    options: dict[int, list[str]] = {}
    for o in await session.scalars(select(CategoryVariantOption)):
        options.setdefault(o.category_id, []).append(o.key)

    await session.execute(insert(Brand).values([
        {"name": f"Load Brand {b}", "slug": f"load-brand-{b}", "is_active": True} for b in range(BRANDS)
    ]))
    brand_ids = list((await session.scalars(select(Brand.id).where(Brand.slug.like("load-%")).order_by(Brand.id))).all())

    images = get_image_service()
    image_keys = [(await images.save("load/shared", placeholder_png(n))).image_key for n in range(12)]

    now = clock.utcnow()
    rows: list[dict[str, Any]] = []
    variant_specs: dict[str, list[dict[str, Any]]] = {}
    bundle_slugs: list[str] = []
    for i in range(PRODUCTS):
        category = categories[i % len(categories)]
        own_options = options.get(category.id, []) or options.get(category.parent_id or -1, [])
        kind = "bundle" if i % 10 == 9 else "variable" if (i % 10 < 3 and own_options and i != 0) else "simple"  # 0: flash sale
        slug = FLASH_SLUG if i == 0 else f"load-{i}"
        attrs: dict[str, Any] = {}
        for a in attributes.get(category.id, []) + attributes.get(category.parent_id or -1, []):
            if a.options and a.type.value == "select":
                attrs[a.key] = RNG.choice(a.options)
        price = Decimal(RNG.randrange(500, 8000)) / 100
        on_sale = RNG.random() < 0.25
        row = {
            "name": "عرض فلاش" if i == 0 else f"{RNG.choice(WORDS)} {i}", "slug": slug, "description": "منتج تجريبي لاختبار الحمل.",
            "category_id": category.id, "brand_id": RNG.choice(brand_ids), "type": kind, "attributes": attrs,
            "sku": None, "price": None, "sale_price": None, "stock": None, "is_new": RNG.random() < 0.15,
            "is_active": True, "search_text": "", "created_at": now, "updated_at": now,
        }
        if kind == "simple":
            row.update(sku=f"LOAD-{i}", price=price, sale_price=(price * Decimal("0.8")).quantize(Decimal("0.01")) if on_sale else None,
                       stock=0 if RNG.random() < 0.1 else RNG.randrange(1, 60))
        elif kind == "bundle":
            row.update(sku=f"LOAD-{i}", price=price + Decimal(20))
            bundle_slugs.append(slug)
        else:
            specs = []
            colors = RNG.sample(COLORS, 3) if "color" in own_options else [None]
            sizes = SIZES[: RNG.randrange(1, 4)] if "size" in own_options else [None]
            n = 0
            for color in colors:
                for size in sizes:
                    opts: dict[str, Any] = {}
                    if color:
                        opts.update(color=color[0], color_hex=color[1])
                    if size:
                        opts["size"] = size
                    specs.append({"sku": f"LOAD-{i}-{n}", "options": opts, "options_signature": build_options_signature(opts),
                                  "price": price, "sale_price": (price * Decimal("0.85")).quantize(Decimal("0.01")) if on_sale and n == 0 else None,
                                  "stock": RNG.randrange(0, 30), "is_active": True, "sort_order": n,
                                  "created_at": now, "updated_at": now})
                    n += 1
            variant_specs[slug] = specs
        rows.append(row)
    await chunked_insert(session, Product, rows)
    ids = {slug: pid for slug, pid in (await session.execute(select(Product.slug, Product.id).where(Product.slug.like("load-%")))).all()}

    variants = [{**spec, "product_id": ids[slug]} for slug, specs in variant_specs.items() for spec in specs]
    await chunked_insert(session, ProductVariant, variants)
    image_rows = [
        {"product_id": pid, "variant_id": None, "image_key": image_keys[(pid + k) % len(image_keys)], "alt_text": None,
         "width": 600, "height": 600, "sort_order": k, "created_at": now}
        for pid in ids.values() for k in range(2)
    ]
    await chunked_insert(session, ProductImage, image_rows)
    simple_ids = [ids[r["slug"]] for r in rows if r["type"] == "simple" and r["slug"] != FLASH_SLUG]
    bundle_rows = []
    for slug in bundle_slugs:
        for component in RNG.sample(simple_ids, RNG.randrange(2, 4)):
            bundle_rows.append({"bundle_product_id": ids[slug], "product_id": component, "variant_id": None, "quantity": RNG.randrange(1, 3)})
    await chunked_insert(session, BundleItem, bundle_rows)
    await session.commit()

    all_ids = sorted(ids.values())
    for start in range(0, len(all_ids), 200):  # summaries (price_from, in_stock, is_on_sale) and search text
        await refresh_products(session, all_ids[start : start + 200])
        await session.commit()
    session.info.clear()  # stashed cache events: nothing to publish from a script
    print(f"Products: {len(rows)} ({len(variants)} variants, {len(bundle_rows)} bundle items, {len(image_rows)} images).")


# --------------------------------------------------------------------------- users


async def seed_users(session: AsyncSession) -> None:
    existing = await session.scalar(select(func.count()).select_from(User).where(User.phone.like("+491512340%")))
    if existing:
        print(f"Users: {existing} load users already there, skipped.")
        return
    password_hash = security.build_password_hasher().hash(LOAD_PASSWORD)  # one hash for all
    now = clock.utcnow()
    users = [{"name": f"مستخدم تجريبي {i}", "phone": phone(i), "auth_provider": "phone", "password_hash": password_hash,
              "role": "customer", "is_active": True, "created_at": now, "updated_at": now} for i in range(USERS)]
    users.append({"name": "Load Admin", "phone": ADMIN_PHONE, "auth_provider": "phone", "password_hash": password_hash,
                  "role": "admin", "is_active": True, "created_at": now, "updated_at": now})
    await chunked_insert(session, User, users)
    user_ids = list((await session.scalars(select(User.id).where(User.phone.like("+491512340%")).order_by(User.phone))).all())
    await chunked_insert(session, UserDevice, [
        {"user_id": uid, "fcm_token": f"load-fcm-{uid}", "platform": "android" if uid % 3 else "ios", "created_at": now, "last_seen_at": now}
        for uid in user_ids
    ])
    await chunked_insert(session, UserAddress, [
        {"user_id": uid, "label": "البيت", "city": "Berlin", "street": f"Teststr. {uid % 200}", "is_default": True,
         "created_at": now, "updated_at": now}
        for uid in user_ids
    ])
    visible = list((await session.scalars(
        select(Product.id).where(Product.slug.like("load-%"), Product.slug != FLASH_SLUG, Product.in_stock.is_(True), Product.type == "simple")
    )).all())
    favorites = [{"user_id": uid, "product_id": pid, "created_at": now}
                 for uid in user_ids if RNG.random() < 0.3 for pid in RNG.sample(visible, 3)]
    await chunked_insert(session, Favorite, favorites)
    carts = [{"user_id": uid, "product_id": pid, "variant_id": None, "quantity": 1, "created_at": now, "updated_at": now}
             for uid in user_ids if RNG.random() < 0.2 for pid in RNG.sample(visible, RNG.randrange(1, 4))]
    await chunked_insert(session, CartItem, carts)
    await session.commit()
    print(f"Users: {len(user_ids)} + 1 admin ({len(favorites)} favorites, {len(carts)} cart lines).")


# ------------------------------------------------------------------------ flash sale


async def reset_flash(session: AsyncSession, stock: int) -> None:
    pid = await session.scalar(select(Product.id).where(Product.slug == FLASH_SLUG))
    if pid is None:
        sys.exit("No flash-sale product: seed first.")
    orders = select(OrderItem.order_id).where(OrderItem.product_id == pid).scalar_subquery()
    await session.execute(delete(OrderStatusHistory).where(OrderStatusHistory.order_id.in_(orders)))
    flash_orders = list((await session.scalars(select(OrderItem.order_id).where(OrderItem.product_id == pid))).all())
    await session.execute(delete(OrderItem).where(OrderItem.order_id.in_(flash_orders)))
    await session.execute(delete(Order).where(Order.id.in_(flash_orders)))
    await session.execute(delete(CartItem).where(CartItem.product_id == pid))
    await session.execute(update(Product).where(Product.id == pid).values(stock=stock))
    await refresh_products(session, [pid])
    await session.commit()
    session.info.clear()
    print(f"Flash-sale product {pid}: stock {stock}, previous flash orders removed.")


async def verify_flash(session: AsyncSession, initial: int) -> bool:
    pid = await session.scalar(select(Product.id).where(Product.slug == FLASH_SLUG))
    stock = await session.scalar(select(Product.stock).where(Product.id == pid))
    sold = int(await session.scalar(select(func.coalesce(func.sum(OrderItem.quantity), 0)).where(OrderItem.product_id == pid)) or 0)
    orders = int(await session.scalar(select(func.count(func.distinct(OrderItem.order_id))).where(OrderItem.product_id == pid)) or 0)
    ok = stock is not None and stock >= 0 and sold == initial - stock
    print(f"FLASH initial={initial} remaining={stock} sold={sold} orders={orders} -> {'OK' if ok else 'FAILED'}")
    return ok


async def broadcast_report(session: AsyncSession) -> None:
    n = await session.scalar(
        select(Notification).where(Notification.type == "marketing").order_by(Notification.id.desc()).limit(1)
    )
    if n is None:
        print("BROADCAST none found")
        return
    took = (n.sent_at - n.created_at).total_seconds() if n.sent_at else None
    print(f"BROADCAST id={n.id} status={n.status} recipients={n.recipients_count} success={n.success_count} "
          f"failure={n.failure_count} created_at={n.created_at} sent_at={n.sent_at} duration_s={took}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--flash-stock", type=int)
    parser.add_argument("--verify-flash", type=int, metavar="INITIAL_STOCK")
    parser.add_argument("--broadcast-report", action="store_true")
    args = parser.parse_args()
    refuse_unless_load_database()
    engine.echo = False
    try:
        async with SessionLocal() as session:
            if args.broadcast_report:
                await broadcast_report(session)
                return
            if args.verify_flash is not None:
                sys.exit(0 if await verify_flash(session, args.verify_flash) else 1)
            if args.flash_stock is not None:
                await reset_flash(session, args.flash_stock)
                return
            if args.reset:
                await reset(session)
            await seed_catalog(session)
            await seed_users(session)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
