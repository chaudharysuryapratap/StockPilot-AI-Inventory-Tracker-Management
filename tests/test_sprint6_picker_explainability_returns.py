from __future__ import annotations

import sqlite3
from decimal import Decimal

from sqlalchemy import inspect, text

from app import create_app, db
from app.models import (
    Bin,
    DemandInsight,
    InventoryMovement,
    ReturnAuthorization,
    ReturnEvent,
    ReturnReceipt,
    SalesOrder,
    StockLevel,
    User,
    Workspace,
)
from app.schema import (
    SPRINT_1_SCHEMA_VERSION,
    SPRINT_2_SCHEMA_VERSION,
    SPRINT_3_SCHEMA_VERSION,
    SPRINT_4_SCHEMA_VERSION,
    SPRINT_5_SCHEMA_VERSION,
    SPRINT_6_SCHEMA_VERSION,
    SPRINT_7_SCHEMA_VERSION,
    SPRINT_8_SCHEMA_VERSION,
    current_schema_versions,
    migrate_schema,
)


INTERNAL_HEADERS = {"X-Internal-Token": "test-internal-token"}


def _headers(actor_email: str | None = None) -> dict:
    headers = dict(INTERNAL_HEADERS)
    if actor_email:
        headers["X-Actor-Email"] = actor_email
    return headers


def _create_order(client, *, external_id: str, quantity: object = 4) -> dict:
    response = client.post(
        "/api/sales-orders",
        headers=INTERNAL_HEADERS,
        json={
            "external_order_id": external_id,
            "location_code": "TEST",
            "channel": "manual",
            "customer_reference": external_id.upper(),
            "items": [{"sku": "TEST-001", "quantity": quantity}],
        },
    )
    assert response.status_code == 201
    return response.json["order"]


def _ship_order(client, order: dict) -> dict:
    order_id = order["id"]
    item_id = order["items"][0]["id"]
    assert client.post(
        f"/api/sales-orders/{order_id}/start-picking", headers=INTERNAL_HEADERS
    ).status_code == 200
    assert client.post(
        f"/api/sales-orders/{order_id}/items/{item_id}/pick",
        headers=INTERNAL_HEADERS,
    ).status_code == 200
    assert client.post(
        f"/api/sales-orders/{order_id}/pack", headers=INTERNAL_HEADERS
    ).status_code == 200
    shipped = client.post(
        f"/api/sales-orders/{order_id}/ship", headers=INTERNAL_HEADERS
    )
    assert shipped.status_code == 200
    return shipped.json["order"]


def _request_return(
    client,
    order: dict,
    *,
    external_id: str,
    quantity: object,
    headers: dict | None = None,
) -> tuple[object, dict]:
    payload = {
        "external_return_id": external_id,
        "reason_code": "customer_return",
        "customer_note": "Customer changed their mind.",
        "items": [{"sku": "TEST-001", "quantity": quantity}],
    }
    response = client.post(
        f"/api/sales-orders/{order['id']}/returns",
        headers=headers or INTERNAL_HEADERS,
        json=payload,
    )
    return response, payload


def test_forecast_persists_and_exposes_readable_explainability_factors(
    client, app, seeded_catalog
):
    analysis = client.post("/api/analysis/run", headers=INTERNAL_HEADERS)
    assert analysis.status_code == 200
    result = analysis.json["results"][0]
    assert result["factors"]["model_version"] == "demand_blend_v1"
    assert result["factors"]["recent_weight_percent"] == 65
    assert result["factors"]["available_stock"] == 10

    insight_feed = client.get("/api/insights", headers=INTERNAL_HEADERS)
    insight = insight_feed.json["insights"][0]
    labels = {row["label"] for row in insight["explainability"]}
    assert "Sales history window" in labels
    assert "Supplier lead time" in labels
    assert insight["factors"]["target_stock"] >= 0

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"AI EXPLAINABILITY" in dashboard.data
    assert b"Why the forecast reached each recommendation" in dashboard.data

    with app.app_context():
        saved = DemandInsight.query.one()
        assert saved.factors["model_version"] == "demand_blend_v1"
        assert saved.factors["lookback_days"] == 28


def test_sprint6_migration_backfills_legacy_forecast_factors(tmp_path):
    database_path = tmp_path / "sprint5.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (
            version VARCHAR(64) PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE demand_insights (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL,
            location_id INTEGER NOT NULL,
            generated_at DATETIME NOT NULL,
            daily_demand FLOAT NOT NULL,
            expected_stockout_at DATETIME,
            recommended_reorder_quantity NUMERIC(12,2) NOT NULL,
            confidence INTEGER NOT NULL,
            narrative TEXT
        );
        INSERT INTO demand_insights VALUES
            (1, 1, 1, '2026-08-01 00:00:00', 2.5, NULL, 4, 70, 'Legacy result');
        """
    )
    connection.executemany(
        "INSERT INTO schema_migrations (version) VALUES (?)",
        [
            (SPRINT_1_SCHEMA_VERSION,),
            (SPRINT_2_SCHEMA_VERSION,),
            (SPRINT_3_SCHEMA_VERSION,),
            (SPRINT_4_SCHEMA_VERSION,),
            (SPRINT_5_SCHEMA_VERSION,),
        ],
    )
    connection.commit()
    connection.close()

    migration_app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
            "BEDROCK_ENABLED": False,
            "SES_ENABLED": False,
            "STAFF_AUTH_ENABLED": False,
        }
    )
    with migration_app.app_context():
        result = migrate_schema()
        columns = {
            column["name"]
            for column in inspect(db.engine).get_columns("demand_insights")
        }
        tables = set(inspect(db.engine).get_table_names())
        factors = db.session.execute(
            text("SELECT factors FROM demand_insights WHERE id = 1")
        ).scalar_one()

        assert result.applied_versions == (
            SPRINT_6_SCHEMA_VERSION,
            SPRINT_7_SCHEMA_VERSION,
            SPRINT_8_SCHEMA_VERSION,
        )
        assert result.version == SPRINT_8_SCHEMA_VERSION
        assert "factors" in columns
        assert factors == "{}"
        assert {
            "return_authorizations",
            "return_items",
            "return_receipts",
            "return_events",
        }.issubset(tables)
        assert current_schema_versions()[-1] == SPRINT_8_SCHEMA_VERSION
        db.session.remove()


def test_rma_partial_restock_and_damage_completion_is_atomic_and_audited(
    client, app, seeded_catalog
):
    order = _ship_order(client, _create_order(client, external_id="rma-order-1"))
    requested, payload = _request_return(
        client, order, external_id="rma-1001", quantity=4
    )
    repeated = client.post(
        f"/api/sales-orders/{order['id']}/returns",
        headers=INTERNAL_HEADERS,
        json=payload,
    )
    assert requested.status_code == 201 and requested.json["created"] is True
    assert repeated.status_code == 200 and repeated.json["created"] is False
    rma = requested.json["return"]
    return_id = rma["id"]
    item_id = rma["items"][0]["id"]

    authorized = client.post(
        f"/api/returns/{return_id}/authorize", headers=INTERNAL_HEADERS
    )
    assert authorized.status_code == 200
    assert authorized.json["return"]["status"] == "authorized"

    restocked = client.post(
        f"/api/returns/{return_id}/items/{item_id}/receive",
        headers=INTERNAL_HEADERS,
        json={
            "external_receipt_id": "receipt-restock-1",
            "quantity": 2,
            "disposition": "restock",
            "location_code": "TEST",
            "note": "Sealed and saleable",
        },
    )
    damaged = client.post(
        f"/api/returns/{return_id}/items/{item_id}/receive",
        headers=INTERNAL_HEADERS,
        json={
            "external_receipt_id": "receipt-damaged-1",
            "quantity": 2,
            "disposition": "damaged",
            "location_code": "TEST",
            "note": "Broken packaging",
        },
    )
    repeated_damage = client.post(
        f"/api/returns/{return_id}/items/{item_id}/receive",
        headers=INTERNAL_HEADERS,
        json={
            "external_receipt_id": "receipt-damaged-1",
            "quantity": 2,
            "disposition": "damaged",
            "location_code": "TEST",
            "note": "Broken packaging",
        },
    )
    assert restocked.status_code == 201
    assert damaged.status_code == 201
    assert repeated_damage.status_code == 200
    assert repeated_damage.json["created"] is False

    detail = client.get(f"/api/returns/{return_id}", headers=INTERNAL_HEADERS)
    saved = detail.json["return"]
    assert saved["status"] == "completed"
    assert saved["items"][0]["quantity_received"] == 4
    assert saved["items"][0]["quantity_restocked"] == 2
    assert [row["disposition"] for row in saved["items"][0]["receipts"]] == [
        "restock",
        "damaged",
    ]

    with app.app_context():
        stock = StockLevel.query.one()
        assert stock.quantity_on_hand == Decimal("8.00")
        assert stock.quantity_reserved == Decimal("0.00")
        return_movement = InventoryMovement.query.filter_by(
            movement_type="return"
        ).one()
        assert return_movement.quantity_delta == Decimal("2.00")
        assert return_movement.reason == "return_customer_return"
        assert ReturnReceipt.query.count() == 2
        assert [event.event_type for event in ReturnEvent.query.order_by(ReturnEvent.id)] == [
            "requested",
            "authorized",
            "item_received",
            "item_received",
            "completed",
        ]


def test_returns_reject_unshipped_unknown_and_over_return_quantities(
    client, app, seeded_catalog
):
    pending = _create_order(client, external_id="not-shipped", quantity=2)
    not_shipped, _ = _request_return(
        client, pending, external_id="not-shipped-return", quantity=1
    )
    assert not_shipped.status_code == 409
    assert "only for shipped" in not_shipped.json["error"]

    shipped = _ship_order(client, pending)
    unknown = client.post(
        f"/api/sales-orders/{shipped['id']}/returns",
        headers=INTERNAL_HEADERS,
        json={
            "external_return_id": "unknown-sku-return",
            "reason_code": "other",
            "items": [{"sku": "NOT-SHIPPED", "quantity": 1}],
        },
    )
    too_many, _ = _request_return(
        client, shipped, external_id="too-many-return", quantity=3
    )
    assert unknown.status_code == 400
    assert too_many.status_code == 409
    assert "only 2 returnable" in too_many.json["error"]

    first, _ = _request_return(
        client, shipped, external_id="claim-return-1", quantity=1.5
    )
    second, _ = _request_return(
        client, shipped, external_id="claim-return-2", quantity=1
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert "only 0.5 returnable" in second.json["error"]


def test_rejected_and_cancelled_rmas_release_the_order_return_allowance(
    client, seeded_catalog
):
    order = _ship_order(client, _create_order(client, external_id="allowance", quantity=3))
    first, _ = _request_return(client, order, external_id="reject-me", quantity=3)
    return_id = first.json["return"]["id"]
    assert client.post(
        f"/api/returns/{return_id}/reject", headers=INTERNAL_HEADERS
    ).status_code == 200

    second, _ = _request_return(client, order, external_id="cancel-me", quantity=3)
    second_id = second.json["return"]["id"]
    assert client.post(
        f"/api/returns/{second_id}/authorize", headers=INTERNAL_HEADERS
    ).status_code == 200
    assert client.post(
        f"/api/returns/{second_id}/cancel", headers=INTERNAL_HEADERS
    ).status_code == 200

    third, _ = _request_return(client, order, external_id="valid-final", quantity=3)
    assert third.status_code == 201


def test_picker_cannot_create_or_authorize_but_can_receive_with_attribution(
    client, app, seeded_catalog
):
    with app.app_context():
        admin = User.query.first()
        picker = User(
            workspace_id=admin.workspace_id,
            name="Returns Picker",
            email="returns-picker@test.local",
            role="picker",
            is_active=True,
        )
        db.session.add(picker)
        db.session.commit()

    order = _ship_order(client, _create_order(client, external_id="role-rma", quantity=2))
    blocked_create, _ = _request_return(
        client,
        order,
        external_id="picker-cannot-create",
        quantity=1,
        headers=_headers("returns-picker@test.local"),
    )
    assert blocked_create.status_code == 403

    created, _ = _request_return(
        client, order, external_id="admin-created-rma", quantity=1
    )
    rma = created.json["return"]
    blocked_authorize = client.post(
        f"/api/returns/{rma['id']}/authorize",
        headers=_headers("returns-picker@test.local"),
    )
    assert blocked_authorize.status_code == 403
    assert client.post(
        f"/api/returns/{rma['id']}/authorize", headers=INTERNAL_HEADERS
    ).status_code == 200

    received = client.post(
        f"/api/returns/{rma['id']}/items/{rma['items'][0]['id']}/receive",
        headers=_headers("returns-picker@test.local"),
        json={
            "external_receipt_id": "picker-receipt",
            "quantity": 1,
            "disposition": "restock",
            "location_code": "TEST",
        },
    )
    assert received.status_code == 201
    assert received.json["receipt"]["received_by"]["email"] == "returns-picker@test.local"

    with app.app_context():
        movement = InventoryMovement.query.filter_by(movement_type="return").one()
        assert movement.user.email == "returns-picker@test.local"


def test_return_restock_capacity_failure_rolls_back_receipt_and_status(
    client, app, seeded_catalog
):
    with app.app_context():
        stock = StockLevel.query.one()
        constrained_bin = Bin(location=stock.location, code="TIGHT", capacity=1)
        db.session.add(constrained_bin)
        db.session.commit()
        constrained_bin_id = constrained_bin.id

    order = _ship_order(client, _create_order(client, external_id="capacity-rma", quantity=2))
    created, _ = _request_return(
        client, order, external_id="capacity-return", quantity=2
    )
    rma = created.json["return"]
    client.post(f"/api/returns/{rma['id']}/authorize", headers=INTERNAL_HEADERS)
    rejected = client.post(
        f"/api/returns/{rma['id']}/items/{rma['items'][0]['id']}/receive",
        headers=INTERNAL_HEADERS,
        json={
            "external_receipt_id": "capacity-receipt",
            "quantity": 2,
            "disposition": "restock",
            "location_code": "TEST",
            "bin_code": "TIGHT",
        },
    )
    assert rejected.status_code == 409
    assert "capacity" in rejected.json["error"]
    with app.app_context():
        saved = ReturnAuthorization.query.one()
        assert saved.status == "authorized"
        assert saved.items[0].quantity_received == Decimal("0.00")
        assert ReturnReceipt.query.count() == 0
        assert StockLevel.query.filter_by(bin_id=constrained_bin_id).count() == 0


def test_returns_fail_closed_if_multiple_workspaces_exist(client, app, seeded_catalog):
    order = _ship_order(client, _create_order(client, external_id="scope-rma", quantity=1))
    created, _ = _request_return(client, order, external_id="scope-return", quantity=1)
    return_id = created.json["return"]["id"]
    with app.app_context():
        other_workspace = Workspace(name="Other returns workspace")
        other_actor = User(
            workspace=other_workspace,
            name="Other Admin",
            email="other-returns@test.local",
            role="admin",
            is_active=True,
        )
        db.session.add_all([other_workspace, other_actor])
        db.session.commit()

    assert client.get(
        f"/api/returns/{return_id}", headers=_headers("other-returns@test.local")
    ).status_code == 503
    own_list = client.get(
        "/api/returns", headers=_headers("other-returns@test.local")
    )
    assert own_list.status_code == 503


def test_mobile_picker_rma_pages_and_offline_shell_load(client, seeded_catalog):
    picker = client.get("/picker")
    returns = client.get("/returns")
    manifest = client.get("/static/manifest.webmanifest")
    worker = client.get("/service-worker.js")
    offline = client.get("/static/offline.html")

    assert picker.status_code == 200
    assert b"Picker workspace" in picker.data
    assert b"Stock actions require a live connection" in picker.data
    assert b"picker.js" in picker.data
    assert returns.status_code == 200
    assert b"RETURN REGISTER" in returns.data
    assert manifest.status_code == 200
    assert b'"start_url": "/picker"' in manifest.data
    assert worker.status_code == 200
    assert worker.headers["Service-Worker-Allowed"] == "/"
    assert b'event.request.method !== "GET"' in worker.data
    assert offline.status_code == 200
    assert b"Stock-changing actions are paused" in offline.data


def test_rma_web_pages_render_shipped_order_and_receiving_controls(
    client, seeded_catalog
):
    order = _ship_order(client, _create_order(client, external_id="web-rma", quantity=1))
    new_form = client.get(f"/orders/{order['id']}/returns/new")
    assert new_form.status_code == 200
    assert b"Select only the quantities physically expected back" in new_form.data

    created, _ = _request_return(client, order, external_id="web-return", quantity=1)
    return_id = created.json["return"]["id"]
    requested_page = client.get(f"/returns/{return_id}")
    assert requested_page.status_code == 200
    assert b"Authorize return" in requested_page.data

    client.post(f"/api/returns/{return_id}/authorize", headers=INTERNAL_HEADERS)
    authorized_page = client.get(f"/returns/{return_id}")
    assert authorized_page.status_code == 200
    assert b"Record physical receipt" in authorized_page.data
    assert b"APPEND-ONLY HISTORY" in authorized_page.data
