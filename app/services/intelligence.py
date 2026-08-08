from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from flask import current_app

from app import db
from app.models import (
    ChatConversation,
    ChatMessage,
    DemandInsight,
    ForecastOutcome,
    InventoryLocation,
    InventoryLot,
    Product,
    Sale,
    SaleItem,
    StockLevel,
    User,
    utcnow,
)
from app.services.bedrock import BedrockNarrator
from app.services.products import number_for_json


def inventory_recommendations(
    *, workspace_id: int, expiry_days: int | None = None,
    dead_stock_days: int | None = None,
) -> dict:
    expiry_days = expiry_days or current_app.config["NEAR_EXPIRY_DAYS"]
    dead_stock_days = dead_stock_days or current_app.config["DEAD_STOCK_DAYS"]
    today = date.today()
    near_expiry = (
        InventoryLot.query.join(Product, InventoryLot.product_id == Product.id)
        .filter(
            InventoryLot.workspace_id == workspace_id,
            InventoryLot.quantity_on_hand > 0,
            InventoryLot.expiry_date.is_not(None),
            InventoryLot.expiry_date <= today + timedelta(days=expiry_days),
        )
        .order_by(InventoryLot.expiry_date, Product.name)
        .all()
    )
    last_sales = (
        db.session.query(
            SaleItem.product_id, func.max(Sale.occurred_at).label("last_sale_at")
        )
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(Sale.workspace_id == workspace_id)
        .group_by(SaleItem.product_id)
        .subquery()
    )
    stock_totals = (
        db.session.query(
            StockLevel.product_id,
            func.sum(StockLevel.quantity_on_hand).label("quantity_on_hand"),
        )
        .join(Product, StockLevel.product_id == Product.id)
        .filter(Product.workspace_id == workspace_id, Product.is_active.is_(True))
        .group_by(StockLevel.product_id)
        .subquery()
    )
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
    def summary(*, workspace_id: int) -> dict:
        rows = ForecastOutcome.query.filter_by(workspace_id=workspace_id).all()
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


def dashboard_context(*, workspace_id: int) -> dict:
    recommendations = inventory_recommendations(workspace_id=workspace_id)
    newest = (
        DemandInsight.query.join(
            InventoryLocation, DemandInsight.location_id == InventoryLocation.id
        )
        .filter(InventoryLocation.workspace_id == workspace_id)
        .order_by(DemandInsight.generated_at.desc(), DemandInsight.id.desc())
        .limit(100)
        .all()
    )
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
        "forecast_accuracy": ForecastAccuracyService.summary(workspace_id=workspace_id),
    }


class DashboardChatService:
    @staticmethod
    def ask(question: object, *, actor: User, conversation_id: int | None = None) -> tuple[ChatConversation, ChatMessage]:
        normalized = str(question or "").strip()
        if not normalized:
            raise ValueError("question is required")
        if len(normalized) > 1000:
            raise ValueError("question must be 1000 characters or fewer")
        conversation = db.session.get(ChatConversation, conversation_id) if conversation_id else None
        if conversation is not None and (
            conversation.workspace_id != actor.workspace_id or conversation.user_id != actor.id
        ):
            raise ValueError("conversation was not found")
        if conversation is None:
            conversation = ChatConversation(workspace_id=actor.workspace_id, user=actor)
            db.session.add(conversation)
            db.session.flush()
        context = dashboard_context(workspace_id=actor.workspace_id)
        db.session.add(ChatMessage(conversation=conversation, role="user", content=normalized))
        answer = BedrockNarrator.answer(normalized, context) or DashboardChatService._fallback(normalized, context)
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
    def _fallback(question: str, context: dict) -> str:
        lowered = question.lower()
        if "expir" in lowered:
            rows = context["near_expiry"]
            if not rows:
                return "No tracked lots are near expiry in the current 30-day window."
            top = rows[0]
            return f"{len(rows)} lot(s) are near expiry. The earliest is {top['sku']} lot {top['lot_number']} at {top['location']}, expiring {top['expiry_date']} with {top['quantity_on_hand']} units remaining."
        if "dead" in lowered or "slow" in lowered:
            rows = context["dead_stock"]
            if not rows:
                return "No stocked products currently meet the 90-day dead-stock rule."
            return f"{len(rows)} product(s) meet the dead-stock rule; {rows[0]['sku']} has the largest remaining balance at {rows[0]['quantity_on_hand']} units."
        if "accuracy" in lowered or "forecast" in lowered:
            summary = context["forecast_accuracy"]
            if not summary["evaluated_forecasts"]:
                return "No forecast has completed its seven-day evaluation horizon yet."
            return f"{summary['evaluated_forecasts']} forecasts have been evaluated. MAE is {summary['mae']} units, MAPE is {summary['mape']}%, and average prediction bias is {summary['bias']} units."
        if "demand" in lowered or "change" in lowered:
            rows = context["demand_changes"]
            if not rows:
                return "The latest persisted forecasts do not show a measurable demand change from their previous runs."
            top = rows[0]
            direction = "increased" if top["change"] > 0 else "decreased"
            return f"The largest demand change is {top['sku']} at {top['location']}: daily demand {direction} from {top['previous_daily_demand']} to {top['current_daily_demand']}."
        risks = context["stock_risks"]
        if not risks:
            return "The latest forecast has no products requiring replenishment."
        top = risks[0]
        return f"{len(risks)} stock position(s) currently need replenishment. The largest recommendation is {top['sku']} at {top['location']}: order {top['reorder_quantity']} units."
