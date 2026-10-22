from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class PageParams:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def get_page_params(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> PageParams:
    return PageParams(page=page, page_size=page_size)


PageParamsDep = Annotated[PageParams, Depends(get_page_params)]


class Page[T](BaseModel):
    items: list[T]
    page: int
    page_size: int
    total: int
