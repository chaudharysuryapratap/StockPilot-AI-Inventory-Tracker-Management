from __future__ import annotations

from flask import current_app

from app import db
from app.models import User, Workspace


def ensure_default_identity(*, commit: bool = True) -> User:
    """Return the durable bootstrap/machine actor without replacing its history."""

    workspace_name = current_app.config["DEFAULT_WORKSPACE_NAME"]
    actor_email = current_app.config["DEFAULT_STAFF_EMAIL"].strip().lower()
    actor_name = (
        current_app.config.get("STAFF_USERNAME") or "StockPilot Staff"
    ).strip()

    workspace = Workspace.query.order_by(Workspace.id).first()
    if workspace is None:
        workspace = Workspace(name=workspace_name)
        db.session.add(workspace)
        db.session.flush()

    actor = User.query.filter_by(email=actor_email).first()
    if actor is None:
        actor = User(
            workspace=workspace,
            name=actor_name,
            email=actor_email,
            role="admin",
            is_active=True,
        )
        db.session.add(actor)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return actor


def resolve_actor(email: str | None = None) -> User:
    """Resolve a trusted API actor or fall back to the shared Sprint 2 actor."""

    if email:
        actor = User.query.filter_by(email=email.strip().lower(), is_active=True).first()
        if actor is None:
            raise ValueError("X-Actor-Email does not match an active user")
        return actor
    return ensure_default_identity()
