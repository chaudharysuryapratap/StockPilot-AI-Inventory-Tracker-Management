from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import re

from sqlalchemy import func, or_
from flask import current_app

from app import db
from app.models import (
    Bin,
    ChatConversation,
    ChatMessage,
    DemandInsight,
    ForecastOutcome,
    InventoryLocation,
    InventoryLot,
    InventoryMovement,
    Product,
    PurchaseOrder,
    ReturnAuthorization,
    Sale,
    SaleItem,
    SalesOrder,
    StockLevel,
    StockTransfer,
    Supplier,
    UnitConversion,
    User,
    Workspace,
    WorkspaceMembership,
    utcnow,
)
from app.services.bedrock import BedrockNarrator
from app.services.products import number_for_json


_ASSISTANT_STOP_WORDS = {
    "about", "all", "and", "are", "can", "could", "current", "does", "for",
    "from", "give", "have", "help", "how", "inventory", "is", "item", "items",
    "latest", "list", "me", "my", "of", "on", "open", "order", "orders", "our",
    "please", "product", "products", "purchase", "sales", "show", "stock", "tell",
    "that", "the", "their", "this", "to", "today", "us", "warehouse", "warehouses",
    "what", "when", "where", "which", "who", "why", "with",
}

_ROLE_CAPABILITIES = {
    "admin": [
        "View every operational and analytical screen in the business.",
        "Create and edit warehouses and bins.",
        "Manage users, invitations, roles, business settings, and SSO.",
        "Manage catalogue, suppliers, stock corrections, purchasing, transfers, sales orders, and returns.",
    ],
    "manager": [
        "Run day-to-day catalogue, supplier, inventory, purchasing, transfer, order, return, and reporting workflows.",
        "Create and approve purchase orders and record receipts.",
        "Cannot create warehouses, manage team access, or change business settings.",
    ],
    "picker": [
        "Work the pick queue, scan products, progress assigned fulfilment steps, receive permitted returns, and view operational records.",
        "Cannot create purchase orders, make stock corrections, manage suppliers, or administer the business.",
    ],
    "viewer": [
        "Read dashboards, catalogue, warehouses, sales orders, transfers, returns, reports, and analytics.",
        "Cannot create, edit, approve, receive, archive, or otherwise mutate business data.",
    ],
}

_PRODUCT_GUIDE = {
    "purpose": (
        "StockPilot is a multi-warehouse inventory operations platform with an auditable "
        "stock ledger, purchasing and partial receiving, fulfilment, returns, expiry-aware "
        "lots, deterministic demand forecasts, and AI-assisted explanations."
    ),
    "navigation": {
        "Overview": "Business-wide or single-warehouse metrics, risks, forecasts, and the assistant.",
        "Stock catalogue": "Search products, inspect stock, edit catalogue records, conversions, and CSV imports.",
        "Warehouses & bins": "View warehouse stock and bins; Admins can create and edit them.",
        "Purchasing & receiving": "Create manual or AI draft purchase orders, approve them, and record partial receipts and lots.",
        "Sales orders": "Create and track outbound orders through pending, picking, packed, shipped, or cancelled states.",
        "Pick queue": "Execute fulfilment against reserved stock positions.",
        "Stock transfers": "Move stock between warehouses and bins with ledger attribution.",
        "Returns / RMA": "Authorize, receive, restock, or mark returned stock damaged.",
        "Reports & alerts": "Review inventory analytics and operational reports.",
    },
    "workflows": {
        "csv_import": (
            "Open Stock catalogue, download the predefined import template, keep its headers, "
            "fill product rows, then import the CSV. Import is atomic: validation errors are "
            "reported and no partial catalogue update is committed."
        ),
        "catalogue_product": (
            "Admins and Managers can create a product from Stock catalogue or Inventory setup. "
            "Enter a workspace-unique SKU, name, category, base unit, prices, reorder settings, "
            "optional barcode and preferred supplier, then save. Viewer and Picker roles are read-only here."
        ),
        "warehouse_setup": (
            "Open Warehouses & bins. Admins can add a warehouse name, unique code and address, "
            "then add capacity-controlled bins inside it. Other roles can view warehouses but cannot create them."
        ),
        "stock_correction": (
            "Admins and Managers use Stock corrections, choose the product, warehouse and "
            "optional bin, enter the signed quantity change and reason, then submit."
        ),
        "purchase_receipt": (
            "Create or open a purchase order, approve it, then receive any outstanding quantity. "
            "Perishable products require a lot number and expiry information. Repeated external "
            "receipt IDs are protected against duplicate stock updates."
        ),
        "ai_purchasing": (
            "Admins and Managers open Purchasing & receiving and generate AI drafts from current "
            "reorder recommendations. Each draft remains reviewable and read-only until an authorized "
            "user approves it; receiving is recorded separately."
        ),
        "sales_fulfilment": (
            "Admins and Managers create a sales order for a warehouse and its product quantities. "
            "StockPilot reserves exact stock positions; the Pick queue then progresses the order through "
            "picking, packed and shipped states. Pickers can execute permitted fulfilment steps."
        ),
        "stock_transfer": (
            "Admins and Managers create transfers between different warehouses or bins. Select the "
            "product, source, destination and quantity; StockPilot validates available stock and writes "
            "the corresponding ledger movements."
        ),
        "returns": (
            "Create an RMA against a shipped sales order, authorize eligible quantities, then record "
            "partial receipts as restock or damaged. Restocked quantities return to the selected stock position."
        ),
        "team_access": (
            "Admins use Users & access to invite staff and assign Admin, Manager, Picker or Viewer roles. "
            "Profile security, password changes, MFA and recovery codes are available from the profile menu."
        ),
        "unit_conversion": (
            "Conversions are product-specific. Add a purchasing unit such as box and its exact "
            "factor in base units; StockPilot converts ordered and received quantities to the base unit."
        ),
        "expiry": (
            "Tracked lots use manufacture and expiry dates. Expired lots are non-saleable; near-expiry "
            "recommendations support FEFO picking, promotion, transfer, supplier return, or disposal."
        ),
        "forecasting": (
            "Demand, safety stock, stockout timing, and reorder quantity are calculated deterministically. "
            "AI explains stored metrics but does not invent or replace forecast numbers."
        ),
        "assistant": (
            "The assistant is read-only. It explains StockPilot and analyzes data from the signed-in "
            "business and selected warehouse; operational changes still require the normal screens and permissions."
        ),
    },
}


def _assistant_terms(question: str) -> list[str]:
    terms: list[str] = []
    for value in re.findall(r"[a-z0-9][a-z0-9._-]+", question.lower()):
        if value in _ASSISTANT_STOP_WORDS or value in terms:
            continue
        terms.append(value)
        if len(terms) == 8:
            break
    return terms


def _matching_filters(columns: tuple, terms: list[str]):
    return [column.ilike(f"%{term}%") for term in terms for column in columns]


def _status_counts(query, status_column) -> dict[str, int]:
    return {
        str(status): int(count)
        for status, count in query.with_entities(
            status_column, func.count(status_column)
        ).group_by(status_column).all()
    }


def _iso(value) -> str | None:
    return value.isoformat() if value else None


class AssistantRateLimitError(ValueError):
    pass


def inventory_recommendations(
    *, workspace_id: int, expiry_days: int | None = None,
    dead_stock_days: int | None = None, location_id: int | None = None,
) -> dict:
    expiry_days = expiry_days or current_app.config["NEAR_EXPIRY_DAYS"]
    dead_stock_days = dead_stock_days or current_app.config["DEAD_STOCK_DAYS"]
    today = date.today()
    near_expiry_query = (
        InventoryLot.query.join(Product, InventoryLot.product_id == Product.id).filter(
            InventoryLot.workspace_id == workspace_id,
            InventoryLot.quantity_on_hand > 0,
            InventoryLot.expiry_date.is_not(None),
            InventoryLot.expiry_date <= today + timedelta(days=expiry_days),
        )
    )
    if location_id is not None:
        near_expiry_query = near_expiry_query.filter(
            InventoryLot.location_id == location_id
        )
    near_expiry = near_expiry_query.order_by(
        InventoryLot.expiry_date, Product.name
    ).all()
    last_sales_query = (
        db.session.query(
            SaleItem.product_id, func.max(Sale.occurred_at).label("last_sale_at")
        )
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(Sale.workspace_id == workspace_id)
    )
    if location_id is not None:
        last_sales_query = last_sales_query.filter(Sale.location_id == location_id)
    last_sales = last_sales_query.group_by(SaleItem.product_id).subquery()
    stock_totals_query = (
        db.session.query(
            StockLevel.product_id,
            func.sum(StockLevel.quantity_on_hand).label("quantity_on_hand"),
        )
        .join(Product, StockLevel.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id, Product.is_active.is_(True))
    )
    if location_id is not None:
        stock_totals_query = stock_totals_query.filter(
            StockLevel.location_id == location_id
        )
    stock_totals = stock_totals_query.group_by(StockLevel.product_id).subquery()
    cutoff = utcnow() - timedelta(days=dead_stock_days)
    dead_rows = (
        db.session.query(Product, stock_totals.c.quantity_on_hand, last_sales.c.last_sale_at)
        .join(stock_totals, stock_totals.c.product_id == Product.id)
        .outerjoin(last_sales, last_sales.c.product_id == Product.id)
        .filter(
            Product.workspace_id == workspace_id,
            stock_totals.c.quantity_on_hand > 0,
            (last_sales.c.last_sale_at.is_(None) | (last_sales.c.last_sale_at < cutoff)),
        )
        .order_by(stock_totals.c.quantity_on_hand.desc())
        .all()
    )
    return {
        "near_expiry": [
            {
                "lot_id": lot.id,
                "sku": lot.product.sku,
                "product": lot.product.name,
                "location": lot.location.code,
                "bin": lot.bin.code if lot.bin else None,
                "lot_number": lot.lot_number,
                "expiry_date": lot.expiry_date.isoformat(),
                "days_remaining": (lot.expiry_date - today).days,
                "quantity_on_hand": number_for_json(lot.quantity_on_hand),
                "recommendation": "Prioritize FEFO picking, promotion, transfer, or supplier return.",
            }
            for lot in near_expiry
        ],
        "dead_stock": [
            {
                "product_id": product.id,
                "sku": product.sku,
                "product": product.name,
                "quantity_on_hand": number_for_json(quantity),
                "last_sale_at": last_sale.isoformat() if last_sale else None,
                "days_without_sale": (
                    (utcnow().date() - last_sale.date()).days if last_sale else None
                ),
                "recommendation": "Review markdown, transfer, bundle, return, or purchasing hold.",
            }
            for product, quantity, last_sale in dead_rows
        ],
        "settings": {
            "near_expiry_days": expiry_days,
            "dead_stock_days": dead_stock_days,
        },
    }


class ForecastAccuracyService:
    @staticmethod
    def evaluate(*, workspace_id: int, now=None) -> list[ForecastOutcome]:
        now = now or utcnow()
        horizon_days = current_app.config["FORECAST_ACCURACY_HORIZON_DAYS"]
        cutoff = now - timedelta(days=horizon_days)
        insights = (
            DemandInsight.query.join(
                InventoryLocation, DemandInsight.location_id == InventoryLocation.id
            )
            .outerjoin(ForecastOutcome, ForecastOutcome.insight_id == DemandInsight.id)
            .filter(
                InventoryLocation.workspace_id == workspace_id,
                DemandInsight.generated_at <= cutoff,
                ForecastOutcome.id.is_(None),
            )
            .order_by(DemandInsight.generated_at, DemandInsight.id)
            .all()
        )
        outcomes: list[ForecastOutcome] = []
        for insight in insights:
            horizon_end = insight.generated_at + timedelta(days=horizon_days)
            actual = db.session.query(func.coalesce(func.sum(SaleItem.quantity), 0)).join(
                Sale, SaleItem.sale_id == Sale.id
            ).filter(
                Sale.workspace_id == workspace_id,
                SaleItem.product_id == insight.product_id,
                Sale.location_id == insight.location_id,
                Sale.occurred_at > insight.generated_at,
                Sale.occurred_at <= horizon_end,
            ).scalar()
            predicted = (Decimal(str(insight.daily_demand)) * horizon_days).quantize(Decimal("0.01"))
            actual_decimal = Decimal(actual or 0).quantize(Decimal("0.01"))
            error = abs(predicted - actual_decimal)
            percentage = float((error / actual_decimal) * 100) if actual_decimal > 0 else None
            outcome = ForecastOutcome(
                workspace_id=workspace_id,
                insight=insight,
                horizon_days=horizon_days,
                predicted_units=predicted,
                actual_units=actual_decimal,
                absolute_error=error,
                absolute_percentage_error=round(percentage, 2) if percentage is not None else None,
                evaluated_at=now,
            )
            db.session.add(outcome)
            outcomes.append(outcome)
        db.session.commit()
        return outcomes

    @staticmethod
    def summary(*, workspace_id: int, location_id: int | None = None) -> dict:
        query = ForecastOutcome.query.filter_by(workspace_id=workspace_id)
        if location_id is not None:
            query = query.join(DemandInsight).filter(
                DemandInsight.location_id == location_id
            )
        rows = query.all()
        if not rows:
            return {"evaluated_forecasts": 0, "mae": None, "mape": None, "bias": None}
        errors = [float(row.absolute_error) for row in rows]
        percentages = [
            row.absolute_percentage_error
            for row in rows
            if row.absolute_percentage_error is not None
        ]
        bias_values = [float(row.predicted_units - row.actual_units) for row in rows]
        return {
            "evaluated_forecasts": len(rows),
            "mae": round(sum(errors) / len(errors), 2),
            "mape": round(sum(percentages) / len(percentages), 2) if percentages else None,
            "bias": round(sum(bias_values) / len(bias_values), 2),
        }


def dashboard_context(*, workspace_id: int, location_id: int | None = None) -> dict:
    recommendations = inventory_recommendations(
        workspace_id=workspace_id, location_id=location_id
    )
    newest_query = (
        DemandInsight.query.join(
            InventoryLocation, DemandInsight.location_id == InventoryLocation.id
        )
        .filter(InventoryLocation.workspace_id == workspace_id)
    )
    if location_id is not None:
        newest_query = newest_query.filter(DemandInsight.location_id == location_id)
    newest = newest_query.order_by(
        DemandInsight.generated_at.desc(), DemandInsight.id.desc()
    ).limit(100).all()
    current: dict[tuple[int, int], DemandInsight] = {}
    previous: dict[tuple[int, int], DemandInsight] = {}
    for insight in newest:
        key = (insight.product_id, insight.location_id)
        if key not in current:
            current[key] = insight
        elif key not in previous:
            previous[key] = insight
    risks = sorted(
        [
            {
                "sku": row.product.sku,
                "location": row.location.code,
                "daily_demand": round(row.daily_demand, 2),
                "reorder_quantity": number_for_json(row.recommended_reorder_quantity),
                "expected_stockout_at": row.expected_stockout_at.isoformat() if row.expected_stockout_at else None,
            }
            for row in current.values()
            if Decimal(row.recommended_reorder_quantity or 0) > 0
        ],
        key=lambda item: item["reorder_quantity"],
        reverse=True,
    )
    demand_changes = []
    for key, row in current.items():
        older = previous.get(key)
        if older:
            change = round(row.daily_demand - older.daily_demand, 2)
            if change:
                demand_changes.append(
                    {
                        "sku": row.product.sku,
                        "location": row.location.code,
                        "previous_daily_demand": round(older.daily_demand, 2),
                        "current_daily_demand": round(row.daily_demand, 2),
                        "change": change,
                    }
                )
    return {
        "stock_risks": risks[:20],
        "demand_changes": sorted(demand_changes, key=lambda row: abs(row["change"]), reverse=True)[:20],
        "near_expiry": recommendations["near_expiry"][:20],
        "dead_stock": recommendations["dead_stock"][:20],
        "forecast_accuracy": ForecastAccuracyService.summary(
            workspace_id=workspace_id, location_id=location_id
        ),
    }


def stockpilot_assistant_context(
    question: str,
    *,
    actor: User,
    location_id: int | None = None,
) -> dict:
    """Build a bounded, tenant-scoped snapshot for operational and product questions."""
    workspace_id = actor.workspace_id
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("workspace was not found")
    supply_access = actor.role in {"admin", "manager"}
    team_access = actor.role == "admin"
    terms = _assistant_terms(question)
    lowered = question.lower()
    dashboard = dashboard_context(workspace_id=workspace_id, location_id=location_id)

    locations = (
        InventoryLocation.query.filter_by(workspace_id=workspace_id, is_active=True)
        .order_by(InventoryLocation.name, InventoryLocation.id)
        .limit(50)
        .all()
    )
    location_ids = [row.id for row in locations]
    stock_by_location = {
        row.location_id: {
            "on_hand": number_for_json(row.on_hand),
            "reserved": number_for_json(row.reserved),
            "available": number_for_json(Decimal(row.on_hand or 0) - Decimal(row.reserved or 0)),
        }
        for row in db.session.query(
            StockLevel.location_id,
            func.coalesce(func.sum(StockLevel.quantity_on_hand), 0).label("on_hand"),
            func.coalesce(func.sum(StockLevel.quantity_reserved), 0).label("reserved"),
        )
        .join(Product, StockLevel.product_id == Product.id)
        .filter(
            Product.workspace_id == workspace_id,
            Product.is_active.is_(True),
            StockLevel.location_id.in_(location_ids or [-1]),
        )
        .group_by(StockLevel.location_id)
        .all()
    }
    bin_counts = {
        location_key: int(count)
        for location_key, count in db.session.query(Bin.location_id, func.count(Bin.id))
        .join(InventoryLocation, Bin.location_id == InventoryLocation.id)
        .filter(
            InventoryLocation.workspace_id == workspace_id,
            Bin.is_active.is_(True),
        )
        .group_by(Bin.location_id)
        .all()
    }
    warehouses = [
        {
            "id": row.id,
            "name": row.name,
            "code": row.code,
            "address": row.address,
            "active_bins": bin_counts.get(row.id, 0),
            "stock": stock_by_location.get(
                row.id, {"on_hand": 0, "reserved": 0, "available": 0}
            ),
            "selected": row.id == location_id,
        }
        for row in locations
    ]

    stock_query = (
        db.session.query(
            func.coalesce(func.sum(StockLevel.quantity_on_hand), 0).label("on_hand"),
            func.coalesce(func.sum(StockLevel.quantity_reserved), 0).label("reserved"),
            func.count(func.distinct(StockLevel.product_id)).label("stocked_products"),
        )
        .join(Product, StockLevel.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id, Product.is_active.is_(True))
    )
    if location_id is not None:
        stock_query = stock_query.filter(StockLevel.location_id == location_id)
    stock_summary = stock_query.one()

    on_hand_sum = func.coalesce(func.sum(StockLevel.quantity_on_hand), 0)
    reserved_sum = func.coalesce(func.sum(StockLevel.quantity_reserved), 0)
    available_sum = on_hand_sum - reserved_sum
    low_stock_query = (
        db.session.query(
            Product.sku,
            Product.name,
            Product.unit_of_measure,
            Product.reorder_point,
            on_hand_sum.label("on_hand"),
            reserved_sum.label("reserved"),
            available_sum.label("available"),
        )
        .join(StockLevel, StockLevel.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id, Product.is_active.is_(True))
    )
    if location_id is not None:
        low_stock_query = low_stock_query.filter(StockLevel.location_id == location_id)
    low_stock_rows = (
        low_stock_query.group_by(
            Product.id,
            Product.sku,
            Product.name,
            Product.unit_of_measure,
            Product.reorder_point,
        )
        .having(available_sum <= Product.reorder_point)
        .order_by(available_sum, Product.sku)
        .limit(20)
        .all()
    )

    product_query = Product.query.filter(Product.workspace_id == workspace_id)
    if "archiv" not in lowered:
        product_query = product_query.filter(Product.is_active.is_(True))
    product_filters = _matching_filters(
        (Product.name, Product.sku, Product.barcode, Product.category), terms
    )
    if product_filters:
        product_query = product_query.filter(or_(*product_filters))
    else:
        product_query = product_query.order_by(Product.updated_at.desc(), Product.id.desc())
    products = product_query.limit(12).all()
    product_ids = [row.id for row in products]
    position_query = StockLevel.query.filter(
        StockLevel.product_id.in_(product_ids or [-1])
    )
    if location_id is not None:
        position_query = position_query.filter(StockLevel.location_id == location_id)
    positions_by_product: dict[int, list[StockLevel]] = {}
    for position in position_query.order_by(StockLevel.product_id, StockLevel.location_id).limit(120).all():
        positions_by_product.setdefault(position.product_id, []).append(position)
    conversions_by_product: dict[int, list[UnitConversion]] = {}
    for conversion in UnitConversion.query.filter(
        UnitConversion.workspace_id == workspace_id,
        UnitConversion.product_id.in_(product_ids or [-1]),
    ).order_by(UnitConversion.product_id, UnitConversion.unit_code).limit(80).all():
        conversions_by_product.setdefault(conversion.product_id, []).append(conversion)
    matched_products = []
    for product in products:
        positions = positions_by_product.get(product.id, [])
        on_hand = sum((Decimal(row.quantity_on_hand or 0) for row in positions), Decimal("0"))
        reserved = sum((Decimal(row.quantity_reserved or 0) for row in positions), Decimal("0"))
        item = {
            "id": product.id,
            "sku": product.sku,
            "barcode": product.barcode,
            "name": product.name,
            "category": product.category,
            "base_unit": product.unit_of_measure,
            "active": product.is_active,
            "perishable": product.is_perishable,
            "reorder_point": product.reorder_point,
            "safety_stock": product.safety_stock,
            "preferred_supplier": product.preferred_supplier.name if product.preferred_supplier else None,
            "stock": {
                "on_hand": number_for_json(on_hand),
                "reserved": number_for_json(reserved),
                "available": number_for_json(on_hand - reserved),
            },
            "positions": [
                {
                    "warehouse": row.location.code,
                    "bin": row.bin.code if row.bin else None,
                    "on_hand": number_for_json(row.quantity_on_hand),
                    "reserved": number_for_json(row.quantity_reserved),
                    "available": number_for_json(row.quantity_available),
                }
                for row in positions[:12]
            ],
            "unit_conversions": [
                {
                    "unit": row.unit_code,
                    "to_base_factor": number_for_json(row.to_base_factor),
                }
                for row in conversions_by_product.get(product.id, [])[:8]
            ],
        }
        if actor.role != "picker":
            item["cost_price"] = number_for_json(product.cost_price)
            item["sell_price"] = number_for_json(product.sell_price)
        matched_products.append(item)

    recent_since = utcnow() - timedelta(days=30)
    sales_scope = Sale.query.filter(
        Sale.workspace_id == workspace_id, Sale.occurred_at >= recent_since
    )
    if location_id is not None:
        sales_scope = sales_scope.filter(Sale.location_id == location_id)
    sale_ids = sales_scope.with_entities(Sale.id)
    units_sold = db.session.query(func.coalesce(func.sum(SaleItem.quantity), 0)).filter(
        SaleItem.sale_id.in_(sale_ids)
    ).scalar()
    top_sellers_query = (
        db.session.query(
            Product.sku,
            Product.name,
            func.coalesce(func.sum(SaleItem.quantity), 0).label("units"),
        )
        .join(SaleItem, SaleItem.product_id == Product.id)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .filter(
            Product.workspace_id == workspace_id,
            Sale.workspace_id == workspace_id,
            Sale.occurred_at >= recent_since,
        )
    )
    if location_id is not None:
        top_sellers_query = top_sellers_query.filter(Sale.location_id == location_id)
    top_sellers = [
        {"sku": sku, "product": name, "units": number_for_json(units)}
        for sku, name, units in top_sellers_query.group_by(Product.id, Product.sku, Product.name)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(10)
        .all()
    ]

    suppliers: list[Supplier] = []
    supplier_product_counts: dict[int, int] = {}
    purchase_scope = PurchaseOrder.query.filter(PurchaseOrder.id == -1)
    purchase_orders: list[PurchaseOrder] = []
    if supply_access:
        supplier_query = Supplier.query.filter(Supplier.workspace_id == workspace_id)
        supplier_filters = _matching_filters((Supplier.name,), terms)
        if supplier_filters:
            supplier_query = supplier_query.filter(or_(*supplier_filters))
        suppliers = supplier_query.order_by(
            Supplier.is_active.desc(), Supplier.name
        ).limit(20).all()
        supplier_ids = [row.id for row in suppliers]
        supplier_product_counts = {
            supplier_id: int(count)
            for supplier_id, count in db.session.query(
                Product.preferred_supplier_id, func.count(Product.id)
            )
            .filter(
                Product.workspace_id == workspace_id,
                Product.preferred_supplier_id.in_(supplier_ids or [-1]),
            )
            .group_by(Product.preferred_supplier_id)
            .all()
        }

        purchase_scope = PurchaseOrder.query.filter(
            PurchaseOrder.workspace_id == workspace_id
        )
        if location_id is not None:
            purchase_scope = purchase_scope.filter(
                PurchaseOrder.location_id == location_id
            )
        purchase_query = purchase_scope
        purchase_filters = _matching_filters(
            (
                PurchaseOrder.external_id,
                PurchaseOrder.status,
                PurchaseOrder.source,
                Supplier.name,
            ),
            terms,
        )
        if purchase_filters:
            purchase_query = purchase_query.join(
                Supplier, PurchaseOrder.supplier_id == Supplier.id
            ).filter(or_(*purchase_filters))
        purchase_orders = purchase_query.order_by(
            PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc()
        ).limit(12).all()

    sales_order_scope = SalesOrder.query.filter(SalesOrder.workspace_id == workspace_id)
    if location_id is not None:
        sales_order_scope = sales_order_scope.filter(SalesOrder.location_id == location_id)
    sales_order_query = sales_order_scope
    sales_order_filters = _matching_filters(
        (SalesOrder.external_id, SalesOrder.status, SalesOrder.channel), terms
    )
    if sales_order_filters:
        sales_order_query = sales_order_query.filter(or_(*sales_order_filters))
    sales_orders = sales_order_query.order_by(
        SalesOrder.created_at.desc(), SalesOrder.id.desc()
    ).limit(12).all()

    transfer_scope = StockTransfer.query.filter(StockTransfer.workspace_id == workspace_id)
    if location_id is not None:
        transfer_scope = transfer_scope.filter(
            or_(
                StockTransfer.source_location_id == location_id,
                StockTransfer.destination_location_id == location_id,
            )
        )
    transfer_query = transfer_scope
    transfer_filters = _matching_filters(
        (StockTransfer.external_id, StockTransfer.status, Product.sku, Product.name),
        terms,
    )
    if transfer_filters:
        transfer_query = transfer_query.join(
            Product, StockTransfer.product_id == Product.id
        ).filter(or_(*transfer_filters))
    transfers = transfer_query.order_by(
        StockTransfer.created_at.desc(), StockTransfer.id.desc()
    ).limit(12).all()

    return_scope = ReturnAuthorization.query.filter(
        ReturnAuthorization.workspace_id == workspace_id
    )
    if location_id is not None:
        return_scope = return_scope.join(SalesOrder).filter(
            SalesOrder.location_id == location_id
        )
    return_query = return_scope
    return_filters = _matching_filters(
        (
            ReturnAuthorization.external_id,
            ReturnAuthorization.status,
            ReturnAuthorization.reason_code,
        ),
        terms,
    )
    if return_filters:
        return_query = return_query.filter(or_(*return_filters))
    returns = return_query.order_by(
        ReturnAuthorization.created_at.desc(), ReturnAuthorization.id.desc()
    ).limit(12).all()

    movement_scope = (
        InventoryMovement.query.join(Product, InventoryMovement.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id)
    )
    if location_id is not None:
        movement_scope = movement_scope.filter(InventoryMovement.location_id == location_id)
    movements = movement_scope.order_by(
        InventoryMovement.created_at.desc(), InventoryMovement.id.desc()
    ).limit(12).all()

    selected_location = next((row for row in locations if row.id == location_id), None)
    role_counts = {}
    if team_access:
        role_counts = {
            str(role): int(count)
            for role, count in db.session.query(
                WorkspaceMembership.role, func.count(WorkspaceMembership.id)
            )
            .filter(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.is_active.is_(True),
            )
            .group_by(WorkspaceMembership.role)
            .all()
        }
    product_count = Product.query.filter_by(
        workspace_id=workspace_id, is_active=True
    ).count()
    return {
        "context_version": 2,
        "generated_at": utcnow().isoformat(),
        "scope": {
            "business": workspace.name,
            "business_username": workspace.business_username,
            "warehouse": (
                {"id": selected_location.id, "name": selected_location.name, "code": selected_location.code}
                if selected_location
                else None
            ),
            "description": (
                f"Selected warehouse {selected_location.code}"
                if selected_location
                else "All warehouses in the signed-in business"
            ),
        },
        "requesting_user": {
            "role": actor.role,
            "capabilities": _ROLE_CAPABILITIES.get(actor.role, []),
        },
        "workspace_settings": {
            "timezone": workspace.settings.timezone if workspace.settings else "Asia/Kolkata",
            "currency": workspace.settings.currency if workspace.settings else current_app.config["REPORT_CURRENCY"],
        },
        "product_guide": _PRODUCT_GUIDE,
        "dashboard": dashboard,
        "inventory": {
            "active_products": product_count,
            "stocked_products": int(stock_summary.stocked_products or 0),
            "on_hand": number_for_json(stock_summary.on_hand),
            "reserved": number_for_json(stock_summary.reserved),
            "available": number_for_json(
                Decimal(stock_summary.on_hand or 0) - Decimal(stock_summary.reserved or 0)
            ),
            "low_stock": [
                {
                    "sku": row.sku,
                    "product": row.name,
                    "unit": row.unit_of_measure,
                    "reorder_point": row.reorder_point,
                    "on_hand": number_for_json(row.on_hand),
                    "reserved": number_for_json(row.reserved),
                    "available": number_for_json(row.available),
                }
                for row in low_stock_rows
            ],
            "matched_products": matched_products,
            "recent_movements": [
                {
                    "sku": row.product.sku,
                    "warehouse": row.location.code,
                    "bin": row.bin.code if row.bin else None,
                    "type": row.movement_type,
                    "quantity_delta": number_for_json(row.quantity_delta),
                    "reason": row.reason,
                    "reference": row.reference_id,
                    "created_at": _iso(row.created_at),
                }
                for row in movements
            ],
        },
        "warehouses": warehouses,
        "sales_activity_30d": {
            "transactions": sales_scope.count(),
            "units_sold": number_for_json(units_sold),
            "top_sellers": top_sellers,
        },
        "suppliers": {
            "available_to_role": supply_access,
            "active": (
                Supplier.query.filter_by(workspace_id=workspace_id, is_active=True).count()
                if supply_access
                else None
            ),
            "matched_or_recent": [
                {
                    "id": row.id,
                    "name": row.name,
                    "lead_time_days": row.lead_time_days,
                    "payment_terms": row.payment_terms,
                    "active": row.is_active,
                    "preferred_product_count": supplier_product_counts.get(row.id, 0),
                }
                for row in suppliers
            ] if supply_access else [],
        },
        "purchase_orders": {
            "available_to_role": supply_access,
            "status_counts": (
                _status_counts(purchase_scope, PurchaseOrder.status)
                if supply_access
                else {}
            ),
            "matched_or_recent": [
                {
                    "id": row.id,
                    "reference": row.external_id or row.po_uid,
                    "status": row.status,
                    "source": row.source,
                    "supplier": row.supplier.name,
                    "warehouse": row.location.code,
                    "expected_at": _iso(row.expected_at),
                    "created_at": _iso(row.created_at),
                    "items": [
                        {
                            "sku": item.product.sku,
                            "ordered": number_for_json(item.ordered_quantity),
                            "received": number_for_json(item.received_quantity),
                            "remaining": number_for_json(
                                Decimal(item.ordered_quantity or 0) - Decimal(item.received_quantity or 0)
                            ),
                            "unit": item.order_unit,
                        }
                        for item in row.items[:8]
                    ],
                }
                for row in purchase_orders
            ] if supply_access else [],
        },
        "sales_orders": {
            "status_counts": _status_counts(sales_order_scope, SalesOrder.status),
            "matched_or_recent": [
                {
                    "id": row.id,
                    "reference": row.external_id or row.order_uid,
                    "status": row.status,
                    "channel": row.channel,
                    "warehouse": row.location.code,
                    "created_at": _iso(row.created_at),
                    "items": [
                        {
                            "sku": item.product.sku,
                            "quantity": number_for_json(item.quantity),
                            "picked": number_for_json(item.picked_quantity),
                        }
                        for item in row.items[:8]
                    ],
                }
                for row in sales_orders
            ],
        },
        "transfers": {
            "status_counts": _status_counts(transfer_scope, StockTransfer.status),
            "matched_or_recent": [
                {
                    "id": row.id,
                    "reference": row.external_id or row.transfer_uid,
                    "status": row.status,
                    "sku": row.product.sku,
                    "quantity": number_for_json(row.quantity),
                    "unit": row.product.unit_of_measure,
                    "source": row.source_location.code,
                    "destination": row.destination_location.code,
                    "created_at": _iso(row.created_at),
                }
                for row in transfers
            ],
        },
        "returns": {
            "status_counts": _status_counts(return_scope, ReturnAuthorization.status),
            "matched_or_recent": [
                {
                    "id": row.id,
                    "reference": row.external_id or row.rma_uid,
                    "status": row.status,
                    "reason": row.reason_code,
                    "sales_order": row.sales_order.external_id or row.sales_order.order_uid,
                    "created_at": _iso(row.created_at),
                    "items": [
                        {
                            "sku": item.sales_order_item.product.sku,
                            "requested": number_for_json(item.quantity_requested),
                            "authorized": number_for_json(item.quantity_authorized),
                            "received": number_for_json(item.quantity_received),
                            "restocked": number_for_json(item.quantity_restocked),
                        }
                        for item in row.items[:8]
                    ],
                }
                for row in returns
            ],
        },
        "team": {
            "available_to_role": team_access,
            "active_memberships_by_role": role_counts if team_access else {},
        },
        "snapshot_limits": {
            "matched_entities_per_section": 12,
            "low_stock": 20,
            "forecast_and_recommendation_rows": 20,
            "recent_movements": 12,
        },
    }


class DashboardChatService:
    @staticmethod
    def ask(
        question: object,
        *,
        actor: User,
        conversation_id: int | None = None,
        location_id: int | None = None,
    ) -> tuple[ChatConversation, ChatMessage]:
        normalized = str(question or "").strip()
        if not normalized:
            raise ValueError("question is required")
        if len(normalized) > 1000:
            raise ValueError("question must be 1000 characters or fewer")
        recent_questions = (
            db.session.query(func.count(ChatMessage.id))
            .join(ChatConversation, ChatMessage.conversation_id == ChatConversation.id)
            .filter(
                ChatConversation.workspace_id == actor.workspace_id,
                ChatConversation.user_id == actor.id,
                ChatMessage.role == "user",
                ChatMessage.created_at >= utcnow() - timedelta(minutes=1),
            )
            .scalar()
        )
        if int(recent_questions or 0) >= current_app.config[
            "ASSISTANT_MAX_REQUESTS_PER_MINUTE"
        ]:
            raise AssistantRateLimitError(
                "Assistant question limit reached. Wait a minute and try again."
            )
        conversation = db.session.get(ChatConversation, conversation_id) if conversation_id else None
        if conversation is not None and (
            conversation.workspace_id != actor.workspace_id or conversation.user_id != actor.id
        ):
            raise ValueError("conversation was not found")
        if conversation is None:
            conversation = ChatConversation(workspace_id=actor.workspace_id, user=actor)
            db.session.add(conversation)
            db.session.flush()
        if location_id is not None:
            location = db.session.get(InventoryLocation, location_id)
            if location is None or location.workspace_id != actor.workspace_id:
                raise ValueError("warehouse was not found")
        history = [
            {"role": row.role, "content": row.content}
            for row in conversation.messages[-10:]
        ]
        context = stockpilot_assistant_context(
            normalized, actor=actor, location_id=location_id
        )
        db.session.add(ChatMessage(conversation=conversation, role="user", content=normalized))
        answer = BedrockNarrator.answer(
            normalized, context, history=history
        ) or DashboardChatService._fallback(normalized, context, history=history)
        response = ChatMessage(
            conversation=conversation,
            role="assistant",
            content=answer,
            context_snapshot=context,
        )
        conversation.updated_at = utcnow()
        db.session.add(response)
        db.session.commit()
        return conversation, response

    @staticmethod
    def _fallback(
        question: str, context: dict, *, history: list[dict] | None = None
    ) -> str:
        lowered = question.lower()
        dashboard = context["dashboard"]
        inventory = context["inventory"]
        role = context["requesting_user"]["role"]
        if any(
            value in lowered
            for value in (
                "needs my attention",
                "need my attention",
                "action summary",
                "priorities today",
                "what should i do",
            )
        ):
            risks = dashboard["stock_risks"]
            expiring = dashboard["near_expiry"]
            dead_stock = dashboard["dead_stock"]
            parts = [
                f"{len(risks)} replenishment risk(s)",
                f"{len(expiring)} near-expiry lot(s)",
                f"{len(dead_stock)} dead-stock product(s)",
            ]
            if context["purchase_orders"]["available_to_role"]:
                counts = context["purchase_orders"]["status_counts"]
                open_orders = sum(
                    count
                    for status, count in counts.items()
                    if status not in {"received", "cancelled"}
                )
                parts.append(f"{open_orders} open purchase order(s)")
            detail = ""
            if risks:
                top = risks[0]
                detail = (
                    f" Start with {top['sku']} at {top['location']}: the current "
                    f"recommendation is {top['reorder_quantity']} units."
                )
            elif expiring:
                top = expiring[0]
                detail = (
                    f" Start with {top['sku']} lot {top['lot_number']}, which expires "
                    f"on {top['expiry_date']}."
                )
            return f"Current action summary: {', '.join(parts)}.{detail}"
        if any(value in lowered for value in ("csv", "spreadsheet", "import template", "excel")):
            return context["product_guide"]["workflows"]["csv_import"]
        if any(value in lowered for value in ("add product", "create product", "new product")):
            return context["product_guide"]["workflows"]["catalogue_product"]
        if "unit conversion" in lowered or any(
            value in lowered for value in ("base unit", "box to", "pack to")
        ):
            return context["product_guide"]["workflows"]["unit_conversion"]
        if any(value in lowered for value in ("invite user", "invite staff", "add user", "team access", "mfa", "recovery code")):
            return context["product_guide"]["workflows"]["team_access"]
        if any(value in lowered for value in ("my role", "permission", "allowed to", "can i", "viewer", "picker")):
            capabilities = " ".join(context["requesting_user"]["capabilities"])
            return f"Your current role is {role.title()}. {capabilities}"
        if any(value in lowered for value in ("warehouse", "warehouses", "bin", "bins")):
            if "how" in lowered or any(value in lowered for value in ("add", "create", "new")):
                return context["product_guide"]["workflows"]["warehouse_setup"]
            warehouses = context["warehouses"]
            if not warehouses:
                return "This business has no active warehouse yet. An Admin can add one from Warehouses & bins."
            names = ", ".join(
                f"{row['name']} ({row['code']}, {row['active_bins']} active bin(s), {row['stock']['available']} available units)"
                for row in warehouses[:6]
            )
            permission = (
                "You can create or edit warehouses and bins from Warehouses & bins."
                if role == "admin"
                else "Warehouse creation and editing require the Admin role."
            )
            return f"There are {len(warehouses)} active warehouse(s): {names}. {permission}"
        if any(value in lowered for value in ("supplier", "vendor", "lead time")):
            suppliers = context["suppliers"]
            if not suppliers["available_to_role"]:
                return (
                    f"Supplier records are not available to the {role.title()} role. "
                    "An Admin or Manager can use the Suppliers screen."
                )
            rows = suppliers["matched_or_recent"]
            if not rows:
                return "No supplier matched that question in the current business."
            preview = ", ".join(
                f"{row['name']} ({row['lead_time_days']} day lead time)" for row in rows[:6]
            )
            return f"{suppliers['active']} supplier(s) are active. Relevant suppliers: {preview}."
        if "purchase" in lowered or "receiv" in lowered or re.search(r"\bpo(?:s)?\b", lowered):
            if not context["purchase_orders"]["available_to_role"]:
                return (
                    f"Purchasing data is not available to the {role.title()} role. "
                    "An Admin or Manager can use Purchasing & receiving."
                )
            if "ai" in lowered or "draft" in lowered:
                return context["product_guide"]["workflows"]["ai_purchasing"]
            if "how" in lowered and "receiv" in lowered:
                return context["product_guide"]["workflows"]["purchase_receipt"]
            section = context["purchase_orders"]
            rows = section["matched_or_recent"]
            counts = section["status_counts"]
            open_count = sum(
                count
                for status, count in counts.items()
                if status not in {"received", "cancelled"}
            )
            if not rows:
                return f"There are {open_count} open purchase order(s), but none matched the identifiers in your question."
            first = rows[0]
            remaining = sum(Decimal(str(item["remaining"])) for item in first["items"])
            return (
                f"There are {open_count} open purchase order(s). The most relevant is "
                f"{first['reference']} for {first['supplier']} at {first['warehouse']}; "
                f"its status is {first['status']} and the listed lines have {number_for_json(remaining)} units remaining."
            )
        if any(value in lowered for value in ("sales order", "customer order", "pick", "pack", "ship")):
            if "how" in lowered or any(value in lowered for value in ("create", "new")):
                return context["product_guide"]["workflows"]["sales_fulfilment"]
            section = context["sales_orders"]
            rows = section["matched_or_recent"]
            counts = section["status_counts"]
            active = sum(
                count for status, count in counts.items() if status in {"pending", "picking", "packed"}
            )
            if not rows:
                return f"There are {active} active sales order(s), but none matched that question."
            first = rows[0]
            return (
                f"There are {active} active sales order(s). The most relevant is "
                f"{first['reference']} at {first['warehouse']} with status {first['status']} "
                f"and {len(first['items'])} listed line(s)."
            )
        if "transfer" in lowered or "move stock" in lowered:
            if "how" in lowered or any(value in lowered for value in ("create", "new")):
                return context["product_guide"]["workflows"]["stock_transfer"]
            rows = context["transfers"]["matched_or_recent"]
            if not rows:
                return "No stock transfer matched that question in the current scope."
            first = rows[0]
            return (
                f"The most relevant transfer is {first['reference']}: {first['quantity']} "
                f"{first['unit']} of {first['sku']} from {first['source']} to "
                f"{first['destination']} ({first['status']})."
            )
        if any(value in lowered for value in ("return", "rma", "restock", "damaged")):
            if "how" in lowered or any(value in lowered for value in ("create", "new")):
                return context["product_guide"]["workflows"]["returns"]
            rows = context["returns"]["matched_or_recent"]
            if not rows:
                return "No return authorization matched that question in the current scope."
            first = rows[0]
            return (
                f"The most relevant return is {first['reference']} for sales order "
                f"{first['sales_order']}; its status is {first['status']} and reason is {first['reason']}."
            )
        matched_products = inventory["matched_products"]
        if matched_products and any(
            value in lowered for value in ("sku", "barcode", "product", "available", "on hand", "price", "cost")
        ):
            first = matched_products[0]
            answer = (
                f"{first['name']} ({first['sku']}) has {first['stock']['on_hand']} "
                f"{first['base_unit']} on hand, {first['stock']['reserved']} reserved, and "
                f"{first['stock']['available']} available in the current scope."
            )
            if "price" in lowered and "sell_price" in first:
                answer += f" Sell price is {first['sell_price']}."
            if "cost" in lowered and "cost_price" in first:
                answer += f" Cost price is {first['cost_price']}."
            elif "cost" in lowered and role == "picker":
                answer += " Cost price is not included for the Picker role."
            if first["positions"]:
                positions = ", ".join(
                    f"{row['warehouse']}/{row['bin'] or 'unassigned'}: {row['available']} available"
                    for row in first["positions"][:6]
                )
                answer += f" Positions: {positions}."
            return answer
        if "expir" in lowered:
            rows = dashboard["near_expiry"]
            if not rows:
                return "No tracked lots are near expiry in the current 30-day window."
            top = rows[0]
            return f"{len(rows)} lot(s) are near expiry. The earliest is {top['sku']} lot {top['lot_number']} at {top['location']}, expiring {top['expiry_date']} with {top['quantity_on_hand']} units remaining."
        if "dead" in lowered or "slow" in lowered:
            rows = dashboard["dead_stock"]
            if not rows:
                return "No stocked products currently meet the 90-day dead-stock rule."
            return f"{len(rows)} product(s) meet the dead-stock rule; {rows[0]['sku']} has the largest remaining balance at {rows[0]['quantity_on_hand']} units."
        if "accuracy" in lowered or "forecast" in lowered:
            summary = dashboard["forecast_accuracy"]
            if not summary["evaluated_forecasts"]:
                return "No forecast has completed its seven-day evaluation horizon yet."
            return f"{summary['evaluated_forecasts']} forecasts have been evaluated. MAE is {summary['mae']} units, MAPE is {summary['mape']}%, and average prediction bias is {summary['bias']} units."
        if "demand" in lowered or "change" in lowered:
            rows = dashboard["demand_changes"]
            if not rows:
                return "The latest persisted forecasts do not show a measurable demand change from their previous runs."
            top = rows[0]
            direction = "increased" if top["change"] > 0 else "decreased"
            return f"The largest demand change is {top['sku']} at {top['location']}: daily demand {direction} from {top['previous_daily_demand']} to {top['current_daily_demand']}."
        if any(value in lowered for value in ("risk", "low stock", "stockout", "attention", "reorder")):
            risks = dashboard["stock_risks"]
            if not risks:
                return "The latest forecast has no products requiring replenishment."
            top = risks[0]
            return f"{len(risks)} stock position(s) currently need replenishment. The largest recommendation is {top['sku']} at {top['location']}: order {top['reorder_quantity']} units."
        if "what is stockpilot" in lowered or "what can you do" in lowered or "help" in lowered:
            return (
                f"{context['product_guide']['purpose']} I can explain every StockPilot "
                "workflow and answer read-only questions about this business's products, stock, "
                "warehouses, suppliers, purchasing, orders, transfers, returns, expiry, demand, "
                "forecast accuracy, and role permissions."
            )
        counts = context["inventory"]
        return (
            f"This {context['scope']['description'].lower()} currently contains "
            f"{counts['active_products']} active products and {counts['available']} available "
            f"units across {len(context['warehouses'])} active warehouse(s). I could not tie "
            "that question to a specific recorded entity, so include a SKU, order reference, "
            "warehouse code, supplier, workflow, or date range for a more exact answer."
        )
