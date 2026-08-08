from __future__ import annotations

from decimal import Decimal

from app import db
from app.models import (
    Bin,
    InventoryLocation,
    InventoryMovement,
    StockLevel,
    StockTransfer,
    User,
)
from app.services.forecast import ForecastService


INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token"}


def _create_location(client, code: str, name: str | None = None) -> dict:
    response = client.post(
        "/api/locations",
        headers=INTERNAL_HEADERS,
        json={"name": name or f"{code} Warehouse", "code": code, "address": "Noida"},
    )
    assert response.status_code == 201
    return response.json["location"]


def _create_bin(client, location_id: int, code: str, capacity: int | None = None) -> dict:
    payload = {"code": code}
    if capacity is not None:
        payload["capacity"] = capacity
    response = client.post(
        f"/api/locations/{location_id}/bins",
        headers=INTERNAL_HEADERS,
        json=payload,
    )
    assert response.status_code == 201
    return response.json["bin"]


def test_location_and_bin_create_edit_list_and_validation(client):
    location = _create_location(client, "north_1", "North Warehouse")
    assert location["code"] == "NORTH_1"
    assert location["is_active"] is True
    assert location["stock"]["quantity_on_hand"] == 0

    duplicate = client.post(
        "/api/locations",
        headers=INTERNAL_HEADERS,
        json={"name": "Duplicate", "code": "NORTH_1"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json["fields"]["code"] == "already exists"

    updated = client.patch(
        f"/api/locations/{location['id']}",
        headers=INTERNAL_HEADERS,
        json={"name": "North Distribution Centre", "address": "Sector 62, Noida"},
    )
    assert updated.status_code == 200
    assert updated.json["location"]["name"] == "North Distribution Centre"

    bin_record = _create_bin(client, location["id"], "a-12-3", capacity=80)
    assert bin_record["code"] == "A-12-3"
    assert bin_record["remaining_capacity"] == 80

    duplicate_bin = client.post(
        f"/api/locations/{location['id']}/bins",
        headers=INTERNAL_HEADERS,
        json={"code": "A-12-3", "capacity": 100},
    )
    assert duplicate_bin.status_code == 409

    edited_bin = client.patch(
        f"/api/bins/{bin_record['id']}",
        headers=INTERNAL_HEADERS,
        json={"code": "A-12-4", "capacity": 120},
    )
    assert edited_bin.status_code == 200
    assert edited_bin.json["bin"]["code"] == "A-12-4"
    assert edited_bin.json["bin"]["capacity"] == 120

    listed = client.get("/api/locations")
    assert listed.status_code == 200
    assert listed.json["locations"][0]["bins"][0]["code"] == "A-12-4"


def test_transfer_is_atomic_idempotent_and_user_attributed(client, app, seeded_catalog):
    destination = _create_location(client, "DEST")
    destination_bin = _create_bin(client, destination["id"], "D-01", capacity=20)

    payload = {
        "external_transfer_id": "transfer-1001",
        "sku": "TEST-001",
        "source_location_code": "TEST",
        "destination_location_code": "DEST",
        "destination_bin_code": "D-01",
        "quantity": "3.50",
        "note": "Rebalance store stock",
    }
    first = client.post("/api/transfers", headers=INTERNAL_HEADERS, json=payload)
    repeat = client.post("/api/transfers", headers=INTERNAL_HEADERS, json=payload)

    assert first.status_code == 201
    assert first.json["created"] is True
    assert repeat.status_code == 200
    assert repeat.json["created"] is False
    assert first.json["transfer"]["destination"] == {
        "location": "DEST",
        "bin": "D-01",
    }
    assert first.json["transfer"]["performed_by"]["email"] == "staff@stockpilot.local"

    with app.app_context():
        source = StockLevel.query.filter_by(
            product_id=seeded_catalog["product_id"],
            location_id=seeded_catalog["location_id"],
            bin_id=None,
        ).one()
        destination_stock = StockLevel.query.filter_by(bin_id=destination_bin["id"]).one()
        assert source.quantity_on_hand == Decimal("6.50")
        assert destination_stock.quantity_on_hand == Decimal("3.50")
        assert StockTransfer.query.count() == 1

        movements = InventoryMovement.query.filter_by(movement_type="transfer").all()
        assert len(movements) == 2
        assert {row.quantity_delta for row in movements} == {
            Decimal("-3.50"),
            Decimal("3.50"),
        }
        assert len({row.reference_id for row in movements}) == 1
        assert all(row.user_id is not None for row in movements)

    audit = client.get(
        "/api/audit/movements?movement_type=transfer", headers=INTERNAL_HEADERS
    )
    assert audit.status_code == 200
    assert len(audit.json["movements"]) == 2
    assert all(row["performed_by"]["email"] == "staff@stockpilot.local" for row in audit.json["movements"])


def test_transfer_protects_reserved_stock_and_rolls_back(client, app, seeded_catalog):
    destination = _create_location(client, "RESERVE-DEST")
    with app.app_context():
        stock = StockLevel.query.one()
        stock.quantity_reserved = Decimal("8.00")
        db.session.commit()

    response = client.post(
        "/api/transfers",
        headers=INTERNAL_HEADERS,
        json={
            "sku": "TEST-001",
            "source_location_code": "TEST",
            "destination_location_code": "RESERVE-DEST",
            "quantity": 3,
        },
    )
    assert response.status_code == 409
    assert "Only 2" in response.json["error"]

    with app.app_context():
        assert StockLevel.query.one().quantity_on_hand == Decimal("10.00")
        assert StockTransfer.query.count() == 0
        assert InventoryMovement.query.count() == 0


def test_destination_capacity_is_enforced_and_cannot_shrink_below_stock(
    client, app, seeded_catalog
):
    destination = _create_location(client, "CAPACITY")
    destination_bin = _create_bin(client, destination["id"], "SMALL", capacity=2)
    payload = {
        "sku": "TEST-001",
        "source_location_code": "TEST",
        "destination_location_code": "CAPACITY",
        "destination_bin_code": "SMALL",
        "quantity": 3,
    }
    rejected = client.post("/api/transfers", headers=INTERNAL_HEADERS, json=payload)
    assert rejected.status_code == 409
    assert "capacity" in rejected.json["error"].lower()

    payload["quantity"] = 2
    accepted = client.post("/api/transfers", headers=INTERNAL_HEADERS, json=payload)
    assert accepted.status_code == 201

    shrink = client.patch(
        f"/api/bins/{destination_bin['id']}",
        headers=INTERNAL_HEADERS,
        json={"capacity": 1},
    )
    assert shrink.status_code == 400
    assert "current on-hand quantity" in shrink.json["fields"]["capacity"]

    with app.app_context():
        assert StockLevel.query.filter_by(bin_id=destination_bin["id"]).one().quantity_on_hand == Decimal("2.00")


def test_same_location_bin_relocation_records_actual_source_bin(
    client, app, seeded_catalog
):
    source_bin = _create_bin(client, seeded_catalog["location_id"], "A-01", capacity=20)
    destination_bin = _create_bin(client, seeded_catalog["location_id"], "B-01", capacity=20)
    with app.app_context():
        stock = StockLevel.query.one()
        stock.bin_id = source_bin["id"]
        db.session.commit()

    response = client.post(
        "/api/transfers",
        headers=INTERNAL_HEADERS,
        json={
            "sku": "TEST-001",
            "source_location_code": "TEST",
            "destination_location_code": "TEST",
            "destination_bin_code": "B-01",
            "quantity": 4,
        },
    )
    assert response.status_code == 201
    assert response.json["transfer"]["source"]["bin"] == "A-01"
    assert response.json["transfer"]["destination"]["bin"] == "B-01"

    with app.app_context():
        assert StockLevel.query.filter_by(bin_id=source_bin["id"]).one().quantity_on_hand == Decimal("6.00")
        assert StockLevel.query.filter_by(bin_id=destination_bin["id"]).one().quantity_on_hand == Decimal("4.00")


def test_multiple_source_bins_require_an_explicit_bin(client, app, seeded_catalog):
    source_a = _create_bin(client, seeded_catalog["location_id"], "A", capacity=20)
    source_b = _create_bin(client, seeded_catalog["location_id"], "B", capacity=20)
    destination = _create_location(client, "MULTI-DEST")
    with app.app_context():
        original = StockLevel.query.one()
        original.bin_id = source_a["id"]
        original.quantity_on_hand = Decimal("5.00")
        db.session.add(
            StockLevel(
                product_id=seeded_catalog["product_id"],
                location_id=seeded_catalog["location_id"],
                bin_id=source_b["id"],
                quantity_on_hand=Decimal("5.00"),
            )
        )
        db.session.commit()

    response = client.post(
        "/api/transfers",
        headers=INTERNAL_HEADERS,
        json={
            "sku": "TEST-001",
            "source_location_code": "TEST",
            "destination_location_code": "MULTI-DEST",
            "quantity": 1,
        },
    )
    assert response.status_code == 400
    assert "source_bin_code is required" in response.json["error"]

    with app.app_context():
        assert StockTransfer.query.count() == 0
        assert sum(row.quantity_on_hand for row in StockLevel.query.all()) == Decimal("10.00")


def test_forecast_aggregates_bins_into_one_product_location_result(
    client, app, seeded_catalog
):
    extra_bin = _create_bin(client, seeded_catalog["location_id"], "OVERFLOW", capacity=50)
    with app.app_context():
        db.session.add(
            StockLevel(
                product_id=seeded_catalog["product_id"],
                location_id=seeded_catalog["location_id"],
                bin_id=extra_bin["id"],
                quantity_on_hand=Decimal("5.00"),
            )
        )
        db.session.commit()
        results = ForecastService.run()
        assert len(results) == 1
        assert results[0].current_stock == 15

    product = client.get("/api/products").json["products"][0]
    assert product["totals"]["quantity_on_hand"] == 15
