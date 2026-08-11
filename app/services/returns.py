from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    Bin,
    InventoryLocation,
    InventoryMovement,
    ReturnAuthorization,
    ReturnEvent,
    ReturnItem,
    ReturnReceipt,
    SalesOrder,
    SalesOrderItem,
    StockLevel,
    User,
    utcnow,
)
from app.services.inventory import (
    CapacityExceededError,
    UnknownInventoryReferenceError,
    _quantity,
)
from app.services.products import number_for_json


RETURN_CREATE_ROLES = ("admin", "manager")
RETURN_REVIEW_ROLES = ("admin", "manager")
RETURN_RECEIVE_ROLES = ("admin", "manager", "picker")
ACTIVE_CLAIM_STATUSES = ("requested", "authorized", "receiving", "completed")
RETURN_REASON_LABELS = {
    "customer_return": "Customer return",
    "damaged": "Damaged item",
    "defective": "Defective item",
    "wrong_item": "Wrong item supplied",
    "other": "Other",
}
RETURN_DISPOSITION_LABELS = {
    "restock": "Accepted for restock",
    "damaged": "Damaged — do not restock",
}


class ReturnConflictError(ValueError):
    pass


class ReturnStateError(ReturnConflictError):
    pass


class ReturnPermissionError(PermissionError):
    pass


def _require_actor(actor: User | None, roles: tuple[str, ...]) -> User:
    if actor is None or not actor.is_active:
        raise ReturnPermissionError("an active user is required")
    if not actor.has_role(*roles):
        raise ReturnPermissionError("your role cannot perform this return action")
    return actor


def _require_workspace(rma: ReturnAuthorization, actor: User) -> None:
    if rma.workspace_id != actor.workspace_id:
        raise UnknownInventoryReferenceError("return authorization was not found")


def _text(
    payload: Mapping[str, object], key: str, limit: int, *, required: bool = False
) -> str | None:
    value = str(payload.get(key, "") or "").strip()
    if not value:
        if required:
            raise ValueError(f"{key} is required")
        return None
    if len(value) > limit:
        raise ValueError(f"{key} must be {limit} characters or fewer")
    return value


def _reason_code(value: object) -> str:
    reason = str(value or "").strip().lower()
    if reason not in RETURN_REASON_LABELS:
        raise ValueError(
            "reason_code must be one of: " + ", ".join(RETURN_REASON_LABELS)
        )
    return reason


def _disposition(value: object) -> str:
    disposition = str(value or "").strip().lower()
    if disposition not in RETURN_DISPOSITION_LABELS:
        raise ValueError("disposition must be 'restock' or 'damaged'")
    return disposition


def _normalize_items(raw_items: object) -> dict[str, Decimal]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty list")
    if len(raw_items) > 100:
        raise ValueError("a return can contain at most 100 item rows")

    requested: dict[str, Decimal] = {}
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping):
            raise ValueError(f"item {index} must be an object")
        sku = str(raw_item.get("sku", "") or "").strip().upper()
        if not sku:
            raise ValueError(f"item {index} needs a sku")
        quantity = _quantity(
            raw_item.get("quantity"), f"quantity for {sku}", positive=True
        )
        requested[sku] = requested.get(sku, Decimal("0.00")) + quantity
    return requested


def _locked_rma(rma: ReturnAuthorization) -> ReturnAuthorization:
    return ReturnAuthorization.query.filter_by(id=rma.id).with_for_update().one()


def _actor_json(actor: User | None) -> dict | None:
    if actor is None:
        return None
    return {"id": actor.id, "name": actor.name, "email": actor.email}


def _add_event(
    rma: ReturnAuthorization, actor: User, event_type: str, detail: dict | None = None
) -> None:
    db.session.add(
        ReturnEvent(
            return_authorization=rma,
            user=actor,
            event_type=event_type,
            detail=detail or {},
        )
    )


def _claimed_quantity(order_item_id: int) -> Decimal:
    value = (
        db.session.query(func.coalesce(func.sum(ReturnItem.quantity_requested), 0))
        .join(
            ReturnAuthorization,
            ReturnItem.return_authorization_id == ReturnAuthorization.id,
        )
        .filter(
            ReturnItem.sales_order_item_id == order_item_id,
            ReturnAuthorization.status.in_(ACTIVE_CLAIM_STATUSES),
        )
        .scalar()
    )
    return Decimal(value or 0)


def returnable_quantity(order_item: SalesOrderItem) -> Decimal:
    return max(Decimal("0.00"), Decimal(order_item.quantity) - _claimed_quantity(order_item.id))


def _matching_rma(
    rma: ReturnAuthorization,
    *,
    order_id: int,
    reason_code: str,
    customer_note: str | None,
    requested: Mapping[str, Decimal],
) -> bool:
    existing_items = {
        item.sales_order_item.product.sku: Decimal(item.quantity_requested)
        for item in rma.items
    }
    return (
        rma.sales_order_id == order_id
        and rma.reason_code == reason_code
        and rma.customer_note == customer_note
        and existing_items == dict(requested)
    )


def _location_and_bin(
    payload: Mapping[str, object], *, workspace_id: int
) -> tuple[InventoryLocation, Bin | None]:
    location_code = str(payload.get("location_code", "") or "").strip().upper()
    if not location_code:
        raise ValueError("location_code is required")
    location = InventoryLocation.query.filter_by(
        workspace_id=workspace_id, code=location_code, is_active=True
    ).first()
    if location is None:
        raise UnknownInventoryReferenceError(
            f"No active inventory location exists for code '{location_code}'"
        )

    bin_code = str(payload.get("bin_code", "") or "").strip().upper()
    if not bin_code:
        return location, None
    bin_record = Bin.query.filter_by(
        location_id=location.id, code=bin_code, is_active=True
    ).first()
    if bin_record is None:
        raise UnknownInventoryReferenceError(
            f"No active bin '{bin_code}' exists at location '{location.code}'"
        )
    return location, bin_record


def _matching_receipt(
    receipt: ReturnReceipt,
    *,
    return_item_id: int,
    quantity: Decimal,
    disposition: str,
    location_id: int,
    bin_id: int | None,
    note: str | None,
) -> bool:
    return (
        receipt.return_item_id == return_item_id
        and Decimal(receipt.quantity) == quantity
        and receipt.disposition == disposition
        and receipt.location_id == location_id
        and receipt.bin_id == bin_id
        and receipt.note == note
    )


class ReturnService:
    """Transactional RMA approval and physical receiving workflow."""

    @staticmethod
    def create(
        order: SalesOrder, payload: Mapping[str, object], *, actor: User
    ) -> tuple[ReturnAuthorization, bool]:
        actor = _require_actor(actor, RETURN_CREATE_ROLES)
        if order.workspace_id != actor.workspace_id:
            raise UnknownInventoryReferenceError("sales order was not found")
        if order.status != "shipped":
            raise ReturnStateError("returns can be created only for shipped sales orders")
        if not isinstance(payload, Mapping):
            raise ValueError("JSON object expected")

        external_id = _text(payload, "external_return_id", 128)
        customer_note = _text(payload, "customer_note", 500)
        reason_code = _reason_code(payload.get("reason_code"))
        requested = _normalize_items(payload.get("items"))

        if external_id:
            existing = ReturnAuthorization.query.filter_by(
                workspace_id=actor.workspace_id, external_id=external_id
            ).first()
            if existing:
                if not _matching_rma(
                    existing,
                    order_id=order.id,
                    reason_code=reason_code,
                    customer_note=customer_note,
                    requested=requested,
                ):
                    raise ReturnConflictError(
                        "external_return_id already belongs to a different return"
                    )
                return existing, False

        try:
            locked_order = SalesOrder.query.filter_by(id=order.id).with_for_update().one()
            if locked_order.status != "shipped":
                raise ReturnStateError(
                    "returns can be created only for shipped sales orders"
                )
            order_items = (
                SalesOrderItem.query.filter_by(order_id=locked_order.id)
                .order_by(SalesOrderItem.id)
                .with_for_update()
                .all()
            )
            items_by_sku = {item.product.sku: item for item in order_items}
            missing = sorted(set(requested) - set(items_by_sku))
            if missing:
                raise UnknownInventoryReferenceError(
                    "SKU(s) were not shipped on this order: " + ", ".join(missing)
                )

            for sku, quantity in requested.items():
                item = items_by_sku[sku]
                remaining = returnable_quantity(item)
                if quantity > remaining:
                    raise ReturnConflictError(
                        f"{sku} has only {number_for_json(remaining)} returnable units"
                    )

            rma = ReturnAuthorization(
                external_id=external_id,
                workspace_id=actor.workspace_id,
                sales_order=locked_order,
                status="requested",
                reason_code=reason_code,
                customer_note=customer_note,
                created_by=actor,
            )
            db.session.add(rma)
            db.session.flush()
            for sku, quantity in requested.items():
                db.session.add(
                    ReturnItem(
                        return_authorization=rma,
                        sales_order_item=items_by_sku[sku],
                        quantity_requested=quantity,
                        quantity_authorized=Decimal("0.00"),
                        quantity_received=Decimal("0.00"),
                        quantity_restocked=Decimal("0.00"),
                    )
                )
            _add_event(
                rma,
                actor,
                "requested",
                {"reason_code": reason_code, "line_count": len(requested)},
            )
            db.session.commit()
            return rma, True
        except IntegrityError as error:
            db.session.rollback()
            if external_id:
                existing = ReturnAuthorization.query.filter_by(
                    workspace_id=actor.workspace_id, external_id=external_id
                ).first()
                if existing and _matching_rma(
                    existing,
                    order_id=order.id,
                    reason_code=reason_code,
                    customer_note=customer_note,
                    requested=requested,
                ):
                    return existing, False
                if existing:
                    raise ReturnConflictError(
                        "external_return_id already belongs to a different return"
                    ) from error
            raise
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def authorize(
        rma: ReturnAuthorization, *, actor: User
    ) -> tuple[ReturnAuthorization, bool]:
        actor = _require_actor(actor, RETURN_REVIEW_ROLES)
        try:
            rma = _locked_rma(rma)
            _require_workspace(rma, actor)
            if rma.status in {"authorized", "receiving", "completed"}:
                db.session.rollback()
                return rma, False
            if rma.status != "requested":
                raise ReturnStateError(
                    f"return cannot be authorized from status '{rma.status}'"
                )
            now = utcnow()
            for item in rma.items:
                item.quantity_authorized = item.quantity_requested
            rma.status = "authorized"
            rma.authorized_by = actor
            rma.authorized_at = now
            _add_event(rma, actor, "authorized")
            db.session.commit()
            return rma, True
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def reject(
        rma: ReturnAuthorization, *, actor: User
    ) -> tuple[ReturnAuthorization, bool]:
        actor = _require_actor(actor, RETURN_REVIEW_ROLES)
        try:
            rma = _locked_rma(rma)
            _require_workspace(rma, actor)
            if rma.status == "rejected":
                db.session.rollback()
                return rma, False
            if rma.status != "requested":
                raise ReturnStateError(
                    f"return cannot be rejected from status '{rma.status}'"
                )
            rma.status = "rejected"
            rma.rejected_by = actor
            rma.rejected_at = utcnow()
            _add_event(rma, actor, "rejected")
            db.session.commit()
            return rma, True
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def cancel(
        rma: ReturnAuthorization, *, actor: User
    ) -> tuple[ReturnAuthorization, bool]:
        actor = _require_actor(actor, RETURN_REVIEW_ROLES)
        try:
            rma = _locked_rma(rma)
            _require_workspace(rma, actor)
            if rma.status == "cancelled":
                db.session.rollback()
                return rma, False
            if rma.status not in {"requested", "authorized"}:
                raise ReturnStateError(
                    f"return cannot be cancelled from status '{rma.status}'"
                )
            if any(Decimal(item.quantity_received or 0) > 0 for item in rma.items):
                raise ReturnStateError("a return with received items cannot be cancelled")
            rma.status = "cancelled"
            rma.cancelled_by = actor
            rma.cancelled_at = utcnow()
            _add_event(rma, actor, "cancelled")
            db.session.commit()
            return rma, True
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def receive_item(
        rma: ReturnAuthorization,
        item_id: int,
        payload: Mapping[str, object],
        *,
        actor: User,
    ) -> tuple[ReturnReceipt, bool]:
        actor = _require_actor(actor, RETURN_RECEIVE_ROLES)
        if not isinstance(payload, Mapping):
            raise ValueError("JSON object expected")
        _require_workspace(rma, actor)

        quantity = _quantity(payload.get("quantity"), "quantity", positive=True)
        disposition = _disposition(payload.get("disposition"))
        external_id = _text(payload, "external_receipt_id", 128)
        note = _text(payload, "note", 255)
        location, bin_record = _location_and_bin(
            payload, workspace_id=actor.workspace_id
        )

        if external_id:
            existing = ReturnReceipt.query.filter_by(external_id=external_id).first()
            if existing:
                if (
                    existing.return_item.return_authorization_id != rma.id
                    or not _matching_receipt(
                        existing,
                        return_item_id=item_id,
                        quantity=quantity,
                        disposition=disposition,
                        location_id=location.id,
                        bin_id=bin_record.id if bin_record else None,
                        note=note,
                    )
                ):
                    raise ReturnConflictError(
                        "external_receipt_id already belongs to a different receipt"
                    )
                return existing, False

        try:
            rma = _locked_rma(rma)
            _require_workspace(rma, actor)
            if rma.status not in {"authorized", "receiving"}:
                raise ReturnStateError(
                    f"items cannot be received while a return is '{rma.status}'"
                )
            item = (
                ReturnItem.query.filter_by(
                    id=item_id, return_authorization_id=rma.id
                )
                .with_for_update()
                .first()
            )
            if item is None:
                raise UnknownInventoryReferenceError("return item was not found")
            remaining = Decimal(item.quantity_authorized) - Decimal(
                item.quantity_received or 0
            )
            if quantity > remaining:
                raise ReturnConflictError(
                    f"only {number_for_json(remaining)} authorized units remain to receive"
                )

            now = utcnow()
            # Update the aggregate receipt quantity before any relationship load can
            # trigger an autoflush; this keeps restocked <= received true throughout.
            item.quantity_received = Decimal(item.quantity_received or 0) + quantity
            if disposition == "restock":
                stock = (
                    StockLevel.query.filter_by(
                        product_id=item.sales_order_item.product_id,
                        location_id=location.id,
                        bin_id=bin_record.id if bin_record else None,
                    )
                    .with_for_update()
                    .first()
                )
                if bin_record and bin_record.capacity is not None:
                    bin_quantity = (
                        db.session.query(
                            func.coalesce(func.sum(StockLevel.quantity_on_hand), 0)
                        )
                        .filter(StockLevel.bin_id == bin_record.id)
                        .scalar()
                    )
                    if Decimal(bin_quantity or 0) + quantity > Decimal(
                        bin_record.capacity
                    ):
                        raise CapacityExceededError(
                            f"bin '{bin_record.code}' capacity would be exceeded"
                        )
                if stock is None:
                    stock = StockLevel(
                        product_id=item.sales_order_item.product_id,
                        location=location,
                        bin=bin_record,
                        quantity_on_hand=Decimal("0.00"),
                        quantity_reserved=Decimal("0.00"),
                    )
                    db.session.add(stock)
                stock.quantity_on_hand = Decimal(stock.quantity_on_hand or 0) + quantity
                stock.updated_at = now
                item.quantity_restocked = Decimal(item.quantity_restocked or 0) + quantity
                db.session.add(
                    InventoryMovement(
                        product=item.sales_order_item.product,
                        location=location,
                        bin=bin_record,
                        user=actor,
                        movement_type="return",
                        quantity_delta=quantity,
                        reason=f"return_{rma.reason_code}",
                        reference_type="return_authorization",
                        reference_id=rma.rma_uid,
                        note=note,
                    )
                )

            receipt = ReturnReceipt(
                external_id=external_id,
                return_item=item,
                quantity=quantity,
                disposition=disposition,
                location=location,
                bin=bin_record,
                received_by=actor,
                note=note,
                created_at=now,
            )
            db.session.add(receipt)
            _add_event(
                rma,
                actor,
                "item_received",
                {
                    "sku": item.sales_order_item.product.sku,
                    "quantity": number_for_json(quantity),
                    "disposition": disposition,
                    "location": location.code,
                    "bin": bin_record.code if bin_record else None,
                    "external_receipt_id": external_id,
                },
            )

            complete = all(
                Decimal(row.quantity_received or 0)
                == Decimal(row.quantity_authorized or 0)
                for row in rma.items
            )
            if complete:
                rma.status = "completed"
                rma.completed_by = actor
                rma.completed_at = now
                _add_event(rma, actor, "completed")
            else:
                rma.status = "receiving"
            db.session.commit()
            return receipt, True
        except IntegrityError as error:
            db.session.rollback()
            if external_id:
                existing = ReturnReceipt.query.filter_by(external_id=external_id).first()
                if existing and existing.return_item.return_authorization_id == rma.id:
                    if _matching_receipt(
                        existing,
                        return_item_id=item_id,
                        quantity=quantity,
                        disposition=disposition,
                        location_id=location.id,
                        bin_id=bin_record.id if bin_record else None,
                        note=note,
                    ):
                        return existing, False
                    raise ReturnConflictError(
                        "external_receipt_id already belongs to a different receipt"
                    ) from error
            raise
        except Exception:
            db.session.rollback()
            raise


def serialize_return_receipt(receipt: ReturnReceipt) -> dict:
    return {
        "id": receipt.id,
        "receipt_uid": receipt.receipt_uid,
        "external_receipt_id": receipt.external_id,
        "quantity": number_for_json(receipt.quantity),
        "disposition": receipt.disposition,
        "disposition_label": RETURN_DISPOSITION_LABELS.get(
            receipt.disposition, receipt.disposition
        ),
        "location": receipt.location.code,
        "bin": receipt.bin.code if receipt.bin else None,
        "received_by": _actor_json(receipt.received_by),
        "note": receipt.note,
        "created_at": receipt.created_at.isoformat(),
    }


def serialize_return(rma: ReturnAuthorization) -> dict:
    return {
        "id": rma.id,
        "rma_uid": rma.rma_uid,
        "external_return_id": rma.external_id,
        "workspace_id": rma.workspace_id,
        "status": rma.status,
        "reason_code": rma.reason_code,
        "reason_label": RETURN_REASON_LABELS.get(rma.reason_code, rma.reason_code),
        "customer_note": rma.customer_note,
        "sales_order": {
            "id": rma.sales_order.id,
            "order_uid": rma.sales_order.order_uid,
            "customer_reference": rma.sales_order.customer_reference,
            "location": rma.sales_order.location.code,
        },
        "created_by": _actor_json(rma.created_by),
        "authorized_by": _actor_json(rma.authorized_by),
        "rejected_by": _actor_json(rma.rejected_by),
        "cancelled_by": _actor_json(rma.cancelled_by),
        "completed_by": _actor_json(rma.completed_by),
        "created_at": rma.created_at.isoformat(),
        "authorized_at": rma.authorized_at.isoformat() if rma.authorized_at else None,
        "rejected_at": rma.rejected_at.isoformat() if rma.rejected_at else None,
        "cancelled_at": rma.cancelled_at.isoformat() if rma.cancelled_at else None,
        "completed_at": rma.completed_at.isoformat() if rma.completed_at else None,
        "items": [
            {
                "id": item.id,
                "sales_order_item_id": item.sales_order_item_id,
                "sku": item.sales_order_item.product.sku,
                "name": item.sales_order_item.product.name,
                "unit_of_measure": item.sales_order_item.product.unit_of_measure,
                "quantity_requested": number_for_json(item.quantity_requested),
                "quantity_authorized": number_for_json(item.quantity_authorized),
                "quantity_received": number_for_json(item.quantity_received),
                "quantity_restocked": number_for_json(item.quantity_restocked),
                "quantity_remaining": number_for_json(
                    Decimal(item.quantity_authorized or 0)
                    - Decimal(item.quantity_received or 0)
                ),
                "receipts": [
                    serialize_return_receipt(receipt) for receipt in item.receipts
                ],
            }
            for item in rma.items
        ],
        "events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "user": _actor_json(event.user),
                "detail": event.detail or {},
                "created_at": event.created_at.isoformat(),
            }
            for event in rma.events
        ],
    }
