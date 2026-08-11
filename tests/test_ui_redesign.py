from app import db
from app.models import Product, StockLevel, User, Workspace, WorkspaceMembership


def _set_default_role(app, role):
    with app.app_context():
        actor = User.query.order_by(User.id).first()
        actor.role = role
        membership = WorkspaceMembership.query.filter_by(
            user_id=actor.id, workspace_id=actor._workspace_id
        ).one()
        membership.role = role
        db.session.commit()


def test_admin_dashboard_exposes_new_decision_ui(client, app, seeded_catalog):
    page = client.get("/")

    assert page.status_code == 200
    assert b'id="command-palette"' in page.data
    assert b'id="forecast-chart"' in page.data
    assert b"dashboard-viz.js" in page.data
    assert b"Manage team access" in page.data
    assert b"Purchasing & receiving" in page.data
    assert b"Add inventory" in page.data


def test_manager_and_picker_receive_distinct_navigation(client, app, seeded_catalog):
    _set_default_role(app, "manager")
    manager = client.get("/")
    assert manager.status_code == 200
    assert b"Today&#39;s inventory decisions" in manager.data or b"Today\xe2\x80\x99s inventory decisions" in manager.data
    assert b"Manage today" in manager.data
    assert b"Users &amp; access" not in manager.data
    assert b'id="forecast-chart"' in manager.data

    _set_default_role(app, "picker")
    picker = client.get("/")
    assert picker.status_code == 200
    assert b"My shift" in picker.data
    assert b"Open my pick queue" in picker.data
    assert b"AI purchasing" not in picker.data
    assert b'id="forecast-chart"' not in picker.data


def test_purchase_order_register_has_ai_and_filter_controls(client, seeded_catalog):
    created = client.post(
        "/api/purchase-orders",
        headers={"X-Internal-Token": "test-internal-token"},
        json={
            "external_purchase_order_id": "UI-PO-1",
            "supplier_id": seeded_catalog["supplier_id"],
            "location_id": seeded_catalog["location_id"],
            "items": [{"sku": "TEST-001", "quantity": 4, "unit": "units"}],
        },
    )
    assert created.status_code == 201
    page = client.get("/purchase-orders")

    assert page.status_code == 200
    assert b"Generate AI drafts" in page.data
    assert b'id="po-search"' in page.data
    assert b'data-po-filter="partially_received"' in page.data
    assert b"Near-expiry lots" in page.data
    assert b'id="catalogue-step"' in page.data
    assert b'id="order-step"' in page.data
    assert b'id="receiving-step"' in page.data
    assert b'name="return_to" value="purchasing"' in page.data
    assert b"manufacturing date" in page.data


def test_product_can_be_created_inside_purchasing_workflow(client, app, seeded_catalog):
    response = client.post(
        "/manage/products",
        data={
            "return_to": "purchasing",
            "sku": "FLOW-001",
            "name": "Workflow product",
            "category": "General",
            "unit_of_measure": "units",
            "cost_price": "12.50",
            "sell_price": "18.00",
            "reorder_point": "3",
            "safety_stock": "2",
            "preferred_supplier_id": str(seeded_catalog["supplier_id"]),
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/purchase-orders#catalogue-step")
    with app.app_context():
        from app.models import Product

        created = Product.query.filter_by(sku="FLOW-001").one()
        assert created.preferred_supplier_id == seeded_catalog["supplier_id"]


def test_product_catalogue_bulk_removal_is_scoped_and_preserves_stock(
    client, app, seeded_catalog
):
    with app.app_context():
        other_workspace = Workspace(
            name="Other business", business_username="other-business"
        )
        db.session.add(other_workspace)
        db.session.flush()
        other_product = Product(
            workspace_id=other_workspace.id,
            sku="OTHER-001",
            name="Other business product",
            category="General",
            unit_of_measure="unit",
        )
        db.session.add(other_product)
        db.session.commit()
        other_product_id = other_product.id

    page = client.get("/products")
    assert page.status_code == 200
    assert b'id="bulk-product-form"' in page.data
    assert b'id="select-all-products"' in page.data
    assert b"Remove selected" in page.data

    removed = client.post(
        "/products/archive-selected",
        data={
            "product_ids": [
                str(seeded_catalog["product_id"]),
                str(other_product_id),
                "not-an-id",
            ]
        },
        follow_redirects=True,
    )
    assert removed.status_code == 200
    assert b"1 product removed from active inventory" in removed.data

    with app.app_context():
        assert db.session.get(Product, seeded_catalog["product_id"]).is_active is False
        assert db.session.get(Product, other_product_id).is_active is True
        stock = StockLevel.query.filter_by(
            product_id=seeded_catalog["product_id"]
        ).one()
        assert stock.quantity_on_hand == 10


def test_picker_cannot_bulk_remove_products(client, app, seeded_catalog):
    _set_default_role(app, "picker")

    page = client.get("/products")
    assert page.status_code == 200
    assert b'id="bulk-product-form"' not in page.data
    assert b'id="select-all-products"' not in page.data

    forbidden = client.post(
        "/products/archive-selected",
        data={"product_ids": str(seeded_catalog["product_id"])},
    )
    assert forbidden.status_code == 403


def test_scanner_result_uses_contained_matched_product_layout(client, seeded_catalog):
    page = client.get("/scanner")

    assert page.status_code == 200
    assert b'class="panel-header scanner-result-header"' in page.data
    assert b'id="result-name"' in page.data
    assert b'class="table-wrap scan-stock-table"' in page.data
