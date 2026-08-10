from __future__ import annotations

import io
import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from app.models import Bin, Product, StockLevel
from app.schema import (
    SPRINT_1_SCHEMA_VERSION,
    SPRINT_2_SCHEMA_VERSION,
    SPRINT_3_SCHEMA_VERSION,
    SPRINT_4_SCHEMA_VERSION,
    SPRINT_5_SCHEMA_VERSION,
    SPRINT_6_SCHEMA_VERSION,
    SPRINT_7_SCHEMA_VERSION,
    SPRINT_8_SCHEMA_VERSION,
    SPRINT_9_SCHEMA_VERSION,
    SPRINT_10_SCHEMA_VERSION,
    migrate_schema,
)


INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token"}


def test_explicit_migration_initializes_an_empty_database(tmp_path):
    database_path = tmp_path / "empty-migration.db"
    migration_app = create_app(
        {
            "TESTING": True,
            "AUTO_CREATE_SCHEMA": False,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
            "BEDROCK_ENABLED": False,
            "SES_ENABLED": False,
        }
    )

    with migration_app.app_context():
        assert "products" not in inspect(db.engine).get_table_names()

        first = migrate_schema()
        second = migrate_schema()
        tables = set(inspect(db.engine).get_table_names())

        assert first.applied is True
        assert first.version == SPRINT_10_SCHEMA_VERSION
        assert second.applied is False
        assert {
            "products",
            "stock_levels",
            "schema_migrations",
            "purchase_orders",
            "purchase_order_items",
            "purchase_receipts",
            "inventory_lots",
            "unit_conversions",
            "forecast_outcomes",
            "chat_conversations",
        } <= tables


def test_unassigned_stock_position_is_unique(app, seeded_catalog):
    with app.app_context():
        existing = StockLevel.query.one()
        db.session.add(
            StockLevel(
                product_id=existing.product_id,
                location_id=existing.location_id,
                bin_id=None,
                quantity_on_hand=Decimal("1.00"),
                quantity_reserved=Decimal("0.00"),
            )
        )
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert StockLevel.query.count() == 1


def test_stock_level_is_the_single_source_for_on_hand_and_available_stock(
    client, app, seeded_catalog
):
    with app.app_context():
        columns = {column["name"] for column in inspect(db.engine).get_columns("stock_levels")}
        assert "quantity_on_hand" in columns
        assert "quantity_reserved" in columns
        assert "quantity" not in columns

        stock = StockLevel.query.one()
        stock.quantity_reserved = Decimal("2.50")
        db.session.commit()

    product_response = client.get("/api/products")
    dashboard_response = client.get("/api/dashboard")

    stock_json = product_response.json["products"][0]["stock"][0]
    assert stock_json == {
        "location": "TEST",
        "quantity_on_hand": 10,
        "quantity_reserved": 2.5,
        "quantity_available": 7.5,
    }
    assert dashboard_response.json["metrics"]["total_units"] == 10


def test_product_api_create_edit_archive_restore_and_validation(client, app):
    created = client.post(
        "/api/products",
        headers=INTERNAL_HEADERS,
        json={
            "sku": "milk-1l",
            "barcode": "890000000001",
            "name": "Full Cream Milk",
            "category": "Dairy",
            "unit_of_measure": "bottle",
            "cost_price": "48.50",
            "sell_price": "55.00",
            "reorder_point": 12,
            "safety_stock": 4,
            "is_perishable": True,
        },
    )
    assert created.status_code == 201
    product_id = created.json["product"]["id"]
    assert created.json["product"]["sku"] == "MILK-1L"
    assert created.json["product"]["cost_price"] == 48.5
    assert created.json["product"]["is_active"] is True

    invalid = client.patch(
        f"/api/products/{product_id}",
        headers=INTERNAL_HEADERS,
        json={"sell_price": "-1", "sku": "contains spaces"},
    )
    assert invalid.status_code == 400
    assert set(invalid.json["fields"]) == {"sell_price", "sku"}

    updated = client.patch(
        f"/api/products/{product_id}",
        headers=INTERNAL_HEADERS,
        json={"name": "Premium Full Cream Milk", "sell_price": "57.00"},
    )
    assert updated.status_code == 200
    assert updated.json["product"]["name"] == "Premium Full Cream Milk"
    assert updated.json["product"]["sell_price"] == 57

    duplicate_barcode = client.post(
        "/api/products",
        headers=INTERNAL_HEADERS,
        json={
            "sku": "MILK-2L",
            "barcode": "890000000001",
            "name": "Another Milk",
        },
    )
    assert duplicate_barcode.status_code == 409

    archived = client.delete(f"/api/products/{product_id}", headers=INTERNAL_HEADERS)
    assert archived.status_code == 200
    assert archived.json["product"]["is_active"] is False
    assert client.get("/api/products").json["products"] == []
    assert len(client.get("/api/products?include_archived=true").json["products"]) == 1

    restored = client.post(
        f"/api/products/{product_id}/restore", headers=INTERNAL_HEADERS
    )
    assert restored.status_code == 200
    assert restored.json["product"]["is_active"] is True


def test_archived_product_cannot_be_sold(client, app, seeded_catalog):
    product_id = seeded_catalog["product_id"]
    client.delete(f"/api/products/{product_id}", headers=INTERNAL_HEADERS)

    response = client.post(
        "/api/webhooks/sales",
        headers={"X-POS-Token": "test-pos-token"},
        json={
            "external_sale_id": "archived-sale",
            "location_code": "TEST",
            "items": [{"sku": "TEST-001", "quantity": 1}],
        },
    )

    assert response.status_code == 400
    with app.app_context():
        assert StockLevel.query.one().quantity_on_hand == Decimal("10.00")


def test_adjustment_cannot_consume_reserved_stock(client, app, seeded_catalog):
    with app.app_context():
        stock = StockLevel.query.one()
        stock.quantity_reserved = Decimal("8.00")
        db.session.commit()

    response = client.post(
        "/api/stock/adjustments",
        headers=INTERNAL_HEADERS,
        json={
            "sku": "TEST-001",
            "location_code": "TEST",
            "quantity_delta": "-2.01",
            "reason": "damage",
        },
    )
    assert response.status_code == 409
    with app.app_context():
        assert StockLevel.query.one().quantity_on_hand == Decimal("10.00")


def test_csv_import_is_atomic_and_can_update_existing_products(client, app):
    valid_csv = (
        "sku,name,barcode,category,unit_of_measure,cost_price,sell_price,"
        "reorder_point,safety_stock,is_perishable\n"
        "TEA-250,Assam Tea 250g,8901001,Grocery,packet,95.50,120.00,10,4,false\n"
        "CURD-500,Fresh Curd 500g,8901002,Dairy,cup,28.00,35.00,8,3,true\n"
    ).encode()
    imported = client.post(
        "/api/products/import",
        headers=INTERNAL_HEADERS,
        data={"file": (io.BytesIO(valid_csv), "products.csv")},
        content_type="multipart/form-data",
    )
    assert imported.status_code == 201
    assert imported.json == {
        "committed": True,
        "created": 2,
        "updated": 0,
        "rows_read": 2,
        "errors": [],
    }

    invalid_csv = (
        "sku,name,cost_price\n"
        "VALID-ROW,Would Otherwise Be Valid,10.00\n"
        "BROKEN-ROW,Broken Product,-2.00\n"
    ).encode()
    rejected = client.post(
        "/api/products/import",
        headers=INTERNAL_HEADERS,
        data={"file": (io.BytesIO(invalid_csv), "invalid.csv")},
        content_type="multipart/form-data",
    )
    assert rejected.status_code == 422
    assert rejected.json["committed"] is False
    with app.app_context():
        assert Product.query.filter_by(sku="VALID-ROW").first() is None
        assert Product.query.count() == 2

    update_csv = b"sku,name,sell_price\nTEA-250,Premium Assam Tea,130.00\n"
    updated = client.post(
        "/api/products/import",
        headers=INTERNAL_HEADERS,
        data={
            "update_existing": "true",
            "file": (io.BytesIO(update_csv), "update.csv"),
        },
        content_type="multipart/form-data",
    )
    assert updated.status_code == 201
    assert updated.json["created"] == 0
    assert updated.json["updated"] == 1
    with app.app_context():
        product = Product.query.filter_by(sku="TEA-250").one()
        assert product.name == "Premium Assam Tea"
        assert product.sell_price == Decimal("130.00")


def test_sprint1_migration_preserves_legacy_stock_data(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE suppliers (
            id INTEGER PRIMARY KEY,
            name VARCHAR(120) NOT NULL UNIQUE,
            email VARCHAR(255),
            phone VARCHAR(40),
            lead_time_days INTEGER NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE inventory_locations (
            id INTEGER PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            code VARCHAR(32) NOT NULL UNIQUE,
            address VARCHAR(255),
            created_at DATETIME NOT NULL
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            sku VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(160) NOT NULL,
            category VARCHAR(80) NOT NULL,
            unit VARCHAR(32) NOT NULL,
            reorder_point INTEGER NOT NULL,
            safety_stock INTEGER NOT NULL,
            preferred_supplier_id INTEGER,
            active BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE stock_levels (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL,
            location_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity >= 0),
            updated_at DATETIME NOT NULL,
            UNIQUE(product_id, location_id)
        );
        CREATE TABLE sale_items (
            id INTEGER PRIMARY KEY,
            sale_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price NUMERIC(10,2)
        );
        CREATE TABLE inventory_movements (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL,
            location_id INTEGER NOT NULL,
            quantity_delta INTEGER NOT NULL,
            reason VARCHAR(64) NOT NULL,
            reference_type VARCHAR(64),
            reference_id VARCHAR(128),
            note VARCHAR(255),
            created_at DATETIME NOT NULL
        );
        INSERT INTO inventory_locations VALUES
            (1, 'Legacy Store', 'LEGACY', NULL, '2026-01-01 00:00:00');
        INSERT INTO products VALUES
            (1, 'LEGACY-1', 'Legacy Product', 'General', 'units', 2, 1, NULL, 1,
             '2026-01-01 00:00:00');
        INSERT INTO stock_levels VALUES
            (1, 1, 1, 7, '2026-01-01 00:00:00');
        """
    )
    connection.commit()
    connection.close()

    migration_app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
            "BEDROCK_ENABLED": False,
            "SES_ENABLED": False,
        }
    )
    with migration_app.app_context():
        first = migrate_schema()
        second = migrate_schema()
        columns = {
            column["name"] for column in inspect(db.engine).get_columns("stock_levels")
        }
        product = db.session.get(Product, 1)
        stock = StockLevel.query.one()

        assert first.applied is True
        assert second.applied is False
        assert first.version == SPRINT_10_SCHEMA_VERSION
        assert first.applied_versions == (
            SPRINT_1_SCHEMA_VERSION,
            SPRINT_2_SCHEMA_VERSION,
            SPRINT_3_SCHEMA_VERSION,
            SPRINT_4_SCHEMA_VERSION,
            SPRINT_5_SCHEMA_VERSION,
            SPRINT_6_SCHEMA_VERSION,
            SPRINT_7_SCHEMA_VERSION,
            SPRINT_8_SCHEMA_VERSION,
            SPRINT_9_SCHEMA_VERSION,
            SPRINT_10_SCHEMA_VERSION,
        )
        assert "quantity_on_hand" in columns
        assert "quantity_reserved" in columns
        assert "quantity" not in columns
        assert "bin_id" in columns
        assert product.unit_of_measure == "units"
        assert product.is_active is True
        assert product.cost_price == Decimal("0.00")
        assert stock.quantity_on_hand == Decimal("7.00")
        assert stock.quantity_reserved == Decimal("0.00")

        # Sprint 2 removes the legacy product/location-only uniqueness rule so
        # one SKU can occupy several bins at the same warehouse.
        bin_record = Bin(location_id=1, code="A-01", capacity=20)
        db.session.add(bin_record)
        db.session.flush()
        db.session.add(
            StockLevel(
                product_id=1,
                location_id=1,
                bin_id=bin_record.id,
                quantity_on_hand=Decimal("2.00"),
            )
        )
        db.session.commit()
        assert StockLevel.query.count() == 2

        db.session.remove()
