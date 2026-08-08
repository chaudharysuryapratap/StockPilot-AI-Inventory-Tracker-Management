from __future__ import annotations

from dataclasses import dataclass
import re

import sqlalchemy as sa

from app import db


SPRINT_1_SCHEMA_VERSION = "20260806_sprint1_foundation"
SPRINT_2_SCHEMA_VERSION = "20260806_sprint2_locations_transfers"
SPRINT_3_SCHEMA_VERSION = "20260806_sprint3_barcode_auth_roles"
SPRINT_4_SCHEMA_VERSION = "20260806_sprint4_outbound_fulfillment"
SPRINT_5_SCHEMA_VERSION = "20260806_sprint5_suppliers_reporting_alerts"
SPRINT_6_SCHEMA_VERSION = "20260806_sprint6_picker_explainability_returns"
SPRINT_7_SCHEMA_VERSION = "20260808_single_workspace_hardening"
SCHEMA_VERSIONS = (
    SPRINT_1_SCHEMA_VERSION,
    SPRINT_2_SCHEMA_VERSION,
    SPRINT_3_SCHEMA_VERSION,
    SPRINT_4_SCHEMA_VERSION,
    SPRINT_5_SCHEMA_VERSION,
    SPRINT_6_SCHEMA_VERSION,
    SPRINT_7_SCHEMA_VERSION,
)


@dataclass(frozen=True)
class MigrationResult:
    version: str
    applied: bool
    applied_versions: tuple[str, ...] = ()


def _columns(connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns(table_name)}


def _indexes(connection, table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(connection).get_indexes(table_name)
        if index.get("name")
    }


def _unique_constraints(connection, table_name: str) -> list[dict]:
    return sa.inspect(connection).get_unique_constraints(table_name)


def _foreign_key_columns(connection, table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(item.get("constrained_columns") or ())
        for item in sa.inspect(connection).get_foreign_keys(table_name)
    }


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise RuntimeError(f"Unsafe database identifier: {value}")
    return value


def _add_column(connection, table_name: str, column_name: str, definition: str) -> None:
    if column_name not in _columns(connection, table_name):
        connection.execute(
            sa.text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
        )


def _rename_column(
    connection,
    table_name: str,
    old_name: str,
    new_name: str,
    mysql_definition: str,
) -> None:
    columns = _columns(connection, table_name)
    if old_name not in columns or new_name in columns:
        return
    if connection.dialect.name in {"mysql", "mariadb"}:
        connection.execute(
            sa.text(
                f"ALTER TABLE {table_name} CHANGE COLUMN {old_name} "
                f"{new_name} {mysql_definition}"
            )
        )
    else:
        connection.execute(
            sa.text(f"ALTER TABLE {table_name} RENAME COLUMN {old_name} TO {new_name}")
        )


def _upgrade_products(connection) -> None:
    _rename_column(
        connection,
        "products",
        "unit",
        "unit_of_measure",
        "VARCHAR(20) NOT NULL DEFAULT 'unit'",
    )
    _rename_column(
        connection,
        "products",
        "active",
        "is_active",
        "BOOLEAN NOT NULL DEFAULT TRUE",
    )

    _add_column(connection, "products", "barcode", "VARCHAR(100) NULL")
    _add_column(
        connection,
        "products",
        "cost_price",
        "NUMERIC(10,2) NOT NULL DEFAULT 0.00",
    )
    _add_column(
        connection,
        "products",
        "sell_price",
        "NUMERIC(10,2) NOT NULL DEFAULT 0.00",
    )
    _add_column(
        connection,
        "products",
        "is_perishable",
        "BOOLEAN NOT NULL DEFAULT FALSE",
    )
    _add_column(connection, "products", "archived_at", "DATETIME NULL")
    # SQLite does not allow a non-constant CURRENT_TIMESTAMP default through
    # ALTER TABLE, so existing rows are backfilled before normal app writes take over.
    _add_column(connection, "products", "updated_at", "DATETIME NULL")
    connection.execute(
        sa.text("UPDATE products SET updated_at = COALESCE(updated_at, created_at)")
    )

    if connection.dialect.name in {"mysql", "mariadb"}:
        connection.execute(
            sa.text(
                "ALTER TABLE products "
                "MODIFY COLUMN sku VARCHAR(100) NOT NULL, "
                "MODIFY COLUMN name VARCHAR(255) NOT NULL, "
                "MODIFY COLUMN category VARCHAR(100) NOT NULL, "
                "MODIFY COLUMN updated_at DATETIME NOT NULL"
            )
        )

    if "ix_products_barcode" not in _indexes(connection, "products"):
        connection.execute(
            sa.text("CREATE UNIQUE INDEX ix_products_barcode ON products (barcode)")
        )


def _upgrade_stock_levels(connection) -> None:
    _rename_column(
        connection,
        "stock_levels",
        "quantity",
        "quantity_on_hand",
        "DECIMAL(12,2) NOT NULL DEFAULT 0.00",
    )
    _add_column(
        connection,
        "stock_levels",
        "quantity_reserved",
        "NUMERIC(12,2) NOT NULL DEFAULT 0.00 CHECK (quantity_reserved >= 0)",
    )

    if connection.dialect.name in {"mysql", "mariadb"}:
        # MySQL requires an explicit type update when migrating from the legacy INT.
        connection.execute(
            sa.text(
                "ALTER TABLE stock_levels "
                "MODIFY COLUMN quantity_on_hand DECIMAL(12,2) NOT NULL DEFAULT 0.00, "
                "MODIFY COLUMN quantity_reserved DECIMAL(12,2) NOT NULL DEFAULT 0.00"
            )
        )


def _upgrade_decimal_quantities(connection) -> None:
    if connection.dialect.name not in {"mysql", "mariadb"}:
        # SQLite uses dynamic numeric affinity, so existing integer values already
        # accept decimal quantities without a destructive table rebuild.
        return
    connection.execute(
        sa.text(
            "ALTER TABLE sale_items MODIFY COLUMN quantity DECIMAL(12,2) NOT NULL"
        )
    )
    connection.execute(
        sa.text(
            "ALTER TABLE inventory_movements "
            "MODIFY COLUMN quantity_delta DECIMAL(12,2) NOT NULL"
        )
    )
    connection.execute(
        sa.text(
            "ALTER TABLE demand_insights "
            "MODIFY COLUMN recommended_reorder_quantity DECIMAL(12,2) NOT NULL"
        )
    )


def _seed_workspace_and_actor(connection) -> tuple[int, int]:
    workspace_id = connection.execute(
        sa.text("SELECT id FROM workspaces ORDER BY id LIMIT 1")
    ).scalar_one_or_none()
    if workspace_id is None:
        connection.execute(
            sa.text("INSERT INTO workspaces (name, created_at) VALUES (:name, CURRENT_TIMESTAMP)"),
            {"name": "StockPilot Workspace"},
        )
        workspace_id = connection.execute(
            sa.text("SELECT id FROM workspaces ORDER BY id LIMIT 1")
        ).scalar_one()

    actor_id = connection.execute(
        sa.text("SELECT id FROM users ORDER BY id LIMIT 1")
    ).scalar_one_or_none()
    if actor_id is None:
        connection.execute(
            sa.text(
                "INSERT INTO users "
                "(workspace_id, name, email, password_hash, role, is_active, created_at) "
                "VALUES (:workspace_id, :name, :email, NULL, 'admin', TRUE, CURRENT_TIMESTAMP)"
            ),
            {
                "workspace_id": workspace_id,
                "name": "StockPilot Staff",
                "email": "staff@stockpilot.local",
            },
        )
        actor_id = connection.execute(
            sa.text("SELECT id FROM users ORDER BY id LIMIT 1")
        ).scalar_one()
    return int(workspace_id), int(actor_id)


def _sqlite_stock_needs_rebuild(connection) -> bool:
    columns = _columns(connection, "stock_levels")
    unique_sets = {
        frozenset(item.get("column_names") or ())
        for item in _unique_constraints(connection, "stock_levels")
    }
    desired = frozenset({"product_id", "location_id", "bin_id"})
    legacy = frozenset({"product_id", "location_id"})
    return "bin_id" not in columns or desired not in unique_sets or legacy in unique_sets


def _rebuild_sqlite_stock_levels(connection) -> None:
    connection.execute(sa.text("DROP TABLE IF EXISTS stock_levels_sprint2"))
    connection.execute(
        sa.text(
            "CREATE TABLE stock_levels_sprint2 ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "product_id INTEGER NOT NULL REFERENCES products(id), "
            "location_id INTEGER NOT NULL REFERENCES inventory_locations(id), "
            "bin_id INTEGER NULL REFERENCES bins(id), "
            "quantity_on_hand NUMERIC(12,2) NOT NULL DEFAULT 0.00 "
            "CHECK (quantity_on_hand >= 0), "
            "quantity_reserved NUMERIC(12,2) NOT NULL DEFAULT 0.00 "
            "CHECK (quantity_reserved >= 0 AND quantity_reserved <= quantity_on_hand), "
            "updated_at DATETIME NOT NULL, "
            "CONSTRAINT uq_stock_product_location_bin "
            "UNIQUE (product_id, location_id, bin_id))"
        )
    )
    source_columns = _columns(connection, "stock_levels")
    bin_expression = "bin_id" if "bin_id" in source_columns else "NULL"
    connection.execute(
        sa.text(
            "INSERT INTO stock_levels_sprint2 "
            "(id, product_id, location_id, bin_id, quantity_on_hand, "
            "quantity_reserved, updated_at) "
            f"SELECT id, product_id, location_id, {bin_expression}, "
            "quantity_on_hand, quantity_reserved, updated_at FROM stock_levels"
        )
    )
    connection.execute(sa.text("DROP TABLE stock_levels"))
    connection.execute(
        sa.text("ALTER TABLE stock_levels_sprint2 RENAME TO stock_levels")
    )


def _upgrade_mysql_stock_constraint(connection) -> None:
    _add_column(connection, "stock_levels", "bin_id", "INTEGER NULL")
    legacy_names: set[str] = set()
    for item in _unique_constraints(connection, "stock_levels"):
        if set(item.get("column_names") or ()) == {"product_id", "location_id"}:
            if item.get("name"):
                legacy_names.add(_safe_identifier(item["name"]))
    for item in sa.inspect(connection).get_indexes("stock_levels"):
        if item.get("unique") and set(item.get("column_names") or ()) == {
            "product_id",
            "location_id",
        }:
            if item.get("name"):
                legacy_names.add(_safe_identifier(item["name"]))
    for name in legacy_names:
        connection.execute(sa.text(f"ALTER TABLE stock_levels DROP INDEX {name}"))

    desired_exists = any(
        set(item.get("column_names") or ())
        == {"product_id", "location_id", "bin_id"}
        for item in _unique_constraints(connection, "stock_levels")
    )
    if not desired_exists:
        connection.execute(
            sa.text(
                "ALTER TABLE stock_levels ADD CONSTRAINT "
                "uq_stock_product_location_bin UNIQUE (product_id, location_id, bin_id)"
            )
        )


def _ensure_mysql_foreign_key(
    connection,
    table: str,
    column: str,
    referred_table: str,
    constraint_name: str,
) -> None:
    if (column,) in _foreign_key_columns(connection, table):
        return
    table = _safe_identifier(table)
    column = _safe_identifier(column)
    referred_table = _safe_identifier(referred_table)
    constraint_name = _safe_identifier(constraint_name)
    connection.execute(
        sa.text(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name} "
            f"FOREIGN KEY ({column}) REFERENCES {referred_table}(id)"
        )
    )


def _upgrade_sprint2(connection) -> None:
    from app.models import Bin, StockTransfer, User, Workspace

    Workspace.__table__.create(bind=connection, checkfirst=True)
    User.__table__.create(bind=connection, checkfirst=True)
    workspace_id, _ = _seed_workspace_and_actor(connection)

    _add_column(connection, "inventory_locations", "workspace_id", "INTEGER NULL")
    _add_column(
        connection,
        "inventory_locations",
        "is_active",
        "BOOLEAN NOT NULL DEFAULT TRUE",
    )
    _add_column(connection, "inventory_locations", "updated_at", "DATETIME NULL")
    connection.execute(
        sa.text(
            "UPDATE inventory_locations SET "
            "workspace_id = COALESCE(workspace_id, :workspace_id), "
            "is_active = COALESCE(is_active, TRUE), "
            "updated_at = COALESCE(updated_at, created_at)"
        ),
        {"workspace_id": workspace_id},
    )

    Bin.__table__.create(bind=connection, checkfirst=True)
    if connection.dialect.name == "sqlite":
        if _sqlite_stock_needs_rebuild(connection):
            _rebuild_sqlite_stock_levels(connection)
    else:
        _upgrade_mysql_stock_constraint(connection)

    _add_column(connection, "inventory_movements", "bin_id", "INTEGER NULL")
    _add_column(connection, "inventory_movements", "user_id", "INTEGER NULL")
    _add_column(
        connection,
        "inventory_movements",
        "movement_type",
        "VARCHAR(30) NULL",
    )
    connection.execute(
        sa.text(
            "UPDATE inventory_movements SET movement_type = "
            "CASE WHEN reason = 'sale' THEN 'sale' ELSE 'adjustment' END "
            "WHERE movement_type IS NULL"
        )
    )

    if connection.dialect.name in {"mysql", "mariadb"}:
        connection.execute(
            sa.text(
                "ALTER TABLE inventory_locations "
                "MODIFY COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE, "
                "MODIFY COLUMN updated_at DATETIME NOT NULL"
            )
        )
        connection.execute(
            sa.text(
                "ALTER TABLE inventory_movements "
                "MODIFY COLUMN movement_type VARCHAR(30) NOT NULL DEFAULT 'adjustment'"
            )
        )
        _ensure_mysql_foreign_key(
            connection,
            "inventory_locations",
            "workspace_id",
            "workspaces",
            "fk_inventory_locations_workspace",
        )
        _ensure_mysql_foreign_key(
            connection, "stock_levels", "bin_id", "bins", "fk_stock_levels_bin"
        )
        _ensure_mysql_foreign_key(
            connection,
            "inventory_movements",
            "bin_id",
            "bins",
            "fk_inventory_movements_bin",
        )
        _ensure_mysql_foreign_key(
            connection,
            "inventory_movements",
            "user_id",
            "users",
            "fk_inventory_movements_user",
        )

    StockTransfer.__table__.create(bind=connection, checkfirst=True)


def _upgrade_sprint3(connection) -> None:
    """Normalize legacy audit identities before enabling account permissions."""

    connection.execute(
        sa.text(
            "UPDATE users SET role = 'admin' "
            "WHERE role IS NULL OR role NOT IN ('admin', 'manager', 'picker')"
        )
    )
    connection.execute(
        sa.text("UPDATE users SET is_active = TRUE WHERE is_active IS NULL")
    )


def _upgrade_sprint4(connection) -> None:
    """Add outbound orders and exact bin-level reservation allocations."""

    from app.models import SalesOrder, SalesOrderAllocation, SalesOrderItem

    SalesOrder.__table__.create(bind=connection, checkfirst=True)
    SalesOrderItem.__table__.create(bind=connection, checkfirst=True)
    SalesOrderAllocation.__table__.create(bind=connection, checkfirst=True)


def _drop_mysql_unique_for_columns(
    connection, table_name: str, expected_columns: set[str]
) -> None:
    names: set[str] = set()
    for item in _unique_constraints(connection, table_name):
        if set(item.get("column_names") or ()) == expected_columns and item.get("name"):
            names.add(_safe_identifier(item["name"]))
    for item in sa.inspect(connection).get_indexes(table_name):
        if (
            item.get("unique")
            and set(item.get("column_names") or ()) == expected_columns
            and item.get("name")
        ):
            names.add(_safe_identifier(item["name"]))
    for name in names:
        connection.execute(
            sa.text(f"ALTER TABLE {_safe_identifier(table_name)} DROP INDEX {name}")
        )


def _upgrade_sprint5(connection) -> None:
    """Scope supplier records to workspaces and add alert-delivery auditing."""

    from app.models import AlertDelivery

    workspace_id, _ = _seed_workspace_and_actor(connection)
    _rename_column(
        connection,
        "suppliers",
        "email",
        "contact_email",
        "VARCHAR(255) NULL",
    )
    _rename_column(
        connection,
        "suppliers",
        "phone",
        "contact_phone",
        "VARCHAR(50) NULL",
    )
    _add_column(connection, "suppliers", "workspace_id", "INTEGER NULL")
    _add_column(connection, "suppliers", "payment_terms", "VARCHAR(100) NULL")
    _add_column(
        connection,
        "suppliers",
        "is_active",
        "BOOLEAN NOT NULL DEFAULT TRUE",
    )
    _add_column(connection, "suppliers", "updated_at", "DATETIME NULL")
    connection.execute(
        sa.text(
            "UPDATE suppliers SET "
            "workspace_id = COALESCE(workspace_id, :workspace_id), "
            "is_active = COALESCE(is_active, TRUE), "
            "updated_at = COALESCE(updated_at, created_at)"
        ),
        {"workspace_id": workspace_id},
    )

    indexes = _indexes(connection, "suppliers")
    if "ix_suppliers_workspace_id" not in indexes:
        connection.execute(
            sa.text(
                "CREATE INDEX ix_suppliers_workspace_id ON suppliers (workspace_id)"
            )
        )

    desired_unique = any(
        set(item.get("column_names") or ()) == {"workspace_id", "name"}
        for item in _unique_constraints(connection, "suppliers")
    ) or any(
        item.get("unique")
        and set(item.get("column_names") or ()) == {"workspace_id", "name"}
        for item in sa.inspect(connection).get_indexes("suppliers")
    )

    if connection.dialect.name in {"mysql", "mariadb"}:
        _drop_mysql_unique_for_columns(connection, "suppliers", {"name"})
        if not desired_unique:
            connection.execute(
                sa.text(
                    "ALTER TABLE suppliers ADD CONSTRAINT "
                    "uq_supplier_workspace_name UNIQUE (workspace_id, name)"
                )
            )
        connection.execute(
            sa.text(
                "ALTER TABLE suppliers "
                "MODIFY COLUMN workspace_id INTEGER NOT NULL, "
                "MODIFY COLUMN name VARCHAR(255) NOT NULL, "
                "MODIFY COLUMN contact_phone VARCHAR(50) NULL, "
                "MODIFY COLUMN lead_time_days INTEGER NOT NULL DEFAULT 3, "
                "MODIFY COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE, "
                "MODIFY COLUMN updated_at DATETIME NOT NULL"
            )
        )
        _ensure_mysql_foreign_key(
            connection,
            "suppliers",
            "workspace_id",
            "workspaces",
            "fk_suppliers_workspace",
        )
    elif not desired_unique:
        # Older SQLite databases keep their original global name constraint, but
        # this additional composite index records the workspace-aware contract.
        connection.execute(
            sa.text(
                "CREATE UNIQUE INDEX uq_supplier_workspace_name "
                "ON suppliers (workspace_id, name)"
            )
        )

    AlertDelivery.__table__.create(bind=connection, checkfirst=True)


def _upgrade_sprint6(connection) -> None:
    """Persist forecast factors and add the audited returns/RMA workflow."""

    from app.models import (
        ReturnAuthorization,
        ReturnEvent,
        ReturnItem,
        ReturnReceipt,
    )

    _add_column(connection, "demand_insights", "factors", "JSON NULL")
    connection.execute(
        sa.text("UPDATE demand_insights SET factors = '{}' WHERE factors IS NULL")
    )
    if connection.dialect.name in {"mysql", "mariadb"}:
        connection.execute(
            sa.text("ALTER TABLE demand_insights MODIFY COLUMN factors JSON NOT NULL")
        )

    ReturnAuthorization.__table__.create(bind=connection, checkfirst=True)
    ReturnItem.__table__.create(bind=connection, checkfirst=True)
    ReturnReceipt.__table__.create(bind=connection, checkfirst=True)
    ReturnEvent.__table__.create(bind=connection, checkfirst=True)


def _upgrade_sprint7(connection) -> None:
    """Enforce the documented single workspace and collision-safe stock positions."""

    workspace_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM workspaces")
    ).scalar_one()
    if int(workspace_count) > 1:
        raise RuntimeError(
            "StockPilot now supports one workspace; consolidate workspace rows before migrating"
        )

    workspace_id, _ = _seed_workspace_and_actor(connection)
    connection.execute(
        sa.text(
            "UPDATE inventory_locations SET workspace_id = :workspace_id "
            "WHERE workspace_id IS NULL"
        ),
        {"workspace_id": workspace_id},
    )

    duplicate = connection.execute(
        sa.text(
            "SELECT product_id, location_id, COUNT(*) AS row_count "
            "FROM stock_levels WHERE bin_id IS NULL "
            "GROUP BY product_id, location_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "Duplicate unassigned stock positions exist; consolidate them before migrating"
        )

    if "position_bin_key" not in _columns(connection, "stock_levels"):
        connection.execute(
            sa.text(
                "ALTER TABLE stock_levels ADD COLUMN position_bin_key INTEGER "
                "GENERATED ALWAYS AS (COALESCE(bin_id, 0)) VIRTUAL"
            )
        )

    desired_columns = {"product_id", "location_id", "position_bin_key"}
    desired_exists = any(
        set(item.get("column_names") or ()) == desired_columns
        for item in _unique_constraints(connection, "stock_levels")
    ) or any(
        item.get("unique")
        and set(item.get("column_names") or ()) == desired_columns
        for item in sa.inspect(connection).get_indexes("stock_levels")
    )
    if not desired_exists:
        connection.execute(
            sa.text(
                "CREATE UNIQUE INDEX uq_stock_product_location_position "
                "ON stock_levels (product_id, location_id, position_bin_key)"
            )
        )

    indexes = _indexes(connection, "demand_insights")
    if "ix_insight_product_location_latest" not in indexes:
        connection.execute(
            sa.text(
                "CREATE INDEX ix_insight_product_location_latest "
                "ON demand_insights (product_id, location_id, id)"
            )
        )


def migrate_schema() -> MigrationResult:
    """Apply all pending StockPilot migrations to SQLite or RDS MySQL."""
    applied_versions: list[str] = []
    with db.engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        tables = set(sa.inspect(connection).get_table_names())
        required = {"products", "stock_levels", "sale_items", "inventory_movements"}
        missing = required - tables
        if missing == required:
            # A new production database is initialized only by this explicit CLI
            # migration path; normal web-process startup never creates schema.
            db.metadata.create_all(bind=connection)
            tables = set(sa.inspect(connection).get_table_names())
            missing = required - tables
        if missing:
            raise RuntimeError(
                "Cannot apply schema migrations; missing base tables: "
                + ", ".join(sorted(missing))
            )

        existing = set(
            connection.execute(
                sa.text("SELECT version FROM schema_migrations")
            ).scalars()
        )
        if SPRINT_1_SCHEMA_VERSION not in existing:
            _upgrade_products(connection)
            _upgrade_stock_levels(connection)
            _upgrade_decimal_quantities(connection)
            connection.execute(
                sa.text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": SPRINT_1_SCHEMA_VERSION},
            )
            applied_versions.append(SPRINT_1_SCHEMA_VERSION)

        if SPRINT_2_SCHEMA_VERSION not in existing:
            _upgrade_sprint2(connection)
            connection.execute(
                sa.text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": SPRINT_2_SCHEMA_VERSION},
            )
            applied_versions.append(SPRINT_2_SCHEMA_VERSION)

        if SPRINT_3_SCHEMA_VERSION not in existing:
            _upgrade_sprint3(connection)
            connection.execute(
                sa.text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": SPRINT_3_SCHEMA_VERSION},
            )
            applied_versions.append(SPRINT_3_SCHEMA_VERSION)

        if SPRINT_4_SCHEMA_VERSION not in existing:
            _upgrade_sprint4(connection)
            connection.execute(
                sa.text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": SPRINT_4_SCHEMA_VERSION},
            )
            applied_versions.append(SPRINT_4_SCHEMA_VERSION)

        if SPRINT_5_SCHEMA_VERSION not in existing:
            _upgrade_sprint5(connection)
            connection.execute(
                sa.text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": SPRINT_5_SCHEMA_VERSION},
            )
            applied_versions.append(SPRINT_5_SCHEMA_VERSION)

        if SPRINT_6_SCHEMA_VERSION not in existing:
            _upgrade_sprint6(connection)
            connection.execute(
                sa.text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": SPRINT_6_SCHEMA_VERSION},
            )
            applied_versions.append(SPRINT_6_SCHEMA_VERSION)

        if SPRINT_7_SCHEMA_VERSION not in existing:
            _upgrade_sprint7(connection)
            connection.execute(
                sa.text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": SPRINT_7_SCHEMA_VERSION},
            )
            applied_versions.append(SPRINT_7_SCHEMA_VERSION)

    return MigrationResult(
        version=applied_versions[-1] if applied_versions else SPRINT_7_SCHEMA_VERSION,
        applied=bool(applied_versions),
        applied_versions=tuple(applied_versions),
    )


def current_schema_versions() -> list[str]:
    inspector = sa.inspect(db.engine)
    if "schema_migrations" not in inspector.get_table_names():
        return []
    with db.engine.connect() as connection:
        recorded = set(
            connection.execute(sa.text("SELECT version FROM schema_migrations")).scalars()
        )
    return [version for version in SCHEMA_VERSIONS if version in recorded]
