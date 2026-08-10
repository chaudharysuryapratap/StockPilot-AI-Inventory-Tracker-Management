from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    Bin,
    DemandInsight,
    InventoryLocation,
    InventoryLot,
    InventoryMovement,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    PurchaseReceiptItem,
    StockLevel,
    Supplier,
    UnitConversion,
    User,
    utcnow,
)
from app.services.bedrock import BedrockNarrator
from app.services.forecast import latest_insights
from app.services.products import number_for_json


TWOPLACES = Decimal("0.01")


class ProcurementError(ValueError):
    pass


class ProcurementStateError(ProcurementError):
    pass


class ProcurementConflictError(ProcurementError):
    pass


def _text(value: object, field: str, limit: int, *, required: bool = False) -> str | None:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ProcurementError(f"{field} is required")
    if len(normalized) > limit:
        raise ProcurementError(f"{field} must be {limit} characters or fewer")
    return normalized or None


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ProcurementError(f"{field} must be numeric")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError) as error:
        raise ProcurementError(f"{field} must be numeric") from error
    if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
        raise ProcurementError(f"{field} must be {'positive' if positive else 'non-negative'}")
    if result.as_tuple().exponent < -6:
        raise ProcurementError(f"{field} supports at most 6 decimal places")
    return result


def _date(value: object, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ProcurementError(f"{field} must be an ISO date (YYYY-MM-DD)") from error


def _require_manager(actor: User) -> User:
    if actor is None or not actor.is_active or actor.role not in {"admin", "manager"}:
        raise ProcurementError("an active Admin or Manager is required")
    return actor


class UnitConversionService:
    @staticmethod
    def define(product: Product, unit_code: object, factor: object, *, actor: User) -> UnitConversion:
        _require_manager(actor)
        if product.workspace_id != actor.workspace_id:
            raise ProcurementError("product was not found")
        unit = (_text(unit_code, "unit_code", 20, required=True) or "").lower()
        if unit == product.unit_of_measure.lower():
            raise ProcurementError("the base unit already has an implicit conversion factor of 1")
        conversion = UnitConversion.query.filter_by(
            workspace_id=actor.workspace_id, product_id=product.id, unit_code=unit
        ).first()
        if conversion is None:
            conversion = UnitConversion(
                workspace_id=actor.workspace_id, product=product, unit_code=unit
            )
            db.session.add(conversion)
        conversion.to_base_factor = _decimal(factor, "to_base_factor", positive=True)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            conversion = UnitConversion.query.filter_by(
                workspace_id=actor.workspace_id,
                product_id=product.id,
                unit_code=unit,
            ).one()
            conversion.to_base_factor = _decimal(
                factor, "to_base_factor", positive=True
            )
            db.session.commit()
        return conversion

    @staticmethod
    def factor(product: Product, unit_code: object, *, workspace_id: int) -> Decimal:
        unit = (_text(unit_code, "unit", 20, required=True) or "").lower()
        if product.workspace_id != workspace_id:
            raise ProcurementError("product was not found")
        if unit == product.unit_of_measure.lower():
            return Decimal("1")
        conversion = UnitConversion.query.filter_by(
            workspace_id=workspace_id, product_id=product.id, unit_code=unit
        ).first()
        if conversion is None:
            raise ProcurementError(
                f"no conversion from '{unit}' to '{product.unit_of_measure}' is configured for {product.sku}"
            )
        return Decimal(conversion.to_base_factor)

    @staticmethod
    def to_base(product: Product, quantity: object, unit_code: object, *, workspace_id: int) -> tuple[Decimal, Decimal]:
        raw_quantity = _decimal(quantity, "quantity", positive=True)
        factor = UnitConversionService.factor(product, unit_code, workspace_id=workspace_id)
        base = (raw_quantity * factor).quantize(TWOPLACES)
        if base <= 0:
            raise ProcurementError("converted quantity must be at least 0.01 base units")
        return base, factor


class PurchaseOrderService:
    @staticmethod
    def _workspace_order(order: PurchaseOrder, actor: User) -> PurchaseOrder:
        if order is None or order.workspace_id != actor.workspace_id:
            raise ProcurementError("purchase order was not found")
        return order

    @staticmethod
    def create(payload: Mapping[str, object], *, actor: User, source: str = "manual") -> PurchaseOrder:
        _require_manager(actor)
        if not isinstance(payload, Mapping):
            raise ProcurementError("JSON object expected")
        supplier = db.session.get(Supplier, int(payload.get("supplier_id") or 0))
        location = db.session.get(InventoryLocation, int(payload.get("location_id") or 0))
        if supplier is None or supplier.workspace_id != actor.workspace_id or not supplier.is_active:
            raise ProcurementError("supplier was not found")
        if location is None or location.workspace_id != actor.workspace_id or not location.is_active:
            raise ProcurementError("location was not found")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ProcurementError("items must be a non-empty list")
        external_id = _text(payload.get("external_purchase_order_id"), "external_purchase_order_id", 128)
        if external_id:
            existing = PurchaseOrder.query.filter_by(
                workspace_id=actor.workspace_id, external_id=external_id
            ).first()
            if existing:
                requested_by_sku: dict[str, tuple[Decimal, str, Decimal, Decimal]] = {}
                for index, raw in enumerate(raw_items, start=1):
                    if not isinstance(raw, Mapping):
                        raise ProcurementError(f"item {index} must be an object")
                    sku = str(raw.get("sku", "")).strip().upper()
                    product = Product.query.filter_by(
                        workspace_id=actor.workspace_id, sku=sku, is_active=True
                    ).first()
                    if product is None or sku in requested_by_sku:
                        raise ProcurementError(f"item {index} is invalid or duplicated")
                    unit = (_text(raw.get("unit") or product.unit_of_measure, "unit", 20, required=True) or "").lower()
                    quantity, factor = UnitConversionService.to_base(
                        product, raw.get("quantity"), unit,
                        workspace_id=actor.workspace_id,
                    )
                    cost = _decimal(
                        raw.get("unit_cost", product.cost_price), "unit_cost"
                    ).quantize(TWOPLACES)
                    requested_by_sku[sku] = (quantity, unit, factor, cost)
                recorded_by_sku = {
                    item.product.sku: (
                        Decimal(item.ordered_quantity),
                        item.order_unit,
                        Decimal(item.conversion_to_base),
                        Decimal(item.unit_cost),
                    )
                    for item in existing.items
                }
                expected_at = _date(payload.get("expected_at"), "expected_at")
                note = _text(payload.get("note"), "note", 500)
                if (
                    existing.supplier_id != supplier.id
                    or existing.location_id != location.id
                    or existing.source != source
                    or existing.expected_at != expected_at
                    or existing.note != note
                    or recorded_by_sku != requested_by_sku
                ):
                    raise ProcurementConflictError(
                        "external_purchase_order_id already belongs to a different purchase order"
                    )
                return existing

        order = PurchaseOrder(
            workspace_id=actor.workspace_id,
            external_id=external_id,
            supplier=supplier,
            location=location,
            status="draft",
            source=source,
            expected_at=_date(payload.get("expected_at"), "expected_at"),
            note=_text(payload.get("note"), "note", 500),
            ai_rationale=_text(payload.get("ai_rationale"), "ai_rationale", 4000),
            created_by=actor,
        )
        db.session.add(order)
        seen: set[int] = set()
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, Mapping):
                raise ProcurementError(f"item {index} must be an object")
            product = Product.query.filter_by(
                workspace_id=actor.workspace_id,
                sku=str(raw.get("sku", "")).strip().upper(),
                is_active=True,
            ).first()
            if product is None:
                raise ProcurementError(f"item {index} product was not found")
            if product.id in seen:
                raise ProcurementError(f"duplicate product {product.sku}")
            seen.add(product.id)
            order_unit = (_text(raw.get("unit") or product.unit_of_measure, "unit", 20, required=True) or "").lower()
            ordered_base, factor = UnitConversionService.to_base(
                product, raw.get("quantity"), order_unit, workspace_id=actor.workspace_id
            )
            db.session.add(
                PurchaseOrderItem(
                    purchase_order=order,
                    product=product,
                    ordered_quantity=ordered_base,
                    received_quantity=Decimal("0.00"),
                    order_unit=order_unit,
                    conversion_to_base=factor,
                    unit_cost=_decimal(raw.get("unit_cost", product.cost_price), "unit_cost").quantize(TWOPLACES),
                )
            )
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise ProcurementConflictError("purchase-order identifier already exists") from error
        return order

    @staticmethod
    def draft_from_recommendations(*, actor: User) -> list[PurchaseOrder]:
        _require_manager(actor)
        insights = [
            insight for insight in latest_insights(limit=500)
            if insight.location.workspace_id == actor.workspace_id
            and Decimal(insight.recommended_reorder_quantity or 0) > 0
            and insight.product.preferred_supplier_id
        ]
        groups: dict[tuple[int, int], list[DemandInsight]] = {}
        for insight in insights:
            groups.setdefault(
                (insight.product.preferred_supplier_id, insight.location_id), []
            ).append(insight)
        orders: list[PurchaseOrder] = []
        for (supplier_id, location_id), rows in groups.items():
            metrics = {
                "supplier": rows[0].product.preferred_supplier.name,
                "location": rows[0].location.code,
                "items": [
                    {
                        "sku": row.product.sku,
                        "recommended_quantity": number_for_json(row.recommended_reorder_quantity),
                        "daily_demand": round(row.daily_demand, 2),
                        "expected_stockout_at": row.expected_stockout_at.isoformat() if row.expected_stockout_at else None,
                    }
                    for row in rows
                ],
            }
            rationale = BedrockNarrator.explain(
                {"task": "explain_purchase_order_draft", **metrics}
            ) or "AI-assisted draft created from the latest approved demand model, available stock, safety stock, and supplier lead time."
            order = PurchaseOrderService.create(
                {
                    "external_purchase_order_id": (
                        f"AI-{supplier_id}-{location_id}-{max(row.id for row in rows)}"
                    ),
                    "supplier_id": supplier_id,
                    "location_id": location_id,
                    "expected_at": (date.today() + timedelta(days=rows[0].product.preferred_supplier.lead_time_days)).isoformat(),
                    "ai_rationale": rationale,
                    "items": [
                        {
                            "sku": row.product.sku,
                            "quantity": row.recommended_reorder_quantity,
                            "unit": row.product.unit_of_measure,
                            "unit_cost": row.product.cost_price,
                        }
                        for row in rows
                    ],
                },
                actor=actor,
                source="ai",
            )
            orders.append(order)
        return orders

    @staticmethod
    def approve(order: PurchaseOrder, *, actor: User) -> PurchaseOrder:
        _require_manager(actor)
        PurchaseOrderService._workspace_order(order, actor)
        if order.status == "approved":
            return order
        if order.status != "draft":
            raise ProcurementStateError("only draft purchase orders can be approved")
        order.status = "approved"
        order.approved_by = actor
        order.approved_at = utcnow()
        order.updated_at = utcnow()
        db.session.commit()
        return order

    @staticmethod
    def receive(order: PurchaseOrder, payload: Mapping[str, object], *, actor: User) -> tuple[PurchaseReceipt, bool]:
        _require_manager(actor)
        PurchaseOrderService._workspace_order(order, actor)
        if order.status not in {"approved", "partially_received"}:
            raise ProcurementStateError("purchase order must be approved before receiving")
        external_id = _text(payload.get("external_receipt_id"), "external_receipt_id", 128, required=True)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ProcurementError("items must be a non-empty list")
        existing = PurchaseReceipt.query.filter_by(
            purchase_order_id=order.id, external_id=external_id
        ).first()
        if existing:
            requested: dict[
                int, tuple[Decimal, str, str | None, date | None, date | None]
            ] = {}
            for index, raw in enumerate(raw_items, start=1):
                if not isinstance(raw, Mapping):
                    raise ProcurementError(f"receipt item {index} must be an object")
                item = db.session.get(PurchaseOrderItem, int(raw.get("item_id") or 0))
                if item is None or item.purchase_order_id != order.id:
                    raise ProcurementError(f"receipt item {index} was not found on this order")
                unit = (_text(raw.get("unit") or item.order_unit, "unit", 20, required=True) or "").lower()
                base_quantity, _ = UnitConversionService.to_base(
                    item.product, raw.get("quantity"), unit, workspace_id=actor.workspace_id
                )
                requested[item.id] = (
                    base_quantity,
                    unit,
                    _text(raw.get("lot_number"), "lot_number", 100),
                    _date(raw.get("manufactured_at"), "manufactured_at"),
                    _date(raw.get("expiry_date"), "expiry_date"),
                )
            recorded = {
                row.purchase_order_item_id: (
                    Decimal(row.quantity_received),
                    row.received_unit,
                    row.inventory_lot.lot_number if row.inventory_lot else None,
                    row.inventory_lot.manufactured_at if row.inventory_lot else None,
                    row.inventory_lot.expiry_date if row.inventory_lot else None,
                )
                for row in existing.items
            }
            if requested != recorded:
                raise ProcurementConflictError(
                    "external_receipt_id already belongs to a different receipt"
                )
            return existing, False
        receipt = PurchaseReceipt(
            purchase_order=order,
            external_id=external_id,
            received_by=actor,
            note=_text(payload.get("note"), "note", 255),
        )
        db.session.add(receipt)
        seen_items: set[int] = set()
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, Mapping):
                raise ProcurementError(f"receipt item {index} must be an object")
            item = db.session.get(PurchaseOrderItem, int(raw.get("item_id") or 0))
            if item is None or item.purchase_order_id != order.id:
                raise ProcurementError(f"receipt item {index} was not found on this order")
            if item.id in seen_items:
                raise ProcurementError(f"receipt item {item.id} is duplicated")
            seen_items.add(item.id)
            unit = (_text(raw.get("unit") or item.order_unit, "unit", 20, required=True) or "").lower()
            base_quantity, factor = UnitConversionService.to_base(
                item.product, raw.get("quantity"), unit, workspace_id=actor.workspace_id
            )
            remaining = Decimal(item.ordered_quantity) - Decimal(item.received_quantity)
            if base_quantity > remaining:
                raise ProcurementConflictError(
                    f"receipt for {item.product.sku} exceeds the remaining ordered quantity of {number_for_json(remaining)} {item.product.unit_of_measure}"
                )
            bin_record = None
            bin_code = _text(raw.get("bin_code"), "bin_code", 50)
            if bin_code:
                bin_record = Bin.query.filter_by(
                    location_id=order.location_id, code=bin_code.upper(), is_active=True
                ).first()
                if bin_record is None:
                    raise ProcurementError(f"bin {bin_code} was not found at {order.location.code}")
            lot_number = _text(raw.get("lot_number"), "lot_number", 100)
            expiry_date = _date(raw.get("expiry_date"), "expiry_date")
            manufactured_at = _date(raw.get("manufactured_at"), "manufactured_at")
            if (manufactured_at or expiry_date) and not lot_number:
                raise ProcurementError(
                    "lot_number is required when manufacturing or expiry dates are recorded"
                )
            if item.product.is_perishable and (not lot_number or not expiry_date):
                raise ProcurementError(
                    f"lot_number and expiry_date are required for perishable product {item.product.sku}"
                )
            if expiry_date and expiry_date < date.today():
                raise ProcurementConflictError("expired stock cannot be received")
            if manufactured_at and expiry_date and manufactured_at > expiry_date:
                raise ProcurementError("manufactured_at cannot be after expiry_date")
            if bin_record and bin_record.capacity is not None:
                current_bin_quantity = db.session.query(
                    func.coalesce(func.sum(StockLevel.quantity_on_hand), 0)
                ).filter(StockLevel.bin_id == bin_record.id).with_for_update().scalar()
                if Decimal(current_bin_quantity or 0) + base_quantity > Decimal(bin_record.capacity):
                    raise ProcurementConflictError(
                        f"bin {order.location.code}/{bin_record.code} does not have enough capacity"
                    )
            stock = StockLevel.query.filter_by(
                product_id=item.product_id,
                location_id=order.location_id,
                bin_id=bin_record.id if bin_record else None,
            ).with_for_update().first()
            if stock is None:
                stock = StockLevel(
                    product=item.product, location=order.location, bin=bin_record,
                    quantity_on_hand=Decimal("0.00"), quantity_reserved=Decimal("0.00")
                )
                db.session.add(stock)
            lot = None
            if lot_number:
                lot = InventoryLot.query.filter_by(
                    workspace_id=actor.workspace_id,
                    product_id=item.product_id,
                    location_id=order.location_id,
                    bin_id=bin_record.id if bin_record else None,
                    lot_number=lot_number,
                ).with_for_update().first()
                if lot is None:
                    lot = InventoryLot(
                        workspace_id=actor.workspace_id,
                        product=item.product,
                        location=order.location,
                        bin=bin_record,
                        lot_number=lot_number,
                        manufactured_at=manufactured_at,
                        expiry_date=expiry_date,
                        quantity_on_hand=Decimal("0.00"),
                    )
                    db.session.add(lot)
                elif lot.expiry_date != expiry_date or lot.manufactured_at != manufactured_at:
                    raise ProcurementConflictError("lot dates conflict with the existing lot record")
                lot.quantity_on_hand += base_quantity
                lot.updated_at = utcnow()
            stock.quantity_on_hand += base_quantity
            stock.updated_at = utcnow()
            item.received_quantity += base_quantity
            db.session.add(
                PurchaseReceiptItem(
                    receipt=receipt,
                    purchase_order_item=item,
                    inventory_lot=lot,
                    quantity_received=base_quantity,
                    received_unit=unit,
                    conversion_to_base=factor,
                )
            )
            db.session.add(
                InventoryMovement(
                    product=item.product,
                    location=order.location,
                    bin=bin_record,
                    user=actor,
                    movement_type="purchase_receipt",
                    quantity_delta=base_quantity,
                    reason="purchase_order_received",
                    reference_type="purchase_order",
                    reference_id=order.po_uid,
                    note=f"Receipt {external_id}; lot {lot_number or 'untracked'}",
                )
            )
        order.status = (
            "received"
            if all(Decimal(item.received_quantity) >= Decimal(item.ordered_quantity) for item in order.items)
            else "partially_received"
        )
        order.updated_at = utcnow()
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            existing = PurchaseReceipt.query.filter_by(
                purchase_order_id=order.id, external_id=external_id
            ).first()
            if existing:
                return existing, False
            raise ProcurementConflictError("receipt could not be committed") from error
        return receipt, True


def consume_tracked_lots(
    *, workspace_id: int, product_id: int, location_id: int,
    bin_id: int | None, quantity: Decimal
) -> Decimal:
    """Consume tracked lots FEFO; untracked legacy stock is left as the remainder."""
    remaining = Decimal(quantity)
    lots = (
        InventoryLot.query.filter_by(
            workspace_id=workspace_id, product_id=product_id,
            location_id=location_id, bin_id=bin_id
        )
        .filter(InventoryLot.quantity_on_hand > 0)
        .order_by(InventoryLot.expiry_date.is_(None), InventoryLot.expiry_date, InventoryLot.received_at, InventoryLot.id)
        .with_for_update().all()
    )
    consumed = Decimal("0.00")
    for lot in lots:
        deduction = min(Decimal(lot.quantity_on_hand), remaining)
        lot.quantity_on_hand -= deduction
        lot.updated_at = utcnow()
        consumed += deduction
        remaining -= deduction
        if remaining <= 0:
            break
    return consumed


def transfer_tracked_lots(
    *, workspace_id: int, product_id: int,
    source_location_id: int, source_bin_id: int | None,
    destination_location_id: int, destination_bin_id: int | None,
    quantity: Decimal,
) -> Decimal:
    """Move tracked balances FEFO while leaving legacy untracked stock implicit."""
    remaining = Decimal(quantity)
    source_lots = (
        InventoryLot.query.filter_by(
            workspace_id=workspace_id,
            product_id=product_id,
            location_id=source_location_id,
            bin_id=source_bin_id,
        )
        .filter(InventoryLot.quantity_on_hand > 0)
        .order_by(
            InventoryLot.expiry_date.is_(None), InventoryLot.expiry_date,
            InventoryLot.received_at, InventoryLot.id
        )
        .with_for_update().all()
    )
    moved = Decimal("0.00")
    for source_lot in source_lots:
        amount = min(Decimal(source_lot.quantity_on_hand), remaining)
        destination_lot = InventoryLot.query.filter_by(
            workspace_id=workspace_id,
            product_id=product_id,
            location_id=destination_location_id,
            bin_id=destination_bin_id,
            lot_number=source_lot.lot_number,
        ).with_for_update().first()
        if destination_lot is None:
            destination_lot = InventoryLot(
                workspace_id=workspace_id,
                product_id=product_id,
                location_id=destination_location_id,
                bin_id=destination_bin_id,
                lot_number=source_lot.lot_number,
                manufactured_at=source_lot.manufactured_at,
                expiry_date=source_lot.expiry_date,
                quantity_on_hand=Decimal("0.00"),
            )
            db.session.add(destination_lot)
        elif (
            destination_lot.manufactured_at != source_lot.manufactured_at
            or destination_lot.expiry_date != source_lot.expiry_date
        ):
            raise ProcurementConflictError(
                f"lot {source_lot.lot_number} has conflicting dates at the destination"
            )
        source_lot.quantity_on_hand -= amount
        destination_lot.quantity_on_hand += amount
        source_lot.updated_at = destination_lot.updated_at = utcnow()
        moved += amount
        remaining -= amount
        if remaining <= 0:
            break
    return moved


def serialize_purchase_order(order: PurchaseOrder) -> dict:
    return {
        "id": order.id,
        "po_uid": order.po_uid,
        "external_purchase_order_id": order.external_id,
        "status": order.status,
        "source": order.source,
        "supplier": {"id": order.supplier.id, "name": order.supplier.name},
        "location": {"id": order.location.id, "code": order.location.code},
        "expected_at": order.expected_at.isoformat() if order.expected_at else None,
        "note": order.note,
        "ai_rationale": order.ai_rationale,
        "approved_at": order.approved_at.isoformat() if order.approved_at else None,
        "created_at": order.created_at.isoformat(),
        "items": [
            {
                "id": item.id,
                "sku": item.product.sku,
                "product": item.product.name,
                "base_unit": item.product.unit_of_measure,
                "order_unit": item.order_unit,
                "conversion_to_base": number_for_json(item.conversion_to_base),
                "ordered_quantity": number_for_json(item.ordered_quantity),
                "received_quantity": number_for_json(item.received_quantity),
                "remaining_quantity": number_for_json(
                    Decimal(item.ordered_quantity) - Decimal(item.received_quantity)
                ),
                "unit_cost": number_for_json(item.unit_cost),
            }
            for item in order.items
        ],
    }


def serialize_receipt(receipt: PurchaseReceipt) -> dict:
    return {
        "id": receipt.id,
        "receipt_uid": receipt.receipt_uid,
        "external_receipt_id": receipt.external_id,
        "purchase_order_id": receipt.purchase_order_id,
        "received_at": receipt.received_at.isoformat(),
        "received_by": receipt.received_by.email,
        "items": [
            {
                "purchase_order_item_id": item.purchase_order_item_id,
                "sku": item.purchase_order_item.product.sku,
                "quantity_received": number_for_json(item.quantity_received),
                "received_unit": item.received_unit,
                "conversion_to_base": number_for_json(item.conversion_to_base),
                "lot_number": item.inventory_lot.lot_number if item.inventory_lot else None,
            }
            for item in receipt.items
        ],
    }
