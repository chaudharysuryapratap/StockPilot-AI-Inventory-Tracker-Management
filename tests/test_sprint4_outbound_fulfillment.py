from __future__ import annotations

from decimal import Decimal

from sqlalchemy import inspect, text

from app import db
from app.models import (
    Bin,
    InventoryMovement,
    Product,
    Sale,
    SaleItem,
    SalesOrder,
    SalesOrderAllocation,
    SalesOrderItem,
    StockLevel,
    User,
)
from app.schema import SPRINT_4_SCHEMA_VERSION, current_schema_versions, migrate_schema


INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token"}


def _headers(actor_email: str | None = None) -> dict:
    headers = dict(INTERNAL_HEADERS)
    if actor_email:
        headers["X-Actor-Email"] = actor_email
    return headers


def _create_order(
    client,
    *,
    external_id: str = "manual-order-1001",
    quantity: object = "3.00",
    headers: dict | None = None,
) -> dict:
    response = client.post(
        "/api/sales-orders",
        headers=headers or INTERNAL_HEADERS,
        json={
            "external_order_id": external_id,
            "location_code": "TEST",
            "channel": "manual",
            "customer_reference": "CUSTOMER-42",
            "items": [{"sku": "TEST-001", "quantity": quantity}],
        },
    )
    assert response.status_code == 201
    return response.json["order"]


def _fulfil_order(client, order: dict, *, headers: dict | None = None) -> None:
    headers = headers or INTERNAL_HEADERS
    order_id = order["id"]
    item_id = order["items"][0]["id"]
    assert client.post(
        f"/api/sales-orders/{order_id}/start-picking", headers=headers
    ).status_code == 200
    assert client.post(
        f"/api/sales-orders/{order_id}/items/{item_id}/pick", headers=headers
    ).status_code == 200
    assert client.post(
        f"/api/sales-orders/{order_id}/pack", headers=headers
    ).status_code == 200


def test_order_creation_reserves_stock_and_is_idempotent(client, app, seeded_catalog):
    payload = {
        "external_order_id": "web-1001",
        "location_code": "TEST",
        "channel": "manual",
        "customer_reference": "WEB-CART-1001",
        "items": [
            {"sku": "TEST-001", "quantity": "1.25"},
            {"sku": "test-001", "quantity": "1.75"},
        ],
    }
    first = client.post("/api/sales-orders", headers=INTERNAL_HEADERS, json=payload)
    repeat = client.post("/api/sales-orders", headers=INTERNAL_HEADERS, json=payload)

    assert first.status_code == 201
    assert first.json["created"] is True
    assert repeat.status_code == 200
    assert repeat.json["created"] is False
    assert first.json["order"]["status"] == "pending"
    assert first.json["order"]["items"][0]["quantity"] == 3
    assert first.json["order"]["items"][0]["allocations"] == [
        {
            "stock_level_id": 1,
            "location": "TEST",
            "bin": None,
            "quantity_reserved": 3,
            "picked_quantity": 0,
        }
    ]

    conflict_payload = dict(payload)
    conflict_payload["items"] = [{"sku": "TEST-001", "quantity": 4}]
    conflict = client.post(
        "/api/sales-orders", headers=INTERNAL_HEADERS, json=conflict_payload
    )
    assert conflict.status_code == 409

    with app.app_context():
        stock = StockLevel.query.one()
        assert stock.quantity_on_hand == Decimal("10.00")
        assert stock.quantity_reserved == Decimal("3.00")
        assert stock.quantity_available == Decimal("7.00")
        assert SalesOrder.query.count() == 1
        assert SalesOrderItem.query.count() == 1
        assert SalesOrderAllocation.query.count() == 1


def test_reservation_is_atomic_when_any_order_line_is_short(client, app, seeded_catalog):
    with app.app_context():
        second = Product(
            workspace_id=db.session.get(Product, seeded_catalog["product_id"]).workspace_id,
            sku="SECOND-001",
            name="Second product",
        )
        db.session.add(second)
        db.session.flush()
        db.session.add(
            StockLevel(
                product=second,
                location_id=seeded_catalog["location_id"],
                quantity_on_hand=Decimal("1.00"),
            )
        )
        db.session.commit()

    rejected = client.post(
        "/api/sales-orders",
        headers=INTERNAL_HEADERS,
        json={
            "external_order_id": "atomic-shortage",
            "location_code": "TEST",
            "items": [
                {"sku": "TEST-001", "quantity": 5},
                {"sku": "SECOND-001", "quantity": 2},
            ],
        },
    )
    assert rejected.status_code == 409
    assert "Only 1" in rejected.json["error"]
    with app.app_context():
        assert SalesOrder.query.count() == 0
        assert all(
            stock.quantity_reserved == Decimal("0.00")
            for stock in StockLevel.query.all()
        )


def test_order_reservation_and_pos_sales_share_the_same_available_stock(
    client, app, seeded_catalog
):
    order = _create_order(client, external_id="pos-protection-order", quantity=8)
    blocked = client.post(
        "/api/webhooks/sales",
        headers={"X-POS-Token": "test-pos-token"},
        json={
            "external_sale_id": "pos-cannot-use-reserved",
            "location_code": "TEST",
            "items": [{"sku": "TEST-001", "quantity": 3}],
        },
    )
    allowed = client.post(
        "/api/webhooks/sales",
        headers={"X-POS-Token": "test-pos-token"},
        json={
            "external_sale_id": "pos-uses-unreserved-only",
            "location_code": "TEST",
            "items": [{"sku": "TEST-001", "quantity": 2}],
        },
    )
    assert blocked.status_code == 409
    assert allowed.status_code == 201

    with app.app_context():
        stock = StockLevel.query.one()
        assert stock.quantity_on_hand == Decimal("8.00")
        assert stock.quantity_reserved == Decimal("8.00")
        assert stock.quantity_available == Decimal("0.00")

    _fulfil_order(client, order)
    shipped = client.post(
        f"/api/sales-orders/{order['id']}/ship", headers=INTERNAL_HEADERS
    )
    assert shipped.status_code == 200
    with app.app_context():
        stock = StockLevel.query.one()
        assert stock.quantity_on_hand == Decimal("0.00")
        assert stock.quantity_reserved == Decimal("0.00")


def test_pick_pack_ship_lifecycle_deducts_once_and_records_sale(
    client, app, seeded_catalog
):
    order = _create_order(client)
    order_id = order["id"]
    item_id = order["items"][0]["id"]

    started = client.post(
        f"/api/sales-orders/{order_id}/start-picking", headers=INTERNAL_HEADERS
    )
    assert started.status_code == 200
    assert started.json["order"]["status"] == "picking"

    premature_pack = client.post(
        f"/api/sales-orders/{order_id}/pack", headers=INTERNAL_HEADERS
    )
    assert premature_pack.status_code == 409
    assert "all items must be picked" in premature_pack.json["error"]

    picked = client.post(
        f"/api/sales-orders/{order_id}/items/{item_id}/pick",
        headers=INTERNAL_HEADERS,
    )
    repeated_pick = client.post(
        f"/api/sales-orders/{order_id}/items/{item_id}/pick",
        headers=INTERNAL_HEADERS,
    )
    assert picked.status_code == 200 and picked.json["changed"] is True
    assert repeated_pick.status_code == 200 and repeated_pick.json["changed"] is False

    packed = client.post(
        f"/api/sales-orders/{order_id}/pack", headers=INTERNAL_HEADERS
    )
    shipped = client.post(
        f"/api/sales-orders/{order_id}/ship", headers=INTERNAL_HEADERS
    )
    repeat_ship = client.post(
        f"/api/sales-orders/{order_id}/ship", headers=INTERNAL_HEADERS
    )
    assert packed.status_code == 200
    assert shipped.status_code == 200 and shipped.json["changed"] is True
    assert repeat_ship.status_code == 200 and repeat_ship.json["changed"] is False
    assert shipped.json["order"]["status"] == "shipped"

    with app.app_context():
        stock = StockLevel.query.one()
        assert stock.quantity_on_hand == Decimal("7.00")
        assert stock.quantity_reserved == Decimal("0.00")
        assert stock.quantity_available == Decimal("7.00")

        saved_order = db.session.get(SalesOrder, order_id)
        assert saved_order.picking_started_by_id is not None
        assert saved_order.items[0].picked_by_id is not None
        assert saved_order.packed_by_id is not None
        assert saved_order.shipped_by_id is not None

        sale = Sale.query.filter_by(
            external_id=f"sales-order:{saved_order.order_uid}"
        ).one()
        assert sale.source == "manual"
        assert SaleItem.query.filter_by(sale_id=sale.id).one().quantity == Decimal(
            "3.00"
        )
        movement = InventoryMovement.query.filter_by(
            reference_type="sales_order", reference_id=saved_order.order_uid
        ).one()
        assert movement.quantity_delta == Decimal("-3.00")
        assert movement.reason == "order_fulfillment"
        assert movement.user_id == saved_order.shipped_by_id


def test_bin_pick_list_and_shipment_movements_match_allocations(
    client, app, seeded_catalog
):
    with app.app_context():
        location_id = seeded_catalog["location_id"]
        first_bin = Bin(location_id=location_id, code="A-01", capacity=20)
        second_bin = Bin(location_id=location_id, code="B-01", capacity=20)
        db.session.add_all([first_bin, second_bin])
        db.session.flush()
        original = StockLevel.query.one()
        original.bin = first_bin
        original.quantity_on_hand = Decimal("4.00")
        db.session.add(
            StockLevel(
                product_id=seeded_catalog["product_id"],
                location_id=location_id,
                bin=second_bin,
                quantity_on_hand=Decimal("6.00"),
            )
        )
        db.session.commit()
        first_bin_id = first_bin.id
        second_bin_id = second_bin.id

    order = _create_order(client, external_id="multi-bin-order", quantity=8)
    allocations = order["items"][0]["allocations"]
    assert [(row["bin"], row["quantity_reserved"]) for row in allocations] == [
        ("A-01", 4),
        ("B-01", 4),
    ]
    pick_list = client.get(
        f"/api/sales-orders/{order['id']}/pick-list"
    )
    assert pick_list.status_code == 200
    assert len(pick_list.json["pick_list"]["items"][0]["allocations"]) == 2

    _fulfil_order(client, order)
    shipped = client.post(
        f"/api/sales-orders/{order['id']}/ship", headers=INTERNAL_HEADERS
    )
    assert shipped.status_code == 200

    with app.app_context():
        first_stock = StockLevel.query.filter_by(bin_id=first_bin_id).one()
        second_stock = StockLevel.query.filter_by(bin_id=second_bin_id).one()
        assert first_stock.quantity_on_hand == Decimal("0.00")
        assert second_stock.quantity_on_hand == Decimal("2.00")
        movements = InventoryMovement.query.filter_by(
            reference_id=order["order_uid"], reason="order_fulfillment"
        ).all()
        assert len(movements) == 2
        assert {row.bin.code for row in movements} == {"A-01", "B-01"}


def test_cancelling_releases_reservation_without_physical_movement(
    client, app, seeded_catalog
):
    order = _create_order(client, external_id="cancel-order", quantity=6)
    cancelled = client.post(
        f"/api/sales-orders/{order['id']}/cancel", headers=INTERNAL_HEADERS
    )
    repeat = client.post(
        f"/api/sales-orders/{order['id']}/cancel", headers=INTERNAL_HEADERS
    )
    cannot_pick = client.post(
        f"/api/sales-orders/{order['id']}/start-picking", headers=INTERNAL_HEADERS
    )
    assert cancelled.status_code == 200 and cancelled.json["changed"] is True
    assert repeat.status_code == 200 and repeat.json["changed"] is False
    assert cannot_pick.status_code == 409

    with app.app_context():
        stock = StockLevel.query.one()
        assert stock.quantity_on_hand == Decimal("10.00")
        assert stock.quantity_reserved == Decimal("0.00")
        assert InventoryMovement.query.count() == 0
        assert Sale.query.count() == 0


def test_picker_can_pick_and_pack_but_cannot_create_or_ship(
    client, app, seeded_catalog
):
    with app.app_context():
        admin = User.query.one()
        picker = User(
            workspace_id=admin.workspace_id,
            name="Pia Picker",
            email="picker@freshmart.test",
            role="picker",
            is_active=True,
        )
        manager = User(
            workspace_id=admin.workspace_id,
            name="Mina Manager",
            email="manager@freshmart.test",
            role="manager",
            is_active=True,
        )
        db.session.add_all([picker, manager])
        db.session.commit()
        admin_email = admin.email

    picker_create = client.post(
        "/api/sales-orders",
        headers=_headers("picker@freshmart.test"),
        json={
            "external_order_id": "picker-forbidden-create",
            "location_code": "TEST",
            "items": [{"sku": "TEST-001", "quantity": 1}],
        },
    )
    assert picker_create.status_code == 403

    order = _create_order(
        client,
        external_id="role-order",
        quantity=2,
        headers=_headers(admin_email),
    )
    picker_headers = _headers("picker@freshmart.test")
    _fulfil_order(client, order, headers=picker_headers)
    forbidden_ship = client.post(
        f"/api/sales-orders/{order['id']}/ship", headers=picker_headers
    )
    assert forbidden_ship.status_code == 403

    manager_ship = client.post(
        f"/api/sales-orders/{order['id']}/ship",
        headers=_headers("manager@freshmart.test"),
    )
    assert manager_ship.status_code == 200
    assert manager_ship.json["order"]["shipped_by"]["email"] == "manager@freshmart.test"


def test_sprint4_migration_and_operational_pages(client, app, seeded_catalog):
    with app.app_context():
        migrate_schema()
        db.session.execute(
            text("DELETE FROM schema_migrations WHERE version = :version"),
            {"version": SPRINT_4_SCHEMA_VERSION},
        )
        db.session.commit()
        first = migrate_schema()
        second = migrate_schema()
        tables = set(inspect(db.engine).get_table_names())
        assert first.applied_versions == (SPRINT_4_SCHEMA_VERSION,)
        assert second.applied is False
        assert SPRINT_4_SCHEMA_VERSION in current_schema_versions()
        assert {
            "sales_orders",
            "sales_order_items",
            "sales_order_allocations",
        }.issubset(tables)

    order = _create_order(client, external_id="page-order", quantity=1)
    listing = client.get("/orders")
    detail = client.get(f"/orders/{order['id']}")
    assert listing.status_code == 200
    assert b"Create and reserve stock" in listing.data
    assert detail.status_code == 200
    assert b"BIN-AWARE PICK LIST" in detail.data
    assert b"Generate pick list" in detail.data
