import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import create_app, db
from app.models import Bin, InventoryLocation, InventoryMovement, Product, StockLevel, User
from app.services.products import ProductCSVImporter


csv_path = Path(__file__).with_name("StockPilot-hardware-Indiranagar-A-840-LKO-120.csv")
application = create_app(
    {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "POS_WEBHOOK_TOKEN": "test-pos-token",
        "INTERNAL_API_TOKEN": "test-internal-token",
        "BEDROCK_ENABLED": False,
        "SES_ENABLED": False,
        "STAFF_AUTH_ENABLED": False,
        "ALLOW_ACTOR_HEADER": True,
    }
)

with application.app_context():
    actor = User.query.order_by(User.id).first()
    if actor is None:
        raise RuntimeError("Application test setup did not create a workspace user")
    location = InventoryLocation(
        workspace_id=actor.workspace_id,
        name="Indiranagar",
        code="INDIRANAGAR",
        address="Indiranagar, Lucknow",
    )
    db.session.add(
        Bin(
            location=location,
            code="A-840-LKO",
            capacity=4522,
        )
    )
    db.session.commit()
    result = ProductCSVImporter.import_bytes(
        csv_path.read_bytes(),
        workspace_id=actor.workspace_id,
        actor=actor,
    )
    product_count = Product.query.filter_by(workspace_id=actor.workspace_id).count()
    stock_rows = StockLevel.query.join(Product).filter(Product.workspace_id == actor.workspace_id).all()
    movement_count = (
        InventoryMovement.query.join(Product)
        .filter(Product.workspace_id == actor.workspace_id)
        .count()
    )
    total_stock = sum((Decimal(row.quantity_on_hand) for row in stock_rows), start=Decimal("0"))
    outcome = {
        **result.as_dict(),
        "workspace_product_count": product_count,
        "stock_row_count": len(stock_rows),
        "movement_count": movement_count,
        "total_opening_stock": str(total_stock),
    }
    print(outcome)
    if (
        not result.committed
        or result.created != 120
        or result.inventory_updated != 115
        or len(stock_rows) != 120
        or movement_count != 115
        or total_stock > Decimal("4522")
        or result.errors
    ):
        raise SystemExit(1)
