from datetime import datetime

from sqlalchemy import BigInteger, DateTime, FetchedValue, text
from sqlalchemy.orm import Mapped, mapped_column

# All timestamps are UTC: every connection runs SET time_zone = '+00:00' (see app/db/session.py).
CURRENT_TIMESTAMP = text("CURRENT_TIMESTAMP")
CURRENT_TIMESTAMP_ON_UPDATE = text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, sort_order=-100)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=CURRENT_TIMESTAMP, sort_order=100
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=CURRENT_TIMESTAMP_ON_UPDATE,
        server_onupdate=FetchedValue(),
        sort_order=101,
    )
