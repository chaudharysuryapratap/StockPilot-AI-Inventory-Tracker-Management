import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import create_app, db
from app.models import Product, User
from app.services.products import ProductCSVImporter


csv_path = Path(__file__).with_name("StockPilot-product-import-test-120.csv")
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
    result = ProductCSVImporter.import_bytes(
        csv_path.read_bytes(),
        workspace_id=actor.workspace_id,
    )
    product_count = Product.query.filter_by(workspace_id=actor.workspace_id).count()
    print(
        {
            "committed": result.committed,
            "created": result.created,
            "updated": result.updated,
            "rows_read": result.rows_read,
            "errors": result.errors,
            "workspace_product_count": product_count,
        }
    )
    if not result.committed or result.created != 120 or result.errors:
        raise SystemExit(1)
