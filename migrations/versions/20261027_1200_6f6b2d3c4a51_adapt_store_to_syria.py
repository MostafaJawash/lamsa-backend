"""adapt store to Syria: currencies, addresses and delivery

Reviewed by hand:
- every surviving money column is DECIMAL(15,2);
- existing catalog amounts are relabelled SYP without conversion;
- existing order amounts remain immutable EUR snapshots in order_totals/order_items;
- only untouched shipped setting values (EUR and Europe/Berlin) are replaced;
- downgrade guards run before DDL and refuse to discard non-legacy currency data or
  values which cannot fit back into DECIMAL(10,2).

Revision ID: 6f6b2d3c4a51
Revises: 08ab51f3ba30
Create Date: 2026-10-27 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6f6b2d3c4a51"
down_revision: Union[str, Sequence[str], None] = "08ab51f3ba30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MONEY_10 = sa.Numeric(precision=10, scale=2)
MONEY_15 = sa.Numeric(precision=15, scale=2)
ZERO_DEFAULT = sa.text("'0.00'")
MAX_OLD_MONEY = "99999999.99"

GOVERNORATE_CODES = (
    "damascus",
    "rif_dimashq",
    "aleppo",
    "homs",
    "hama",
    "latakia",
    "tartus",
    "idlib",
    "deir_ez_zor",
    "raqqa",
    "hasakah",
    "daraa",
    "as_suwayda",
    "quneitra",
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _widen_money_columns() -> None:
    op.alter_column(
        "delivery_methods",
        "fee",
        existing_type=MONEY_10,
        type_=MONEY_15,
        existing_nullable=False,
        existing_server_default=ZERO_DEFAULT,
    )
    op.alter_column(
        "delivery_methods",
        "free_from",
        existing_type=MONEY_10,
        type_=MONEY_15,
        existing_nullable=True,
    )
    for column, nullable, default in (
        ("price", True, None),
        ("sale_price", True, None),
        ("price_from", False, ZERO_DEFAULT),
    ):
        op.alter_column(
            "products",
            column,
            existing_type=MONEY_10,
            type_=MONEY_15,
            existing_nullable=nullable,
            existing_server_default=default,
        )
    for column, nullable in (("price", False), ("sale_price", True)):
        op.alter_column(
            "product_variants",
            column,
            existing_type=MONEY_10,
            type_=MONEY_15,
            existing_nullable=nullable,
        )
    for column in ("unit_price", "line_total"):
        op.alter_column(
            "order_items",
            column,
            existing_type=MONEY_10,
            type_=MONEY_15,
            existing_nullable=False,
        )


def _narrow_money_columns() -> None:
    for column in ("unit_price", "line_total"):
        op.alter_column(
            "order_items",
            column,
            existing_type=MONEY_15,
            type_=MONEY_10,
            existing_nullable=False,
        )
    for column, nullable in (("price", False), ("sale_price", True)):
        op.alter_column(
            "product_variants",
            column,
            existing_type=MONEY_15,
            type_=MONEY_10,
            existing_nullable=nullable,
        )
    for column, nullable, default in (
        ("price", True, None),
        ("sale_price", True, None),
        ("price_from", False, ZERO_DEFAULT),
    ):
        op.alter_column(
            "products",
            column,
            existing_type=MONEY_15,
            type_=MONEY_10,
            existing_nullable=nullable,
            existing_server_default=default,
        )
    op.alter_column(
        "delivery_methods",
        "free_from",
        existing_type=MONEY_15,
        type_=MONEY_10,
        existing_nullable=True,
    )
    op.alter_column(
        "delivery_methods",
        "fee",
        existing_type=MONEY_15,
        type_=MONEY_10,
        existing_nullable=False,
        existing_server_default=ZERO_DEFAULT,
    )


def _migrate_untouched_settings() -> None:
    # StoreSetting.value is JSON: compare and write the unquoted scalar explicitly.
    op.execute(
        sa.text(
            """
            UPDATE store_settings
               SET value = JSON_QUOTE('SYP')
             WHERE `key` = 'currency'
               AND JSON_TYPE(value) = 'STRING'
               AND JSON_UNQUOTE(value) = 'EUR'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE store_settings
               SET value = JSON_QUOTE('Asia/Damascus')
             WHERE `key` = 'timezone'
               AND JSON_TYPE(value) = 'STRING'
               AND JSON_UNQUOTE(value) = 'Europe/Berlin'
            """
        )
    )


def upgrade() -> None:
    """Upgrade schema without converting any monetary amount."""
    _widen_money_columns()

    # A product owns one currency; variants inherit it.
    op.add_column("products", sa.Column("currency", sa.CHAR(length=3), nullable=True))
    op.execute("UPDATE products SET currency = 'SYP'")
    op.alter_column(
        "products",
        "currency",
        existing_type=sa.CHAR(length=3),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_products_currency_supported"),
        "products",
        "currency IN ('SYP', 'USD')",
    )

    # NULL allowed_governorates means unrestricted. The Syrian seed writes explicit lists
    # for both live methods; NULL remains useful for inactive historical methods.
    op.add_column("delivery_methods", sa.Column("currency", sa.CHAR(length=3), nullable=True))
    op.add_column("delivery_methods", sa.Column("allowed_governorates", sa.JSON(), nullable=True))
    op.add_column(
        "delivery_methods",
        sa.Column(
            "fee_paid_to_carrier",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.execute("UPDATE delivery_methods SET currency = 'SYP'")
    op.alter_column(
        "delivery_methods",
        "currency",
        existing_type=sa.CHAR(length=3),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_delivery_methods_currency_supported"),
        "delivery_methods",
        "currency IN ('SYP', 'USD')",
    )

    # Convert the development addresses into valid Syrian addresses before dropping the
    # German-shaped fields. The suffix only handles pathological rows shorter than the new
    # ten-character minimum; normal rows keep exactly their old text, joined in order.
    op.add_column("user_addresses", sa.Column("governorate", sa.String(length=32), nullable=True))
    op.add_column("user_addresses", sa.Column("area", sa.String(length=100), nullable=True))
    op.add_column("user_addresses", sa.Column("details", sa.String(length=500), nullable=True))
    op.add_column("user_addresses", sa.Column("latitude", sa.Numeric(9, 6), nullable=True))
    op.add_column("user_addresses", sa.Column("longitude", sa.Numeric(10, 6), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE user_addresses
               SET governorate = 'damascus',
                   details = CASE
                       WHEN CHAR_LENGTH(CONCAT_WS('، ',
                           NULLIF(TRIM(city), ''),
                           NULLIF(TRIM(street), ''),
                           NULLIF(TRIM(building), '')
                       )) >= 10
                       THEN CONCAT_WS('، ',
                           NULLIF(TRIM(city), ''),
                           NULLIF(TRIM(street), ''),
                           NULLIF(TRIM(building), '')
                       )
                       ELSE CONCAT(
                           COALESCE(NULLIF(CONCAT_WS('، ',
                               NULLIF(TRIM(city), ''),
                               NULLIF(TRIM(street), ''),
                               NULLIF(TRIM(building), '')
                           ), ''), 'دمشق'),
                           ' — عنوان قديم'
                       )
                   END
            """
        )
    )
    op.alter_column(
        "user_addresses",
        "governorate",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.alter_column(
        "user_addresses",
        "details",
        existing_type=sa.String(length=500),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_user_addresses_governorate_supported"),
        "user_addresses",
        f"governorate IN ({_quoted(GOVERNORATE_CODES)})",
    )
    op.create_check_constraint(
        op.f("ck_user_addresses_location_in_syria"),
        "user_addresses",
        "((latitude IS NULL AND longitude IS NULL) OR "
        "(latitude IS NOT NULL AND longitude IS NOT NULL AND "
        "latitude BETWEEN 32.300000 AND 37.400000 AND "
        "longitude BETWEEN 35.600000 AND 42.400000))",
    )
    op.create_check_constraint(
        op.f("ck_user_addresses_details_length"),
        "user_addresses",
        "CHAR_LENGTH(details) BETWEEN 10 AND 500",
    )
    op.drop_column("user_addresses", "building")
    op.drop_column("user_addresses", "street")
    op.drop_column("user_addresses", "city")

    # Delivery facts needed to render an immutable order snapshot even after the method changes.
    op.add_column("orders", sa.Column("delivery_method_currency", sa.CHAR(length=3), nullable=True))
    op.add_column(
        "orders",
        sa.Column(
            "delivery_fee_paid_to_carrier",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.execute("UPDATE orders SET delivery_method_currency = 'EUR'")
    op.alter_column(
        "orders",
        "delivery_method_currency",
        existing_type=sa.CHAR(length=3),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_orders_delivery_method_currency_known"),
        "orders",
        "delivery_method_currency IN ('SYP', 'USD', 'EUR')",
    )

    op.create_table(
        "order_totals",
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("subtotal", MONEY_15, nullable=False),
        sa.Column("discount", MONEY_15, server_default=ZERO_DEFAULT, nullable=False),
        sa.Column("shipping_fee", MONEY_15, nullable=False),
        sa.Column("total", MONEY_15, nullable=False),
        sa.Column("is_legacy", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("subtotal >= 0", name=op.f("ck_order_totals_subtotal_non_negative")),
        sa.CheckConstraint("discount >= 0", name=op.f("ck_order_totals_discount_non_negative")),
        sa.CheckConstraint("shipping_fee >= 0", name=op.f("ck_order_totals_shipping_fee_non_negative")),
        sa.CheckConstraint("total >= 0", name=op.f("ck_order_totals_total_non_negative")),
        sa.CheckConstraint(
            "discount <= subtotal",
            name=op.f("ck_order_totals_discount_not_above_subtotal"),
        ),
        sa.CheckConstraint(
            "total = subtotal - discount + shipping_fee",
            name=op.f("ck_order_totals_total_matches_parts"),
        ),
        sa.CheckConstraint(
            "(is_legacy = 1 AND currency = 'EUR') OR "
            "(is_legacy = 0 AND currency IN ('SYP', 'USD'))",
            name=op.f("ck_order_totals_currency_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_totals_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("order_id", "currency", name=op.f("pk_order_totals")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
        mysql_engine="InnoDB",
    )
    op.execute(
        """
        INSERT INTO order_totals
            (order_id, currency, subtotal, discount, shipping_fee, total, is_legacy)
        SELECT id, 'EUR', subtotal, discount, shipping_fee, total, 1
          FROM orders
        """
    )

    op.add_column("order_items", sa.Column("currency", sa.CHAR(length=3), nullable=True))
    op.execute("UPDATE order_items SET currency = 'EUR'")
    op.alter_column(
        "order_items",
        "currency",
        existing_type=sa.CHAR(length=3),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_order_items_currency_known"),
        "order_items",
        "currency IN ('SYP', 'USD', 'EUR')",
    )

    for constraint in (
        "ck_orders_total_non_negative",
        "ck_orders_shipping_fee_non_negative",
        "ck_orders_discount_non_negative",
        "ck_orders_subtotal_non_negative",
    ):
        op.drop_constraint(op.f(constraint), "orders", type_="check")
    op.drop_column("orders", "total")
    op.drop_column("orders", "shipping_fee")
    op.drop_column("orders", "discount")
    op.drop_column("orders", "subtotal")

    _migrate_untouched_settings()


def _guard_downgrade() -> None:
    connection = op.get_bind()
    non_legacy = connection.scalar(
        sa.text("SELECT COUNT(*) FROM order_totals WHERE is_legacy = 0")
    )
    if non_legacy:
        raise RuntimeError(
            "Refusing downgrade: non-legacy SYP/USD orders would lose their currency snapshots."
        )

    mixed = connection.scalar(
        sa.text(
            """
            SELECT COUNT(*)
              FROM (
                  SELECT o.id
                    FROM orders AS o
                    LEFT JOIN order_totals AS t ON t.order_id = o.id
                   GROUP BY o.id
                  HAVING COUNT(t.order_id) <> 1
                      OR SUM(
                          CASE WHEN t.is_legacy = 1 AND t.currency = 'EUR'
                               THEN 1 ELSE 0 END
                      ) <> 1
              ) AS unsafe_orders
            """
        )
    )
    if mixed:
        raise RuntimeError(
            "Refusing downgrade: an order does not have exactly one legacy currency total."
        )

    overflow_checks = (
        ("delivery_methods", "fee"),
        ("delivery_methods", "free_from"),
        ("products", "price"),
        ("products", "sale_price"),
        ("products", "price_from"),
        ("product_variants", "price"),
        ("product_variants", "sale_price"),
        ("order_items", "unit_price"),
        ("order_items", "line_total"),
    )
    for table, column in overflow_checks:
        too_large = connection.scalar(
            sa.text(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column} > {MAX_OLD_MONEY}"
            )
        )
        if too_large:
            raise RuntimeError(
                f"Refusing downgrade: {table}.{column} contains values above DECIMAL(10,2)."
            )
    total_overflow = connection.scalar(
        sa.text(
            f"""
            SELECT COUNT(*)
              FROM order_totals
             WHERE subtotal > {MAX_OLD_MONEY}
                OR discount > {MAX_OLD_MONEY}
                OR shipping_fee > {MAX_OLD_MONEY}
                OR total > {MAX_OLD_MONEY}
            """
        )
    )
    if total_overflow:
        raise RuntimeError(
            "Refusing downgrade: order_totals contains values above DECIMAL(10,2)."
        )


def downgrade() -> None:
    """Restore the old schema only while doing so cannot discard real currency data."""
    _guard_downgrade()

    # Restore the scalar order snapshot while the legacy rows still exist.
    op.add_column("orders", sa.Column("subtotal", MONEY_10, nullable=True))
    op.add_column(
        "orders",
        sa.Column("discount", MONEY_10, server_default=ZERO_DEFAULT, nullable=True),
    )
    op.add_column("orders", sa.Column("shipping_fee", MONEY_10, nullable=True))
    op.add_column("orders", sa.Column("total", MONEY_10, nullable=True))
    op.execute(
        """
        UPDATE orders AS o
        JOIN order_totals AS t ON t.order_id = o.id
           SET o.subtotal = t.subtotal,
               o.discount = t.discount,
               o.shipping_fee = t.shipping_fee,
               o.total = t.total
        """
    )
    for column in ("subtotal", "discount", "shipping_fee", "total"):
        op.alter_column(
            "orders",
            column,
            existing_type=MONEY_10,
            nullable=False,
            existing_server_default=ZERO_DEFAULT if column == "discount" else None,
        )
    op.create_check_constraint(
        op.f("ck_orders_subtotal_non_negative"), "orders", "subtotal >= 0"
    )
    op.create_check_constraint(
        op.f("ck_orders_discount_non_negative"), "orders", "discount >= 0"
    )
    op.create_check_constraint(
        op.f("ck_orders_shipping_fee_non_negative"), "orders", "shipping_fee >= 0"
    )
    op.create_check_constraint(op.f("ck_orders_total_non_negative"), "orders", "total >= 0")
    op.drop_table("order_totals")

    op.drop_constraint(op.f("ck_order_items_currency_known"), "order_items", type_="check")
    op.drop_column("order_items", "currency")
    op.drop_constraint(
        op.f("ck_orders_delivery_method_currency_known"), "orders", type_="check"
    )
    op.drop_column("orders", "delivery_fee_paid_to_carrier")
    op.drop_column("orders", "delivery_method_currency")

    # The new address intentionally merged the old fields. A downgrade recreates a usable old
    # shape, but cannot infer the original split: governorate becomes city and details street.
    op.add_column("user_addresses", sa.Column("city", sa.String(length=100), nullable=True))
    op.add_column("user_addresses", sa.Column("street", sa.String(length=255), nullable=True))
    op.add_column("user_addresses", sa.Column("building", sa.String(length=100), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE user_addresses
               SET city = CASE governorate
                   WHEN 'damascus' THEN 'دمشق'
                   WHEN 'rif_dimashq' THEN 'ريف دمشق'
                   WHEN 'aleppo' THEN 'حلب'
                   WHEN 'homs' THEN 'حمص'
                   WHEN 'hama' THEN 'حماة'
                   WHEN 'latakia' THEN 'اللاذقية'
                   WHEN 'tartus' THEN 'طرطوس'
                   WHEN 'idlib' THEN 'إدلب'
                   WHEN 'deir_ez_zor' THEN 'دير الزور'
                   WHEN 'raqqa' THEN 'الرقة'
                   WHEN 'hasakah' THEN 'الحسكة'
                   WHEN 'daraa' THEN 'درعا'
                   WHEN 'as_suwayda' THEN 'السويداء'
                   WHEN 'quneitra' THEN 'القنيطرة'
                   ELSE governorate
               END,
                   street = LEFT(details, 255),
                   building = NULL
            """
        )
    )
    op.alter_column(
        "user_addresses",
        "city",
        existing_type=sa.String(length=100),
        nullable=False,
    )
    op.alter_column(
        "user_addresses",
        "street",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_constraint(
        op.f("ck_user_addresses_details_length"), "user_addresses", type_="check"
    )
    op.drop_constraint(
        op.f("ck_user_addresses_location_in_syria"), "user_addresses", type_="check"
    )
    op.drop_constraint(
        op.f("ck_user_addresses_governorate_supported"), "user_addresses", type_="check"
    )
    op.drop_column("user_addresses", "longitude")
    op.drop_column("user_addresses", "latitude")
    op.drop_column("user_addresses", "details")
    op.drop_column("user_addresses", "area")
    op.drop_column("user_addresses", "governorate")

    op.drop_constraint(
        op.f("ck_delivery_methods_currency_supported"), "delivery_methods", type_="check"
    )
    op.drop_column("delivery_methods", "fee_paid_to_carrier")
    op.drop_column("delivery_methods", "allowed_governorates")
    op.drop_column("delivery_methods", "currency")
    op.drop_constraint(op.f("ck_products_currency_supported"), "products", type_="check")
    op.drop_column("products", "currency")

    _narrow_money_columns()

    # Settings have no provenance column. A value equal to SYP or Asia/Damascus may have
    # been set by an administrator after upgrade, so a downgrade deliberately preserves it
    # rather than risk overwriting a customized store setting.
