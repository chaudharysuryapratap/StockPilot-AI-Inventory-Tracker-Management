from __future__ import annotations

import base64

import pytest

from app import create_app, db
from app.models import (
    AuthToken,
    InventoryLocation,
    LoginAttempt,
    Product,
    User,
    Workspace,
    WorkspaceIntegration,
    WorkspaceMembership,
    WorkspaceSetting,
)
from app.services.saas_auth import AuthTokenService, MFAService, WorkspaceService


@pytest.fixture
def saas_app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "APP_ENV": "testing",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'saas.db'}",
            "STAFF_AUTH_ENABLED": True,
            "ALLOW_WEB_SIGNUP": True,
            "REQUIRE_EMAIL_VERIFICATION": False,
            "STAFF_USERNAME": "",
            "STAFF_PASSWORD": "",
            "BEDROCK_ENABLED": False,
            "SES_ENABLED": False,
            "AUTH_EMAIL_ENABLED": False,
            "MFA_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"m" * 32).decode(),
            "DEFAULT_PAGE_SIZE": 10,
            "MAX_PAGE_SIZE": 50,
            "LOGIN_MAX_ATTEMPTS": 3,
        }
    )
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def saas_client(saas_app):
    return saas_app.test_client()


def _csrf(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    with client.session_transaction() as browser_session:
        return browser_session["csrf_token"]


def _signup(client, *, handle="freshmart"):
    token = _csrf(client, "/signup")
    response = client.post(
        "/signup",
        data={
            "csrf_token": token,
            "business_name": "FreshMart Retail",
            "business_username": handle,
            "warehouse_name": "Central Warehouse",
            "warehouse_address": "12 Market Road, Bengaluru 560001",
            "name": "Surya Admin",
            "email": "admin@freshmart.test",
            "password": "correct-horse-admin",
            "password_confirm": "correct-horse-admin",
        },
    )
    assert response.status_code == 302
    assert response.location == "/"


def _logout(client):
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302


def _login(
    client,
    *,
    handle="freshmart",
    email="admin@freshmart.test",
    password="correct-horse-admin",
):
    token = _csrf(client, "/login")
    return client.post(
        "/login",
        data={
            "csrf_token": token,
            "business_username": handle,
            "identifier": email,
            "password": password,
        },
    )


def test_modern_signup_creates_unique_business_warehouse_and_membership(
    saas_client, saas_app
):
    page = saas_client.get("/signup")
    assert page.status_code == 200
    assert b"Business username" in page.data
    assert b"Warehouse address" in page.data
    assert b"Build your inventory command centre" in page.data

    _signup(saas_client)
    with saas_app.app_context():
        workspace = Workspace.query.one()
        admin = User.query.one()
        warehouse = InventoryLocation.query.one()
        membership = WorkspaceMembership.query.one()
        assert workspace.name == "FreshMart Retail"
        assert workspace.business_username == "freshmart"
        assert warehouse.name == "Central Warehouse"
        assert warehouse.address == "12 Market Road, Bengaluru 560001"
        assert membership.user_id == admin.id
        assert membership.workspace_id == workspace.id
        assert membership.role == "admin"
        assert WorkspaceSetting.query.filter_by(workspace_id=workspace.id).one()
        assert admin.check_password("correct-horse-admin")

    availability = saas_client.get(
        "/api/workspaces/availability?username=freshmart"
    )
    assert availability.json == {
        "username": "freshmart",
        "valid": True,
        "available": False,
    }


def test_single_business_warehouse_creation_settings_and_catalogue_isolation(
    saas_client, saas_app
):
    _signup(saas_client)
    with saas_app.app_context():
        first = Workspace.query.filter_by(business_username="freshmart").one()
        db.session.add(
            Product(
                workspace=first,
                sku="FRESH-ONLY",
                name="FreshMart product",
                category="General",
                unit_of_measure="unit",
            )
        )
        db.session.commit()
        first_id = first.id

    with saas_client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    redirected = saas_client.post(
        "/workspaces/new",
        data={
            "csrf_token": token,
            "business_name": "Northwind Foods",
            "business_username": "northwind",
            "warehouse_name": "North Dock",
            "warehouse_address": "8 Harbour Street, Chennai",
        },
    )
    assert redirected.status_code == 302
    assert redirected.location == "/warehouses"

    with saas_app.app_context():
        assert Workspace.query.count() == 1
        assert WorkspaceMembership.query.count() == 1

    with saas_client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    warehouse = saas_client.post(
        "/warehouses",
        data={
            "csrf_token": token,
            "name": "North Dock",
            "code": "NORTH",
            "address": "8 Harbour Street, Chennai",
        },
    )
    assert warehouse.location == "/warehouses"
    first_catalogue = saas_client.get("/api/products")
    assert [row["sku"] for row in first_catalogue.json["products"]] == ["FRESH-ONLY"]
    with saas_app.app_context():
        assert InventoryLocation.query.filter_by(workspace_id=first_id).count() == 2

    with saas_client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    settings = saas_client.post(
        "/workspace/settings",
        data={
            "csrf_token": token,
            "business_name": "FreshMart Retail",
            "business_username": "freshmart",
            "timezone": "Asia/Kolkata",
            "currency": "INR",
            "oidc_enabled": "on",
            "oidc_issuer": "https://identity.example.test",
            "oidc_client_id": "stockpilot-client",
            "oidc_secret_reference": "OIDC_SECRET_FRESHMART",
            "oidc_default_role": "manager",
        },
    )
    assert settings.status_code == 302
    with saas_app.app_context():
        integration = WorkspaceIntegration.query.filter_by(
            workspace_id=first_id, provider="oidc"
        ).one()
        assert integration.secret_reference == "OIDC_SECRET_FRESHMART"
        assert "secret" not in integration.config_json
        assert Workspace.query.count() == 1


def test_invitation_verification_password_reset_and_durable_throttle(
    saas_client, saas_app
):
    _signup(saas_client)
    with saas_app.app_context():
        admin = User.query.one()
        workspace = Workspace.query.one()
        verification_raw, _ = AuthTokenService.verification(admin)
        invitation_raw, _ = WorkspaceService.invite(
            workspace=workspace,
            acting_user=admin,
            email="manager@freshmart.test",
            role="manager",
        )

    verified = saas_client.get(f"/verify-email/{verification_raw}")
    assert verified.status_code == 302
    invitee = saas_app.test_client()
    token = _csrf(invitee, f"/invitations/{invitation_raw}")
    accepted = invitee.post(
        f"/invitations/{invitation_raw}",
        data={
            "csrf_token": token,
            "name": "Mira Manager",
            "password": "manager-password-1",
            "password_confirm": "manager-password-1",
        },
    )
    assert accepted.status_code == 302
    assert b"Today\xe2\x80\x99s inventory decisions" in invitee.get("/").data

    with saas_app.app_context():
        manager = User.query.filter_by(email="manager@freshmart.test").one()
        assert manager.email_verified_at is not None
        assert WorkspaceMembership.query.filter_by(user_id=manager.id).one().role == "manager"
        admin = User.query.filter_by(email="admin@freshmart.test").one()
        reset_raw, _ = AuthTokenService.password_reset(admin)

    reset_client = saas_app.test_client()
    token = _csrf(reset_client, f"/reset-password/{reset_raw}")
    reset = reset_client.post(
        f"/reset-password/{reset_raw}",
        data={
            "csrf_token": token,
            "password": "new-correct-horse",
            "password_confirm": "new-correct-horse",
        },
    )
    assert reset.status_code == 302
    with saas_app.app_context():
        assert User.query.filter_by(email="admin@freshmart.test").one().check_password(
            "new-correct-horse"
        )
        assert AuthToken.query.filter_by(purpose="password_reset").one().consumed_at

    throttle_client = saas_app.test_client()
    token = _csrf(throttle_client, "/login")
    payload = {
        "csrf_token": token,
        "business_username": "freshmart",
        "identifier": "admin@freshmart.test",
        "password": "wrong-password",
    }
    for _ in range(3):
        assert throttle_client.post("/login", data=payload).status_code == 200
    assert throttle_client.post("/login", data=payload).status_code == 429
    with saas_app.app_context():
        assert LoginAttempt.query.count() == 3


def test_mfa_challenge_and_tenant_bound_cursor_pagination(saas_client, saas_app):
    _signup(saas_client)
    with saas_app.app_context():
        admin = User.query.one()
        workspace = Workspace.query.one()
        for index in range(26):
            db.session.add(
                Product(
                    workspace=workspace,
                    sku=f"PAGE-{index:03d}",
                    name=f"Pagination product {index:03d}",
                    category="Scale",
                    unit_of_measure="unit",
                )
            )
        secret = MFAService.generate_secret()
        admin.mfa_secret_encrypted = MFAService.encrypt(secret)
        db.session.commit()
        recovery_codes = MFAService.enable(admin, secret, MFAService.code(secret))
        workspace_id = workspace.id

    _logout(saas_client)
    login = _login(saas_client)
    assert login.location == "/mfa/challenge"
    token = _csrf(saas_client, "/mfa/challenge")
    challenged = saas_client.post(
        "/mfa/challenge",
        data={"csrf_token": token, "code": recovery_codes[0]},
    )
    assert challenged.location == "/"

    first_page = saas_client.get("/api/products?limit=10&q=Pagination")
    assert first_page.status_code == 200
    assert len(first_page.json["products"]) == 10
    assert first_page.json["pagination"]["total"] == 26
    next_url = first_page.json["pagination"]["next_url"]
    second_page = saas_client.get(next_url)
    assert second_page.status_code == 200
    assert {
        row["id"] for row in first_page.json["products"]
    }.isdisjoint({row["id"] for row in second_page.json["products"]})
    assert second_page.json["pagination"]["previous_url"]
    returned_first_page = saas_client.get(
        second_page.json["pagination"]["previous_url"]
    )
    assert returned_first_page.status_code == 200
    assert [row["id"] for row in returned_first_page.json["products"]] == [
        row["id"] for row in first_page.json["products"]
    ]

    tampered_cursor = first_page.json["pagination"]["next_cursor"] + "x"
    assert saas_client.get(
        f"/api/products?limit=10&cursor={tampered_cursor}"
    ).status_code == 400

    with saas_app.app_context():
        _, _ = WorkspaceService.create_for_user(
            {
                "business_name": "Cursor Other",
                "business_username": "cursor-other",
                "warehouse_name": "Other Warehouse",
                "warehouse_address": "Other Address",
                "name": "Other Admin",
                "email": "other@cursor.test",
                "password": "correct-horse-admin",
                "password_confirm": "correct-horse-admin",
            },
        )
    other_client = saas_app.test_client()
    assert _login(
        other_client, handle="cursor-other", email="other@cursor.test"
    ).status_code == 302
    assert other_client.get(next_url).status_code == 400
    with saas_app.app_context():
        assert WorkspaceMembership.query.filter_by(
            workspace_id=workspace_id, role="admin"
        ).count() == 1


def test_viewer_is_read_only_and_manager_cannot_add_warehouses(
    saas_client, saas_app
):
    _signup(saas_client)
    with saas_client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    created = saas_client.post(
        "/users",
        data={
            "csrf_token": token,
            "name": "Vera Viewer",
            "email": "viewer@freshmart.test",
            "role": "viewer",
            "password": "viewer-password-1",
            "is_active": "true",
        },
    )
    assert created.status_code == 302

    template = saas_client.get("/templates/products-import.csv")
    assert template.status_code == 200
    assert template.headers["Content-Disposition"].startswith("attachment;")
    assert b"sku,barcode,name,category,unit_of_measure" in template.data
    assert b"location_code,bin_code,quantity_on_hand" in template.data
    assert b"EXAMPLE-001" in template.data

    _logout(saas_client)
    viewer_login = _login(
        saas_client,
        email="viewer@freshmart.test",
        password="viewer-password-1",
    )
    assert viewer_login.location == "/"
    dashboard = saas_client.get("/")
    assert b"Inventory at a glance" in dashboard.data
    assert b"Read-only views" in dashboard.data
    assert b"AI purchasing" not in dashboard.data
    warehouses = saas_client.get("/warehouses")
    assert b"Read-only warehouse view" in warehouses.data
    assert b"Add a new warehouse" not in warehouses.data
    with saas_client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    assert saas_client.post(
        "/warehouses",
        data={"csrf_token": token, "name": "Blocked", "code": "BLOCKED"},
    ).status_code == 403
    assert saas_client.get("/manage").status_code == 403
    assert saas_client.get("/reports").status_code == 200

    with saas_app.app_context():
        viewer = User.query.filter_by(email="viewer@freshmart.test").one()
        membership = WorkspaceMembership.query.filter_by(user_id=viewer.id).one()
        membership.role = "manager"
        viewer._role = "manager"
        db.session.commit()
    assert saas_client.post(
        "/warehouses",
        data={"csrf_token": token, "name": "Still blocked", "code": "MANAGER"},
    ).status_code == 403


def test_profile_menu_settings_and_warehouse_command_centre(
    saas_client, saas_app
):
    _signup(saas_client)
    with saas_client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    created = saas_client.post(
        "/warehouses",
        data={
            "csrf_token": token,
            "name": "South Warehouse",
            "code": "SOUTH",
            "address": "South Industrial Estate",
        },
    )
    assert created.location == "/warehouses"
    with saas_app.app_context():
        south = InventoryLocation.query.filter_by(code="SOUTH").one()
        south_id = south.id

    command_centre = saas_client.get("/?scope=all")
    assert b'id="profile-menu"' in command_centre.data
    assert b"Inventory Command Centre" in command_centre.data
    assert b"Every warehouse, one command centre" in command_centre.data
    assert b"South Warehouse" in command_centre.data

    focused = saas_client.get(f"/?scope=warehouse&warehouse_id={south_id}")
    assert b"Dashboard details are filtered to this warehouse" in focused.data
    assert b'data-warehouse-id="' + str(south_id).encode() + b'"' in focused.data

    updated = saas_client.post(
        "/profile",
        data={
            "csrf_token": token,
            "name": "Surya Profile",
            "email": "profile@freshmart.test",
        },
    )
    assert updated.location == "/profile"
    profile = saas_client.get("/profile")
    assert b"Surya Profile" in profile.data
    assert b"profile@freshmart.test" in profile.data
