from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.local_cache import local_cache
from app.modules.delivery.models import DeliveryMethod
from app.modules.delivery.schemas import AdminDeliveryMethodUpdate
from app.shared.exceptions import ErrorCode, NotFound


class AdminDeliveryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[DeliveryMethod]:
        result = await self.session.scalars(
            select(DeliveryMethod).order_by(DeliveryMethod.sort_order, DeliveryMethod.id)
        )
        return list(result.all())

    async def update(self, method_id: int, data: AdminDeliveryMethodUpdate) -> DeliveryMethod:
        method = await self.session.get(DeliveryMethod, method_id, with_for_update=True)
        if method is None:
            raise NotFound(ErrorCode.DELIVERY_METHOD_NOT_FOUND)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(method, field, value)
        await self.session.commit()
        local_cache.clear()
        return method
