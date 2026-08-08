from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    Bin,
    InventoryLocation,
    InventoryMovement,
    Product,
    Sale,
    SaleItem,
    StockLevel,
    User,
    utcnow,
)
from app.services.products import number_for_json


class InventoryError(Exception):
    """Base exception for expected inventory workflow failures."""


class InsufficientStockError(InventoryError):
    pass


class CapacityExceededError(InventoryError):
    pass


class UnknownInventoryReferenceError(InventoryError):
    pass


class InventoryConflictError(InventoryError):
    """An idempotency key was reused for a different inventory operation."""

    pass


def _quantity(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a {'positive' if positive else 'non-zero'} quantity")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError) as error:
        raise ValueError(
            f"{field_name} must be a {'positive' if positive else 'non-zero'} quantity"
        ) from error
    if not number.is_finite() or (number <= 0 if positive else number == 0):
        raise ValueError(f"{field_name} must be a {'positive' if positive else 'non-zero'} quantity")
    if number.as_tuple().exponent < -2:
        raise ValueError(f"{field_name} must have at most 2 decimal places")
    return number.quantize(Decimal("0.01"))


def _parse_datetime(value: object | None) -> datetime:
    if not value:
        return utcnow()
    if not isinstance(value, str):
        raise ValueError("occurred_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("occurred_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unit_price(value: object, sku: str) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"unit_price for {sku} must be a non-negative amount")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"unit_price for {sku} must be numeric") from error
    if (
        not amount.is_finite()
        or amount < 0
        or amount > Decimal("99999999.99")
        or amount.as_tuple().exponent < -2
    ):
        raise ValueError(
            f"unit_price for {sku} must be between 0 and 99999999.99 "
            "with at most 2 decimal places"
        )
    return amount.quantize(Decimal("0.01"))


def _matching_sale(
    sale: Sale,
    *,
    source: str,
    location_id: int,
    occurred_at: datetime | None,
    requested: dict[str, dict],
) -> bool:
    existing_items = {
        item.product.sku: {
            "quantity": Decimal(item.quantity),
            "unit_price": (
                Decimal(item.unit_price) if item.unit_price is not None else None
            ),
        }
        for item in sale.items
    }
    if sale.source != source or sale.location_id != location_id:
        return False
    if occurred_at is not None:
        saved_time = sale.occurred_at
        if saved_time.tzinfo is None:
            saved_time = saved_time.replace(tzinfo=timezone.utc)
        else:
            saved_time = saved_time.astimezone(timezone.utc)
        if saved_time != occurred_at:
            return False
    return existing_items == requested


def serialize_sale(sale: Sale) -> dict:
    return {
        "id": sale.id,
        "external_sale_id": sale.external_id,
        "source": sale.source,
        "location": sale.location.code,
        "occurred_at": sale.occurred_at.isoformat(),
        "items": [
            {
                "sku": item.product.sku,
                "quantity": number_for_json(item.quantity),
                "unit_price": float(item.unit_price) if item.unit_price is not None else None,
            }
            for item in sale.items
        ],
    }


class InventoryService:
    """Transactional stock operations shared by POS and manual workflows."""

    @staticmethod
    def record_sale(payload: dict, *, actor: User | None = None) -> tuple[Sale, bool]:
        if not isinstance(payload, dict):
            raise ValueError("JSON object expected")

        external_id = str(payload.get("external_sale_id", "")).strip()
        location_code = str(payload.get("location_code", "")).strip().upper()
        raw_items = payload.get("items")
        if not external_id:
            raise ValueError("external_sale_id is required")
        if not location_code:
            raise ValueError("location_code is required")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("items must be a non-empty list")

        source = str(payload.get("source", "pos") or "pos").strip()
        if len(source) > 64:
            raise ValueError("source must be 64 characters or fewer")
        occurred_at = (
            _parse_datetime(payload.get("occurred_at"))
            if payload.get("occurred_at")
            else None
        )

        location = InventoryLocation.query.filter_by(
            code=location_code, is_active=True
        ).first()
        if not location:
            raise UnknownInventoryReferenceError(
                f"No inventory location exists for code '{location_code}'"
            )

        # Aggregate duplicate SKUs before applying the update, so one line cannot
        # bypass the available-stock check by appearing several times in a sale.
        requested: dict[str, dict] = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValueError("every item must be an object")
            sku = str(raw_item.get("sku", "")).strip().upper()
            if not sku:
                raise ValueError("each item needs a sku")
            quantity = _quantity(
                raw_item.get("quantity"), f"quantity for {sku}", positive=True
            )
            unit_price = _unit_price(raw_item.get("unit_price"), sku)
            current = requested.setdefault(
                sku, {"quantity": Decimal("0.00"), "unit_price": unit_price}
            )
            if current["unit_price"] != unit_price:
                raise ValueError(
                    f"duplicate lines for {sku} must use the same unit_price"
                )
            current["quantity"] += quantity

        existing = Sale.query.filter_by(external_id=external_id).first()
        if existing:
            if not _matching_sale(
                existing,
                source=source,
                location_id=location.id,
                occurred_at=occurred_at,
                requested=requested,
            ):
                raise InventoryConflictError(
                    "external_sale_id already belongs to a different sale"
                )
            return existing, False

        sale = Sale(
            external_id=external_id,
            source=source,
            location=location,
            occurred_at=occurred_at or utcnow(),
        )

        try:
            db.session.add(sale)
            db.session.flush()

            for sku, requested_item in requested.items():
                product = Product.query.filter_by(sku=sku, active=True).first()
                if not product:
                    raise UnknownInventoryReferenceError(
                        f"No active product exists for SKU '{sku}'"
                    )

                # MySQL locks this row until commit, preventing two simultaneous
                # checkout messages from selling the same final units.
                stock_rows = (
                    StockLevel.query.filter_by(
                        product_id=product.id, location_id=location.id
                    )
                    .order_by(StockLevel.bin_id, StockLevel.id)
                    .with_for_update()
                    .all()
                )
                if not stock_rows:
                    raise UnknownInventoryReferenceError(
                        f"SKU '{sku}' is not stocked at location '{location_code}'"
                    )
                total_available = sum(
                    (row.quantity_available for row in stock_rows), Decimal("0.00")
                )
                if total_available < requested_item["quantity"]:
                    raise InsufficientStockError(
                        f"Only {number_for_json(total_available)} {product.unit} "
                        f"of '{sku}' are available at {location_code}"
                    )

                db.session.add(
                    SaleItem(
                        sale=sale,
                        product=product,
                        quantity=requested_item["quantity"],
                        unit_price=requested_item["unit_price"],
                    )
                )
                remaining = requested_item["quantity"]
                for stock in stock_rows:
                    allocated = min(stock.quantity_available, remaining)
                    if allocated <= 0:
                        continue
                    stock.quantity_on_hand -= allocated
                    stock.updated_at = utcnow()
                    db.session.add(
                        InventoryMovement(
                            product=product,
                            location=location,
                            bin=stock.bin,
                            user=actor,
                            movement_type="sale",
                            quantity_delta=-allocated,
                            reason="sale",
                            reference_type="sale",
                            reference_id=external_id,
                        )
                    )
                    remaining -= allocated
                    if remaining <= 0:
                        break

            db.session.commit()
            return sale, True
        except IntegrityError:
            db.session.rollback()
            # The only expected race here is an idempotent duplicate arriving at
            # the same time. If it is something else, surface the original issue.
            duplicate = Sale.query.filter_by(external_id=external_id).first()
            if duplicate:
                if _matching_sale(
                    duplicate,
                    source=source,
                    location_id=location.id,
                    occurred_at=occurred_at,
                    requested=requested,
                ):
                    return duplicate, False
                raise InventoryConflictError(
                    "external_sale_id already belongs to a different sale"
                )
            raise
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def adjust_stock(payload: dict, *, actor: User | None = None) -> StockLevel:
        if not isinstance(payload, dict):
            raise ValueError("JSON object expected")
        sku = str(payload.get("sku", "")).strip().upper()
        location_code = str(payload.get("location_code", "")).strip().upper()
        if not sku or not location_code:
            raise ValueError("sku and location_code are required")
        delta = _quantity(payload.get("quantity_delta"), "quantity_delta")

        product = Product.query.filter_by(sku=sku).first()
        location = InventoryLocation.query.filter_by(
            code=location_code, is_active=True
        ).first()
        if not product or not location:
            raise UnknownInventoryReferenceError("product or location was not found")

        bin_code = str(payload.get("bin_code", "") or "").strip().upper()
        bin_record = None
        if bin_code:
            bin_record = Bin.query.filter_by(
                location_id=location.id, code=bin_code, is_active=True
            ).first()
            if bin_record is None:
                raise UnknownInventoryReferenceError(
                    f"active bin '{bin_code}' was not found at {location_code}"
                )

        stock = (
            StockLevel.query.filter_by(
                product_id=product.id,
                location_id=location.id,
                bin_id=bin_record.id if bin_record else None,
            )
            .with_for_update()
            .first()
        )
        if not stock:
            stock = StockLevel(
                product=product,
                location=location,
                bin=bin_record,
                quantity_on_hand=Decimal("0.00"),
                quantity_reserved=Decimal("0.00"),
            )
            db.session.add(stock)
            db.session.flush()
        if bin_record and bin_record.capacity is not None and delta > 0:
            rows_in_bin = (
                StockLevel.query.filter_by(bin_id=bin_record.id)
                .order_by(StockLevel.id)
                .with_for_update()
                .all()
            )
            current_quantity = sum(
                (Decimal(row.quantity_on_hand or 0) for row in rows_in_bin),
                start=Decimal("0.00"),
            )
            if current_quantity + delta > Decimal(bin_record.capacity):
                db.session.rollback()
                raise CapacityExceededError(
                    f"bin {location.code}/{bin_record.code} does not have enough capacity"
                )
        if Decimal(stock.quantity_on_hand or 0) + delta < Decimal(
            stock.quantity_reserved or 0
        ):
            db.session.rollback()
            raise InsufficientStockError(
                "adjustment would reduce on-hand stock below the reserved quantity"
            )

        try:
            stock.quantity_on_hand += delta
            stock.updated_at = utcnow()
            db.session.add(
                InventoryMovement(
                    product=product,
                    location=location,
                    bin=bin_record,
                    user=actor,
                    movement_type="adjustment",
                    quantity_delta=delta,
                    reason=str(payload.get("reason", "manual_adjustment"))[:64],
                    reference_type="manual",
                    reference_id=str(payload.get("reference_id", ""))[:128] or None,
                    note=str(payload.get("note", ""))[:255] or None,
                )
            )
            db.session.commit()
            return stock
        except Exception:
            db.session.rollback()
            raise
