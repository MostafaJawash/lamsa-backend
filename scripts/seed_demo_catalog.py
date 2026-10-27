"""Demo catalog from docs/prototype/lamsa-presentation.html (development only).

- Refuses to run unless APP_ENV=development.
- Idempotent: existing demo items (slug "demo-..." / SKU "DEMO-...") are left as they are.
- --update rewrites the text fields of existing demo rows by slug without deleting anything.
- --reset removes only demo data (products, brands, the demo-gifts category, their images).
- Everything goes through the normal service layer, so all rules, SKUs and summaries apply.

Run with:  python -m scripts.seed_demo_catalog [--reset | --update]
Requires the base categories:  python -m scripts.seed_base
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.modules.brands.models import Brand
from app.modules.brands.schemas import BrandCreate, BrandUpdate
from app.modules.brands.service import AdminBrandService
from app.modules.categories.models import Category
from app.modules.categories.schemas import CategoryCreate, CategoryUpdate
from app.modules.categories.service import AdminCategoryService
from app.modules.products.enums import ProductType
from app.modules.products.models import Product
from app.modules.products.schemas import BundleItemIn, ProductCreate, ProductUpdate, VariantIn
from app.modules.products.service import AdminProductService

GIFTS_SLUG = "demo-gifts"
IMAGE_SIZE = 800

# ------------------------------------------------------------------------------ data

BRANDS: list[dict[str, Any]] = [
    {"id": "luma", "name": "لُمى", "description": "مستحضرات مكياج عصرية بألوان غنية.", "color": "#B8174E"},
    {"id": "bella", "name": "بيلا", "description": "أساسيات مكياج عملية لإطلالة يومية.", "color": "#7A4E9C"},
    {"id": "ward", "name": "وَرد", "description": "عناية لطيفة بالبشرة بمكونات مختارة.", "color": "#C8607F"},
    {"id": "nour", "name": "نُور", "description": "عطور ناعمة بنفحات زهرية دافئة.", "color": "#B88A2E"},
    {"id": "misk", "name": "مِسك", "description": "عطور يومية بنفحات شرقية منعشة.", "color": "#6B4430"},
    {"id": "sidr", "name": "سِدر", "description": "منتجات للعناية بالشعر وتصفيفه.", "color": "#3F7253"},
    {"id": "baity", "name": "بيتي", "description": "منتجات لمسة الأساسية بجودة موثوقة.", "color": "#2D1B2E", "own": True},
]

SKIN_TYPES = {"بشرة جافة": "جافة", "بشرة دهنية": "دهنية", "الجسم": "كل الأنواع"}
# صبغات (hair dye) maps to "مصبوغ" (colored hair) as the better match; تصفيف (styling) to "عادي".
HAIR_TYPES = {"شعر جاف": "جاف", "شعر دهني": "دهني", "صبغات": "مصبوغ", "تصفيف": "عادي"}


@dataclass
class DemoProduct:
    n: int
    slug: str
    name: str
    brand: str
    cat: str
    shape: str
    tint: str
    price: str
    desc: str
    ing: str
    color: str | None = None
    sale: str | None = None
    stock: int | None = None
    is_new: bool = False
    type: str | None = None  # prototype care/hair "type"
    finish: str | None = None
    variants: dict[str, Any] | None = None
    swatches: list[str] = field(default_factory=list)


P: list[DemoProduct] = [
    DemoProduct(1, "velvet-lipstick", "أحمر شفاه مخملي", "luma", "makeup", "lipstick", "#E9B7C0", "8.95", is_new=True, finish="مطفي",
                variants={"type": "color", "opts": [{"n": "وردي ترابي", "hex": "#B5656B", "stock": 12}, {"n": "أحمر كلاسيكي", "hex": "#B3202C", "stock": 3}, {"n": "نيود دافئ", "hex": "#C98F78", "stock": 0}]},
                desc="لون غني بلمسة مخملية مطفية، يدوم لساعات دون أن يُجفّف الشفاه.", ing="زبدة الشيا، فيتامين E، زيت الجوجوبا، صبغات معدنية."),
    DemoProduct(2, "light-foundation", "كريم أساس خفيف", "bella", "makeup", "tube", "#EED9C8", "12.45", finish="ساتان",
                variants={"type": "color", "opts": [{"n": "فاتح", "hex": "#E6C2A6", "stock": 8}, {"n": "متوسط", "hex": "#C99572", "stock": 10}, {"n": "داكن", "hex": "#8D5A3B", "stock": 5}]},
                desc="تغطية خفيفة وطبيعية توحّد لون البشرة، مع عامل حماية SPF 15.", ing="ماء، جلسرين، حمض الهيالورونيك، أكسيد الزنك."),
    DemoProduct(3, "eyeshadow-palette-6", "لوحة ظلال ٦ ألوان", "luma", "makeup", "palette", "#E3C9D6", "15.95", sale="11.95", stock=6,
                swatches=["#E8C2A8", "#C98F78", "#8E5A4E", "#D9B98C", "#7A4E9C", "#5A3A48"],
                desc="ألوان مطفية ولامعة تندمج بسهولة، لإطلالات النهار والسهرة.", ing="ميكا، تالك، زيت الأرغان، صبغات معدنية."),
    DemoProduct(4, "volume-mascara", "ماسكارا كثافة", "baity", "makeup", "mascara", "#D8CCD6", "4.45", color="#2D1B2E", is_new=True, stock=20,
                desc="فرشاة منحنية تكثّف الرموش وترفعها من الطبقة الأولى.", ing="شمع العسل، زيت الخروع، بروفيتامين B5."),
    DemoProduct(5, "pressed-powder", "بودرة مضغوطة", "baity", "makeup", "compact", "#F0DCCB", "5.25", sale="3.95", finish="مطفي",
                variants={"type": "color", "opts": [{"n": "فاتح", "hex": "#EED3BC", "stock": 4}, {"n": "متوسط", "hex": "#D3A784", "stock": 7}]},
                desc="تثبّت المكياج وتخفّف اللمعان طوال اليوم.", ing="سيليكا، نشا الأرز، فيتامين E."),
    DemoProduct(6, "lip-liner", "قلم تحديد شفاه", "bella", "makeup", "mascara", "#EBC6C6", "3.95",
                variants={"type": "color", "opts": [{"n": "وردي", "hex": "#C0727A", "stock": 9}, {"n": "بني", "hex": "#8A4F45", "stock": 9}]},
                desc="قلم ناعم يحدّد الشفاه بدقة ويمنع سيلان اللون.", ing="شمع الكانديليلا، زيت جوز الهند."),
    DemoProduct(7, "intense-moisturizer", "كريم ترطيب مكثّف", "ward", "care", "jar", "#E9C4CF", "6.95", color="#E7B9C8", type="بشرة جافة", is_new=True, stock=15,
                desc="ترطيب عميق لمدة ٢٤ ساعة للبشرة الجافة والحساسة.", ing="سيراميد، زبدة الشيا، ماء الورد، جلسرين."),
    DemoProduct(8, "green-tea-cleanser", "جل تنظيف بالشاي الأخضر", "ward", "care", "tube", "#CFE3D2", "4.75", color="#8DBE95", type="بشرة دهنية", stock=11,
                desc="ينظّف المسام ويخفّف الزيوت دون أن يسبّب جفاف البشرة.", ing="مستخلص الشاي الأخضر، حمض الساليسيليك، ألوفيرا."),
    DemoProduct(9, "vitamin-c-serum", "سيروم فيتامين C", "baity", "care", "serum", "#F3D9B0", "9.95", color="#E39A33", type="بشرة جافة", sale="7.45", stock=3,
                desc="يمنح البشرة إشراقة ويوحّد لونها مع الاستخدام اليومي.", ing="فيتامين C بنسبة ١٠٪، فيتامين E، حمض الفيروليك."),
    DemoProduct(10, "almond-body-lotion", "مرطّب جسم باللوز", "baity", "care", "bottle", "#EFE2D0", "2.95", color="#DCC3A4", type="الجسم", stock=30,
                desc="مرطّب خفيف سريع الامتصاص يترك البشرة ناعمة.", ing="زيت اللوز الحلو، جلسرين، فيتامين E."),
    DemoProduct(11, "jasmine-night-perfume", "عطر ليل الياسمين", "nour", "care", "perfume", "#E6D3E6", "19.95", color="#B98DB9", type="عطور", is_new=True,
                variants={"type": "size", "opts": [{"n": "30 ml", "price": "19.95", "stock": 6}, {"n": "50 ml", "price": "27.95", "stock": 4}, {"n": "100 ml", "price": "39.95", "stock": 2}]},
                desc="عطر زهري ناعم بنفحات الياسمين والفانيلا، يدوم طوال المساء.", ing="كحول، عطر، ماء. النفحات: ياسمين، برغموت، فانيلا."),
    DemoProduct(12, "wood-musk-perfume", "عطر خشب ومسك", "misk", "care", "perfume", "#E2CDBE", "24.95", color="#8A5A3C", type="عطور",
                variants={"type": "size", "opts": [{"n": "50 ml", "price": "24.95", "sale": "19.95", "stock": 5}, {"n": "100 ml", "price": "36.95", "sale": "29.95", "stock": 0}]},
                desc="عطر دافئ بنفحات خشب الصندل والمسك الأبيض.", ing="كحول، عطر، ماء. النفحات: صندل، مسك، عنبر."),
    DemoProduct(13, "fresh-body-mist", "بخاخ جسم منعش", "misk", "care", "perfume", "#D3E6EE", "6.95", color="#7FB3C9", type="عطور", sale="4.95", stock=0,
                desc="بخاخ خفيف ومنعش للاستعمال اليومي.", ing="كحول، ماء، عطر، ألوفيرا."),
    DemoProduct(14, "argan-shampoo", "شامبو بزيت الأرغان", "sidr", "hair", "bottle", "#F0DDBF", "3.95", color="#C98A3C", type="شعر جاف", is_new=True, stock=25,
                desc="يغذّي الشعر الجاف ويمنحه لمعانًا ونعومة.", ing="زيت الأرغان، بروتين القمح، بانثينول."),
    DemoProduct(15, "moisturizing-conditioner", "بلسم مرطّب", "sidr", "hair", "bottle", "#F2E4CF", "3.95", color="#D9B07E", type="شعر جاف", stock=18,
                desc="يفكّ التشابك ويرطّب الشعر من الجذور إلى الأطراف.", ing="زبدة الشيا، زيت جوز الهند، بانثينول."),
    DemoProduct(16, "oily-hair-shampoo", "شامبو للشعر الدهني", "baity", "hair", "bottle", "#D2E6DB", "1.95", color="#6FA38B", type="شعر دهني", stock=40,
                desc="ينظّف فروة الرأس بعمق ويحافظ على خفة الشعر مدة أطول.", ing="نعناع، شاي أخضر، زنك."),
    DemoProduct(17, "permanent-hair-color", "صبغة شعر دائمة", "baity", "hair", "box", "#E4D3C6", "4.95", type="صبغات", sale="3.45",
                variants={"type": "color", "opts": [{"n": "بني فاتح", "hex": "#8A5A3C", "stock": 6}, {"n": "أسود", "hex": "#2B2226", "stock": 9}, {"n": "أشقر عسلي", "hex": "#C9984F", "stock": 2}]},
                desc="لون ثابت لمدة تصل إلى ٨ أسابيع، مع تغطية كاملة للشعر الأبيض.", ing="زيت الأرغان، كيراتين، صبغات."),
    DemoProduct(18, "strong-styling-gel", "جل تصفيف قوي", "sidr", "hair", "jar", "#D5E3EF", "3.25", color="#7FA7C9", type="تصفيف", stock=14,
                desc="تثبيت قوي طوال اليوم دون ترك قشور.", ing="ماء، بانثينول، ألوفيرا."),
    DemoProduct(19, "hair-ends-oil", "زيت لأطراف الشعر", "sidr", "hair", "serum", "#F2E1BC", "8.45", color="#D9A441", type="شعر جاف", is_new=True, stock=7,
                desc="يعالج الأطراف المتقصفة ويحمي من حرارة مجفّف الشعر.", ing="زيت الأرغان، زيت الكاميليا، فيتامين E."),
]

BUNDLE = {
    "slug": "demo-care-box", "name": "علبة العناية", "price": "16.95", "components": [7, 9, 10],
    "desc": "علبة هدايا متكاملة للعناية: كريم الترطيب المكثّف، سيروم فيتامين C ومرطّب الجسم باللوز، بسعر أقل من مجموعها.",
}


# ---------------------------------------------------------------------------- images


def _rgb(hex_value: str) -> tuple[int, int, int]:
    h = hex_value.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shade(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)  # type: ignore[return-value]


def placeholder(shape: str, background: str, color: str, swatches: list[str] | None = None) -> bytes:
    """Soft background with a simple product silhouette (no text)."""
    s = IMAGE_SIZE
    image = Image.new("RGB", (s, s), _rgb(background))
    draw = ImageDraw.Draw(image)
    main, dark, light = _rgb(color), _shade(_rgb(color), 0.7), _shade(_rgb(background), 0.92)
    draw.ellipse((s * 0.18, s * 0.78, s * 0.82, s * 0.86), fill=light)  # floor shadow
    if shape == "lipstick":
        draw.rounded_rectangle((s * 0.40, s * 0.50, s * 0.60, s * 0.80), 14, fill=dark)
        draw.rectangle((s * 0.42, s * 0.36, s * 0.58, s * 0.52), fill=(214, 186, 120))
        draw.polygon([(s * 0.43, s * 0.36), (s * 0.57, s * 0.36), (s * 0.57, s * 0.20), (s * 0.43, s * 0.27)], fill=main)
    elif shape == "tube":
        draw.polygon([(s * 0.38, s * 0.18), (s * 0.62, s * 0.18), (s * 0.58, s * 0.66), (s * 0.42, s * 0.66)], fill=main)
        draw.rounded_rectangle((s * 0.43, s * 0.66, s * 0.57, s * 0.80), 10, fill=dark)
    elif shape == "palette":
        draw.rounded_rectangle((s * 0.18, s * 0.28, s * 0.82, s * 0.76), 30, fill=(60, 44, 52))
        colors = swatches or [color] * 6
        for i, swatch in enumerate(colors[:6]):
            cx, cy = s * (0.30 + (i % 3) * 0.20), s * (0.42 + (i // 3) * 0.20)
            draw.ellipse((cx - s * 0.07, cy - s * 0.07, cx + s * 0.07, cy + s * 0.07), fill=_rgb(swatch))
    elif shape == "mascara":
        draw.rounded_rectangle((s * 0.45, s * 0.14, s * 0.55, s * 0.48), 12, fill=dark)
        draw.rounded_rectangle((s * 0.44, s * 0.48, s * 0.56, s * 0.80), 12, fill=main)
    elif shape == "compact":
        draw.ellipse((s * 0.22, s * 0.30, s * 0.78, s * 0.80), fill=dark)
        draw.ellipse((s * 0.28, s * 0.36, s * 0.72, s * 0.74), fill=main)
    elif shape == "jar":
        draw.rounded_rectangle((s * 0.26, s * 0.40, s * 0.74, s * 0.80), 28, fill=main)
        draw.rounded_rectangle((s * 0.24, s * 0.30, s * 0.76, s * 0.42), 18, fill=dark)
    elif shape == "serum":
        draw.rounded_rectangle((s * 0.36, s * 0.40, s * 0.64, s * 0.80), 30, fill=main)
        draw.rectangle((s * 0.44, s * 0.30, s * 0.56, s * 0.40), fill=dark)
        draw.ellipse((s * 0.43, s * 0.16, s * 0.57, s * 0.32), fill=dark)
    elif shape == "perfume":
        draw.rounded_rectangle((s * 0.30, s * 0.38, s * 0.70, s * 0.80), 26, fill=main)
        draw.rectangle((s * 0.45, s * 0.30, s * 0.55, s * 0.38), fill=dark)
        draw.rounded_rectangle((s * 0.40, s * 0.18, s * 0.60, s * 0.30), 10, fill=(214, 186, 120))
    elif shape == "box":
        draw.rectangle((s * 0.26, s * 0.30, s * 0.74, s * 0.80), fill=main)
        draw.rectangle((s * 0.47, s * 0.30, s * 0.53, s * 0.80), fill=dark)
        draw.rectangle((s * 0.26, s * 0.44, s * 0.74, s * 0.50), fill=dark)
    else:  # bottle
        draw.rounded_rectangle((s * 0.34, s * 0.34, s * 0.66, s * 0.80), 30, fill=main)
        draw.rounded_rectangle((s * 0.42, s * 0.20, s * 0.58, s * 0.34), 10, fill=dark)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def logo(color: str) -> bytes:
    """Generated brand logo: a colored disc with a soft ring (no text)."""
    s = 400
    image = Image.new("RGB", (s, s), (250, 246, 244))
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 40, s - 40, s - 40), fill=_rgb(color))
    draw.ellipse((110, 110, s - 110, s - 110), outline=(250, 246, 244), width=14)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ----------------------------------------------------------------------------- build


def _attributes(p: DemoProduct) -> dict[str, Any]:
    attrs: dict[str, Any] = {"ingredients": p.ing}
    if p.cat == "makeup" and p.finish:
        attrs["finish"] = p.finish
    if p.cat == "care" and p.type in SKIN_TYPES:
        attrs["skin_type"] = SKIN_TYPES[p.type]
    if p.type == "عطور":
        attrs["notes"] = p.ing.split("النفحات:")[-1].strip(" .") if "النفحات:" in p.ing else p.desc
        attrs["concentration"] = "Body Mist" if "بخاخ" in p.name else "EDP"
    if p.cat == "hair" and p.type in HAIR_TYPES:
        attrs["hair_type"] = HAIR_TYPES[p.type]
    return attrs


def _variants(p: DemoProduct) -> list[VariantIn]:
    assert p.variants is not None
    result = []
    for k, opt in enumerate(p.variants["opts"], start=1):
        if p.variants["type"] == "color":
            options = {"color": opt["n"], "color_hex": opt["hex"]}
            price, sale = Decimal(p.price), Decimal(p.sale) if p.sale else None
        else:
            options = {"size": opt["n"]}
            price, sale = Decimal(opt["price"]), Decimal(opt["sale"]) if opt.get("sale") else None
        result.append(VariantIn(options=options, sku=f"DEMO-{p.n:02d}-{k}", price=price, sale_price=sale,
                                stock=opt["stock"], sort_order=k))
    return result


async def _existing_product(session: AsyncSession, slug: str) -> Product | None:
    return await session.scalar(select(Product).where(Product.slug == slug))


async def seed(session: AsyncSession, *, update: bool = False) -> dict[str, int]:
    categories = {c.slug: c for c in (await session.scalars(select(Category))).all()}
    missing = {"makeup", "care", "perfumes", "hair"} - set(categories)
    if missing:
        raise SystemExit(f"Base categories missing ({', '.join(sorted(missing))}): run python -m scripts.seed_base first")
    stats = {"brands": 0, "products": 0, "images": 0, "updated": 0, "skipped": 0}

    brand_ids: dict[str, int] = {}
    brands_service = AdminBrandService(session)
    for b in BRANDS:
        slug = f"demo-{b['id']}"
        existing = await session.scalar(select(Brand).where(Brand.slug == slug))
        if existing is None:
            created = await brands_service.create(BrandCreate(
                name=b["name"], slug=slug, description=b["description"], is_own_brand=b.get("own", False)
            ))
            await brands_service.set_logo(created.id, logo(b["color"]))
            brand_ids[b["id"]] = created.id
            stats["brands"] += 1
            stats["images"] += 1
        else:
            brand_ids[b["id"]] = existing.id
            if update:
                await brands_service.update(
                    existing.id, BrandUpdate(name=b["name"], description=b["description"])
                )
                stats["updated"] += 1

    gifts = categories.get(GIFTS_SLUG)
    if gifts is None:
        detail = await AdminCategoryService(session).create(
            CategoryCreate(name="علبة هدايا", slug=GIFTS_SLUG, sort_order=10)
        )
        gifts_id = detail.id
    else:
        gifts_id = gifts.id
        if update:
            await AdminCategoryService(session).update(
                gifts.id, CategoryUpdate(name="علبة هدايا")
            )
            stats["updated"] += 1

    products = AdminProductService(session)
    product_ids: dict[int, int] = {}
    for p in P:
        slug = f"demo-{p.slug}"
        existing = await _existing_product(session, slug)
        if existing is not None:
            product_ids[p.n] = existing.id
            if update:
                await products.update(
                    existing.id,
                    ProductUpdate(name=p.name, description=p.desc, attributes=_attributes(p)),
                )
                stats["updated"] += 1
            else:
                stats["skipped"] += 1
            continue
        category_slug = "perfumes" if p.type == "عطور" else p.cat
        is_variable = p.variants is not None
        created = await products.create(ProductCreate(
            type=ProductType.variable if is_variable else ProductType.simple, name=p.name, slug=slug,
            description=p.desc, category_id=categories[category_slug].id, brand_id=brand_ids[p.brand],
            sku=None if is_variable else f"DEMO-{p.n:02d}",
            price=None if is_variable else Decimal(p.price),
            sale_price=None if is_variable or not p.sale else Decimal(p.sale),
            stock=None if is_variable else (p.stock or 0),
            attributes=_attributes(p), is_new=p.is_new,
            variants=_variants(p) if is_variable else None,
        ))
        product_ids[p.n] = created.id
        base_color = p.color or (p.variants["opts"][0].get("hex") if p.variants and p.variants["type"] == "color" else None) or "#B8174E"
        await products.upload_images(created.id, [placeholder(p.shape, p.tint, base_color, p.swatches)], None, None)
        stats["images"] += 1
        if p.variants and p.variants["type"] == "color":
            for opt in p.variants["opts"]:
                await products.upload_images(created.id, [placeholder(p.shape, p.tint, opt["hex"])], None, opt["n"])
                stats["images"] += 1
        await products.update(created.id, ProductUpdate(is_active=True))
        stats["products"] += 1

    existing_bundle = await _existing_product(session, BUNDLE["slug"])
    if existing_bundle is None:
        bundle = await products.create(ProductCreate(
            type=ProductType.bundle, name=BUNDLE["name"], slug=BUNDLE["slug"], description=BUNDLE["desc"],
            category_id=gifts_id, sku="DEMO-BOX-1", price=Decimal(BUNDLE["price"]),
            bundle_items=[BundleItemIn(product_id=product_ids[n]) for n in BUNDLE["components"]],
        ))
        await products.upload_images(bundle.id, [placeholder("box", "#F3E3E8", "#B8174E")], None, None)
        await products.update(bundle.id, ProductUpdate(is_active=True))
        stats["products"] += 1
        stats["images"] += 1
    elif update:
        await products.update(
            existing_bundle.id,
            ProductUpdate(name=BUNDLE["name"], description=BUNDLE["desc"], category_id=gifts_id),
        )
        stats["updated"] += 1
    else:
        stats["skipped"] += 1
    return stats


async def reset(session: AsyncSession) -> dict[str, int]:
    """Remove demo data only. Bundles first (their components are RESTRICTed while referenced)."""
    products = AdminProductService(session)
    rows = (await session.execute(
        select(Product.id, Product.type).where(Product.slug.like("demo-%"))
    )).all()
    ordered = sorted(rows, key=lambda r: 0 if r.type == ProductType.bundle else 1)
    for row in ordered:
        await products.delete(row.id)
    brands = AdminBrandService(session)
    brand_ids = (await session.scalars(select(Brand.id).where(Brand.slug.like("demo-%")))).all()
    for brand_id in brand_ids:
        await brands.delete(brand_id)
    gifts = await session.scalar(select(Category.id).where(Category.slug == GIFTS_SLUG))
    if gifts is not None:
        await AdminCategoryService(session).delete(gifts)
    return {"products": len(rows), "brands": len(brand_ids), "categories": int(gifts is not None)}


async def main(*, do_reset: bool, do_update: bool) -> int:
    if settings.APP_ENV != "development":
        print(f"Refusing to run: APP_ENV={settings.APP_ENV!r} (demo data is for development only).")
        return 2
    engine.echo = False
    try:
        async with SessionLocal() as session:
            if do_reset:
                print("Removed demo data:", await reset(session))
            else:
                stats = await seed(session, update=do_update)
                print(
                    f"Demo catalog: {stats['brands']} brands and {stats['products']} products created, "
                    f"{stats['images']} images, {stats['updated']} updated, "
                    f"{stats['skipped']} already present."
                )
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed, update or reset the demo catalog.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="Remove only demo data")
    mode.add_argument("--update", action="store_true", help="Update demo text fields in place")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(do_reset=args.reset, do_update=args.update)))
