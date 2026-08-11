from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from datetime import timedelta, timezone
from urllib.parse import quote

import boto3
from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    AuthToken,
    InventoryLocation,
    LoginAttempt,
    MFARecoveryCode,
    User,
    Workspace,
    WorkspaceIntegration,
    WorkspaceMembership,
    WorkspaceSetting,
    utcnow,
)
from app.services.auth import EMAIL_PATTERN, ROLES, UserService, UserValidationError
from app.services.identity import normalize_business_username


BUSINESS_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


def _aware(value):
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _workspace_code(name: str) -> str:
    letters = re.sub(r"[^A-Z0-9]+", "", name.upper())
    return (letters[:12] or "MAIN")


class WorkspaceValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{key}: {value}" for key, value in errors.items()))


class WorkspaceService:
    @staticmethod
    def validate_identity(
        payload: dict,
        *,
        require_warehouse: bool = True,
        existing_workspace: Workspace | None = None,
    ) -> dict:
        errors: dict[str, str] = {}
        business_name = str(
            payload.get("business_name") or payload.get("workspace_name") or ""
        ).strip()
        business_username = normalize_business_username(
            payload.get("business_username") or business_name
        )
        warehouse_name = str(payload.get("warehouse_name") or "").strip()
        warehouse_address = str(payload.get("warehouse_address") or "").strip()
        if not business_name:
            errors["business_name"] = "is required"
        elif len(business_name) > 255:
            errors["business_name"] = "must be 255 characters or fewer"
        if not BUSINESS_USERNAME_PATTERN.fullmatch(business_username):
            errors["business_username"] = (
                "must be 3–63 lowercase letters, numbers, or hyphens"
            )
        elif Workspace.query.filter(
            func.lower(Workspace.business_username) == business_username,
            Workspace.id != (existing_workspace.id if existing_workspace else -1),
        ).first():
            errors["business_username"] = "is already in use"
        if require_warehouse:
            if not warehouse_name:
                errors["warehouse_name"] = "is required"
            elif len(warehouse_name) > 120:
                errors["warehouse_name"] = "must be 120 characters or fewer"
            if not warehouse_address:
                errors["warehouse_address"] = "is required"
            elif len(warehouse_address) > 255:
                errors["warehouse_address"] = "must be 255 characters or fewer"
        if errors:
            raise WorkspaceValidationError(errors)
        return {
            "business_name": business_name,
            "business_username": business_username,
            "warehouse_name": warehouse_name,
            "warehouse_address": warehouse_address,
        }

    @staticmethod
    def create_for_user(payload: dict, *, user: User | None = None) -> tuple[Workspace, User]:
        if user is not None:
            raise WorkspaceValidationError(
                {"account": "an account can belong to only one business"}
            )
        identity = WorkspaceService.validate_identity(payload)
        creating_user = user is None
        user_values = None
        if creating_user:
            account_payload = dict(payload)
            account_payload["role"] = "admin"
            account_payload["is_active"] = True
            user_values = UserService.validate(account_payload)
            if payload.get("password_confirm") != user_values["password"]:
                raise UserValidationError(
                    {"password_confirm": "does not match password"}
                )

        workspace = Workspace(
            name=identity["business_name"],
            business_username=identity["business_username"],
        )
        db.session.add(workspace)
        db.session.flush()
        db.session.add(
            WorkspaceSetting(
                workspace=workspace,
                timezone=str(payload.get("timezone") or "Asia/Kolkata")[:64],
                currency=str(payload.get("currency") or "INR").upper()[:3],
            )
        )
        db.session.add(
            InventoryLocation(
                workspace=workspace,
                name=identity["warehouse_name"],
                code=_workspace_code(identity["warehouse_name"]),
                address=identity["warehouse_address"],
                is_active=True,
            )
        )
        if creating_user:
            password = user_values.pop("password")
            user = User(workspace=workspace, **user_values)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
        membership = WorkspaceMembership(
            workspace=workspace, user=user, role="admin", is_active=True
        )
        db.session.add(membership)
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise WorkspaceValidationError(
                {"business_username": "is already in use"}
            ) from error
        return workspace, user

    @staticmethod
    def membership(user: User, workspace_id: int) -> WorkspaceMembership | None:
        return WorkspaceMembership.query.filter_by(
            user_id=user.id, workspace_id=workspace_id, is_active=True
        ).first()

    @staticmethod
    def invite(
        *, workspace: Workspace, acting_user: User, email: object, role: object
    ) -> tuple[str, AuthToken]:
        normalized_email = str(email or "").strip().lower()
        normalized_role = str(role or "").strip().lower()
        errors = {}
        if not EMAIL_PATTERN.fullmatch(normalized_email):
            errors["email"] = "must be a valid email address"
        if normalized_role not in ROLES:
            errors["role"] = "must be admin, manager, picker, or viewer"
        existing_user = User.query.filter_by(email=normalized_email).first()
        if existing_user and WorkspaceMembership.query.filter_by(
            user_id=existing_user.id, workspace_id=workspace.id
        ).first():
            errors["email"] = "already belongs to this business"
        elif existing_user and WorkspaceMembership.query.filter_by(
            user_id=existing_user.id, is_active=True
        ).first():
            errors["email"] = "already belongs to another business"
        if errors:
            raise WorkspaceValidationError(errors)
        return AuthTokenService.issue(
            "invitation",
            workspace=workspace,
            email=normalized_email,
            payload={"role": normalized_role, "invited_by": acting_user.id},
            ttl=timedelta(hours=current_app.config["INVITATION_EXPIRY_HOURS"]),
        )

    @staticmethod
    def accept_invitation(token: AuthToken, payload: dict, *, current_user: User | None) -> User:
        email = str(token.email or "").lower()
        user = current_user or User.query.filter_by(email=email).first()
        if current_user and current_user.email.lower() != email:
            raise WorkspaceValidationError(
                {"email": "sign in with the invited email address"}
            )
        if user is not None:
            other_membership = WorkspaceMembership.query.filter(
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.workspace_id != token.workspace_id,
                WorkspaceMembership.is_active.is_(True),
            ).first()
            if other_membership is not None:
                raise WorkspaceValidationError(
                    {"account": "this account already belongs to another business"}
                )
        if user is None:
            account_payload = dict(payload)
            account_payload["email"] = email
            account_payload["role"] = token.payload_json.get("role", "picker")
            account_payload["is_active"] = True
            values = UserService.validate(account_payload)
            if payload.get("password_confirm") != values["password"]:
                raise UserValidationError(
                    {"password_confirm": "does not match password"}
                )
            password = values.pop("password")
            user = User(workspace_id=token.workspace_id, **values)
            user.set_password(password)
            user.email_verified_at = utcnow()
            db.session.add(user)
            db.session.flush()
        membership = WorkspaceMembership.query.filter_by(
            user_id=user.id, workspace_id=token.workspace_id
        ).first()
        if membership is None:
            membership = WorkspaceMembership(
                user=user,
                workspace_id=token.workspace_id,
                role=token.payload_json.get("role", "picker"),
                is_active=True,
            )
            db.session.add(membership)
        else:
            membership.is_active = True
            membership.role = token.payload_json.get("role", membership.role)
        token.consumed_at = utcnow()
        db.session.commit()
        return user


class AuthTokenService:
    @staticmethod
    def issue(
        purpose: str,
        *,
        user: User | None = None,
        workspace: Workspace | None = None,
        email: str | None = None,
        payload: dict | None = None,
        ttl: timedelta,
    ) -> tuple[str, AuthToken]:
        query = AuthToken.query.filter_by(purpose=purpose, consumed_at=None)
        if workspace:
            query = query.filter_by(workspace_id=workspace.id)
        if user:
            query = query.filter_by(user_id=user.id)
        elif email:
            query = query.filter(func.lower(AuthToken.email) == email.lower())
        for existing in query.all():
            existing.consumed_at = utcnow()
        raw = secrets.token_urlsafe(32)
        record = AuthToken(
            purpose=purpose,
            user=user,
            workspace=workspace,
            email=email or (user.email if user else None),
            token_hash=_token_hash(raw),
            payload_json=payload or {},
            expires_at=utcnow() + ttl,
        )
        db.session.add(record)
        db.session.commit()
        return raw, record

    @staticmethod
    def resolve(raw: object, purpose: str) -> AuthToken | None:
        normalized = str(raw or "").strip()
        if not normalized:
            return None
        record = AuthToken.query.filter_by(
            token_hash=_token_hash(normalized), purpose=purpose, consumed_at=None
        ).first()
        if record is None or _aware(record.expires_at) <= utcnow():
            return None
        return record

    @staticmethod
    def verification(user: User) -> tuple[str, AuthToken]:
        return AuthTokenService.issue(
            "email_verification",
            user=user,
            workspace=user.workspace,
            ttl=timedelta(hours=current_app.config["EMAIL_VERIFICATION_HOURS"]),
        )

    @staticmethod
    def password_reset(user: User) -> tuple[str, AuthToken]:
        return AuthTokenService.issue(
            "password_reset",
            user=user,
            workspace=user.workspace,
            ttl=timedelta(minutes=current_app.config["PASSWORD_RESET_MINUTES"]),
        )


class LoginThrottle:
    @staticmethod
    def key(remote_addr: str | None, identifier: object) -> str:
        material = f"{remote_addr or 'unknown'}:{str(identifier or '').strip().lower()}"
        return _token_hash(material)

    @staticmethod
    def is_limited(remote_addr: str | None, identifier: object) -> bool:
        key = LoginThrottle.key(remote_addr, identifier)
        cutoff = utcnow() - timedelta(seconds=current_app.config["LOGIN_WINDOW_SECONDS"])
        return LoginAttempt.query.filter(
            LoginAttempt.attempt_key_hash == key,
            LoginAttempt.succeeded.is_(False),
            LoginAttempt.attempted_at >= cutoff,
        ).count() >= current_app.config["LOGIN_MAX_ATTEMPTS"]

    @staticmethod
    def record(remote_addr: str | None, identifier: object, *, succeeded: bool) -> None:
        key = LoginThrottle.key(remote_addr, identifier)
        if succeeded:
            LoginAttempt.query.filter_by(attempt_key_hash=key, succeeded=False).delete()
        else:
            db.session.add(LoginAttempt(attempt_key_hash=key, succeeded=False))
        cutoff = utcnow() - timedelta(days=1)
        LoginAttempt.query.filter(LoginAttempt.attempted_at < cutoff).delete()
        db.session.commit()


class MFAService:
    @staticmethod
    def _fernet():
        try:
            from cryptography.fernet import Fernet
        except ImportError as error:
            raise RuntimeError("cryptography is required to enable MFA") from error
        configured = current_app.config.get("MFA_ENCRYPTION_KEY", "").strip()
        if configured:
            key = configured.encode("ascii")
        else:
            digest = hashlib.sha256(
                (current_app.config["SECRET_KEY"] + ":stockpilot:mfa").encode("utf-8")
            ).digest()
            key = base64.urlsafe_b64encode(digest)
        return Fernet(key)

    @staticmethod
    def generate_secret() -> str:
        return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")

    @staticmethod
    def encrypt(secret: str) -> str:
        return MFAService._fernet().encrypt(secret.encode("ascii")).decode("ascii")

    @staticmethod
    def decrypt(user: User) -> str:
        if not user.mfa_secret_encrypted:
            raise ValueError("MFA is not configured")
        return MFAService._fernet().decrypt(
            user.mfa_secret_encrypted.encode("ascii")
        ).decode("ascii")

    @staticmethod
    def code(secret: str, timestamp: int | None = None) -> str:
        counter = int(timestamp or time.time()) // 30
        padded = secret + "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode(padded, casefold=True)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        number = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{number:06d}"

    @staticmethod
    def verify(user: User, supplied: object, *, allow_recovery: bool = True) -> bool:
        normalized = re.sub(r"[\s-]+", "", str(supplied or "")).upper()
        if re.fullmatch(r"\d{6}", normalized):
            secret = MFAService.decrypt(user)
            now = int(time.time())
            if any(
                secrets.compare_digest(normalized, MFAService.code(secret, now + offset))
                for offset in (-30, 0, 30)
            ):
                return True
        if allow_recovery and normalized:
            code_hash = _token_hash(normalized)
            record = MFARecoveryCode.query.filter_by(
                user_id=user.id, code_hash=code_hash, used_at=None
            ).first()
            if record:
                record.used_at = utcnow()
                db.session.commit()
                return True
        return False

    @staticmethod
    def provisioning_uri(user: User, secret: str) -> str:
        issuer = str(current_app.config["MFA_ISSUER"])
        label = quote(f"{issuer}:{user.email}")
        return (
            f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
            "&algorithm=SHA1&digits=6&period=30"
        )

    @staticmethod
    def enable(user: User, secret: str, supplied: object) -> list[str]:
        user.mfa_secret_encrypted = MFAService.encrypt(secret)
        db.session.flush()
        if not MFAService.verify(user, supplied, allow_recovery=False):
            db.session.rollback()
            raise ValueError("The authenticator code is incorrect")
        user.mfa_enabled_at = utcnow()
        MFARecoveryCode.query.filter_by(user_id=user.id).delete()
        codes = [secrets.token_hex(5).upper() for _ in range(8)]
        for code in codes:
            db.session.add(MFARecoveryCode(user=user, code_hash=_token_hash(code)))
        db.session.commit()
        return codes


class AuthMailer:
    @staticmethod
    def send_link(*, recipient: str, subject: str, text_body: str, html_body: str) -> bool:
        if not (current_app.config.get("AUTH_EMAIL_ENABLED") or current_app.config.get("SES_ENABLED")):
            current_app.logger.info("Auth email disabled; would send %s to %s", subject, recipient)
            return False
        sender = current_app.config.get("SES_FROM_EMAIL", "").strip()
        if not sender:
            current_app.logger.warning("Auth email not sent: SES_FROM_EMAIL is missing")
            return False
        client = boto3.client("sesv2", region_name=current_app.config["AWS_REGION"])
        client.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject},
                    "Body": {
                        "Text": {"Data": text_body},
                        "Html": {"Data": html_body},
                    },
                }
            },
        )
        return True


def oidc_secret(integration: WorkspaceIntegration) -> str:
    reference = str(integration.secret_reference or "").strip()
    return os.getenv(reference, "") if reference else ""
