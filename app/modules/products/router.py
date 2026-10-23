from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.modules.products.catalog_service import CatalogProductService, ListQuery
from app.modules.products.schemas import FacetsOut, ProductCardOut, ProductDetailOut, ProductSort
from app.shared.dependencies.auth import DbSession, OptionalUser
from app.shared.dependencies.pagination import Page, PageParamsDep

router = APIRouter(prefix="/products", tags=["catalog"])

FILTERS_DOC = (
    "Attribute filters: `attr.<key>=<value>` (repeat for OR, different keys are AND), "
    "`attr.<key>.min` / `attr.<key>.max` for number attributes, `attr.<key>=true|false` for booleans. "
    "Variant option filters: `opt.<key>=<value>` (repeatable, OR): products with an active variant "
    "having that value. Keys must be filterable definitions of the category."
)


def get_service(session: DbSession) -> CatalogProductService:
    return CatalogProductService(session)


Service = Annotated[CatalogProductService, Depends(get_service)]


def list_query(
    request: Request,
    category: Annotated[str | None, Query(description="Category slug (includes its children)")] = None,
    brand: Annotated[list[str], Query(description="Brand slug, repeatable (OR)")] = [],  # noqa: B006
    q: Annotated[str | None, Query(max_length=200, description="Search text (Arabic or English)")] = None,
    min_price: Annotated[Decimal | None, Query(description="On the shown price")] = None,
    max_price: Annotated[Decimal | None, Query()] = None,
    in_stock: Annotated[bool, Query()] = False,
    on_sale: Annotated[bool, Query()] = False,
    is_new: Annotated[bool, Query()] = False,
) -> ListQuery:
    return ListQuery(
        category=category, brands=brand, q=q, min_price=min_price, max_price=max_price, in_stock=in_stock,
        on_sale=on_sale, is_new=is_new, params=list(request.query_params.multi_items()),
    )


ListQueryDep = Annotated[ListQuery, Depends(list_query)]


@router.get("", response_model=Page[ProductCardOut], description=FILTERS_DOC)
async def list_products(
    query: ListQueryDep,
    params: PageParamsDep,
    service: Service,
    user: OptionalUser,
    sort: Annotated[ProductSort | None, Query(description="Default: relevance with q, else newest")] = None,
) -> Page[ProductCardOut]:
    return await service.list(query, sort, params, user.id if user else None)


@router.get("/facets", response_model=FacetsOut, description=FILTERS_DOC)
async def product_facets(query: ListQueryDep, service: Service) -> FacetsOut:
    """Counts for the filter UI. Each facet ignores its own filter. Attribute and option facets
    need a category."""
    return await service.facets(query)


@router.get("/{slug}", response_model=ProductDetailOut)
async def get_product(slug: str, service: Service, user: OptionalUser) -> ProductDetailOut:
    return await service.detail(slug, user.id if user else None)


@router.get("/{slug}/related", response_model=list[ProductCardOut])
async def related_products(
    slug: str, service: Service, user: OptionalUser, limit: Annotated[int, Query(ge=1, le=12)] = 8
) -> list[ProductCardOut]:
    return await service.related(slug, limit, user.id if user else None)
