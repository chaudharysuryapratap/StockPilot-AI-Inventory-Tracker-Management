from __future__ import annotations

import re
import secrets
from collections.abc import Mapping

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import User, Workspace
from app.services.identity import ensure_default_identity


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


def authenticate(identifier: object, password: object) -> User | None:
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
        workspace_name = str(payload.get("workspace_name", "")).strip()
        if len(workspace_name) > 255:
            raise UserValidationError(
                {"workspace_name": "must be 255 characters or fewer"}
            )

        actor = ensure_default_identity(commit=False)
        if workspace_name:
            actor.workspace.name = workspace_name
        actor.name = values["name"]
        actor.email = values["email"]
        actor.role = "admin"
        actor.is_active = True
        actor.set_password(values["password"])
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

        requested_role = values.get("role", user.role)
        requested_active = values.get("is_active", user.is_active)
        if user.id == acting_user.id and (
            requested_role != "admin" or not requested_active
        ):
            raise UserValidationError(
                {"account": "you cannot demote or deactivate your signed-in account"}
            )
        if user.role == "admin" and user.is_active and (
            requested_role != "admin" or not requested_active
        ):
            other_admin = User.query.filter(
                User.id != user.id,
                User.workspace_id == user.workspace_id,
                User.role == "admin",
                User.is_active.is_(True),
            ).first()
            if other_admin is None:
                raise UserValidationError(
                    {"account": "at least one active admin must remain"}
                )

        password = values.pop("password", None)
        for field, value in values.items():
            setattr(user, field, value)
        if password:
            user.set_password(password)
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
