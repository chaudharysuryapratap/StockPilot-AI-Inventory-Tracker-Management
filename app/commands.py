from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from random import Random

import click
from flask import Flask
from flask.cli import with_appcontext

from app import db
from app.models import (
    Bin,
    InventoryLocation,
    Product,
    Sale,
    SaleItem,
    StockLevel,
    Supplier,
    utcnow,
)
from app.services.identity import ensure_default_identity
from app.services.auth import AuthenticationError, UserService, UserValidationError
from app.services.emailer import ReportMailer
from app.services.forecast import ForecastService
from app.schema import current_schema_versions, migrate_schema


@click.command("seed-demo")
@click.option("--reset", is_flag=True, help="Delete local data before adding the demo data.")
@with_appcontext
def seed_demo(reset: bool) -> None:
    """Create a realistic 35-day sample inventory and sales history."""
    if reset:
        db.drop_all()
        db.create_all()
    elif Product.query.first():
        raise click.ClickException("Database already has products. Use --reset to rebuild demo data.")

    # db.create_all() builds the current clean schema, while migrate_schema()
    # also records every revision so later deployments can prove their state.
    migrate_schema()

    actor = ensure_default_identity()
    fresh_supplier = Supplier(
        workspace_id=actor.workspace_id,
        name="FreshCart Wholesale",
        contact_email="orders@freshcart.example",
        lead_time_days=2,
        payment_terms="Net 15",
    )
    dry_supplier = Supplier(
        workspace_id=actor.workspace_id,
        name="Metro Staples",
        contact_email="supply@metrostaples.example",
        lead_time_days=4,
        payment_terms="Net 30",
    )
    main_store = InventoryLocation(
        workspace_id=actor.workspace_id,
        name="Main Store", code="MAIN", address="Sector 18, Noida"
    )
    kitchen = InventoryLocation(
        workspace_id=actor.workspace_id,
        name="Kitchen Store", code="KITCHEN", address="Sector 18, Noida"
    )
    main_bin = Bin(location=main_store, code="A-01", capacity=500)
    kitchen_bin = Bin(location=kitchen, code="K-01", capacity=200)
    products = [
        Product(
            sku="COFFEE-250", name="Cold Coffee 250 ml", category="Beverages", unit="bottles",
            cost_price=Decimal("55.00"), sell_price=Decimal("85.00"),
            reorder_point=25, safety_stock=18, preferred_supplier=fresh_supplier,
        ),
        Product(
            sku="RICE-5KG", name="Basmati Rice 5 kg", category="Grocery", unit="bags",
            cost_price=Decimal("430.00"), sell_price=Decimal("520.00"),
            reorder_point=18, safety_stock=12, preferred_supplier=dry_supplier,
        ),
        Product(
            sku="OIL-1L", name="Sunflower Oil 1 L", category="Grocery", unit="bottles",
            cost_price=Decimal("118.00"), sell_price=Decimal("145.00"),
            reorder_point=24, safety_stock=15, preferred_supplier=dry_supplier,
        ),
        Product(
            sku="WRAP-BOX", name="Food Wrap Box", category="Packaging", unit="boxes",
            cost_price=Decimal("38.00"), sell_price=Decimal("65.00"),
            reorder_point=10, safety_stock=8, preferred_supplier=dry_supplier,
        ),
    ]
    db.session.add_all(
        [fresh_supplier, dry_supplier, main_store, kitchen, main_bin, kitchen_bin, *products]
    )
    db.session.flush()

    stock_by_sku = {"COFFEE-250": 22, "RICE-5KG": 46, "OIL-1L": 19, "WRAP-BOX": 38}
    for product in products:
        db.session.add(
            StockLevel(
                product=product,
                location=main_store,
                bin=main_bin,
                quantity=stock_by_sku[product.sku],
            )
        )
    db.session.add(
        StockLevel(product=products[0], location=kitchen, bin=kitchen_bin, quantity=14)
    )
    db.session.add(
        StockLevel(product=products[3], location=kitchen, bin=kitchen_bin, quantity=27)
    )

    rng = Random(42)
    start = utcnow() - timedelta(days=35)
    for day_offset in range(35):
        occurred_at = start + timedelta(days=day_offset, hours=12)
        weekend_multiplier = 1.45 if occurred_at.weekday() in {4, 5, 6} else 1.0
        sale = Sale(
            external_id=f"demo-main-{day_offset}",
            source="demo_pos",
            location=main_store,
            occurred_at=occurred_at,
        )
        db.session.add(sale)
        for product, base_quantity, price in [
            (products[0], 8, "85.00"),
            (products[1], 3, "520.00"),
            (products[2], 5, "145.00"),
            (products[3], 2, "65.00"),
        ]:
            quantity = max(1, round((base_quantity + rng.randint(-2, 3)) * weekend_multiplier))
            db.session.add(
                SaleItem(
                    sale=sale,
                    product=product,
                    quantity=quantity,
                    unit_price=Decimal(price),
                )
            )

    db.session.commit()
    click.echo("Demo inventory and 35 days of POS history created.")


@click.command("migrate-schema")
@with_appcontext
def migrate_schema_command() -> None:
    """Upgrade an existing StockPilot database to the latest application schema."""
    result = migrate_schema()
    if result.applied:
        click.echo("Applied schema migrations: " + ", ".join(result.applied_versions))
    else:
        click.echo(f"Schema is already current: {result.version}")


@click.command("schema-version")
@with_appcontext
def schema_version_command() -> None:
    """Show applied StockPilot schema revisions."""
    versions = current_schema_versions()
    click.echo("\n".join(versions) if versions else "No schema migrations recorded.")


@click.command("analyze-inventory")
@click.option("--send-email", is_flag=True, help="Send the report through SES after analysis.")
@click.option(
    "--critical-only",
    is_flag=True,
    help="When emailing, send only critical stockout alerts instead of all reorder actions.",
)
@with_appcontext
def analyze_inventory(send_email: bool, critical_only: bool) -> None:
    """Calculate demand forecasts and optionally send the daily action report."""
    results = ForecastService.run()
    at_risk = sum(item.recommended_reorder_quantity > 0 for item in results)
    click.echo(f"Analyzed {len(results)} stock positions; {at_risk} need replenishment.")
    if send_email:
        workspace_id = ensure_default_identity().workspace_id
        report = (
            ReportMailer.send_critical_alerts(results, workspace_id=workspace_id)
            if critical_only
            else ReportMailer.send_daily_report(results, workspace_id=workspace_id)
        )
        click.echo(report.reason)


@click.command("create-admin")
@click.option("--workspace-name", prompt="Workspace name", default="StockPilot Workspace")
@click.option("--name", prompt="Administrator name")
@click.option("--email", prompt="Administrator email")
@click.password_option(confirmation_prompt=True)
@with_appcontext
def create_admin(workspace_name: str, name: str, email: str, password: str) -> None:
    """Securely create the first named administrator from the server console."""

    try:
        user = UserService.bootstrap_admin(
            {
                "workspace_name": workspace_name,
                "name": name,
                "email": email,
                "password": password,
                "password_confirm": password,
            }
        )
    except (AuthenticationError, UserValidationError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Administrator account created for {user.email}.")


def register_commands(app: Flask) -> None:
    app.cli.add_command(migrate_schema_command)
    app.cli.add_command(schema_version_command)
    app.cli.add_command(seed_demo)
    app.cli.add_command(analyze_inventory)
    app.cli.add_command(create_admin)
