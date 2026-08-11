from __future__ import annotations

from dataclasses import asdict, dataclass
from html import escape

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import current_app

from app import db
from app.models import AlertDelivery, utcnow
from app.services.forecast import ForecastResult
from app.services.identity import ensure_default_identity


@dataclass
class EmailResult:
    sent: bool
    reason: str
    delivery_id: int | None = None
    provider_message_id: str | None = None
    item_count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class ReportMailer:
    @staticmethod
    def send_daily_report(
        results: list[ForecastResult], *, workspace_id: int | None = None
    ) -> EmailResult:
        at_risk = [item for item in results if item.recommended_reorder_quantity > 0]
        return ReportMailer._send(
            at_risk,
            workspace_id=workspace_id,
            report_type="daily_inventory",
            severity="action",
            empty_reason="No replenishment actions require an email today.",
        )

    @staticmethod
    def send_critical_alerts(
        results: list[ForecastResult], *, workspace_id: int | None = None
    ) -> EmailResult:
        critical = [item for item in results if item.risk == "critical"]
        return ReportMailer._send(
            critical,
            workspace_id=workspace_id,
            report_type="critical_inventory",
            severity="critical",
            empty_reason="No critical inventory risks require an email.",
        )

    @staticmethod
    def _send(
        items: list[ForecastResult],
        *,
        workspace_id: int | None,
        report_type: str,
        severity: str,
        empty_reason: str,
    ) -> EmailResult:
        workspace_id = workspace_id or ensure_default_identity().workspace_id
        if not items:
            return ReportMailer._finish(
                workspace_id=workspace_id,
                report_type=report_type,
                severity=severity,
                status="skipped",
                recipient_count=0,
                item_count=0,
                detail=empty_reason,
            )
        if not current_app.config["SES_ENABLED"]:
            return ReportMailer._finish(
                workspace_id=workspace_id,
                report_type=report_type,
                severity=severity,
                status="skipped",
                recipient_count=0,
                item_count=len(items),
                detail="SES is disabled",
            )

        sender = current_app.config["SES_FROM_EMAIL"]
        recipients = list(dict.fromkeys(current_app.config["ALERT_RECIPIENTS"]))
        if not sender or not recipients:
            return ReportMailer._finish(
                workspace_id=workspace_id,
                report_type=report_type,
                severity=severity,
                status="skipped",
                recipient_count=len(recipients),
                item_count=len(items),
                detail="SES sender or recipients are not configured",
            )

        label = "Critical inventory alert" if severity == "critical" else "Inventory action report"
        subject = f"{label}: {len(items)} item(s) need attention"
        try:
            client = boto3.client("sesv2", region_name=current_app.config["AWS_REGION"])
            response = client.send_email(
                FromEmailAddress=sender,
                Destination={"ToAddresses": recipients},
                Content={
                    "Simple": {
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {
                            "Text": {
                                "Data": ReportMailer._text_body(items, severity=severity),
                                "Charset": "UTF-8",
                            },
                            "Html": {
                                "Data": ReportMailer._html_body(items, severity=severity),
                                "Charset": "UTF-8",
                            },
                        },
                    }
                },
            )
            message_id = response.get("MessageId")
            return ReportMailer._finish(
                workspace_id=workspace_id,
                report_type=report_type,
                severity=severity,
                status="sent",
                recipient_count=len(recipients),
                item_count=len(items),
                detail=f"{label.lower()} sent",
                provider_message_id=message_id,
            )
        except (ClientError, BotoCoreError) as error:
            current_app.logger.warning("SES report failed: %s", error)
            return ReportMailer._finish(
                workspace_id=workspace_id,
                report_type=report_type,
                severity=severity,
                status="failed",
                recipient_count=len(recipients),
                item_count=len(items),
                detail=f"SES failed: {str(error)[:450]}",
            )

    @staticmethod
    def _finish(
        *,
        workspace_id: int,
        report_type: str,
        severity: str,
        status: str,
        recipient_count: int,
        item_count: int,
        detail: str,
        provider_message_id: str | None = None,
    ) -> EmailResult:
        delivery = AlertDelivery(
            workspace_id=workspace_id,
            report_type=report_type,
            severity=severity,
            status=status,
            recipient_count=recipient_count,
            item_count=item_count,
            provider_message_id=provider_message_id,
            detail=detail[:500],
            sent_at=utcnow() if status == "sent" else None,
        )
        db.session.add(delivery)
        db.session.commit()
        return EmailResult(
            sent=status == "sent",
            reason=detail,
            delivery_id=delivery.id,
            provider_message_id=provider_message_id,
            item_count=item_count,
        )

    @staticmethod
    def _text_body(items: list[ForecastResult], *, severity: str = "action") -> str:
        heading = (
            "Critical inventory risks:"
            if severity == "critical"
            else "Inventory actions for today:"
        )
        lines = [heading]
        for item in items:
            stockout = (
                item.expected_stockout_at.strftime("%d %b")
                if item.expected_stockout_at
                else "n/a"
            )
            lines.append(
                f"- {item.product_name} ({item.product_sku}, {item.location_code}): "
                f"available {item.current_stock}; order {item.recommended_reorder_quantity}; "
                f"projected stockout {stockout}."
            )
        return "\n".join(lines)

    @staticmethod
    def _html_body(items: list[ForecastResult], *, severity: str = "action") -> str:
        heading = (
            "Critical inventory risks"
            if severity == "critical"
            else "Inventory actions for today"
        )
        rows = "".join(
            "<tr>"
            f"<td>{escape(item.product_name)}</td>"
            f"<td>{escape(item.product_sku)}</td>"
            f"<td>{escape(item.location_code)}</td>"
            f"<td>{item.current_stock}</td>"
            f"<td>{item.recommended_reorder_quantity}</td>"
            f"<td>{escape(item.narrative)}</td>"
            "</tr>"
            for item in items
        )
        return (
            f"<h2>{heading}</h2>"
            "<table border='1' cellpadding='8' cellspacing='0'>"
            "<thead><tr><th>Product</th><th>SKU</th><th>Location</th>"
            "<th>Available</th><th>Order</th><th>Action</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
