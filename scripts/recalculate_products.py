"""Recompute price_from / in_stock / is_on_sale and search_text for every product (idempotent).

Run with:  python -m scripts.recalculate_products
"""

import asyncio

from sqlalchemy import select

from app.db.session import SessionLocal, engine
from app.modules.products.models import Product
from app.modules.products.summary import refresh_products

BATCH_SIZE = 200


async def recalculate_all() -> int:
    async with SessionLocal() as session:
        ids = list((await session.scalars(select(Product.id).order_by(Product.id))).all())
        for start in range(0, len(ids), BATCH_SIZE):
            await refresh_products(session, ids[start : start + BATCH_SIZE])
            await session.commit()
    return len(ids)


async def main() -> None:
    engine.echo = False
    try:
        count = await recalculate_all()
    finally:
        await engine.dispose()
    print(f"Recalculated {count} products.")


if __name__ == "__main__":
    asyncio.run(main())
