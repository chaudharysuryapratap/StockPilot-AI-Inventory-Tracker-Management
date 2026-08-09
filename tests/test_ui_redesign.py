from app import db
from app.models import User, WorkspaceMembership


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
    assert b"AI purchasing" in page.data


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
