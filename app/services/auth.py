from __future__ import annotations

import re
import secrets
from collections.abc import Mapping

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import InventoryLocation, User, Workspace, WorkspaceMembership
from app.services.identity import ensure_default_identity, normalize_business_username


ROLES = ("admin", "manager", "picker")
ROLE_LABELS = {
    "admin": "Admin",
    "manager": "Manager",
    "picker": "Picker",
}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128


class AuthenticationError(ValueError):
    pass


class UserValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]):
        self.errors = dict(errors)
        super().__init__(
            "; ".join(f"{field}: {message}" for field, message in self.errors.items())
        )


def _name(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("is required")
    if len(normalized) > 255:
        raise ValueError("must be 255 characters or fewer")
    return normalized


def _email(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("is required")
    if len(normalized) > 255 or not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("must be a valid email address")
    return normalized


def _password(value: object, *, required: bool = True) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError("is required")
        return None
    if not isinstance(value, str):
        raise ValueError("must be text")
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(value) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"must be {MAX_PASSWORD_LENGTH} characters or fewer")
    return value


def _role(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in ROLES:
        raise ValueError("must be admin, manager, or picker")
    return normalized


def authentication_setup_required() -> bool:
    return not db.session.query(User.id).filter(
        User.is_active.is_(True),
        User.password_hash.is_not(None),
        User.password_hash != "",
    ).first()


def activate_legacy_credentials() -> User | None:
    """Move the former environment-based shared login into the user table once."""

    username = current_app.config.get("STAFF_USERNAME", "").strip()
    password = current_app.config.get("STAFF_PASSWORD", "")
    if not username or not password:
        return None
    actor = ensure_default_identity()
    if not actor.password_hash:
        actor.name = username[:255]
        actor.set_password(password)
        actor.role = "admin"
        actor.is_active = True
        db.session.commit()
    return actor


def authenticate(
    identifier: object, password: object, *, business_username: object | None = None
) -> User | None:
    normalized = str(identifier or "").strip().lower()
    supplied_password = str(password or "")
    if not normalized or not supplied_password:
        return None

    user = User.query.filter(func.lower(User.email) == normalized).first()
    legacy_username = current_app.config.get("STAFF_USERNAME", "").strip().lower()
    if user is None and legacy_username and secrets.compare_digest(
        normalized, legacy_username
    ):
        user = User.query.filter_by(
            email=current_app.config["DEFAULT_STAFF_EMAIL"].strip().lower()
        ).first()
    if user is None or not user.is_active or not user.check_password(supplied_password):
        return None
    if business_username:
        workspace = Workspace.query.filter(
            func.lower(Workspace.business_username)
            == normalize_business_username(business_username)
        ).first()
        if workspace is None or WorkspaceMembership.query.filter_by(
            user_id=user.id, workspace_id=workspace.id, is_active=True
        ).first() is None:
            return None
    elif WorkspaceMembership.query.filter_by(
        user_id=user.id, is_active=True
    ).count() > 1:
        # Do not guess which tenant a shared identity intended to enter.
        return None
    return user


class UserService:
    @staticmethod
    def validate(
        payload: Mapping[str, object],
        *,
        partial: bool = False,
        current_user: User | None = None,
    ) -> dict:
        if not isinstance(payload, Mapping):
            raise UserValidationError({"payload": "must be an object"})
        errors: dict[str, str] = {}
        values: dict[str, object] = {}

        for field, parser in (("name", _name), ("email", _email), ("role", _role)):
            if field not in payload:
                if not partial:
                    errors[field] = "is required"
                continue
            try:
                values[field] = parser(payload.get(field))
            except ValueError as error:
                errors[field] = str(error)

        password_supplied = "password" in payload and payload.get("password") not in (
            None,
            "",
        )
        if not partial or password_supplied:
            try:
                values["password"] = _password(
                    payload.get("password"), required=not partial
                )
            except ValueError as error:
                errors["password"] = str(error)

        if "is_active" in payload:
            raw_active = payload.get("is_active")
            if isinstance(raw_active, bool):
                values["is_active"] = raw_active
            else:
                normalized = str(raw_active or "").strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    values["is_active"] = True
                elif normalized in {"0", "false", "no", "off", ""}:
                    values["is_active"] = False
                else:
                    errors["is_active"] = "must be true or false"
        elif not partial:
            values["is_active"] = True

        email = values.get("email")
        if email:
            query = User.query.filter(func.lower(User.email) == email)
            if current_user is not None:
                query = query.filter(User.id != current_user.id)
            if query.first():
                errors["email"] = "already exists"

        if errors:
            raise UserValidationError(errors)
        return values

    @staticmethod
    def bootstrap_admin(payload: Mapping[str, object]) -> User:
        if not authentication_setup_required():
            raise AuthenticationError("Account setup has already been completed.")

        setup_payload = dict(payload)
        setup_payload["role"] = "admin"
        setup_payload["is_active"] = True
        values = UserService.validate(setup_payload)
        password_confirmation = payload.get("password_confirm")
        if password_confirmation is not None and password_confirmation != values["password"]:
            raise UserValidationError({"password_confirm": "does not match password"})
        workspace_name = str(
            payload.get("business_name") or payload.get("workspace_name") or ""
        ).strip()
        if len(workspace_name) > 255:
            raise UserValidationError(
                {"workspace_name": "must be 255 characters or fewer"}
            )

        actor = ensure_default_identity(commit=False)
        if workspace_name:
            actor.workspace.name = workspace_name
            requested_username = normalize_business_username(
                payload.get("business_username") or workspace_name
            )
            conflict = Workspace.query.filter(
                func.lower(Workspace.business_username) == requested_username,
                Workspace.id != actor.workspace.id,
            ).first()
            if conflict:
                raise UserValidationError(
                    {"business_username": "is already in use"}
                )
            actor.workspace.business_username = requested_username
        actor.name = values["name"]
        actor.email = values["email"]
        actor.role = "admin"
        actor.is_active = True
        actor.set_password(values["password"])
        membership = WorkspaceMembership.query.filter_by(
            user_id=actor.id, workspace_id=actor._workspace_id
        ).first()
        if membership:
            membership.role = "admin"
            membership.is_active = True
        warehouse_name = str(payload.get("warehouse_name") or "").strip()
        warehouse_address = str(payload.get("warehouse_address") or "").strip()
        if warehouse_name and not InventoryLocation.query.filter_by(
            workspace_id=actor._workspace_id
        ).first():
            code = re.sub(r"[^A-Z0-9]+", "", warehouse_name.upper())[:12] or "MAIN"
            db.session.add(
                InventoryLocation(
                    workspace_id=actor._workspace_id,
                    name=warehouse_name[:120],
                    code=code,
                    address=warehouse_address[:255] or None,
                )
            )
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise UserValidationError({"email": "already exists"}) from error
        return actor

    @staticmethod
    def create(
        payload: Mapping[str, object], *, workspace: Workspace
    ) -> User:
        values = UserService.validate(payload)
        password = values.pop("password")
        user = User(workspace=workspace, **values)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        db.session.add(
            WorkspaceMembership(
                user=user, workspace=workspace, role=user._role, is_active=True
            )
        )
        UserService._commit()
        return user

    @staticmethod
    def update(
        user: User,
        payload: Mapping[str, object],
        *,
        acting_user: User,
    ) -> User:
        values = UserService.validate(payload, partial=True, current_user=user)
        if not values:
            raise UserValidationError({"payload": "include at least one editable field"})

        membership = WorkspaceMembership.query.filter_by(
            user_id=user.id, workspace_id=acting_user.workspace_id
        ).first()
        if membership is None:
            raise UserValidationError({"account": "does not belong to this workspace"})
        requested_role = values.get("role", membership.role)
        requested_active = values.get("is_active", membership.is_active)
        if user.id == acting_user.id and (
            requested_role != "admin" or not requested_active
        ):
            raise UserValidationError(
                {"account": "you cannot demote or deactivate your signed-in account"}
            )
        if membership.role == "admin" and membership.is_active and (
            requested_role != "admin" or not requested_active
        ):
            other_admin = WorkspaceMembership.query.join(User).filter(
                WorkspaceMembership.workspace_id == acting_user.workspace_id,
                WorkspaceMembership.user_id != user.id,
                WorkspaceMembership.role == "admin",
                WorkspaceMembership.is_active.is_(True),
                User.is_active.is_(True),
            ).first()
            if other_admin is None:
                raise UserValidationError(
                    {"account": "at least one active admin must remain"}
                )

        password = values.pop("password", None)
        role = values.pop("role", None)
        active = values.pop("is_active", None)
        for field, value in values.items():
            setattr(user, field, value)
        if password:
            user.set_password(password)
        if role is not None:
            membership.role = str(role)
            if user._workspace_id == membership.workspace_id:
                user._role = str(role)
        if active is not None:
            membership.is_active = bool(active)
            # Preserve legacy one-workspace account deactivation semantics, but
            # never disable a person's global identity because one tenant did.
            if len(user.memberships) == 1:
                user.is_active = bool(active)
        UserService._commit()
        return user

    @staticmethod
    def _commit() -> None:
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise UserValidationError({"email": "already exists"}) from error


def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "workspace_id": user.workspace_id,
        "created_at": user.created_at.isoformat(),
    }
