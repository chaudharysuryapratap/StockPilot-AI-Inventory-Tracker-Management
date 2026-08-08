from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text

from app import db
from app.models import InventoryMovement, Product, StockLevel, StockTransfer, User
from app.schema import SPRINT_3_SCHEMA_VERSION, current_schema_versions, migrate_schema


def _enable_named_auth(app) -> None:
    app.config.update(
        STAFF_AUTH_ENABLED=True,
        STAFF_USERNAME="",
        STAFF_PASSWORD="",
    )


def _csrf(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    with client.session_transaction() as browser_session:
        return browser_session["csrf_token"]


def _bootstrap_admin(client, app) -> None:
    _enable_named_auth(app)
    token = _csrf(client, "/signup")
    response = client.post(
        "/signup",
        data={
            "csrf_token": token,
            "workspace_name": "FreshMart",
            "name": "Surya Admin",
            "email": "admin@freshmart.test",
            "password": "correct-horse-admin",
            "password_confirm": "correct-horse-admin",
        },
    )
    assert response.status_code == 302
    assert response.location == "/"


def _logout(client) -> None:
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 302


def _login(client, email: str, password: str, *, next_url: str = "") -> None:
    token = _csrf(client, "/login")
    target = f"/login?next={next_url}" if next_url else "/login"
    response = client.post(
        target,
        data={"csrf_token": token, "identifier": email, "password": password},
    )
    assert response.status_code == 302


def _create_user(client, *, name: str, email: str, role: str, password: str) -> None:
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    response = client.post(
        "/users",
        data={
            "csrf_token": token,
            "name": name,
            "email": email,
            "role": role,
            "password": password,
            "is_active": "true",
        },
    )
    assert response.status_code == 302


def test_first_run_signup_claims_the_audit_identity_and_hashes_password(
    client, app, seeded_catalog
):
    _enable_named_auth(app)
    with app.app_context():
        placeholder = User.query.one()
        placeholder_id = placeholder.id
        product = db.session.get(Product, seeded_catalog["product_id"])
        stock = StockLevel.query.one()
        movement = InventoryMovement(
            product=product,
            location=stock.location,
            user=placeholder,
            movement_type="adjustment",
            quantity_delta=Decimal("1.00"),
            reason="pre_sprint3_history",
        )
        db.session.add(movement)
        db.session.commit()
        movement_id = movement.id

    protected = client.get("/")
    assert protected.status_code == 302
    assert protected.location.startswith("/signup")

    _bootstrap_admin(client, app)
    with app.app_context():
        admin = User.query.one()
        assert admin.id == placeholder_id
        assert admin.email == "admin@freshmart.test"
        assert admin.role == "admin"
        assert admin.password_hash != "correct-horse-admin"
        assert admin.check_password("correct-horse-admin") is True
        assert db.session.get(InventoryMovement, movement_id).user_id == placeholder_id
        assert admin.workspace.name == "FreshMart"


def test_login_logout_private_reads_and_safe_redirect(client, app):
    _bootstrap_admin(client, app)
    _logout(client)

    assert client.get("/").status_code == 302
    assert client.get("/api/products").status_code == 401
    assert (
        client.get(
            "/api/products",
            headers={"X-Internal-Token": "test-internal-token"},
        ).status_code
        == 200
    )

    token = _csrf(client, "/login")
    rejected = client.post(
        "/login",
        data={
            "csrf_token": token,
            "identifier": "admin@freshmart.test",
            "password": "wrong-password",
        },
    )
    assert rejected.status_code == 200
    assert b"Incorrect email or password" in rejected.data

    _login(
        client,
        "admin@freshmart.test",
        "correct-horse-admin",
        next_url="//malicious.example",
    )
    assert client.get("/").status_code == 200


def test_admin_manager_and_picker_permissions(client, app, seeded_catalog):
    _bootstrap_admin(client, app)
    _create_user(
        client,
        name="Mina Manager",
        email="manager@freshmart.test",
        role="manager",
        password="correct-horse-manager",
    )
    _create_user(
        client,
        name="Pia Picker",
        email="picker@freshmart.test",
        role="picker",
        password="correct-horse-picker",
    )
    _logout(client)

    _login(client, "manager@freshmart.test", "correct-horse-manager")
    assert client.get("/manage").status_code == 200
    assert client.get("/users").status_code == 403
    _logout(client)

    _login(client, "picker@freshmart.test", "correct-horse-picker")
    assert client.get("/scanner").status_code == 200
    assert client.get("/products").status_code == 200
    assert client.get("/locations").status_code == 200
    assert client.get("/transfers").status_code == 200
    assert client.get("/manage").status_code == 403
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    forbidden_adjustment = client.post(
        "/manage/stock",
        data={
            "csrf_token": token,
            "sku": "TEST-001",
            "location_code": "TEST",
            "quantity_delta": "1",
        },
    )
    assert forbidden_adjustment.status_code == 403


def test_picker_can_scan_and_transfer_but_cannot_see_cost(client, app, seeded_catalog):
    with app.app_context():
        product = db.session.get(Product, seeded_catalog["product_id"])
        product.barcode = "8901234567890"
        db.session.commit()

    _bootstrap_admin(client, app)
    _create_user(
        client,
        name="Pia Picker",
        email="picker@freshmart.test",
        role="picker",
        password="correct-horse-picker",
    )
    destination = client.post(
        "/api/locations",
        headers={"X-Internal-Token": "test-internal-token"},
        json={"name": "Packing Area", "code": "PACK"},
    )
    assert destination.status_code == 201
    _logout(client)
    _login(client, "picker@freshmart.test", "correct-horse-picker")

    scanner_page = client.get("/scanner")
    assert scanner_page.status_code == 200
    assert b"vendor/html5-qrcode.min.js" in scanner_page.data
    assert b"Camera barcode scanner" in scanner_page.data

    lookup = client.get("/api/barcodes/lookup?code=8901234567890")
    assert lookup.status_code == 200
    assert lookup.json["matched_by"] == "barcode"
    assert lookup.json["product"]["sku"] == "TEST-001"
    assert lookup.json["product"]["category"] == "General"
    assert "cost_price" not in lookup.json["product"]

    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    transferred = client.post(
        "/transfers",
        data={
            "csrf_token": token,
            "external_transfer_id": "picker-transfer-1",
            "sku": "TEST-001",
            "source_location_code": "TEST",
            "destination_location_code": "PACK",
            "quantity": "2",
            "note": "Picker replenishment",
        },
    )
    assert transferred.status_code == 302
    with app.app_context():
        transfer = StockTransfer.query.filter_by(external_id="picker-transfer-1").one()
        movement = InventoryMovement.query.filter_by(
            movement_type="transfer", reference_id=transfer.transfer_uid
        ).first()
        assert movement is not None
        assert movement.user.email == "picker@freshmart.test"


def test_admin_user_management_validates_roles_and_protects_current_admin(client, app):
    _bootstrap_admin(client, app)
    _create_user(
        client,
        name="Second Admin",
        email="admin2@freshmart.test",
        role="admin",
        password="correct-horse-second",
    )
    with app.app_context():
        current_id = User.query.filter_by(email="admin@freshmart.test").one().id
        second_id = User.query.filter_by(email="admin2@freshmart.test").one().id

    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    self_demote = client.post(
        f"/users/{current_id}/edit",
        data={
            "csrf_token": token,
            "name": "Surya Admin",
            "email": "admin@freshmart.test",
            "role": "manager",
            "is_active": "true",
            "password": "",
        },
    )
    assert self_demote.status_code == 200
    assert b"cannot demote or deactivate" in self_demote.data

    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    demote_other = client.post(
        f"/users/{second_id}/edit",
        data={
            "csrf_token": token,
            "name": "Second Admin",
            "email": "admin2@freshmart.test",
            "role": "manager",
            "is_active": "true",
            "password": "",
        },
    )
    assert demote_other.status_code == 302
    with app.app_context():
        assert db.session.get(User, current_id).role == "admin"
        assert db.session.get(User, second_id).role == "manager"


def test_sprint3_migration_normalizes_legacy_roles_and_is_idempotent(app):
    with app.app_context():
        migrate_schema()
        db.session.execute(
            text("DELETE FROM schema_migrations WHERE version = :version"),
            {"version": SPRINT_3_SCHEMA_VERSION},
        )
        user = User.query.one()
        user.role = "owner"
        db.session.commit()

        first = migrate_schema()
        second = migrate_schema()
        db.session.expire_all()

        assert first.applied_versions == (SPRINT_3_SCHEMA_VERSION,)
        assert second.applied is False
        assert User.query.one().role == "admin"
        assert SPRINT_3_SCHEMA_VERSION in current_schema_versions()
