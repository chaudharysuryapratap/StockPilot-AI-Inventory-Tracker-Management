from __future__ import annotations

import re
from decimal import Decimal
from typing import Mapping

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Bin, InventoryLocation, StockLevel, User, utcnow
from app.services.identity import ensure_default_identity
from app.services.products import number_for_json


CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]*$")


class LocationValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]):
        self.errors = dict(errors)
        super().__init__("; ".join(f"{key}: {value}" for key, value in self.errors.items()))


def _required_text(value: object, field: str, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("is required")
    if len(normalized) > limit:
        raise ValueError(f"must be {limit} characters or fewer")
    return normalized


def _optional_text(value: object, limit: int) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) > limit:
        raise ValueError(f"must be {limit} characters or fewer")
    return normalized


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("must be true or false")


def _capacity(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not re.fullmatch(r"\+?\d+", str(value).strip()):
        raise ValueError("must be a positive whole number")
    number = int(str(value).strip())
    if number <= 0:
        raise ValueError("must be a positive whole number")
    return number


def _bin_quantity(bin_id: int) -> Decimal:
    value = (
        db.session.query(func.coalesce(func.sum(StockLevel.quantity_on_hand), 0))
        .filter(StockLevel.bin_id == bin_id)
        .scalar()
    )
    return Decimal(value or 0)


class LocationService:
    @staticmethod
    def validate(
        payload: Mapping[str, object],
        *,
        partial: bool = False,
        current: InventoryLocation | None = None,
        workspace_id: int | None = None,
    ) -> dict:
        if not isinstance(payload, Mapping):
            raise LocationValidationError({"payload": "must be an object"})
        errors: dict[str, str] = {}
        values: dict[str, object] = {}

        for field, limit in {"name": 120, "code": 32}.items():
            if field not in payload:
                if not partial:
                    errors[field] = "is required"
                continue
            try:
                value = _required_text(payload[field], field, limit)
                if field == "code":
                    value = value.upper()
                    if not CODE_PATTERN.fullmatch(value):
                        raise ValueError(
                            "may contain only letters, numbers, underscores, and hyphens"
                        )
                values[field] = value
            except ValueError as error:
                errors[field] = str(error)

        if "address" in payload or not partial:
            try:
                values["address"] = _optional_text(payload.get("address"), 255)
            except ValueError as error:
                errors["address"] = str(error)

        if "is_active" in payload:
            try:
                values["is_active"] = _boolean(payload["is_active"])
            except ValueError as error:
                errors["is_active"] = str(error)

        code = values.get("code")
        if code:
            query = InventoryLocation.query.filter_by(code=code)
            effective_workspace_id = workspace_id or (
                current.workspace_id if current is not None else None
            )
            if effective_workspace_id is not None:
                query = query.filter_by(workspace_id=effective_workspace_id)
            if current is not None:
                query = query.filter(InventoryLocation.id != current.id)
            if query.first():
                errors["code"] = "already exists"

        if current is not None and values.get("is_active") is False:
            stock = (
                db.session.query(func.coalesce(func.sum(StockLevel.quantity_on_hand), 0))
                .filter(StockLevel.location_id == current.id)
                .scalar()
            )
            if Decimal(stock or 0) > 0:
                errors["is_active"] = "cannot be disabled while stock remains at the location"

        if errors:
            raise LocationValidationError(errors)
        return values

    @staticmethod
    def create(payload: Mapping[str, object], *, actor: User | None = None) -> InventoryLocation:
        actor = actor or ensure_default_identity(commit=False)
        values = LocationService.validate(payload, workspace_id=actor.workspace_id)
        location = InventoryLocation(workspace_id=actor.workspace_id, **values)
        db.session.add(location)
        LocationService._commit()
        return location

    @staticmethod
    def update(location: InventoryLocation, payload: Mapping[str, object]) -> InventoryLocation:
        values = LocationService.validate(
            payload,
            partial=True,
            current=location,
            workspace_id=location.workspace_id,
        )
        if not values:
            raise LocationValidationError({"payload": "include at least one editable field"})
        for field, value in values.items():
            setattr(location, field, value)
        location.updated_at = utcnow()
        LocationService._commit()
        return location

    @staticmethod
    def _commit() -> None:
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise LocationValidationError(
                {"location": "conflicts with an existing location code"}
            ) from error


class BinService:
    @staticmethod
    def validate(
        payload: Mapping[str, object],
        *,
        location: InventoryLocation,
        partial: bool = False,
        current: Bin | None = None,
    ) -> dict:
        if not isinstance(payload, Mapping):
            raise LocationValidationError({"payload": "must be an object"})
        errors: dict[str, str] = {}
        values: dict[str, object] = {}

        if "code" in payload:
            try:
                code = _required_text(payload["code"], "code", 50).upper()
                if not CODE_PATTERN.fullmatch(code):
                    raise ValueError(
                        "may contain only letters, numbers, underscores, and hyphens"
                    )
                values["code"] = code
            except ValueError as error:
                errors["code"] = str(error)
        elif not partial:
            errors["code"] = "is required"

        if "capacity" in payload or not partial:
            try:
                values["capacity"] = _capacity(payload.get("capacity"))
            except ValueError as error:
                errors["capacity"] = str(error)

        if "is_active" in payload:
            try:
                values["is_active"] = _boolean(payload["is_active"])
            except ValueError as error:
                errors["is_active"] = str(error)

        code = values.get("code")
        if code:
            query = Bin.query.filter_by(location_id=location.id, code=code)
            if current is not None:
                query = query.filter(Bin.id != current.id)
            if query.first():
                errors["code"] = "already exists at this location"

        if current is not None:
            current_quantity = _bin_quantity(current.id)
            new_capacity = values.get("capacity")
            if new_capacity is not None and current_quantity > Decimal(new_capacity):
                errors["capacity"] = (
                    f"cannot be below the current on-hand quantity "
                    f"({number_for_json(current_quantity)})"
                )
            if values.get("is_active") is False and current_quantity > 0:
                errors["is_active"] = "cannot be disabled while stock remains in the bin"

        if errors:
            raise LocationValidationError(errors)
        return values

    @staticmethod
    def create(location: InventoryLocation, payload: Mapping[str, object]) -> Bin:
        if not location.is_active:
            raise LocationValidationError({"location": "must be active"})
        values = BinService.validate(payload, location=location)
        bin_record = Bin(location=location, **values)
        db.session.add(bin_record)
        BinService._commit()
        return bin_record

    @staticmethod
    def update(bin_record: Bin, payload: Mapping[str, object]) -> Bin:
        values = BinService.validate(
            payload,
            location=bin_record.location,
            partial=True,
            current=bin_record,
        )
        if not values:
            raise LocationValidationError({"payload": "include at least one editable field"})
        for field, value in values.items():
            setattr(bin_record, field, value)
        bin_record.updated_at = utcnow()
        BinService._commit()
        return bin_record

    @staticmethod
    def _commit() -> None:
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise LocationValidationError(
                {"bin": "conflicts with an existing bin at this location"}
            ) from error


def serialize_bin(bin_record: Bin) -> dict:
    on_hand = _bin_quantity(bin_record.id)
    return {
        "id": bin_record.id,
        "code": bin_record.code,
        "capacity": bin_record.capacity,
        "quantity_on_hand": number_for_json(on_hand),
        "remaining_capacity": (
            number_for_json(Decimal(bin_record.capacity) - on_hand)
            if bin_record.capacity is not None
            else None
        ),
        "is_active": bin_record.is_active,
    }


def serialize_location(location: InventoryLocation) -> dict:
    on_hand, reserved = (
        db.session.query(
            func.coalesce(func.sum(StockLevel.quantity_on_hand), 0),
            func.coalesce(func.sum(StockLevel.quantity_reserved), 0),
        )
        .filter(StockLevel.location_id == location.id)
        .one()
    )
    return {
        "id": location.id,
        "name": location.name,
        "code": location.code,
        "address": location.address,
        "is_active": location.is_active,
        "stock": {
            "quantity_on_hand": number_for_json(on_hand),
            "quantity_reserved": number_for_json(reserved),
            "quantity_available": number_for_json(Decimal(on_hand) - Decimal(reserved)),
        },
        "bins": [serialize_bin(item) for item in sorted(location.bins, key=lambda row: row.code)],
    }
