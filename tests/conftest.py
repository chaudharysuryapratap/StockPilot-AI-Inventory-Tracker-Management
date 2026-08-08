from __future__ import annotations

import pytest

from app import create_app, db
from app.models import InventoryLocation, Product, StockLevel, Supplier, User


@pytest.fixture
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "POS_WEBHOOK_TOKEN": "test-pos-token",
            "INTERNAL_API_TOKEN": "test-internal-token",
            "BEDROCK_ENABLED": False,
            "SES_ENABLED": False,
            "FORECAST_LOOKBACK_DAYS": 28,
            "STAFF_AUTH_ENABLED": False,
        }
    )
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seeded_catalog(app):
    with app.app_context():
        actor = User.query.order_by(User.id).first()
        supplier = Supplier(
            workspace_id=actor.workspace_id, name="Test supplier", lead_time_days=2
        )
        location = InventoryLocation(
            workspace_id=actor.workspace_id, name="Test location", code="TEST"
        )
        product = Product(
            sku="TEST-001",
            name="Test Product",
            unit="units",
            reorder_point=4,
            safety_stock=3,
            preferred_supplier=supplier,
        )
        db.session.add_all([supplier, location, product])
        db.session.flush()
        stock = StockLevel(product=product, location=location, quantity=10)
        db.session.add(stock)
        db.session.commit()
        return {"supplier_id": supplier.id, "location_id": location.id, "product_id": product.id}
