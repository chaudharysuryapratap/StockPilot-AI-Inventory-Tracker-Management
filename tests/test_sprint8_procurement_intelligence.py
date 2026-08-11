from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app import db
from app.models import (
    DemandInsight,
    ForecastOutcome,
    InventoryLocation,
    InventoryLot,
    InventoryMovement,
    Product,
    PurchaseOrder,
    Sale,
    SaleItem,
    StockLevel,
    Workspace,
    utcnow,
)


INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token"}


def _create_purchase_order(client, seeded_catalog, *, quantity=2, unit="box"):
    response = client.post(
        "/api/purchase-orders",
        headers=INTERNAL_HEADERS,
        json={
            "external_purchase_order_id": "PO-1001",
            "supplier_id": seeded_catalog["supplier_id"],
            "location_id": seeded_catalog["location_id"],
            "items": [
                {
                    "sku": "TEST-001",
                    "quantity": quantity,
                    "unit": unit,
                    "unit_cost": 8.5,
                }
            ],
        },
    )
    assert response.status_code == 201
    return response.json["purchase_order"]


def test_unit_conversion_purchase_approval_partial_receiving_and_lot_tracking(
    client, app, seeded_catalog
):
    with app.app_context():
        product = db.session.get(Product, seeded_catalog["product_id"])
        product.is_perishable = True
        db.session.commit()

    conversion = client.post(
        f"/api/products/{seeded_catalog['product_id']}/unit-conversions",
        headers=INTERNAL_HEADERS,
        json={"unit_code": "box", "to_base_factor": 12},
    )
    assert conversion.status_code == 201
    assert conversion.json["conversion"]["base_unit"] == "units"

    order = _create_purchase_order(client, seeded_catalog)
    assert order["status"] == "draft"
    assert order["items"][0]["ordered_quantity"] == 24
    order_id = order["id"]
    item_id = order["items"][0]["id"]
    conflicting_order_retry = client.post(
        "/api/purchase-orders",
        headers=INTERNAL_HEADERS,
        json={
            "external_purchase_order_id": "PO-1001",
            "supplier_id": seeded_catalog["supplier_id"],
            "location_id": seeded_catalog["location_id"],
            "items": [
                {"sku": "TEST-001", "quantity": 3, "unit": "box", "unit_cost": 8.5}
            ],
        },
    )
    assert conflicting_order_retry.status_code == 409
    page = client.get("/purchase-orders")
    assert page.status_code == 200
    assert b"Approve purchase order" in page.data

    approved = client.post(
        f"/api/purchase-orders/{order_id}/approve", headers=INTERNAL_HEADERS
    )
    assert approved.status_code == 200
    assert approved.json["purchase_order"]["status"] == "approved"

    expiry = (date.today() + timedelta(days=14)).isoformat()
    first_payload = {
        "external_receipt_id": "GRN-1",
        "items": [
            {
                "item_id": item_id,
                "quantity": 1,
                "unit": "box",
                "lot_number": "LOT-24-A",
                "manufactured_at": date.today().isoformat(),
                "expiry_date": expiry,
            }
        ],
    }
    first = client.post(
        f"/api/purchase-orders/{order_id}/receipts",
        headers=INTERNAL_HEADERS,
        json=first_payload,
    )
    repeat = client.post(
        f"/api/purchase-orders/{order_id}/receipts",
        headers=INTERNAL_HEADERS,
        json=first_payload,
    )
    assert first.status_code == 201 and first.json["created"] is True
    assert repeat.status_code == 200 and repeat.json["created"] is False

    conflicting_repeat = client.post(
        f"/api/purchase-orders/{order_id}/receipts",
        headers=INTERNAL_HEADERS,
        json={
            **first_payload,
            "items": [
                {
                    **first_payload["items"][0],
                    "expiry_date": (date.today() + timedelta(days=21)).isoformat(),
                }
            ],
        },
    )
    assert conflicting_repeat.status_code == 409

    excessive = client.post(
        f"/api/purchase-orders/{order_id}/receipts",
        headers=INTERNAL_HEADERS,
        json={
            "external_receipt_id": "GRN-TOO-MUCH",
            "items": [
                {
                    "item_id": item_id,
                    "quantity": 2,
                    "unit": "box",
                    "lot_number": "LOT-24-A",
                    "manufactured_at": date.today().isoformat(),
                    "expiry_date": expiry,
                }
            ],
        },
    )
    assert excessive.status_code == 409

    second = client.post(
        f"/api/purchase-orders/{order_id}/receipts",
        headers=INTERNAL_HEADERS,
        json={**first_payload, "external_receipt_id": "GRN-2"},
    )
    assert second.status_code == 201

    refreshed = client.get(
        f"/api/purchase-orders/{order_id}", headers=INTERNAL_HEADERS
    ).json["purchase_order"]
    assert refreshed["status"] == "received"
    assert refreshed["items"][0]["received_quantity"] == 24

    recommendations = client.get(
        "/api/recommendations/inventory", headers=INTERNAL_HEADERS
    )
    assert recommendations.status_code == 200
    assert recommendations.json["near_expiry"][0]["lot_number"] == "LOT-24-A"
    assert recommendations.json["dead_stock"][0]["sku"] == "TEST-001"

    sold = client.post(
        "/api/webhooks/sales",
        headers={"X-POS-Token": "test-pos-token"},
        json={
            "external_sale_id": "LOT-SALE-1",
            "location_code": "TEST",
            "items": [{"sku": "TEST-001", "quantity": 5}],
        },
    )
    assert sold.status_code == 201

    with app.app_context():
        assert StockLevel.query.one().quantity_on_hand == Decimal("29.00")
        lot = InventoryLot.query.one()
        assert lot.quantity_on_hand == Decimal("19.00")
        assert lot.manufactured_at == date.today()
        assert lot.expiry_date == date.fromisoformat(expiry)
        assert InventoryMovement.query.filter_by(
            movement_type="purchase_receipt"
        ).count() == 2


def test_perishable_receipt_requires_lot_and_expiry(client, app, seeded_catalog):
    with app.app_context():
        product = db.session.get(Product, seeded_catalog["product_id"])
        product.is_perishable = True
        db.session.commit()
    order = _create_purchase_order(client, seeded_catalog, quantity=1, unit="units")
    client.post(
        f"/api/purchase-orders/{order['id']}/approve", headers=INTERNAL_HEADERS
    )
    response = client.post(
        f"/api/purchase-orders/{order['id']}/receipts",
        headers=INTERNAL_HEADERS,
        json={
            "external_receipt_id": "GRN-NO-LOT",
            "items": [
                {"item_id": order["items"][0]["id"], "quantity": 1, "unit": "units"}
            ],
        },
    )
    assert response.status_code == 400
    assert "lot_number and expiry_date" in response.json["error"]


def test_receipt_dates_require_a_lot_number(client, seeded_catalog):
    order = _create_purchase_order(client, seeded_catalog, quantity=1, unit="units")
    client.post(
        f"/api/purchase-orders/{order['id']}/approve", headers=INTERNAL_HEADERS
    )
    response = client.post(
        f"/api/purchase-orders/{order['id']}/receipts",
        headers=INTERNAL_HEADERS,
        json={
            "external_receipt_id": "GRN-DATES-NO-LOT",
            "items": [
                {
                    "item_id": order["items"][0]["id"],
                    "quantity": 1,
                    "unit": "units",
                    "manufactured_at": date.today().isoformat(),
                }
            ],
        },
    )
    assert response.status_code == 400
    assert "lot_number is required" in response.json["error"]


def test_ai_draft_approval_forecast_accuracy_and_grounded_chat(
    client, app, seeded_catalog
):
    with app.app_context():
        stock = StockLevel.query.one()
        stock.quantity_on_hand = Decimal("0.00")
        product = stock.product
        location = stock.location
        old_insight = DemandInsight(
            product=product,
            location=location,
            generated_at=utcnow() - timedelta(days=8),
            daily_demand=2,
            recommended_reorder_quantity=Decimal("5.00"),
            confidence=70,
            factors={"model_version": "test"},
        )
        sale = Sale(
            workspace_id=product.workspace_id,
            external_id="accuracy-sale",
            source="test",
            location=location,
            occurred_at=utcnow() - timedelta(days=5),
        )
        db.session.add_all([old_insight, sale])
        db.session.flush()
        db.session.add(
            SaleItem(sale=sale, product=product, quantity=Decimal("10.00"))
        )
        db.session.commit()
        old_insight_id = old_insight.id

    client.post("/api/analysis/run", headers=INTERNAL_HEADERS)
    drafts = client.post("/api/purchase-orders/ai-drafts", headers=INTERNAL_HEADERS)
    assert drafts.status_code == 201
    draft = drafts.json["purchase_orders"][0]
    assert draft["source"] == "ai"
    assert draft["status"] == "draft"
    assert draft["ai_rationale"]
    approved = client.post(
        f"/api/purchase-orders/{draft['id']}/approve", headers=INTERNAL_HEADERS
    )
    assert approved.json["purchase_order"]["status"] == "approved"

    accuracy = client.get("/api/forecast-accuracy", headers=INTERNAL_HEADERS)
    assert accuracy.json["evaluated_forecasts"] >= 1
    with app.app_context():
        outcome = ForecastOutcome.query.filter_by(insight_id=old_insight_id).one()
        assert outcome.predicted_units == Decimal("14.00")
        assert outcome.actual_units == Decimal("10.00")

    chat = client.post(
        "/api/assistant/chat", json={"question": "What stock is most at risk?"}
    )
    assert chat.status_code == 200
    assert chat.json["conversation_id"]
    assert "replenishment" in chat.json["answer"].lower()


def test_catalogue_identifiers_are_workspace_scoped(app, seeded_catalog):
    with app.app_context():
        second_workspace = Workspace(name="Second isolated catalogue")
        db.session.add(second_workspace)
        db.session.flush()
        duplicate_sku = Product(
            workspace=second_workspace,
            sku="TEST-001",
            barcode="SECOND-BC",
            name="Independent product",
        )
        duplicate_location = InventoryLocation(
            workspace=second_workspace,
            name="Independent location",
            code="TEST",
        )
        db.session.add_all([duplicate_sku, duplicate_location])
        db.session.commit()

        assert Product.query.filter_by(sku="TEST-001").count() == 2
        assert InventoryLocation.query.filter_by(code="TEST").count() == 2
