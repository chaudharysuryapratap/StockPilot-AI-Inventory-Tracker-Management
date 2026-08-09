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


def _login(client, *, handle="freshmart", email="admin@freshmart.test"):
    token = _csrf(client, "/login")
    return client.post(
        "/login",
        data={
            "csrf_token": token,
            "business_username": handle,
            "identifier": email,
            "password": "correct-horse-admin",
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


def test_workspace_creation_switching_settings_and_catalogue_isolation(
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
    created = saas_client.post(
        "/workspaces/new",
        data={
            "csrf_token": token,
            "business_name": "Northwind Foods",
            "business_username": "northwind",
            "warehouse_name": "North Dock",
            "warehouse_address": "8 Harbour Street, Chennai",
        },
    )
    assert created.status_code == 302

    with saas_app.app_context():
        second = Workspace.query.filter_by(business_username="northwind").one()
        second_id = second.id
        assert WorkspaceMembership.query.count() == 2
        db.session.add(
            Product(
                workspace=second,
                sku="NORTH-ONLY",
                name="Northwind product",
                category="General",
                unit_of_measure="unit",
            )
        )
        db.session.commit()

    second_catalogue = saas_client.get("/api/products")
    assert [row["sku"] for row in second_catalogue.json["products"]] == ["NORTH-ONLY"]

    with saas_client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    switched = saas_client.post(
        f"/workspaces/{first_id}/switch",
        data={"csrf_token": token, "next": "/products"},
    )
    assert switched.location == "/products"
    first_catalogue = saas_client.get("/api/products")
    assert [row["sku"] for row in first_catalogue.json["products"]] == ["FRESH-ONLY"]

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
        assert db.session.get(Workspace, second_id).business_username == "northwind"


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

    tampered_cursor = first_page.json["pagination"]["next_cursor"] + "x"
    assert saas_client.get(
        f"/api/products?limit=10&cursor={tampered_cursor}"
    ).status_code == 400

    with saas_app.app_context():
        admin = User.query.one()
        other, _ = WorkspaceService.create_for_user(
            {
                "business_name": "Cursor Other",
                "business_username": "cursor-other",
                "warehouse_name": "Other Warehouse",
                "warehouse_address": "Other Address",
            },
            user=admin,
        )
        other_id = other.id
    with saas_client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]
    saas_client.post(
        f"/workspaces/{other_id}/switch", data={"csrf_token": token}
    )
    assert saas_client.get(next_url).status_code == 400
    with saas_app.app_context():
        assert WorkspaceMembership.query.filter_by(
            workspace_id=workspace_id, role="admin"
        ).count() == 1
