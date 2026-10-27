"""Cart business rules. Each public method is one transaction and commits itself.

- Every mutation first locks the user's row (SELECT ... FOR UPDATE), which serialises one
  user's cart operations; parallel duplicate inserts and deadlocks are retried once anyway.
- The cart does NOT reserve stock: stock is checked on add/update and reported on every read.
- Prices are never stored: every read computes them live with the catalog's price_info().
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TypeVar

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.cart import repository as repo
from app.modules.cart.repository import LineRef, LineRow, TargetProduct
from app.modules.cart.schemas import (
    CartBrand,
    CartBundleItem,
    CartCountOut,
    CartLineOut,
    CartMergeItem,
    CartMergeOut,
    CartNoticeOut,
    CartOut,
    CartPreviewItem,
    CartPreviewLineOut,
    CartPreviewOut,
    CartProduct,
    CartVariant,
    DeliveryQuote,
    LineIssue,
    MergeReportItem,
)
from app.modules.delivery.models import DeliveryMethod
from app.modules.products import repository as product_repo
from app.modules.products.enums import ProductType
from app.modules.products.presenters import (
    LoadedImage,
    LoadedVariant,
    color_image_ids,
    color_key_of,
    image_colors,
    PriceInfo,
    main_image,
    price_info,
    stock_status,
)
from app.modules.products.repository import BundleComponentRow, derived_bundle_stock
from app.modules.products.validation import variant_label
from app.modules.settings.service import StoreLimits, load_limits
from app.modules.users.models import User
from app.shared.exceptions import (
    ISSUE_MESSAGES,
    NOTICE_MESSAGES,
    AppError,
    CartIssue,
    CartNotice,
    Conflict,
    ErrorCode,
    NotFound,
    ServiceBusy,
    ValidationFailed,
    insufficient_stock,
    out_of_stock,
)
from app.shared.schemas import image_urls

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
DEADLOCK = 1213
DEADLOCK_ATTEMPTS = 3  # in all, for one write transaction
REMOVABLE_ISSUES = {CartIssue.UNAVAILABLE, CartIssue.VARIANT_UNAVAILABLE, CartIssue.OUT_OF_STOCK}
T = TypeVar("T")


# =============================================================================== loading


class CartLoader:
    """Per-request cache so variants and bundle components are loaded at most once."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.variants: dict[int, list[LoadedVariant]] = {}
        self.components: dict[int, list[BundleComponentRow]] = {}

    async def ensure_variants(self, product_ids: set[int]) -> None:
        missing = sorted(product_ids - set(self.variants))
        if missing:
            loaded = await product_repo.variants_of(self.session, missing, active_only=False)
            for product_id in missing:
                self.variants[product_id] = loaded.get(product_id, [])

    async def ensure_components(self, bundle_ids: set[int]) -> None:
        missing = sorted(bundle_ids - set(self.components))
        if missing:
            loaded = await product_repo.bundle_components_for(self.session, missing)
            for bundle_id in missing:
                self.components[bundle_id] = loaded.get(bundle_id, [])

    def variant(self, product_id: int, variant_id: int | None) -> LoadedVariant | None:
        return next((v for v in self.variants.get(product_id, []) if v.id == variant_id), None)

    def bundle_stock(self, bundle_id: int) -> int:
        return derived_bundle_stock(self.components.get(bundle_id, []))


# ============================================================================ evaluation


@dataclass
class EvaluatedLine:
    row: LineRow
    available: int
    issue: CartIssue | None
    max_quantity: int

    @property
    def allowed(self) -> int:
        """Largest quantity this line could have right now."""
        return max(0, min(self.available, self.max_quantity))


def evaluate(row: LineRow, limits: StoreLimits, loader: CartLoader) -> EvaluatedLine:
    """Issue precedence: UNAVAILABLE > VARIANT_UNAVAILABLE > OUT_OF_STOCK > INSUFFICIENT_STOCK >
    QUANTITY_LIMIT_EXCEEDED."""
    if row.type == ProductType.variable:
        available = row.variant_stock or 0
    elif row.type == ProductType.bundle:
        available = loader.bundle_stock(row.product_id)
    else:
        available = row.stock or 0
    issue: CartIssue | None = None
    if not row.visible:
        issue = CartIssue.UNAVAILABLE
    elif row.type == ProductType.variable and (row.variant_id is None or not row.variant_active):
        issue = CartIssue.VARIANT_UNAVAILABLE
    elif available <= 0:
        issue = CartIssue.OUT_OF_STOCK
    elif row.quantity > available:
        issue = CartIssue.INSUFFICIENT_STOCK
    elif row.quantity > limits.cart_max_quantity_per_line:
        issue = CartIssue.QUANTITY_LIMIT_EXCEEDED
    return EvaluatedLine(row=row, available=available, issue=issue, max_quantity=limits.cart_max_quantity_per_line)


def line_prices(row: LineRow) -> PriceInfo:
    """THE price rule for a cart line (also used for the order snapshot)."""
    if row.type == ProductType.variable:
        return price_info(row.variant_price, row.variant_sale_price)
    return price_info(row.price, row.sale_price)


def line_image(row: LineRow, images: Sequence[LoadedImage], loader: CartLoader) -> str | None:
    """First image of the variant's color if any, else the product's main image."""
    if row.variant_id is not None:
        variant = loader.variant(row.product_id, row.variant_id)
        if variant is not None:
            color_key = color_key_of(variant.options)
            variants = loader.variants.get(row.product_id, [])
            colors = image_colors(images, variants, color_key)
            ids = color_image_ids(variant, images, colors, color_key)
            if ids:
                return next(i.image_key for i in images if i.id == ids[0])
    main = main_image(images)
    return main.image_key if main else None


def build_cart(
    rows: Sequence[LineRow],
    limits: StoreLimits,
    loader: CartLoader,
    images: dict[int, list[LoadedImage]],
    methods: Sequence[DeliveryMethod],
    notices: Sequence[CartNoticeOut] = (),
) -> tuple[CartOut, list[EvaluatedLine]]:
    lines: list[CartLineOut] = []
    evaluated: list[EvaluatedLine] = []
    subtotal = savings = ZERO
    items_count = 0
    for row in rows:
        ev = evaluate(row, limits, loader)
        evaluated.append(ev)
        prices = line_prices(row)
        status, left = stock_status(ev.available, limits.low_stock_threshold)
        ok = ev.issue is None
        line_total = prices.current * row.quantity if ok else ZERO
        if ok:
            subtotal += line_total
            items_count += row.quantity
            if prices.compare_at is not None:
                savings += (prices.compare_at - prices.current) * row.quantity
        issue = None
        if ev.issue is not None:
            issue = LineIssue(
                code=ev.issue, message=ISSUE_MESSAGES[ev.issue],
                available=ev.available if ev.issue == CartIssue.INSUFFICIENT_STOCK else None,
                max=ev.max_quantity if ev.issue == CartIssue.QUANTITY_LIMIT_EXCEEDED else None,
            )
        lines.append(
            CartLineOut(
                id=row.id, quantity=row.quantity,
                product=CartProduct(
                    id=row.product_id, slug=row.slug, name=row.name, type=row.type,
                    brand=CartBrand(name=row.brand_name, slug=row.brand_slug) if row.brand_name and row.brand_slug else None,
                ),
                variant=CartVariant(
                    id=row.variant_id, options=dict(row.variant_options or {}), label=variant_label(row.variant_options or {})
                ) if row.variant_id is not None else None,
                image=image_urls(line_image(row, images.get(row.product_id, []), loader)),
                unit_price=prices.current, regular_price=prices.regular, compare_at_price=prices.compare_at,
                discount_percent=prices.discount_percent, line_total=line_total,
                stock_status=status, stock_left=left,
                items=[
                    CartBundleItem(
                        name=c.product_name, variant_label=variant_label(c.variant_options) if c.variant_options else None,
                        quantity=c.quantity,
                    )
                    for c in loader.components.get(row.product_id, [])
                ] if row.type == ProductType.bundle else [],
                issue=issue,
            )
        )
    issues_count = sum(1 for ev in evaluated if ev.issue is not None)
    cart = CartOut(
        lines=lines, items_count=items_count, lines_count=len(lines), subtotal=subtotal, savings=savings,
        delivery_quotes=[delivery_quote(m, subtotal) for m in methods],
        can_checkout=bool(lines) and issues_count == 0, issues_count=issues_count, notices=list(notices),
    )
    return cart, evaluated


def delivery_quote(method: DeliveryMethod, subtotal: Decimal) -> DeliveryQuote:
    free = method.free_from is not None and subtotal >= method.free_from
    fee = ZERO if free else method.fee
    to_free = method.free_from - subtotal if method.free_from is not None and not free else None
    return DeliveryQuote(
        code=method.code, name=method.name, description=method.description, fee=fee, is_free=fee == 0,
        free_from=method.free_from, amount_to_free_shipping=to_free, requires_address=method.requires_address,
    )


def notice(code: CartNotice, **fields: object) -> CartNoticeOut:
    return CartNoticeOut(code=code, message=NOTICE_MESSAGES[code], **fields)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CheckoutSnapshot:
    cart: CartOut
    rows: list[LineRow]
    evaluated: list[EvaluatedLine]
    images: dict[int, list[LoadedImage]]
    loader: CartLoader
    limits: StoreLimits
    methods: list[DeliveryMethod]  # active, as quoted in `cart`

    def method(self, code: str) -> DeliveryMethod | None:
        return next((m for m in self.methods if m.code == code), None)



# ============================================================================== planning


class Skip(Exception):
    """Merge: the item is skipped for this reason (an error code)."""

    def __init__(self, reason: ErrorCode) -> None:
        self.reason = reason


@dataclass
class PlannedLine:
    product_id: int
    variant_id: int | None
    quantity: int
    line_id: int | None  # None: to be inserted
    original: int = 0


@dataclass
class CartPlan:
    """In-memory view of the cart lines while a request decides what to write."""

    lines: dict[tuple[int, int | None], PlannedLine] = field(default_factory=dict)

    @classmethod
    def from_refs(cls, refs: Sequence[LineRef]) -> CartPlan:
        return cls({(r.product_id, r.variant_id): PlannedLine(r.product_id, r.variant_id, r.quantity, r.id, r.quantity) for r in refs})

    def writes(self) -> tuple[list[tuple[int, int | None, int]], list[tuple[int, int]]]:
        inserts = [(p.product_id, p.variant_id, p.quantity) for p in self.lines.values() if p.line_id is None]
        updates = [(p.line_id, p.quantity) for p in self.lines.values() if p.line_id is not None and p.quantity != p.original]
        return inserts, updates  # type: ignore[return-value]


@dataclass(frozen=True)
class PlanResult:
    status: str  # added | merged | capped
    quantity: int
    capped_reason: CartIssue | None = None  # why it was capped


def resolve_variant(
    target: TargetProduct | None, variant_id: int | None, loader: CartLoader
) -> tuple[TargetProduct, LoadedVariant | None]:
    """Shared target rules: visible product; variant required/forbidden/active and belonging."""
    if target is None or not target.visible:
        raise NotFound(ErrorCode.PRODUCT_NOT_FOUND)
    if target.type == ProductType.variable:
        if variant_id is None:
            raise ValidationFailed(ErrorCode.VARIANT_REQUIRED)
        variant = loader.variant(target.id, variant_id)
        if variant is None or not variant.is_active:
            raise NotFound(ErrorCode.VARIANT_NOT_FOUND)
        return target, variant
    if variant_id is not None:
        raise ValidationFailed(ErrorCode.VARIANT_NOT_ALLOWED)
    return target, None


def available_stock(target: TargetProduct, variant: LoadedVariant | None, loader: CartLoader) -> int:
    if target.type == ProductType.variable:
        return variant.stock if variant is not None else 0
    if target.type == ProductType.bundle:
        return loader.bundle_stock(target.id)
    return target.stock or 0


def plan_add(
    plan: CartPlan,
    limits: StoreLimits,
    target: TargetProduct,
    variant: LoadedVariant | None,
    quantity: int,
    loader: CartLoader,
    *,
    strict: bool,
) -> PlanResult:
    """Add quantity for (product, variant). strict: raise like POST /cart/items; otherwise cap or
    raise Skip (merge)."""
    key = (target.id, variant.id if variant else None)
    available = available_stock(target, variant, loader)
    existing = plan.lines.get(key)
    current = existing.quantity if existing else 0
    wanted = current + quantity
    maximum = limits.cart_max_quantity_per_line

    if available <= 0:
        if strict:
            raise out_of_stock(target.id, key[1])
        raise Skip(ErrorCode.OUT_OF_STOCK)
    if existing is None and len(plan.lines) >= limits.cart_max_lines:
        if strict:
            raise ValidationFailed(ErrorCode.CART_LIMIT_REACHED, details={"max": limits.cart_max_lines})
        raise Skip(ErrorCode.CART_LIMIT_REACHED)
    if strict:
        if wanted > maximum:
            raise ValidationFailed(ErrorCode.QUANTITY_LIMIT_EXCEEDED, details={"max": maximum})
        if wanted > available:
            raise insufficient_stock(target.id, key[1], available)
    final = min(wanted, maximum, available)
    if existing is None:
        plan.lines[key] = PlannedLine(target.id, key[1], final, None)
    else:
        # Lenient merges never shrink a line the user already had.
        existing.quantity = final if strict else max(final, existing.quantity)
    reason: CartIssue | None = None
    if final < wanted:
        reason = CartIssue.INSUFFICIENT_STOCK if available < min(wanted, maximum) else CartIssue.QUANTITY_LIMIT_EXCEEDED
    status = "capped" if reason else ("merged" if existing is not None else "added")
    return PlanResult(status=status, quantity=plan.lines[key].quantity, capped_reason=reason)


# =============================================================================== service


class CartService:
    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user
        # Kept as a plain int: a rollback (retry) expires ORM objects such as `user`.
        self.user_id = user.id
        self.loader = CartLoader(session)

    # ----------------------------------------------------------------------- reads

    async def get(self) -> CartOut:
        return await self.render(None)

    async def render(self, limits: StoreLimits | None, notices: Sequence[CartNoticeOut] = ()) -> CartOut:
        cart, _ = await self._render(limits, notices)
        return cart

    async def _render(
        self,
        limits: StoreLimits | None,
        notices: Sequence[CartNoticeOut] = (),
        *,
        lock_lines: bool = False,
        lock_products: bool = False,
    ) -> tuple[CartOut, tuple[list[LineRow], dict[int, list[LoadedImage]], list[DeliveryMethod], list[EvaluatedLine], StoreLimits]]:
        """limits=None: take them from the delivery-methods query (no extra statement)."""
        rows = await repo.load_lines(self.session, self.user_id, lock=lock_lines, lock_products=lock_products)
        await self.loader.ensure_variants({r.product_id for r in rows if r.type == ProductType.variable})
        images = await repo.images_for(self.session, sorted({r.product_id for r in rows}))
        await self.loader.ensure_components({r.product_id for r in rows if r.type == ProductType.bundle})
        # Checkout (lock_products) always reads delivery methods fresh; the cart view may use the
        # per-process copy (a stale fee then ends in a clean PRICE_CHANGED at checkout).
        if lock_products:
            methods, riding_limits = await repo.active_delivery_methods(self.session)
        else:
            methods, riding_limits = await repo.cached_delivery_methods(self.session)
        limits = limits or riding_limits or await load_limits(self.session, fresh=lock_products)
        cart, evaluated = build_cart(rows, limits, self.loader, images, methods, notices)
        return cart, (rows, images, methods, evaluated, limits)

    async def count(self) -> CartCountOut:
        """The badge numbers, counted exactly like the cart (lines with an issue add no items)."""
        rows, limits = await repo.load_lines_with_limits(self.session, self.user_id)
        if not rows or limits is None:
            return CartCountOut(items_count=0, lines_count=0)
        await self.loader.ensure_components({r.product_id for r in rows if r.type == ProductType.bundle})
        items = sum(r.quantity for r in rows if evaluate(r, limits, self.loader).issue is None)
        return CartCountOut(items_count=items, lines_count=len(rows))

    async def checkout_snapshot(self, limits: StoreLimits) -> CheckoutSnapshot:
        """The cart exactly as GET /cart computes it, plus the loaded data placement needs for
        order snapshots. Call only after the product/variant rows are locked (see orders)."""
        cart, (rows, images, methods, evaluated, limits) = await self._render(limits, lock_products=True)
        return CheckoutSnapshot(
            cart=cart, rows=rows, evaluated=evaluated, images=images, loader=self.loader, limits=limits, methods=methods
        )

    # ---------------------------------------------------------------- transaction

    async def retrying(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run the transaction; retry it after a duplicate-key race (once) or an InnoDB deadlock
        (up to DEADLOCK_ATTEMPTS in all, with a short random backoff). A deadlock that persists
        under heavy contention (a flash sale) is overload: 503 SERVICE_BUSY, never a 500."""
        attempt = 0
        duplicate_retried = False
        while True:
            attempt += 1
            try:
                return await operation()
            except (IntegrityError, OperationalError) as exc:
                is_deadlock = isinstance(exc, OperationalError) and getattr(exc.orig, "args", [None])[0] == DEADLOCK
                if isinstance(exc, OperationalError) and not is_deadlock:
                    raise
                await self.session.rollback()
                self.loader = CartLoader(self.session)
                if is_deadlock:
                    if attempt >= DEADLOCK_ATTEMPTS:
                        logger.warning("Cart write for user %s deadlocked %d times: SERVICE_BUSY", self.user_id, attempt)
                        raise ServiceBusy(retry_after=1) from exc
                    await asyncio.sleep(random.uniform(0.01, 0.05) * attempt)
                elif duplicate_retried:
                    raise
                else:
                    duplicate_retried = True
                logger.info("Retrying cart write for user %s after %s", self.user_id, type(exc).__name__)

    async def lock(self) -> tuple[StoreLimits, CartPlan, list[LineRef]]:
        limits, refs = await repo.lock_user_with_lines(self.session, self.user_id)
        return limits, CartPlan.from_refs(refs), refs

    async def preload(self, refs: Sequence[LineRef], target_ids: set[int], targets: dict[int, TargetProduct]) -> None:
        """Variants and bundle components for the cart AND the targets, one query each."""
        variable = {r.product_id for r in refs if r.product_type == ProductType.variable}
        variable |= {t.id for t in targets.values() if t.type == ProductType.variable}
        bundles = {r.product_id for r in refs if r.product_type == ProductType.bundle}
        bundles |= {t.id for t in targets.values() if t.type == ProductType.bundle}
        await self.loader.ensure_variants(variable)
        await self.loader.ensure_components(bundles)

    async def write(self, plan: CartPlan, deletes: Sequence[int] = ()) -> None:
        now = clock.utcnow()
        inserts, updates = plan.writes()
        await repo.delete_lines(self.session, self.user_id, list(deletes))
        await repo.insert_lines(self.session, self.user_id, inserts, now)
        if updates:
            await repo.set_quantities(self.session, updates, now, touch_user_id=self.user_id)
        elif inserts or deletes:
            await repo.touch_cart(self.session, self.user_id, now)

    # ---------------------------------------------------------------------- add

    async def add_item(self, product_id: int, variant_id: int | None, quantity: int) -> tuple[CartOut, bool]:
        """Returns the cart and whether a new line was created."""

        async def operation() -> tuple[CartOut, bool]:
            limits, created = await self.add_in_transaction(product_id, variant_id, quantity)
            await self.session.commit()
            return await self.render(limits), created

        return await self.retrying(operation)

    async def add_in_transaction(
        self, product_id: int, variant_id: int | None, quantity: int, *, pick_variant: bool = False
    ) -> tuple[StoreLimits, bool]:
        """Lock, validate and write (no commit). pick_variant: for a variable product without
        variant_id, use the first active variant in stock (sort_order), else 409 OUT_OF_STOCK."""
        limits, plan, refs = await self.lock()
        targets = await repo.target_products(self.session, [product_id])
        await self.preload(refs, {product_id}, targets)
        target = targets.get(product_id)
        if pick_variant and variant_id is None and target is not None and target.type == ProductType.variable and target.visible:
            in_stock = [v for v in self.loader.variants.get(product_id, []) if v.is_active and v.stock > 0]
            if not in_stock:
                raise out_of_stock(product_id, None)
            variant_id = min(in_stock, key=lambda v: (v.sort_order, v.id)).id
        target, variant = resolve_variant(target, variant_id, self.loader)
        key = (target.id, variant.id if variant else None)
        created = key not in plan.lines
        plan_add(plan, limits, target, variant, quantity, self.loader, strict=True)
        await self.write(plan)
        return limits, created

    # -------------------------------------------------------------------- update

    async def update_item(self, item_id: int, quantity: int | None, variant_id: int | None) -> CartOut:
        async def operation() -> CartOut:
            limits, plan, refs = await self.lock()
            line = next((r for r in refs if r.id == item_id), None)
            if line is None:
                raise NotFound(ErrorCode.CART_ITEM_NOT_FOUND)
            targets = await repo.target_products(self.session, [line.product_id])
            await self.preload(refs, {line.product_id}, targets)
            new_variant_id = variant_id if variant_id is not None else line.variant_id
            target, variant = resolve_variant(targets.get(line.product_id), new_variant_id, self.loader)
            wanted = quantity if quantity is not None else line.quantity
            available = available_stock(target, variant, self.loader)
            notices: list[CartNoticeOut] = []
            now = clock.utcnow()

            other = None
            if variant is not None and variant.id != line.variant_id:
                other = plan.lines.get((target.id, variant.id))
            if other is not None and other.line_id is not None:
                # The user picked a variant they already have: merge the two lines, capping silently.
                if available <= 0:
                    raise out_of_stock(target.id, variant.id)
                summed = other.quantity + wanted
                final = min(summed, limits.cart_max_quantity_per_line, available)
                await repo.delete_lines(self.session, self.user_id, [line.id])
                await repo.set_quantities(self.session, [(other.line_id, final)], now, touch_user_id=self.user_id)
                notices.append(notice(CartNotice.LINES_MERGED, item_id=other.line_id, product_id=target.id,
                                      from_quantity=other.quantity, to_quantity=final))
                if final < summed:
                    notices.append(notice(CartNotice.QUANTITY_CAPPED, item_id=other.line_id, product_id=target.id,
                                          from_quantity=summed, to_quantity=final))
            else:
                variant_key = variant.id if variant else None
                if available <= 0:
                    raise out_of_stock(target.id, variant_key)
                if wanted > limits.cart_max_quantity_per_line:
                    raise ValidationFailed(ErrorCode.QUANTITY_LIMIT_EXCEEDED, details={"max": limits.cart_max_quantity_per_line})
                if wanted > available:
                    raise insufficient_stock(target.id, variant_key, available)
                await repo.set_line(self.session, line.id, quantity=wanted, variant_id=variant.id if variant else None, now=now)
                await repo.touch_cart(self.session, self.user_id, now)
            await self.session.commit()
            return await self.render(limits, notices)

        return await self.retrying(operation)

    # -------------------------------------------------------------------- delete

    async def delete_item(self, item_id: int) -> CartOut:
        limits, _, refs = await self.lock()
        if not any(r.id == item_id for r in refs):
            raise NotFound(ErrorCode.CART_ITEM_NOT_FOUND)
        await repo.delete_lines(self.session, self.user_id, [item_id])
        await repo.touch_cart(self.session, self.user_id, clock.utcnow())
        await self.session.commit()
        return await self.render(limits)

    async def clear(self) -> CartOut:
        limits = await repo.lock_user(self.session, self.user_id)
        await repo.delete_lines(self.session, self.user_id)
        await repo.touch_cart(self.session, self.user_id, clock.utcnow())
        await self.session.commit()
        return await self.render(limits)

    # ------------------------------------------------------------ resolve issues

    async def resolve_issues(self) -> CartOut:
        """Remove unavailable/out-of-stock lines, cap the others; report every change."""
        limits = await repo.lock_user(self.session, self.user_id)
        _, (rows, images, methods, evaluated, _) = await self._render(limits, lock_lines=True)
        notices: list[CartNoticeOut] = []
        removed: list[int] = []
        capped: list[tuple[int, int]] = []
        for ev in evaluated:
            if ev.issue in REMOVABLE_ISSUES:
                removed.append(ev.row.id)
                notices.append(notice(CartNotice.LINE_REMOVED, item_id=ev.row.id, product_id=ev.row.product_id,
                                      from_quantity=ev.row.quantity, reason=ev.issue))
            elif ev.issue is not None:
                capped.append((ev.row.id, ev.allowed))
                notices.append(notice(CartNotice.QUANTITY_CAPPED, item_id=ev.row.id, product_id=ev.row.product_id,
                                      from_quantity=ev.row.quantity, to_quantity=ev.allowed, reason=ev.issue))
        now = clock.utcnow()
        await repo.delete_lines(self.session, self.user_id, removed)
        if capped:
            await repo.set_quantities(self.session, capped, now, touch_user_id=self.user_id)
        elif removed:
            await repo.touch_cart(self.session, self.user_id, now)
        await self.session.commit()
        # Re-render from what is already loaded (no new queries).
        new_quantity = dict(capped)
        remaining = [r for r in rows if r.id not in removed]
        for row in remaining:
            if row.id in new_quantity:
                row.quantity = new_quantity[row.id]
        cart, _ = build_cart(remaining, limits, self.loader, images, methods, notices)
        return cart

    # --------------------------------------------------------------------- merge

    async def merge(self, items: Sequence[CartMergeItem]) -> CartMergeOut:
        """Guest cart after login: never fails because of one item (merged, capped or skipped)."""

        async def operation() -> CartMergeOut:
            limits, plan, refs = await self.lock()
            targets = await repo.target_products(self.session, sorted({i.product_id for i in items}))
            await self.preload(refs, set(targets), targets)
            report: list[MergeReportItem] = []
            for index, item in enumerate(items):
                try:
                    target, variant = resolve_variant(targets.get(item.product_id), item.variant_id, self.loader)
                    result = plan_add(plan, limits, target, variant, item.quantity, self.loader, strict=False)
                except AppError as exc:
                    report.append(MergeReportItem(index=index, product_id=item.product_id, variant_id=item.variant_id,
                                                  status="skipped", reason=exc.code, quantity=0))
                    continue
                except Skip as skip:
                    report.append(MergeReportItem(index=index, product_id=item.product_id, variant_id=item.variant_id,
                                                  status="skipped", reason=skip.reason.value, quantity=0))
                    continue
                report.append(
                    MergeReportItem(
                        index=index, product_id=item.product_id, variant_id=item.variant_id,
                        status=result.status,  # type: ignore[arg-type]
                        reason=result.capped_reason.value if result.capped_reason else None, quantity=result.quantity,
                    )
                )
            await self.write(plan)
            await self.session.commit()
            cart = await self.render(limits)
            return CartMergeOut(**cart.model_dump(), merge_report=report)

        return await self.retrying(operation)


# =============================================================================== preview


def preview_key(product_id: int, variant_id: int | None) -> str:
    return f"{product_id}:{variant_id or 0}"


def unknown_product_row(product_id: int, variant_id: int | None, quantity: int) -> LineRow:
    """A product that does not exist (any more): an UNAVAILABLE line with only its id."""
    return LineRow(
        id=0, quantity=quantity, product_id=product_id, slug="", name="", type=ProductType.simple, price=None,
        sale_price=None, stock=0, visible=False, brand_name=None, brand_slug=None, variant_id=variant_id,
        variant_options=None, variant_price=None, variant_sale_price=None, variant_stock=None, variant_active=None,
    )


class CartPreviewService:
    """POST /cart/preview: the cart a device keeps (guests), computed by the same rules as GET /cart
    (build_cart / evaluate), without storing anything. Bounded statements for any item count."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.loader = CartLoader(session)

    async def preview(self, items: Sequence[CartPreviewItem]) -> CartPreviewOut:
        methods, limits = await repo.cached_delivery_methods(self.session)
        limits = limits or await load_limits(self.session)

        # Same product and variant twice: one line, capped at the per-line maximum.
        wanted: dict[tuple[int, int | None], int] = {}
        repeated: set[tuple[int, int | None]] = set()
        for item in items:
            key = (item.product_id, item.variant_id)
            if key in wanted:
                repeated.add(key)
            wanted[key] = wanted.get(key, 0) + item.quantity
        notices: list[CartNoticeOut] = []
        for key in repeated:
            final = min(wanted[key], limits.cart_max_quantity_per_line)
            notices.append(notice(CartNotice.LINES_MERGED, product_id=key[0], to_quantity=final))
            if final < wanted[key]:
                notices.append(notice(CartNotice.QUANTITY_CAPPED, product_id=key[0], from_quantity=wanted[key],
                                      to_quantity=final, reason=CartIssue.QUANTITY_LIMIT_EXCEEDED))
            wanted[key] = final

        found = await repo.preview_rows(self.session, list(wanted))
        rows: list[LineRow] = []
        for (product_id, variant_id), quantity in wanted.items():
            base = found.get((product_id, None))
            if base is None:
                rows.append(unknown_product_row(product_id, variant_id, quantity))
                continue
            row = found.get((product_id, variant_id)) if variant_id is not None else base
            if row is None or row.variant_id != variant_id:
                # A variant that is not this product's, or one for a product without variants.
                row = replace(base, variant_id=None)
                if base.type != ProductType.variable:
                    row.visible = False
            rows.append(replace(row, quantity=quantity))

        await self.loader.ensure_variants({r.product_id for r in rows if r.type == ProductType.variable})
        images = await repo.images_for(self.session, sorted({r.product_id for r in rows if r.slug}))
        await self.loader.ensure_components({r.product_id for r in rows if r.type == ProductType.bundle})
        cart, _ = build_cart(rows, limits, self.loader, images, methods, notices)
        lines = [
            CartPreviewLineOut(**line.model_dump(exclude={"id"}), key=preview_key(*key))
            for line, key in zip(cart.lines, wanted, strict=True)
        ]
        return CartPreviewOut(**cart.model_dump(exclude={"lines"}), lines=lines)
