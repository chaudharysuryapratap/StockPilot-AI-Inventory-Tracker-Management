from __future__ import annotations

import re
from typing import Mapping

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Product, Supplier, User, utcnow


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SupplierValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]):
        self.errors = dict(errors)
        super().__init__(
            "; ".join(f"{field}: {message}" for field, message in self.errors.items())
        )


def _optional_text(value: object, max_length: int) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"must be {max_length} characters or fewer")
    return normalized


def _required_text(value: object, max_length: int) -> str:
    normalized = _optional_text(value, max_length)
    if normalized is None:
        raise ValueError("is required")
    return normalized


def _lead_time(value: object) -> int:
    normalized = str(value if value is not None else "").strip()
    if not re.fullmatch(r"\d+", normalized):
        raise ValueError("must be a whole number from 0 to 3650")
    result = int(normalized)
    if result > 3650:
        raise ValueError("must be a whole number from 0 to 3650")
    return result


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError("must be true or false")


class SupplierService:
    """Workspace-scoped supplier CRUD shared by browser and API workflows."""

    FIELD_ALIASES = {
        "email": "contact_email",
        "phone": "contact_phone",
    }

    @staticmethod
    def _read(payload: Mapping[str, object], field_name: str) -> tuple[object, bool]:
        if field_name in payload:
            return payload[field_name], True
        for alias, canonical in SupplierService.FIELD_ALIASES.items():
            if canonical == field_name and alias in payload:
                return payload[alias], True
        return None, False

    @staticmethod
    def validate(
        payload: Mapping[str, object],
        *,
        workspace_id: int,
        partial: bool = False,
        current_supplier: Supplier | None = None,
    ) -> dict:
        if not isinstance(payload, Mapping):
            raise SupplierValidationError({"payload": "must be an object"})

        values: dict[str, object] = {}
        errors: dict[str, str] = {}

        text_fields = {
            "name": (255, True),
            "contact_email": (255, False),
            "contact_phone": (50, False),
            "payment_terms": (100, False),
        }
        for field_name, (max_length, required) in text_fields.items():
            raw, supplied = SupplierService._read(payload, field_name)
            if not supplied and not partial:
                if required:
                    errors[field_name] = "is required"
                else:
                    values[field_name] = None
                continue
            if not supplied:
                continue
            try:
                value = (
                    _required_text(raw, max_length)
                    if required
                    else _optional_text(raw, max_length)
                )
                if field_name == "contact_email" and value:
                    value = value.lower()
                    if not EMAIL_PATTERN.fullmatch(value):
                        raise ValueError("must be a valid email address")
                values[field_name] = value
            except ValueError as error:
                errors[field_name] = str(error)

        raw_lead_time, lead_time_supplied = SupplierService._read(
            payload, "lead_time_days"
        )
        if not lead_time_supplied and not partial:
            raw_lead_time, lead_time_supplied = 3, True
        if lead_time_supplied:
            try:
                values["lead_time_days"] = _lead_time(raw_lead_time)
            except ValueError as error:
                errors["lead_time_days"] = str(error)

        raw_active, active_supplied = SupplierService._read(payload, "is_active")
        if active_supplied:
            try:
                values["is_active"] = _boolean(raw_active)
            except ValueError as error:
                errors["is_active"] = str(error)

        name = values.get("name")
        if name:
            query = Supplier.query.filter(
                Supplier.workspace_id == workspace_id,
                func.lower(Supplier.name) == str(name).lower(),
            )
            if current_supplier is not None:
                query = query.filter(Supplier.id != current_supplier.id)
            if query.first():
                errors["name"] = "already exists in this workspace"

        if errors:
            raise SupplierValidationError(errors)
        return values

    @staticmethod
    def create(payload: Mapping[str, object], *, actor: User) -> Supplier:
        values = SupplierService.validate(payload, workspace_id=actor.workspace_id)
        supplier = Supplier(workspace_id=actor.workspace_id, **values)
        db.session.add(supplier)
        SupplierService._commit()
        return supplier

    @staticmethod
    def update(
        supplier: Supplier, payload: Mapping[str, object], *, actor: User
    ) -> Supplier:
        SupplierService._require_workspace(supplier, actor)
        values = SupplierService.validate(
            payload,
            workspace_id=actor.workspace_id,
            partial=True,
            current_supplier=supplier,
        )
        if not values:
            raise SupplierValidationError(
                {"payload": "include at least one editable field"}
            )
        for field_name, value in values.items():
            setattr(supplier, field_name, value)
        supplier.updated_at = utcnow()
        SupplierService._commit()
        return supplier

    @staticmethod
    def archive(supplier: Supplier, *, actor: User) -> Supplier:
        SupplierService._require_workspace(supplier, actor)
        supplier.is_active = False
        supplier.updated_at = utcnow()
        SupplierService._commit()
        return supplier

    @staticmethod
    def restore(supplier: Supplier, *, actor: User) -> Supplier:
        SupplierService._require_workspace(supplier, actor)
        supplier.is_active = True
        supplier.updated_at = utcnow()
        SupplierService._commit()
        return supplier

    @staticmethod
    def _require_workspace(supplier: Supplier, actor: User) -> None:
        if supplier.workspace_id != actor.workspace_id:
            raise SupplierValidationError({"supplier": "was not found"})

    @staticmethod
    def _commit() -> None:
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise SupplierValidationError(
                {"supplier": "conflicts with an existing supplier"}
            ) from error


def serialize_supplier(supplier: Supplier) -> dict:
    return {
        "id": supplier.id,
        "workspace_id": supplier.workspace_id,
        "name": supplier.name,
        "contact_email": supplier.contact_email,
        "contact_phone": supplier.contact_phone,
        "lead_time_days": supplier.lead_time_days,
        "payment_terms": supplier.payment_terms,
        "is_active": supplier.is_active,
        "product_count": Product.query.filter_by(
            preferred_supplier_id=supplier.id
        ).count(),
        "created_at": supplier.created_at.isoformat() if supplier.created_at else None,
        "updated_at": supplier.updated_at.isoformat() if supplier.updated_at else None,
    }
