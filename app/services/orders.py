from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import re

from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    InventoryLocation,
    InventoryMovement,
    Product,
    Sale,
    SaleItem,
    SalesOrder,
    SalesOrderAllocation,
    SalesOrderItem,
    StockLevel,
    User,
    utcnow,
)
from app.services.inventory import (
    InsufficientStockError,
    UnknownInventoryReferenceError,
    _quantity,
)
from app.services.products import number_for_json


CHANNEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ORDER_CREATE_ROLES = ("admin", "manager")
ORDER_FULFILL_ROLES = ("admin", "manager", "picker")
ORDER_CONTROL_ROLES = ("admin", "manager")


class SalesOrderConflictError(ValueError):
    pass


class SalesOrderStateError(SalesOrderConflictError):
    pass


class SalesOrderPermissionError(PermissionError):
    pass


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


def _require_actor(actor: User | None, roles: tuple[str, ...]) -> User:
    if actor is None or not actor.is_active:
        raise SalesOrderPermissionError("an active user is required")
    if not actor.has_role(*roles):
        raise SalesOrderPermissionError("your role cannot perform this order action")
    return actor


def _require_workspace(order: SalesOrder, actor: User) -> None:
    if order.workspace_id != actor.workspace_id:
        raise UnknownInventoryReferenceError("sales order was not found")


def _normalize_channel(value: object) -> str:
    channel = str(value or "manual").strip().lower()
    if not channel or len(channel) > 50 or not CHANNEL_PATTERN.fullmatch(channel):
        raise ValueError(
            "channel must use 1-50 lowercase letters, numbers, hyphens, or underscores"
        )
    return channel


def _normalize_items(raw_items: object) -> dict[str, Decimal]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty list")
    if len(raw_items) > 100:
        raise ValueError("an order can contain at most 100 item rows")

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


def _matching_order(
    order: SalesOrder,
    *,
    location_id: int,
    channel: str,
    customer_reference: str | None,
    requested: Mapping[str, Decimal],
) -> bool:
    existing_items = {
        item.product.sku: Decimal(item.quantity) for item in order.items
    }
    return (
        order.location_id == location_id
        and order.channel == channel
        and order.customer_reference == customer_reference
        and existing_items == dict(requested)
    )


def _locked_order(order: SalesOrder) -> SalesOrder:
    return (
        SalesOrder.query.filter_by(id=order.id)
        .with_for_update()
        .one()
    )


def _actor_json(actor: User | None) -> dict | None:
    if actor is None:
        return None
    return {"id": actor.id, "name": actor.name, "email": actor.email}


class SalesOrderService:
    """Transactional reservation, picking, packing, and shipping workflow."""

    @staticmethod
    def create(
        payload: Mapping[str, object], *, actor: User
    ) -> tuple[SalesOrder, bool]:
        actor = _require_actor(actor, ORDER_CREATE_ROLES)
        if not isinstance(payload, Mapping):
            raise ValueError("JSON object expected")

        external_id = _text(payload, "external_order_id", 128)
        customer_reference = _text(payload, "customer_reference", 128)
        note = _text(payload, "note", 255)
        channel = _normalize_channel(payload.get("channel", "manual"))
        location_code = str(payload.get("location_code", "") or "").strip().upper()
        if not location_code:
            raise ValueError("location_code is required")
        requested = _normalize_items(payload.get("items"))

        location = InventoryLocation.query.filter_by(
            workspace_id=actor.workspace_id, code=location_code, is_active=True
        ).first()
        if location is None or location.workspace_id not in (None, actor.workspace_id):
            raise UnknownInventoryReferenceError(
                f"No active inventory location exists for code '{location_code}'"
            )

        products = Product.query.filter(
            Product.workspace_id == actor.workspace_id,
            Product.sku.in_(requested), Product.is_active.is_(True)
        ).all()
        products_by_sku = {product.sku: product for product in products}
        missing_skus = sorted(set(requested) - set(products_by_sku))
        if missing_skus:
            raise UnknownInventoryReferenceError(
                "No active product exists for SKU(s): " + ", ".join(missing_skus)
            )

        if external_id:
            existing = SalesOrder.query.filter_by(
                workspace_id=actor.workspace_id, external_id=external_id
            ).first()
            if existing:
                if existing.workspace_id != actor.workspace_id or not _matching_order(
                    existing,
                    location_id=location.id,
                    channel=channel,
                    customer_reference=customer_reference,
                    requested=requested,
                ):
                    raise SalesOrderConflictError(
                        "external_order_id already belongs to a different sales order"
                    )
                return existing, False

        product_ids = sorted(product.id for product in products)
        try:
            stock_rows = (
                StockLevel.query.filter(
                    StockLevel.location_id == location.id,
                    StockLevel.product_id.in_(product_ids),
                )
                .order_by(StockLevel.product_id, StockLevel.bin_id, StockLevel.id)
                .with_for_update()
                .all()
            )
            rows_by_product: dict[int, list[StockLevel]] = {}
            for stock in stock_rows:
                rows_by_product.setdefault(stock.product_id, []).append(stock)

            for sku, quantity in requested.items():
                product = products_by_sku[sku]
                available = sum(
                    (
                        stock.quantity_available
                        for stock in rows_by_product.get(product.id, [])
                    ),
                    Decimal("0.00"),
                )
                if available < quantity:
                    raise InsufficientStockError(
                        f"Only {number_for_json(available)} {product.unit_of_measure} "
                        f"of '{sku}' are available at {location.code}"
                    )

            order = SalesOrder(
                external_id=external_id,
                workspace_id=actor.workspace_id,
                location=location,
                channel=channel,
                status="pending",
                customer_reference=customer_reference,
                note=note,
                created_by=actor,
            )
            db.session.add(order)
            db.session.flush()

            for sku, quantity in requested.items():
                product = products_by_sku[sku]
                order_item = SalesOrderItem(
                    order=order,
                    product=product,
                    quantity=quantity,
                    picked_quantity=Decimal("0.00"),
                )
                db.session.add(order_item)
                db.session.flush()

                remaining = quantity
                for stock in rows_by_product[product.id]:
                    allocation_quantity = min(stock.quantity_available, remaining)
                    if allocation_quantity <= 0:
                        continue
                    stock.quantity_reserved += allocation_quantity
                    stock.updated_at = utcnow()
                    db.session.add(
                        SalesOrderAllocation(
                            order_item=order_item,
                            stock_level=stock,
                            quantity_reserved=allocation_quantity,
                            picked_quantity=Decimal("0.00"),
                        )
                    )
                    remaining -= allocation_quantity
                    if remaining <= 0:
                        break

            db.session.commit()
            return order, True
        except IntegrityError as error:
            db.session.rollback()
            if external_id:
                existing = SalesOrder.query.filter_by(
                    workspace_id=actor.workspace_id, external_id=external_id
                ).first()
                if existing and existing.workspace_id == actor.workspace_id:
                    if _matching_order(
                        existing,
                        location_id=location.id,
                        channel=channel,
                        customer_reference=customer_reference,
                        requested=requested,
                    ):
                        return existing, False
                    raise SalesOrderConflictError(
                        "external_order_id already belongs to a different sales order"
                    ) from error
            raise
        except InsufficientStockError:
            # A concurrent retry can wait behind the first request's stock-row
            # lock and then observe less available stock. Re-check its stable
            # external ID before reporting a false shortage.
            db.session.rollback()
            if external_id:
                existing = SalesOrder.query.filter_by(
                    workspace_id=actor.workspace_id, external_id=external_id
                ).first()
                if existing and existing.workspace_id == actor.workspace_id:
                    if _matching_order(
                        existing,
                        location_id=location.id,
                        channel=channel,
                        customer_reference=customer_reference,
                        requested=requested,
                    ):
                        return existing, False
            raise
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def start_picking(order: SalesOrder, *, actor: User) -> tuple[SalesOrder, bool]:
        actor = _require_actor(actor, ORDER_FULFILL_ROLES)
        try:
            order = _locked_order(order)
            _require_workspace(order, actor)
            if order.status == "picking":
                db.session.rollback()
                return order, False
            if order.status != "pending":
                raise SalesOrderStateError(
                    f"order cannot enter picking from status '{order.status}'"
                )
            order.status = "picking"
            order.picking_started_by = actor
            order.picking_started_at = utcnow()
            db.session.commit()
            return order, True
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def confirm_item_picked(
        order: SalesOrder, item_id: int, *, actor: User
    ) -> tuple[SalesOrderItem, bool]:
        actor = _require_actor(actor, ORDER_FULFILL_ROLES)
        try:
            order = _locked_order(order)
            _require_workspace(order, actor)
            if order.status != "picking":
                raise SalesOrderStateError(
                    f"items can be picked only while an order is picking, not '{order.status}'"
                )
            item = SalesOrderItem.query.filter_by(
                id=item_id, order_id=order.id
            ).with_for_update().first()
            if item is None:
                raise UnknownInventoryReferenceError("sales order item was not found")
            if Decimal(item.picked_quantity or 0) == Decimal(item.quantity):
                db.session.rollback()
                return item, False

            now = utcnow()
            item.picked_quantity = item.quantity
            item.picked_by = actor
            item.picked_at = now
            for allocation in item.allocations:
                allocation.picked_quantity = allocation.quantity_reserved
            db.session.commit()
            return item, True
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def confirm_packing(order: SalesOrder, *, actor: User) -> tuple[SalesOrder, bool]:
        actor = _require_actor(actor, ORDER_FULFILL_ROLES)
        try:
            order = _locked_order(order)
            _require_workspace(order, actor)
            if order.status == "packed":
                db.session.rollback()
                return order, False
            if order.status != "picking":
                raise SalesOrderStateError(
                    f"order cannot be packed from status '{order.status}'"
                )
            unpicked = [
                item.product.sku
                for item in order.items
                if Decimal(item.picked_quantity or 0) != Decimal(item.quantity)
            ]
            if unpicked:
                raise SalesOrderStateError(
                    "all items must be picked before packing: " + ", ".join(unpicked)
                )
            order.status = "packed"
            order.packed_by = actor
            order.packed_at = utcnow()
            db.session.commit()
            return order, True
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def ship(order: SalesOrder, *, actor: User) -> tuple[SalesOrder, bool]:
        actor = _require_actor(actor, ORDER_CONTROL_ROLES)
        try:
            order = _locked_order(order)
            _require_workspace(order, actor)
            if order.status == "shipped":
                db.session.rollback()
                return order, False
            if order.status != "packed":
                raise SalesOrderStateError(
                    f"order cannot be shipped from status '{order.status}'"
                )

            allocations = (
                SalesOrderAllocation.query.join(SalesOrderItem)
                .filter(SalesOrderItem.order_id == order.id)
                .order_by(SalesOrderAllocation.stock_level_id)
                .all()
            )
            stock_ids = sorted({allocation.stock_level_id for allocation in allocations})
            stocks = (
                StockLevel.query.filter(StockLevel.id.in_(stock_ids))
                .order_by(StockLevel.id)
                .with_for_update()
                .all()
            )
            stock_by_id = {stock.id: stock for stock in stocks}
            if len(stock_by_id) != len(stock_ids):
                raise SalesOrderStateError("a reserved stock position no longer exists")

            for allocation in allocations:
                stock = stock_by_id[allocation.stock_level_id]
                quantity = Decimal(allocation.quantity_reserved)
                if (
                    Decimal(stock.quantity_reserved or 0) < quantity
                    or Decimal(stock.quantity_on_hand or 0) < quantity
                ):
                    raise SalesOrderStateError(
                        "reserved stock is inconsistent; shipment was not committed"
                    )

            shipped_at = utcnow()
            sale = Sale(
                workspace_id=order.workspace_id,
                external_id=f"sales-order:{order.order_uid}",
                source=order.channel,
                location=order.location,
                occurred_at=shipped_at,
            )
            db.session.add(sale)
            db.session.flush()
            for item in order.items:
                db.session.add(
                    SaleItem(
                        sale=sale,
                        product=item.product,
                        quantity=item.quantity,
                        unit_price=item.product.sell_price,
                    )
                )

            for allocation in allocations:
                stock = stock_by_id[allocation.stock_level_id]
                quantity = Decimal(allocation.quantity_reserved)
                stock.quantity_on_hand -= quantity
                stock.quantity_reserved -= quantity
                stock.updated_at = shipped_at
                from app.services.procurement import consume_tracked_lots

                consume_tracked_lots(
                    workspace_id=order.workspace_id,
                    product_id=allocation.order_item.product_id,
                    location_id=stock.location_id,
                    bin_id=stock.bin_id,
                    quantity=quantity,
                )
                db.session.add(
                    InventoryMovement(
                        product=allocation.order_item.product,
                        location=stock.location,
                        bin=stock.bin,
                        user=actor,
                        movement_type="sale",
                        quantity_delta=-quantity,
                        reason="order_fulfillment",
                        reference_type="sales_order",
                        reference_id=order.order_uid,
                    )
                )

            order.status = "shipped"
            order.shipped_by = actor
            order.shipped_at = shipped_at
            db.session.commit()
            return order, True
        except IntegrityError:
            db.session.rollback()
            refreshed = db.session.get(SalesOrder, order.id)
            if refreshed and refreshed.status == "shipped":
                return refreshed, False
            raise
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def cancel(order: SalesOrder, *, actor: User) -> tuple[SalesOrder, bool]:
        actor = _require_actor(actor, ORDER_CONTROL_ROLES)
        try:
            order = _locked_order(order)
            _require_workspace(order, actor)
            if order.status == "cancelled":
                db.session.rollback()
                return order, False
            if order.status == "shipped":
                raise SalesOrderStateError("a shipped order cannot be cancelled")

            allocations = (
                SalesOrderAllocation.query.join(SalesOrderItem)
                .filter(SalesOrderItem.order_id == order.id)
                .order_by(SalesOrderAllocation.stock_level_id)
                .all()
            )
            stock_ids = sorted({allocation.stock_level_id for allocation in allocations})
            stocks = (
                StockLevel.query.filter(StockLevel.id.in_(stock_ids))
                .order_by(StockLevel.id)
                .with_for_update()
                .all()
            )
            stock_by_id = {stock.id: stock for stock in stocks}
            if len(stock_by_id) != len(stock_ids):
                raise SalesOrderStateError("a reserved stock position no longer exists")
            for allocation in allocations:
                stock = stock_by_id[allocation.stock_level_id]
                quantity = Decimal(allocation.quantity_reserved)
                if Decimal(stock.quantity_reserved or 0) < quantity:
                    raise SalesOrderStateError(
                        "reserved stock is inconsistent; cancellation was not committed"
                    )
                stock.quantity_reserved -= quantity
                stock.updated_at = utcnow()

            order.status = "cancelled"
            order.cancelled_by = actor
            order.cancelled_at = utcnow()
            db.session.commit()
            return order, True
        except Exception:
            db.session.rollback()
            raise


def serialize_order(order: SalesOrder) -> dict:
    return {
        "id": order.id,
        "order_uid": order.order_uid,
        "external_order_id": order.external_id,
        "workspace_id": order.workspace_id,
        "channel": order.channel,
        "status": order.status,
        "customer_reference": order.customer_reference,
        "note": order.note,
        "location": {
            "id": order.location.id,
            "code": order.location.code,
            "name": order.location.name,
        },
        "created_by": _actor_json(order.created_by),
        "picking_started_by": _actor_json(order.picking_started_by),
        "packed_by": _actor_json(order.packed_by),
        "shipped_by": _actor_json(order.shipped_by),
        "cancelled_by": _actor_json(order.cancelled_by),
        "created_at": order.created_at.isoformat(),
        "picking_started_at": (
            order.picking_started_at.isoformat() if order.picking_started_at else None
        ),
        "packed_at": order.packed_at.isoformat() if order.packed_at else None,
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
        "items": [
            {
                "id": item.id,
                "sku": item.product.sku,
                "name": item.product.name,
                "category": item.product.category,
                "unit_of_measure": item.product.unit_of_measure,
                "quantity": number_for_json(item.quantity),
                "picked_quantity": number_for_json(item.picked_quantity),
                "picked_by": _actor_json(item.picked_by),
                "picked_at": item.picked_at.isoformat() if item.picked_at else None,
                "allocations": [
                    {
                        "stock_level_id": allocation.stock_level_id,
                        "location": allocation.stock_level.location.code,
                        "bin": (
                            allocation.stock_level.bin.code
                            if allocation.stock_level.bin
                            else None
                        ),
                        "quantity_reserved": number_for_json(
                            allocation.quantity_reserved
                        ),
                        "picked_quantity": number_for_json(
                            allocation.picked_quantity
                        ),
                    }
                    for allocation in item.allocations
                ],
            }
            for item in order.items
        ],
    }


def serialize_pick_list(order: SalesOrder) -> dict:
    serialized = serialize_order(order)
    return {
        "order_uid": serialized["order_uid"],
        "status": serialized["status"],
        "location": serialized["location"],
        "customer_reference": serialized["customer_reference"],
        "items": serialized["items"],
    }
