from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import ceil

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app import db
from app.models import DemandInsight, Product, Sale, SaleItem, StockLevel, utcnow
from app.services.bedrock import BedrockNarrator
from app.services.products import number_for_json


@dataclass
class ForecastResult:
    product_id: int
    product_sku: str
    product_name: str
    location_id: int
    location_code: str
    current_stock: float
    daily_demand: float
    expected_stockout_at: datetime | None
    recommended_reorder_quantity: float
    confidence: int
    narrative: str
    factors: dict = field(default_factory=dict)

    @property
    def risk(self) -> str:
        from flask import current_app

        critical_days = current_app.config.get("CRITICAL_STOCKOUT_DAYS", 3)
        return (
            "critical"
            if self.current_stock <= 0
            or (
                self.expected_stockout_at
                and self.expected_stockout_at
                <= utcnow() + timedelta(days=critical_days)
            )
            else "watch"
            if self.recommended_reorder_quantity > 0
            else "healthy"
        )

    def as_dict(self) -> dict:
        data = asdict(self)
        data["expected_stockout_at"] = (
            self.expected_stockout_at.isoformat() if self.expected_stockout_at else None
        )
        data["risk"] = self.risk
        return data


class ForecastService:
    @staticmethod
    def run(now: datetime | None = None) -> list[ForecastResult]:
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        from flask import current_app

        lookback_days = current_app.config["FORECAST_LOOKBACK_DAYS"]
        cutoff = now - timedelta(days=lookback_days)
        raw_sales = (
            db.session.query(
                SaleItem.product_id,
                Sale.location_id,
                Sale.occurred_at,
                SaleItem.quantity,
            )
            .join(Sale, SaleItem.sale_id == Sale.id)
            .filter(Sale.occurred_at >= cutoff)
            .all()
        )
        sales_by_stock: dict[tuple[int, int], dict] = defaultdict(lambda: defaultdict(int))
        for product_id, location_id, occurred_at, quantity in raw_sales:
            sale_date = occurred_at.date()
            sales_by_stock[(product_id, location_id)][sale_date] += float(quantity)

        stock_levels = (
            StockLevel.query.options(
                joinedload(StockLevel.product).joinedload(Product.preferred_supplier),
                joinedload(StockLevel.location),
            )
            .join(Product, StockLevel.product_id == Product.id)
            .filter(Product.active.is_(True))
            .all()
        )

        stock_by_location: dict[tuple[int, int], list[StockLevel]] = defaultdict(list)
        for stock in stock_levels:
            stock_by_location[(stock.product_id, stock.location_id)].append(stock)

        results: list[ForecastResult] = []
        for (product_id, location_id), position_rows in stock_by_location.items():
            product = position_rows[0].product
            location = position_rows[0].location
            daily_sales = sales_by_stock[(product.id, location.id)]
            total_sales = sum(daily_sales.values())
            recent_start = now.date() - timedelta(days=6)
            recent_sales = sum(
                quantity for day, quantity in daily_sales.items() if day >= recent_start
            )
            baseline = total_sales / lookback_days
            recent_average = recent_sales / 7
            # Recent demand gets more weight but cannot erase the longer baseline.
            blended_daily_demand = (0.65 * recent_average) + (0.35 * baseline)

            weekday_values = [
                quantity
                for day, quantity in daily_sales.items()
                if day.weekday() == now.date().weekday()
            ]
            weekday_average = (
                sum(weekday_values) / len(weekday_values) if weekday_values else baseline
            )
            weekday_factor = 1.0
            if baseline > 0:
                weekday_factor = max(0.75, min(1.35, weekday_average / baseline))
            daily_demand = round(blended_daily_demand * weekday_factor, 2)

            lead_time = (
                product.preferred_supplier.lead_time_days
                if product.preferred_supplier
                else current_app.config["DEFAULT_SUPPLIER_LEAD_TIME_DAYS"]
            )
            target_stock = ceil(daily_demand * lead_time) + product.safety_stock
            if daily_demand == 0:
                target_stock = max(target_stock, product.reorder_point + product.safety_stock)
            available_stock = float(
                sum(
                    (row.quantity_available for row in position_rows),
                    start=Decimal("0.00"),
                )
            )
            reorder_quantity = round(max(0, target_stock - available_stock), 2)
            expected_stockout_at = (
                now + timedelta(days=(available_stock / daily_demand))
                if daily_demand > 0
                else None
            )
            active_sales_days = sum(1 for quantity in daily_sales.values() if quantity > 0)
            confidence = min(
                95,
                max(15, round(25 + (active_sales_days / lookback_days) * 70)),
            )
            factors = {
                "model_version": "demand_blend_v1",
                "lookback_days": lookback_days,
                "total_units_sold": round(total_sales, 2),
                "baseline_daily_demand": round(baseline, 2),
                "recent_7_day_units": round(recent_sales, 2),
                "recent_daily_average": round(recent_average, 2),
                "recent_weight_percent": 65,
                "baseline_weight_percent": 35,
                "weekday_factor": round(weekday_factor, 3),
                "weekday_sample_days": len(weekday_values),
                "active_sales_days": active_sales_days,
                "supplier_lead_time_days": lead_time,
                "safety_stock": product.safety_stock,
                "reorder_point": product.reorder_point,
                "available_stock": round(available_stock, 2),
                "target_stock": round(target_stock, 2),
            }

            fallback_narrative = ForecastService._fallback_narrative(
                product.name,
                product.unit,
                available_stock,
                daily_demand,
                reorder_quantity,
                lead_time,
                expected_stockout_at,
            )
            result = ForecastResult(
                product_id=product.id,
                product_sku=product.sku,
                product_name=product.name,
                location_id=location.id,
                location_code=location.code,
                current_stock=available_stock,
                daily_demand=daily_demand,
                expected_stockout_at=expected_stockout_at,
                recommended_reorder_quantity=reorder_quantity,
                confidence=confidence,
                narrative=fallback_narrative,
                factors=factors,
            )
            narrative = BedrockNarrator.explain(result.as_dict()) or fallback_narrative
            result.narrative = narrative
            db.session.add(
                DemandInsight(
                    product_id=result.product_id,
                    location_id=result.location_id,
                    daily_demand=result.daily_demand,
                    expected_stockout_at=result.expected_stockout_at,
                    recommended_reorder_quantity=result.recommended_reorder_quantity,
                    confidence=result.confidence,
                    narrative=result.narrative,
                    factors=result.factors,
                )
            )
            results.append(result)

        db.session.commit()
        return results

    @staticmethod
    def _fallback_narrative(
        product_name: str,
        unit: str,
        stock: float,
        daily_demand: float,
        reorder_quantity: float,
        lead_time: int,
        stockout_at: datetime | None,
    ) -> str:
        if reorder_quantity <= 0:
            return f"{product_name} is adequately covered at {stock} {unit}."
        coverage = (
            f"stock may run out by {stockout_at.strftime('%d %b')}"
            if stockout_at
            else "current coverage should be reviewed"
        )
        return (
            f"Order {reorder_quantity} {unit} of {product_name}; projected demand is "
            f"{daily_demand:.2f} per day and {coverage} "
            f"with a {lead_time}-day lead time."
        )


def latest_insights(limit: int = 50) -> list[DemandInsight]:
    """Return only the newest analysis per product/location pair."""
    newest_ids = (
        db.session.query(func.max(DemandInsight.id).label("id"))
        .group_by(DemandInsight.product_id, DemandInsight.location_id)
        .subquery()
    )
    return (
        DemandInsight.query.options(
            joinedload(DemandInsight.product), joinedload(DemandInsight.location)
        )
        .join(newest_ids, DemandInsight.id == newest_ids.c.id)
        .join(Product, DemandInsight.product_id == Product.id)
        .filter(Product.is_active.is_(True))
        .order_by(DemandInsight.generated_at.desc(), DemandInsight.id.desc())
        .limit(limit)
        .all()
    )


def serialize_insight(insight: DemandInsight) -> dict:
    available_stock = sum(
        (
            row.quantity_available
            for row in StockLevel.query.filter_by(
                product_id=insight.product_id, location_id=insight.location_id
            ).all()
        ),
        start=Decimal("0.00"),
    )
    return {
        "id": insight.id,
        "generated_at": insight.generated_at.isoformat(),
        "sku": insight.product.sku,
        "product": insight.product.name,
        "location": insight.location.code,
        "current_stock": number_for_json(available_stock),
        "unit": insight.product.unit,
        "daily_demand": round(insight.daily_demand, 2),
        "expected_stockout_at": (
            insight.expected_stockout_at.isoformat() if insight.expected_stockout_at else None
        ),
        "recommended_reorder_quantity": number_for_json(
            insight.recommended_reorder_quantity
        ),
        "confidence": insight.confidence,
        "narrative": insight.narrative,
        "factors": insight.factors or {},
        "explainability": readable_factors(insight.factors or {}, insight.product.unit),
    }


def readable_factors(factors: dict, unit: str = "units") -> list[dict]:
    """Turn stored model inputs into stable, user-facing labels without hiding values."""

    specifications = (
        ("lookback_days", "Sales history window", "days"),
        ("total_units_sold", "Units sold in window", unit),
        ("baseline_daily_demand", "Long-term daily average", f"{unit}/day"),
        ("recent_7_day_units", "Units sold in last 7 days", unit),
        ("recent_daily_average", "Recent daily average", f"{unit}/day"),
        ("weekday_factor", "Day-of-week multiplier", "×"),
        ("active_sales_days", "Days with recorded sales", "days"),
        ("supplier_lead_time_days", "Supplier lead time", "days"),
        ("safety_stock", "Safety stock", unit),
        ("reorder_point", "Configured reorder point", unit),
        ("available_stock", "Available stock used", unit),
        ("target_stock", "Calculated target stock", unit),
    )
    rows: list[dict] = []
    for key, label, suffix in specifications:
        if key not in factors:
            continue
        value = factors[key]
        display_value = f"{value}×" if suffix == "×" else f"{value} {suffix}"
        rows.append({"key": key, "label": label, "value": value, "display": display_value})
    return rows
