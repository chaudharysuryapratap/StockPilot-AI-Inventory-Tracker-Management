from __future__ import annotations

import re

from flask import current_app, g, has_request_context

from app import db
from app.models import User, Workspace, WorkspaceMembership, WorkspaceSetting


def normalize_business_username(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized[:63]


def _ensure_membership(user: User, workspace: Workspace, role: str | None = None) -> WorkspaceMembership:
    membership = WorkspaceMembership.query.filter_by(
        user_id=user.id, workspace_id=workspace.id
    ).first()
    if membership is None:
        membership = WorkspaceMembership(
            user=user,
            workspace=workspace,
            role=role or user._role or "admin",
            is_active=True,
        )
        db.session.add(membership)
        db.session.flush()
    return membership


def ensure_default_identity(*, commit: bool = True) -> User:
    """Return the backwards-compatible actor for unattended/local deployments."""

    workspace_name = current_app.config["DEFAULT_WORKSPACE_NAME"]
    business_username = normalize_business_username(
        current_app.config.get("DEFAULT_BUSINESS_USERNAME") or workspace_name
    ) or "stockpilot"
    actor_email = current_app.config["DEFAULT_STAFF_EMAIL"].strip().lower()
    actor_name = (current_app.config.get("STAFF_USERNAME") or "StockPilot Staff").strip()

    workspace = Workspace.query.filter_by(business_username=business_username).first()
    if workspace is None:
        workspace = Workspace(name=workspace_name, business_username=business_username)
        db.session.add(workspace)
        db.session.flush()
        db.session.add(WorkspaceSetting(workspace=workspace))

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
        db.session.flush()
    _ensure_membership(actor, workspace, actor._role)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return actor


def memberships_for(user: User) -> list[WorkspaceMembership]:
    memberships = (
        WorkspaceMembership.query.filter_by(user_id=user.id, is_active=True)
        .join(Workspace)
        .order_by(Workspace.name, Workspace.id)
        .all()
    )
    if not memberships and user._workspace_id:
        membership = _ensure_membership(user, user.workspace, user._role)
        memberships = [membership]
    return memberships


def activate_workspace_context(
    user: User, requested_workspace_id: int | None = None
) -> WorkspaceMembership:
    """Resolve one active membership and expose it to legacy workspace-aware code."""

    query = WorkspaceMembership.query.filter_by(user_id=user.id, is_active=True)
    membership = None
    if requested_workspace_id:
        membership = query.filter_by(workspace_id=requested_workspace_id).first()
        if membership is None:
            # Users written by a pre-membership deployment (or an old private
            # integration) can claim only their durable home workspace once.
            if (
                requested_workspace_id == user._workspace_id
                and not WorkspaceMembership.query.filter_by(user_id=user.id).first()
            ):
                membership = _ensure_membership(user, user.workspace, user._role)
                db.session.commit()
            else:
                raise RuntimeError("This account does not belong to the requested workspace")
    else:
        membership = query.filter_by(workspace_id=user._workspace_id).first()
    if membership is None:
        memberships = memberships_for(user)
        membership = memberships[0] if memberships else None
    if membership is None:
        raise RuntimeError("This account does not belong to an active workspace")

    if has_request_context():
        g.current_user = user
        g.active_membership = membership
        g.active_workspace = membership.workspace
        g.active_workspace_id = membership.workspace_id
        g.active_workspace_role = membership.role
    return membership


def resolve_actor(
    email: str | None = None, business_username: str | None = None
) -> User:
    """Resolve a trusted actor and optional tenant for internal/API attribution."""

    if email:
        if not current_app.config.get("ALLOW_ACTOR_HEADER", False):
            raise ValueError("X-Actor-Email attribution is disabled")
        actor = User.query.filter_by(email=email.strip().lower(), is_active=True).first()
        if actor is None:
            raise ValueError("X-Actor-Email does not match an active user")
        workspace_id = None
        if business_username:
            workspace = Workspace.query.filter_by(
                business_username=normalize_business_username(business_username)
            ).first()
            if workspace is None:
                raise ValueError("X-Workspace does not match a workspace")
            workspace_id = workspace.id
        try:
            activate_workspace_context(actor, workspace_id)
        except RuntimeError as error:
            raise ValueError("Actor does not belong to the requested workspace") from error
        return actor

    actor = ensure_default_identity()
    activate_workspace_context(actor)
    return actor
