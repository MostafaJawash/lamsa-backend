from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, declared_attr

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TABLE_OPTIONS: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def table_args(*items: Any, **options: Any) -> tuple[Any, ...]:
    """Build __table_args__ with the shared MySQL table options always included.

    Models that need constraints or indexes must use this instead of a bare tuple:
        __table_args__ = table_args(Index(...), CheckConstraint(...))
    """
    return (*items, {**TABLE_OPTIONS, **options})


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        return table_args()

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        # Re-select server-generated values (created_at, ON UPDATE updated_at) right after
        # INSERT/UPDATE, so reading them later never triggers an implicit load under asyncio.
        return {"eager_defaults": True}
