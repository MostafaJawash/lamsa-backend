"""Home-screen banners. Links go through the shared resolver (app.modules.notifications.links)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import clock
from app.modules.banners.models import Banner
from app.modules.banners.schemas import AdminBannerOut, BannerOut, BannerState, BannerUpdate
from app.modules.notifications.links import Link, is_visible, validate_catalog_link, visible_link_targets
from app.modules.notifications.schemas import LinkIn, LinkOut
from app.integrations.storage.transaction import MediaTransaction
from app.shared.events import commit_and_publish
from app.shared.enums import LinkType
from app.shared.exceptions import ErrorCode, NotFound, ValidationFailed
from app.shared.schemas import image_urls

PUBLIC_MAX = 10
# Read a few extra so banners skipped for a hidden link target do not shrink the list.
PUBLIC_FETCH = 30


def banner_state(banner: Banner, now: datetime) -> BannerState:
    if not banner.is_active:
        return BannerState.inactive
    if banner.starts_at is not None and banner.starts_at > now:
        return BannerState.scheduled
    if banner.ends_at is not None and banner.ends_at <= now:
        return BannerState.expired
    return BannerState.active


def banner_out(banner: Banner) -> BannerOut:
    return BannerOut(id=banner.id, title=banner.title, subtitle=banner.subtitle, image=image_urls(banner.image_key),
                     link=LinkOut(type=banner.link_type, value=banner.link_value))


def admin_banner_out(banner: Banner, now: datetime) -> AdminBannerOut:
    return AdminBannerOut(
        **banner_out(banner).model_dump(), sort_order=banner.sort_order, is_active=banner.is_active,
        starts_at=banner.starts_at, ends_at=banner.ends_at, state=banner_state(banner, now),
        created_at=banner.created_at, updated_at=banner.updated_at,
    )


def check_window(starts_at: datetime | None, ends_at: datetime | None) -> None:
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise ValidationFailed(ErrorCode.VALIDATION_ERROR, details={"field": "ends_at", "reason": "must be after starts_at"})


class BannerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def public(self) -> list[BannerOut]:
        """Active banners in their window, at most 10, skipping links to hidden targets: 2 statements."""
        now = clock.now_utc()
        banners = list(
            await self.session.scalars(
                select(Banner)
                .where(
                    Banner.is_active.is_(True),
                    or_(Banner.starts_at.is_(None), Banner.starts_at <= now),
                    or_(Banner.ends_at.is_(None), Banner.ends_at > now),
                )
                .order_by(Banner.sort_order, Banner.id)
                .limit(PUBLIC_FETCH)
            )
        )
        visible = await visible_link_targets(self.session, (Link(b.link_type, b.link_value) for b in banners))
        shown = [b for b in banners if is_visible(Link(b.link_type, b.link_value), visible)]
        return [banner_out(b) for b in shown[:PUBLIC_MAX]]


class AdminBannerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[AdminBannerOut]:
        now = clock.now_utc()
        banners = await self.session.scalars(select(Banner).order_by(Banner.sort_order, Banner.id))
        return [admin_banner_out(b, now) for b in banners]

    async def get(self, banner_id: int) -> AdminBannerOut:
        return admin_banner_out(await self._get(banner_id), clock.now_utc())

    async def create(self, *, title: str, subtitle: str | None, link: LinkIn, is_active: bool,
                     starts_at: datetime | None, ends_at: datetime | None, image: bytes) -> AdminBannerOut:
        check_window(starts_at, ends_at)
        resolved = await validate_catalog_link(self.session, link.type, link.value)
        now = clock.now_utc()
        last = await self.session.scalar(select(func.max(Banner.sort_order)))
        banner = Banner(
            title=title.strip(), subtitle=(subtitle or "").strip() or None, image_key="",
            link_type=resolved.type, link_value=resolved.value, sort_order=(last + 1) if last is not None else 0,
            is_active=is_active, starts_at=starts_at, ends_at=ends_at, created_at=now, updated_at=now,
        )
        self.session.add(banner)
        await self.session.flush()  # the id names the image folder
        media = MediaTransaction(self.session)
        try:
            saved = await media.save_image(f"banners/{banner.id}", image)
        except BaseException:
            await self.session.rollback()
            raise
        banner.image_key = saved.image_key
        await media.commit()
        return admin_banner_out(banner, now)

    async def update(self, banner_id: int, data: BannerUpdate) -> AdminBannerOut:
        banner = await self._get(banner_id, for_update=True)
        fields = data.model_fields_set
        if data.title is not None:
            banner.title = data.title.strip()
        if "subtitle" in fields:
            banner.subtitle = (data.subtitle or "").strip() or None
        if "link" in fields:
            link = data.link or LinkIn(type=LinkType.none)
            resolved = await validate_catalog_link(self.session, link.type, link.value)
            banner.link_type, banner.link_value = resolved.type, resolved.value
        if data.is_active is not None:
            banner.is_active = data.is_active
        if "starts_at" in fields:
            banner.starts_at = data.starts_at
        if "ends_at" in fields:
            banner.ends_at = data.ends_at
        check_window(banner.starts_at, banner.ends_at)
        now = clock.now_utc()
        banner.updated_at = now
        await commit_and_publish(self.session)
        return admin_banner_out(banner, now)

    async def set_image(self, banner_id: int, image: bytes) -> AdminBannerOut:
        banner = await self._get(banner_id, for_update=True)
        media = MediaTransaction(self.session)
        saved = await media.save_image(f"banners/{banner.id}", image)
        media.delete_after_commit(banner.image_key)
        banner.image_key = saved.image_key
        now = clock.now_utc()
        banner.updated_at = now
        await media.commit()
        return admin_banner_out(banner, now)

    async def delete(self, banner_id: int) -> None:
        """The image files are removed only after the commit succeeded."""
        banner = await self._get(banner_id, for_update=True)
        media = MediaTransaction(self.session)
        media.delete_after_commit(banner.image_key)
        await self.session.delete(banner)
        await media.commit()

    async def reorder(self, banner_ids: Sequence[int]) -> list[AdminBannerOut]:
        banners = list(await self.session.scalars(select(Banner).with_for_update()))
        if len(banner_ids) != len(set(banner_ids)) or set(banner_ids) != {b.id for b in banners}:
            raise ValidationFailed(ErrorCode.BANNER_ORDER_MISMATCH, details={"expected": sorted(b.id for b in banners)})
        by_id = {b.id: b for b in banners}
        for index, banner_id in enumerate(banner_ids):
            by_id[banner_id].sort_order = index
        await commit_and_publish(self.session)
        now = clock.now_utc()
        return [admin_banner_out(by_id[i], now) for i in banner_ids]

    async def _get(self, banner_id: int, *, for_update: bool = False) -> Banner:
        query = select(Banner).where(Banner.id == banner_id)
        if for_update:
            query = query.with_for_update()
        banner = await self.session.scalar(query)
        if banner is None:
            raise NotFound(ErrorCode.BANNER_NOT_FOUND)
        return banner
