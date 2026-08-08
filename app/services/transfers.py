from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    Bin,
    InventoryLocation,
    InventoryMovement,
    Product,
    StockLevel,
    StockTransfer,
    User,
    utcnow,
)
from app.services.inventory import InsufficientStockError, UnknownInventoryReferenceError, _quantity
from app.services.products import number_for_json


class TransferConflictError(ValueError):
    pass


def _trimmed(payload: Mapping[str, object], key: str, limit: int) -> str | None:
    value = str(payload.get(key, "") or "").strip()
    if not value:
        return None
    if len(value) > limit:
        raise ValueError(f"{key} must be {limit} characters or fewer")
    return value


def _find_location(code: object, field: str) -> InventoryLocation:
    normalized = str(code or "").strip().upper()
    if not normalized:
        raise ValueError(f"{field} is required")
    location = InventoryLocation.query.filter_by(code=normalized, is_active=True).first()
    if location is None:
        raise UnknownInventoryReferenceError(
            f"No active inventory location exists for code '{normalized}'"
        )
    return location


def _find_bin(location: InventoryLocation, code: object, field: str) -> Bin | None:
    normalized = str(code or "").strip().upper()
    if not normalized:
        return None
    bin_record = Bin.query.filter_by(
        location_id=location.id, code=normalized, is_active=True
    ).first()
    if bin_record is None:
        raise UnknownInventoryReferenceError(
            f"No active bin '{normalized}' exists at {location.code}"
        )
    return bin_record


def _matching_transfer(transfer: StockTransfer, expected: dict) -> bool:
    return all(
        (
            Decimal(getattr(transfer, key)) == Decimal(value)
            if key == "quantity"
            else getattr(transfer, key) == value
        )
        for key, value in expected.items()
    )


class TransferService:
    """Atomic location/bin transfers backed by paired append-only movements."""

    @staticmethod
    def transfer(
        payload: Mapping[str, object], *, actor: User
    ) -> tuple[StockTransfer, bool]:
        if not isinstance(payload, Mapping):
            raise ValueError("JSON object expected")
        if actor is None or not actor.is_active:
            raise ValueError("an active user is required to perform a transfer")

        sku = str(payload.get("sku", "") or "").strip().upper()
        if not sku:
            raise ValueError("sku is required")
        product = Product.query.filter_by(sku=sku, is_active=True).first()
        if product is None:
            raise UnknownInventoryReferenceError(
                f"No active product exists for SKU '{sku}'"
            )

        source = _find_location(payload.get("source_location_code"), "source_location_code")
        destination = _find_location(
            payload.get("destination_location_code"), "destination_location_code"
        )
        source_bin = _find_bin(source, payload.get("source_bin_code"), "source_bin_code")
        destination_bin = _find_bin(
            destination, payload.get("destination_bin_code"), "destination_bin_code"
        )
        quantity = _quantity(payload.get("quantity"), "quantity", positive=True)
        note = _trimmed(payload, "note", 255)
        external_id = _trimmed(payload, "external_transfer_id", 128)

        idempotency_match = {
            "product_id": product.id,
            "source_location_id": source.id,
            "destination_location_id": destination.id,
            "destination_bin_id": destination_bin.id if destination_bin else None,
            "quantity": quantity,
        }
        if source_bin is not None:
            idempotency_match["source_bin_id"] = source_bin.id
        if external_id:
            existing = StockTransfer.query.filter_by(external_id=external_id).first()
            if existing:
                if not _matching_transfer(existing, idempotency_match):
                    raise TransferConflictError(
                        "external_transfer_id already belongs to a different transfer"
                    )
                return existing, False

        # Lock all rows for this SKU at both locations in deterministic order.
        # This protects the available-stock check and destination capacity value
        # against concurrent POS sales, adjustments, or opposite transfers.
        position_rows = (
            StockLevel.query.filter(
                StockLevel.product_id == product.id,
                StockLevel.location_id.in_([source.id, destination.id]),
            )
            .order_by(StockLevel.location_id, StockLevel.bin_id, StockLevel.id)
            .with_for_update()
            .all()
        )

        source_rows = [row for row in position_rows if row.location_id == source.id]
        if source_bin:
            source_stock = next(
                (row for row in source_rows if row.bin_id == source_bin.id), None
            )
        elif len(source_rows) == 1:
            source_stock = source_rows[0]
        else:
            source_stock = next(
                (row for row in source_rows if row.bin_id is None), None
            )
            if source_stock is None and len(source_rows) > 1:
                raise ValueError(
                    "source_bin_code is required because this SKU is stored in multiple bins"
                )
        if source_stock is None:
            position = f" bin {source_bin.code}" if source_bin else ""
            raise UnknownInventoryReferenceError(
                f"SKU '{sku}' has no stock position at {source.code}{position}"
            )
        # When a location has exactly one stock position, callers may omit its
        # bin. Persist the actual bin used so the audit trail remains precise.
        source_bin = source_stock.bin
        source_position = (source.id, source_bin.id if source_bin else None)
        destination_position = (
            destination.id,
            destination_bin.id if destination_bin else None,
        )
        if source_position == destination_position:
            raise ValueError("source and destination stock positions must be different")
        if source_stock.quantity_available < quantity:
            raise InsufficientStockError(
                f"Only {number_for_json(source_stock.quantity_available)} "
                f"{product.unit_of_measure} of '{sku}' are available at {source.code}"
            )

        destination_stock = next(
            (
                row
                for row in position_rows
                if row.location_id == destination.id
                and row.bin_id == (destination_bin.id if destination_bin else None)
            ),
            None,
        )
        if destination_bin and destination_bin.capacity is not None:
            destination_bin_rows = (
                StockLevel.query.filter_by(bin_id=destination_bin.id)
                .order_by(StockLevel.id)
                .with_for_update()
                .all()
            )
            current_bin_quantity = sum(
                (Decimal(row.quantity_on_hand or 0) for row in destination_bin_rows),
                start=Decimal("0.00"),
            )
            if current_bin_quantity + quantity > Decimal(destination_bin.capacity):
                remaining = max(Decimal("0"), Decimal(destination_bin.capacity) - current_bin_quantity)
                raise TransferConflictError(
                    f"Destination bin {destination.code}/{destination_bin.code} has capacity "
                    f"for only {number_for_json(remaining)} more units"
                )

        if destination_stock is None:
            destination_stock = StockLevel(
                product=product,
                location=destination,
                bin=destination_bin,
                quantity_on_hand=Decimal("0.00"),
                quantity_reserved=Decimal("0.00"),
            )
            db.session.add(destination_stock)

        transfer = StockTransfer(
            external_id=external_id,
            product=product,
            source_location=source,
            destination_location=destination,
            source_bin=source_bin,
            destination_bin=destination_bin,
            quantity=quantity,
            user=actor,
            note=note,
        )
        expected = {
            **idempotency_match,
            "source_bin_id": source_bin.id if source_bin else None,
        }
        try:
            db.session.add(transfer)
            db.session.flush()
            source_stock.quantity_on_hand -= quantity
            source_stock.updated_at = utcnow()
            destination_stock.quantity_on_hand += quantity
            destination_stock.updated_at = utcnow()
            db.session.add_all(
                [
                    InventoryMovement(
                        product=product,
                        location=source,
                        bin=source_bin,
                        user=actor,
                        movement_type="transfer",
                        quantity_delta=-quantity,
                        reason="transfer_out",
                        reference_type="stock_transfer",
                        reference_id=transfer.transfer_uid,
                        note=note,
                    ),
                    InventoryMovement(
                        product=product,
                        location=destination,
                        bin=destination_bin,
                        user=actor,
                        movement_type="transfer",
                        quantity_delta=quantity,
                        reason="transfer_in",
                        reference_type="stock_transfer",
                        reference_id=transfer.transfer_uid,
                        note=note,
                    ),
                ]
            )
            db.session.commit()
            return transfer, True
        except IntegrityError:
            db.session.rollback()
            if external_id:
                existing = StockTransfer.query.filter_by(external_id=external_id).first()
                if existing and _matching_transfer(existing, expected):
                    return existing, False
            raise
        except Exception:
            db.session.rollback()
            raise


def serialize_transfer(transfer: StockTransfer) -> dict:
    return {
        "id": transfer.id,
        "transfer_uid": transfer.transfer_uid,
        "external_transfer_id": transfer.external_id,
        "status": transfer.status,
        "sku": transfer.product.sku,
        "product": transfer.product.name,
        "quantity": number_for_json(transfer.quantity),
        "unit_of_measure": transfer.product.unit_of_measure,
        "source": {
            "location": transfer.source_location.code,
            "bin": transfer.source_bin.code if transfer.source_bin else None,
        },
        "destination": {
            "location": transfer.destination_location.code,
            "bin": transfer.destination_bin.code if transfer.destination_bin else None,
        },
        "performed_by": {
            "id": transfer.user.id,
            "name": transfer.user.name,
            "email": transfer.user.email,
        },
        "note": transfer.note,
        "created_at": transfer.created_at.isoformat(),
    }


def serialize_movement(movement: InventoryMovement) -> dict:
    return {
        "id": movement.id,
        "movement_type": movement.movement_type,
        "reason_code": movement.reason,
        "sku": movement.product.sku,
        "location": movement.location.code,
        "bin": movement.bin.code if movement.bin else None,
        "quantity_delta": number_for_json(movement.quantity_delta),
        "reference_type": movement.reference_type,
        "reference_id": movement.reference_id,
        "performed_by": (
            {
                "id": movement.user.id,
                "name": movement.user.name,
                "email": movement.user.email,
            }
            if movement.user
            else None
        ),
        "note": movement.note,
        "created_at": movement.created_at.isoformat(),
    }
