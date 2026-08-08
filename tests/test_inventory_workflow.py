from __future__ import annotations

from decimal import Decimal

from datetime import timedelta

import pytest

from app import create_app, db
from app.models import DemandInsight, Product, Sale, SaleItem, StockLevel, Supplier, utcnow
from app.services.forecast import ForecastService


def test_sale_webhook_decrements_once_and_is_idempotent(client, app, seeded_catalog):
    payload = {
        "external_sale_id": "pos-1001",
        "location_code": "TEST",
        "items": [{"sku": "TEST-001", "quantity": 3, "unit_price": "10.00"}],
    }
    headers = {"X-POS-Token": "test-pos-token"}

    first = client.post("/api/webhooks/sales", json=payload, headers=headers)
    repeat = client.post("/api/webhooks/sales", json=payload, headers=headers)

    assert first.status_code == 201
    assert first.json["created"] is True
    assert repeat.status_code == 200
    assert repeat.json["created"] is False
    with app.app_context():
        assert StockLevel.query.one().quantity == 7


def test_production_configuration_rejects_placeholder_secrets(tmp_path):
    with pytest.raises(RuntimeError, match="Unsafe production configuration"):
        create_app(
            {
                "APP_ENV": "production",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'unsafe.db'}",
                "SECRET_KEY": "local-development-only-change-me",
                "POS_WEBHOOK_TOKEN": "local-pos-token",
                "INTERNAL_API_TOKEN": "local-job-token",
                "SESSION_COOKIE_SECURE": False,
                "ALLOW_WEB_SIGNUP": True,
            }
        )


def test_sale_webhook_rejects_conflicting_idempotency_retry(
    client, app, seeded_catalog
):
    headers = {"X-POS-Token": "test-pos-token"}
    payload = {
        "external_sale_id": "sale-conflict-1",
        "location_code": "TEST",
        "items": [{"sku": "TEST-001", "quantity": 2, "unit_price": "5.00"}],
    }
    assert client.post("/api/webhooks/sales", json=payload, headers=headers).status_code == 201

    conflicting = {
        **payload,
        "items": [{"sku": "TEST-001", "quantity": 3, "unit_price": "5.00"}],
    }
    response = client.post(
        "/api/webhooks/sales", json=conflicting, headers=headers
    )
    assert response.status_code == 409
    assert "different sale" in response.json["error"]
    with app.app_context():
        assert StockLevel.query.one().quantity_on_hand == Decimal("8.00")


def test_sale_webhook_rejects_oversell_without_partial_update(client, app, seeded_catalog):
    response = client.post(
        "/api/webhooks/sales",
        headers={"X-POS-Token": "test-pos-token"},
        json={
            "external_sale_id": "pos-oversell",
            "location_code": "TEST",
            "items": [{"sku": "TEST-001", "quantity": 11}],
        },
    )

    assert response.status_code == 409
    with app.app_context():
        assert StockLevel.query.one().quantity == 10


def test_forecast_creates_reorder_recommendation(app, seeded_catalog):
    with app.app_context():
        product = db.session.get(Product, seeded_catalog["product_id"])
        location_id = seeded_catalog["location_id"]
        now = utcnow()
        for day in range(1, 12):
            sale = Sale(
                workspace_id=product.workspace_id,
                external_id=f"forecast-{day}",
                source="test",
                location_id=location_id,
                occurred_at=now - timedelta(days=day),
            )
            db.session.add(sale)
            db.session.flush()
            db.session.add(SaleItem(sale=sale, product=product, quantity=4))
        db.session.commit()

        results = ForecastService.run(now=now)

        assert len(results) == 1
        assert results[0].daily_demand > 0
        assert results[0].recommended_reorder_quantity > 0
        assert DemandInsight.query.count() == 1


def test_dashboard_renders_after_analysis(client, app, seeded_catalog):
    with app.app_context():
        ForecastService.run()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Inventory, without the guesswork" in response.data
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json["database"] == "ok"
    assert "Content-Security-Policy" in response.headers


def test_staff_sign_in_protects_management_forms(client, app):
    app.config.update(
        STAFF_AUTH_ENABLED=True,
        STAFF_USERNAME="owner",
        STAFF_PASSWORD="correct-horse-battery-staple",
    )
    assert client.get("/").status_code == 302

    login_page = client.get("/login")
    assert login_page.status_code == 200
    with client.session_transaction() as session:
        csrf_token = session["csrf_token"]

    signed_in = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "username": "owner",
            "password": "correct-horse-battery-staple",
        },
    )
    assert signed_in.status_code == 302

    with client.session_transaction() as session:
        csrf_token = session["csrf_token"]
    created = client.post(
        "/manage/suppliers",
        data={"csrf_token": csrf_token, "name": "Authenticated Supplier", "lead_time_days": "2"},
    )
    assert created.status_code == 302
    with app.app_context():
        assert Supplier.query.filter_by(name="Authenticated Supplier").count() == 1
