from __future__ import annotations

import secrets
import time
from datetime import timedelta
from functools import wraps
from io import BytesIO
from urllib.parse import urlsplit
from uuid import uuid4

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from sqlalchemy import func, text
from sqlalchemy.orm import selectinload

from app import db
from app.models import (
    AlertDelivery,
    Bin,
    InventoryLocation,
    InventoryMovement,
    Product,
    ReturnAuthorization,
    SalesOrder,
    StockLevel,
    StockTransfer,
    Supplier,
    User,
    utcnow,
)
from app.services.forecast import ForecastService, latest_insights, serialize_insight
from app.services.emailer import ReportMailer
from app.services.auth import (
    ROLE_LABELS,
    ROLES,
    AuthenticationError,
    UserService,
    UserValidationError,
    activate_legacy_credentials,
    authenticate,
    authentication_setup_required,
)
from app.services.inventory import (
    CapacityExceededError,
    InsufficientStockError,
    InventoryService,
    InventoryConflictError,
    UnknownInventoryReferenceError,
    serialize_sale,
)
from app.services.identity import ensure_default_identity, resolve_actor
from app.services.locations import (
    BinService,
    LocationService,
    LocationValidationError,
    serialize_bin,
    serialize_location,
)
from app.services.orders import (
    SalesOrderConflictError,
    SalesOrderPermissionError,
    SalesOrderService,
    SalesOrderStateError,
    serialize_order,
    serialize_pick_list,
)
from app.services.products import (
    ProductCSVImporter,
    ProductService,
    ProductValidationError,
    number_for_json,
    serialize_product,
)
from app.services.reports import ReportExporter, ReportService
from app.services.returns import (
    RETURN_DISPOSITION_LABELS,
    RETURN_REASON_LABELS,
    ReturnConflictError,
    ReturnPermissionError,
    ReturnService,
    ReturnStateError,
    returnable_quantity,
    serialize_return,
    serialize_return_receipt,
)
from app.services.suppliers import (
    SupplierService,
    SupplierValidationError,
    serialize_supplier,
)
from app.services.transfers import (
    TransferConflictError,
    TransferService,
    serialize_movement,
    serialize_transfer,
)


web_bp = Blueprint("web", __name__)
api_bp = Blueprint("api", __name__)


def _require_token(header_name: str, config_name: str) -> None:
    provided = request.headers.get(header_name, "")
    expected = current_app.config[config_name]
    if not provided or not expected or not secrets.compare_digest(provided, expected):
        abort(401, description="valid API token required")


def _session_user() -> User | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    actor = db.session.get(User, user_id)
    if actor is None or not actor.is_active:
        session.clear()
        return None
    return actor


def _current_actor() -> User:
    actor = getattr(g, "current_user", None) or _session_user()
    if actor:
        return actor
    try:
        return resolve_actor(request.headers.get("X-Actor-Email"))
    except ValueError as error:
        abort(403, description=str(error))
    except RuntimeError as error:
        abort(503, description=str(error))


def _validate_csrf() -> None:
    supplied = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        abort(400, description="invalid form token")


def _safe_next_target(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return None
    if parsed.path.startswith("//"):
        return None
    return value


def _login_attempt_key(identifier: object) -> str:
    return f"{request.remote_addr or 'unknown'}:{str(identifier or '').strip().lower()}"


def _login_is_rate_limited(identifier: object) -> bool:
    attempts = current_app.extensions.setdefault("stockpilot_login_attempts", {})
    key = _login_attempt_key(identifier)
    cutoff = time.monotonic() - current_app.config["LOGIN_WINDOW_SECONDS"]
    recent = [timestamp for timestamp in attempts.get(key, []) if timestamp >= cutoff]
    attempts[key] = recent
    return len(recent) >= current_app.config["LOGIN_MAX_ATTEMPTS"]


def _record_login_result(identifier: object, *, succeeded: bool) -> None:
    attempts = current_app.extensions.setdefault("stockpilot_login_attempts", {})
    key = _login_attempt_key(identifier)
    if succeeded:
        attempts.pop(key, None)
    else:
        attempts.setdefault(key, []).append(time.monotonic())


def roles_required(*allowed_roles: str):
    if not allowed_roles or any(role not in ROLES for role in allowed_roles):
        raise RuntimeError("roles_required received an unsupported role")

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            actor = _current_actor()
            if not actor.has_role(*allowed_roles):
                abort(403, description="your role cannot perform this action")
            return view(*args, **kwargs)

        return wrapped

    return decorator


def api_read_access(view):
    """Allow session users or trusted internal clients to read private data."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_app.config["STAFF_AUTH_ENABLED"]:
            return view(*args, **kwargs)
        if _session_user() is not None:
            return view(*args, **kwargs)
        provided = request.headers.get("X-Internal-Token", "")
        expected = current_app.config["INTERNAL_API_TOKEN"]
        if provided and expected and secrets.compare_digest(provided, expected):
            return view(*args, **kwargs)
        return jsonify({"error": "authentication required"}), 401

    return wrapped


def session_api_access(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        actor = _session_user()
        if current_app.config["STAFF_AUTH_ENABLED"] and actor is None:
            return jsonify({"error": "authentication required"}), 401
        g.current_user = actor or ensure_default_identity()
        return view(*args, **kwargs)

    return wrapped


def _location_error_status(error: LocationValidationError) -> int:
    return 409 if any("already exists" in value for value in error.errors.values()) else 400


@web_bp.before_request
def require_staff_login():
    g.current_user = _session_user()
    if not current_app.config["STAFF_AUTH_ENABLED"]:
        g.current_user = g.current_user or ensure_default_identity()
        return None

    if authentication_setup_required():
        activate_legacy_credentials()

    if request.endpoint in {"web.login", "web.signup"}:
        return None
    if g.current_user is None:
        if authentication_setup_required() and not current_app.config["ALLOW_WEB_SIGNUP"]:
            abort(
                503,
                description=(
                    "administrator setup is required; run "
                    "'flask --app run create-admin' on the server"
                ),
            )
        destination = "web.signup" if authentication_setup_required() else "web.login"
        return redirect(url_for(destination, next=request.path))
    if request.method == "POST":
        _validate_csrf()
    return None


@web_bp.app_context_processor
def inject_csrf_token():
    def csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    return {
        "csrf_token": csrf_token,
        "current_user": getattr(g, "current_user", None),
        "auth_enabled": current_app.config["STAFF_AUTH_ENABLED"],
        "role_labels": ROLE_LABELS,
        "report_currency": current_app.config["REPORT_CURRENCY"],
    }


@web_bp.errorhandler(403)
def forbidden_page(error):
    return render_template("forbidden.html", message=error.description), 403


def _stockout_is_soon(serialized: dict) -> bool:
    value = serialized.get("expected_stockout_at")
    if not value:
        return False
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= utcnow() + timedelta(
        days=current_app.config["CRITICAL_STOCKOUT_DAYS"]
    )


def _dashboard_data() -> dict:
    insights = [serialize_insight(row) for row in latest_insights()]
    total_units = (
        db.session.query(func.coalesce(func.sum(StockLevel.quantity_on_hand), 0))
        .join(Product, StockLevel.product_id == Product.id)
        .filter(Product.is_active.is_(True))
        .scalar()
    )
    at_risk = [
        insight
        for insight in insights
        if insight["recommended_reorder_quantity"] > 0 or _stockout_is_soon(insight)
    ]
    return {
        "metrics": {
            "active_products": Product.query.filter_by(is_active=True).count(),
            "locations": InventoryLocation.query.count(),
            "total_units": number_for_json(total_units),
            "items_at_risk": len(at_risk),
        },
        "insights": insights,
    }


def _product_error_status(error: ProductValidationError) -> int:
    return 409 if any(message == "already exists" for message in error.errors.values()) else 400


def _supplier_error_status(error: SupplierValidationError) -> int:
    return (
        409
        if any("already exists" in message for message in error.errors.values())
        else 400
    )


def _supplier_for_actor(supplier_id: int) -> Supplier:
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None or supplier.workspace_id != _current_actor().workspace_id:
        abort(404)
    return supplier


def _workspace_forecasts(results: list, workspace_id: int) -> list:
    location_ids = {
        row.id
        for row in InventoryLocation.query.filter_by(workspace_id=workspace_id).all()
    }
    return [item for item in results if item.location_id in location_ids]


def _uploaded_csv_bytes() -> bytes:
    upload = request.files.get("file")
    if not upload or not upload.filename:
        raise ValueError("Choose a CSV file to import.")
    if not upload.filename.lower().endswith(".csv"):
        raise ValueError("The import file must use the .csv extension.")
    content = upload.read(current_app.config["MAX_PRODUCT_CSV_BYTES"] + 1)
    if not content:
        raise ValueError("The CSV file is empty.")
    if len(content) > current_app.config["MAX_PRODUCT_CSV_BYTES"]:
        raise ValueError(
            f"The CSV file must be smaller than "
            f"{current_app.config['MAX_PRODUCT_CSV_BYTES'] // (1024 * 1024)} MB."
        )
    return content


def _sales_order_for_actor(order_id: int) -> SalesOrder:
    order = db.session.get(SalesOrder, order_id)
    if order is None or order.workspace_id != _current_actor().workspace_id:
        abort(404)
    return order


def _return_for_actor(return_id: int) -> ReturnAuthorization:
    rma = db.session.get(ReturnAuthorization, return_id)
    if rma is None or rma.workspace_id != _current_actor().workspace_id:
        abort(404)
    return rma


def _order_form_payload() -> dict:
    skus = request.form.getlist("sku")
    quantities = request.form.getlist("quantity")
    items = [
        {"sku": sku, "quantity": quantity}
        for sku, quantity in zip(skus, quantities, strict=False)
        if str(sku).strip() or str(quantity).strip()
    ]
    return {
        "external_order_id": request.form.get("external_order_id"),
        "location_code": request.form.get("location_code"),
        "channel": request.form.get("channel", "manual"),
        "customer_reference": request.form.get("customer_reference"),
        "note": request.form.get("note"),
        "items": items,
    }


def _return_form_payload() -> dict:
    skus = request.form.getlist("sku")
    quantities = request.form.getlist("quantity")
    return {
        "external_return_id": request.form.get("external_return_id"),
        "reason_code": request.form.get("reason_code"),
        "customer_note": request.form.get("customer_note"),
        "items": [
            {"sku": sku, "quantity": quantity}
            for sku, quantity in zip(skus, quantities, strict=False)
            if str(quantity).strip()
        ],
    }


def _order_action_redirect(order_id: int):
    if request.form.get("return_to") == "picker":
        return redirect(url_for("web.picker_page"))
    return redirect(url_for("web.order_detail_page", order_id=order_id))


@web_bp.get("/")
def dashboard():
    data = _dashboard_data()
    return render_template("dashboard.html", **data)


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    if not current_app.config["STAFF_AUTH_ENABLED"]:
        return redirect(url_for("web.dashboard"))
    if getattr(g, "current_user", None):
        return redirect(url_for("web.dashboard"))
    if authentication_setup_required():
        return redirect(url_for("web.signup"))
    if request.method == "POST":
        _validate_csrf()
        identifier = request.form.get(
            "identifier", request.form.get("username", "")
        )
        if _login_is_rate_limited(identifier):
            abort(429, description="too many sign-in attempts; try again later")
        actor = authenticate(
            identifier,
            request.form.get("password", ""),
        )
        if actor:
            _record_login_result(identifier, succeeded=True)
            session.clear()
            session["user_id"] = actor.id
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            target = request.args.get("next", "")
            return redirect(_safe_next_target(target) or url_for("web.dashboard"))
        _record_login_result(identifier, succeeded=False)
        flash("Incorrect email or password.", "error")
    return render_template("login.html")


@web_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if not current_app.config["STAFF_AUTH_ENABLED"]:
        return redirect(url_for("web.dashboard"))
    if not current_app.config["ALLOW_WEB_SIGNUP"]:
        abort(404)
    if not authentication_setup_required():
        return redirect(url_for("web.login"))
    if request.method == "POST":
        _validate_csrf()
        try:
            actor = UserService.bootstrap_admin(request.form.to_dict())
            session.clear()
            session["user_id"] = actor.id
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            flash("Your administrator account is ready.", "success")
            return redirect(url_for("web.dashboard"))
        except (AuthenticationError, UserValidationError) as error:
            flash(str(error), "error")
    return render_template("signup.html")


@web_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@web_bp.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users_page():
    actor = _current_actor()
    if request.method == "POST":
        try:
            user = UserService.create(request.form.to_dict(), workspace=actor.workspace)
            flash(f"{user.name} was added as {ROLE_LABELS[user.role]}.", "success")
            return redirect(url_for("web.users_page"))
        except UserValidationError as error:
            flash(str(error), "error")
    return render_template(
        "users.html",
        users=User.query.filter_by(workspace_id=actor.workspace_id)
        .order_by(User.name)
        .all(),
        roles=ROLE_LABELS,
    )


@web_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_user_form(user_id: int):
    actor = _current_actor()
    user = db.get_or_404(User, user_id)
    if user.workspace_id != actor.workspace_id:
        abort(404)
    if request.method == "POST":
        payload = request.form.to_dict()
        payload["is_active"] = "is_active" in request.form
        try:
            UserService.update(user, payload, acting_user=actor)
            flash(f"{user.name}'s account was updated.", "success")
            return redirect(url_for("web.users_page"))
        except UserValidationError as error:
            flash(str(error), "error")
    return render_template("user_form.html", user=user, roles=ROLE_LABELS)


@web_bp.get("/scanner")
def scanner_page():
    return render_template("scanner.html")


@web_bp.get("/picker")
@roles_required("admin", "manager", "picker")
def picker_page():
    actor = _current_actor()
    orders = (
        SalesOrder.query.filter(
            SalesOrder.workspace_id == actor.workspace_id,
            SalesOrder.status.in_(("pending", "picking", "packed")),
        )
        .order_by(SalesOrder.created_at, SalesOrder.id)
        .limit(50)
        .all()
    )
    return render_template("picker.html", orders=orders)


@web_bp.get("/products")
def products_page():
    include_archived = request.args.get("include_archived", "").lower() in {
        "1",
        "true",
        "yes",
    }
    query = Product.query
    if not include_archived:
        query = query.filter_by(is_active=True)
    return render_template(
        "products.html",
        products=query.order_by(Product.name).all(),
        include_archived=include_archived,
    )


@web_bp.get("/manage")
@roles_required("admin", "manager")
def manage_page():
    actor = _current_actor()
    return render_template(
        "manage.html",
        products=Product.query.filter_by(is_active=True).order_by(Product.name).all(),
        suppliers=Supplier.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        )
        .order_by(Supplier.name)
        .all(),
        locations=InventoryLocation.query.filter_by(is_active=True)
        .order_by(InventoryLocation.name)
        .all(),
        bins=Bin.query.filter_by(is_active=True).order_by(Bin.code).all(),
    )


@web_bp.post("/manage/suppliers")
@roles_required("admin", "manager")
def add_supplier_form():
    try:
        supplier = SupplierService.create(request.form.to_dict(), actor=_current_actor())
        flash(f"{supplier.name} was added.", "success")
    except SupplierValidationError as error:
        flash(str(error), "error")
    target = request.form.get("return_to", "")
    return redirect(
        url_for("web.suppliers_page")
        if target == "suppliers"
        else url_for("web.manage_page")
    )


@web_bp.get("/suppliers")
@roles_required("admin", "manager")
def suppliers_page():
    actor = _current_actor()
    include_inactive = request.args.get("include_inactive", "").lower() in {
        "1",
        "true",
        "yes",
    }
    query = Supplier.query.filter_by(workspace_id=actor.workspace_id)
    if not include_inactive:
        query = query.filter_by(is_active=True)
    return render_template(
        "suppliers.html",
        suppliers=query.order_by(Supplier.name).all(),
        include_inactive=include_inactive,
    )


@web_bp.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "manager")
def edit_supplier_form(supplier_id: int):
    supplier = _supplier_for_actor(supplier_id)
    if request.method == "POST":
        payload = request.form.to_dict()
        payload["is_active"] = "is_active" in request.form
        try:
            SupplierService.update(supplier, payload, actor=_current_actor())
            flash(f"{supplier.name} was updated.", "success")
            return redirect(url_for("web.suppliers_page", include_inactive=1))
        except SupplierValidationError as error:
            flash(str(error), "error")
    return render_template("supplier_form.html", supplier=supplier)


@web_bp.post("/suppliers/<int:supplier_id>/archive")
@roles_required("admin", "manager")
def archive_supplier_form(supplier_id: int):
    supplier = SupplierService.archive(
        _supplier_for_actor(supplier_id), actor=_current_actor()
    )
    flash(
        f"{supplier.name} was archived; existing product links were preserved.",
        "success",
    )
    return redirect(url_for("web.suppliers_page", include_inactive=1))


@web_bp.post("/suppliers/<int:supplier_id>/restore")
@roles_required("admin", "manager")
def restore_supplier_form(supplier_id: int):
    supplier = SupplierService.restore(
        _supplier_for_actor(supplier_id), actor=_current_actor()
    )
    flash(f"{supplier.name} was restored.", "success")
    return redirect(url_for("web.suppliers_page", include_inactive=1))


@web_bp.post("/manage/locations")
@roles_required("admin", "manager")
def add_location_form():
    try:
        location = LocationService.create(request.form.to_dict(), actor=_current_actor())
        flash(f"{location.name} was added.", "success")
    except LocationValidationError as error:
        flash(str(error), "error")
    return redirect(url_for("web.manage_page"))


@web_bp.get("/locations")
def locations_page():
    return render_template(
        "locations.html",
        locations=InventoryLocation.query.order_by(InventoryLocation.name).all(),
    )


@web_bp.route("/locations/<int:location_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "manager")
def edit_location_form(location_id: int):
    location = db.get_or_404(InventoryLocation, location_id)
    if request.method == "POST":
        payload = request.form.to_dict()
        payload["is_active"] = "is_active" in request.form
        try:
            LocationService.update(location, payload)
            flash(f"{location.name} was updated.", "success")
            return redirect(url_for("web.locations_page"))
        except LocationValidationError as error:
            flash(str(error), "error")
    return render_template("location_form.html", location=location)


@web_bp.post("/locations/<int:location_id>/bins")
@roles_required("admin", "manager")
def add_bin_form(location_id: int):
    location = db.get_or_404(InventoryLocation, location_id)
    try:
        bin_record = BinService.create(location, request.form.to_dict())
        flash(f"Bin {location.code}/{bin_record.code} was added.", "success")
    except LocationValidationError as error:
        flash(str(error), "error")
    return redirect(url_for("web.locations_page"))


@web_bp.route("/bins/<int:bin_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "manager")
def edit_bin_form(bin_id: int):
    bin_record = db.get_or_404(Bin, bin_id)
    if request.method == "POST":
        payload = request.form.to_dict()
        payload["is_active"] = "is_active" in request.form
        try:
            BinService.update(bin_record, payload)
            flash(
                f"Bin {bin_record.location.code}/{bin_record.code} was updated.",
                "success",
            )
            return redirect(url_for("web.locations_page"))
        except LocationValidationError as error:
            flash(str(error), "error")
    return render_template("bin_form.html", bin=bin_record)


@web_bp.post("/manage/products")
@roles_required("admin", "manager")
def add_product_form():
    payload = request.form.to_dict()
    payload["is_perishable"] = "is_perishable" in request.form
    try:
        product = ProductService.create(
            payload, workspace_id=_current_actor().workspace_id
        )
        flash(f"{product.name} was added. Add its starting stock below.", "success")
    except ProductValidationError as error:
        flash(str(error), "error")
    return redirect(url_for("web.manage_page"))


@web_bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "manager")
def edit_product_form(product_id: int):
    product = db.get_or_404(Product, product_id)
    if request.method == "POST":
        payload = request.form.to_dict()
        payload["is_perishable"] = "is_perishable" in request.form
        try:
            ProductService.update(
                product, payload, workspace_id=_current_actor().workspace_id
            )
            flash(f"{product.name} was updated.", "success")
            return redirect(url_for("web.products_page", include_archived=1))
        except ProductValidationError as error:
            flash(str(error), "error")
    return render_template(
        "product_form.html",
        product=product,
        suppliers=Supplier.query.filter_by(
            workspace_id=_current_actor().workspace_id, is_active=True
        )
        .order_by(Supplier.name)
        .all(),
    )


@web_bp.post("/products/<int:product_id>/archive")
@roles_required("admin", "manager")
def archive_product_form(product_id: int):
    product = db.get_or_404(Product, product_id)
    ProductService.archive(product)
    flash(f"{product.name} was archived. Its history and stock were preserved.", "success")
    return redirect(url_for("web.products_page", include_archived=1))


@web_bp.post("/products/<int:product_id>/restore")
@roles_required("admin", "manager")
def restore_product_form(product_id: int):
    product = db.get_or_404(Product, product_id)
    ProductService.restore(product)
    flash(f"{product.name} was restored.", "success")
    return redirect(url_for("web.products_page", include_archived=1))


@web_bp.post("/manage/products/import")
@roles_required("admin", "manager")
def import_products_form():
    try:
        content = _uploaded_csv_bytes()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("web.manage_page"))
    result = ProductCSVImporter.import_bytes(
        content,
        update_existing="update_existing" in request.form,
        max_rows=current_app.config["MAX_PRODUCT_CSV_ROWS"],
        workspace_id=_current_actor().workspace_id,
    )
    if result.committed:
        flash(
            f"CSV import complete: {result.created} created and {result.updated} updated.",
            "success",
        )
    else:
        preview = "; ".join(
            f"row {item['row']}: "
            + ", ".join(f"{field} {message}" for field, message in item["errors"].items())
            for item in result.errors[:5]
        )
        suffix = "" if len(result.errors) <= 5 else f"; plus {len(result.errors) - 5} more"
        flash(f"Nothing was imported. {preview}{suffix}", "error")
    return redirect(url_for("web.manage_page"))


@web_bp.post("/manage/stock")
@roles_required("admin", "manager")
def adjust_stock_form():
    try:
        stock = InventoryService.adjust_stock(
            {
                "sku": request.form.get("sku"),
                "location_code": request.form.get("location_code"),
                "bin_code": request.form.get("bin_code"),
                "quantity_delta": request.form.get("quantity_delta"),
                "reason": request.form.get("reason", "manual_adjustment"),
                "note": request.form.get("note", ""),
            },
            actor=_current_actor(),
        )
        flash(
            f"{stock.product.sku} at {stock.location.code} is now "
            f"{number_for_json(stock.quantity_on_hand)} {stock.product.unit}.",
            "success",
        )
    except (
        ValueError,
        UnknownInventoryReferenceError,
        InsufficientStockError,
        CapacityExceededError,
    ) as error:
        flash(str(error), "error")
    return redirect(url_for("web.manage_page"))


@web_bp.get("/transfers")
def transfers_page():
    return render_template(
        "transfers.html",
        products=Product.query.filter_by(is_active=True).order_by(Product.name).all(),
        locations=InventoryLocation.query.filter_by(is_active=True)
        .order_by(InventoryLocation.name)
        .all(),
        transfers=StockTransfer.query.order_by(StockTransfer.created_at.desc()).limit(50).all(),
        transfer_request_id=str(uuid4()),
        selected_sku=request.args.get("sku", "").strip().upper(),
    )


@web_bp.post("/transfers")
@roles_required("admin", "manager", "picker")
def create_transfer_form():
    try:
        transfer, created = TransferService.transfer(
            request.form.to_dict(), actor=_current_actor()
        )
        message = (
            f"Transferred {number_for_json(transfer.quantity)} "
            f"{transfer.product.unit_of_measure} of {transfer.product.sku}."
            if created
            else f"Transfer {transfer.transfer_uid} was already completed."
        )
        flash(message, "success")
    except (
        ValueError,
        UnknownInventoryReferenceError,
        InsufficientStockError,
        TransferConflictError,
    ) as error:
        flash(str(error), "error")
    return redirect(url_for("web.transfers_page"))


@web_bp.get("/orders")
def orders_page():
    actor = _current_actor()
    return render_template(
        "orders.html",
        orders=SalesOrder.query.filter_by(workspace_id=actor.workspace_id)
        .order_by(SalesOrder.created_at.desc(), SalesOrder.id.desc())
        .limit(100)
        .all(),
        products=Product.query.filter_by(is_active=True).order_by(Product.name).all(),
        locations=InventoryLocation.query.filter_by(is_active=True)
        .order_by(InventoryLocation.name)
        .all(),
        order_request_id=str(uuid4()),
    )


@web_bp.post("/orders")
@roles_required("admin", "manager")
def create_order_form():
    try:
        order, created = SalesOrderService.create(
            _order_form_payload(), actor=_current_actor()
        )
        flash(
            (
                f"Sales order {order.order_uid[:8]} was created and stock was reserved."
                if created
                else f"Sales order {order.order_uid[:8]} already exists."
            ),
            "success",
        )
        return redirect(url_for("web.order_detail_page", order_id=order.id))
    except (
        ValueError,
        UnknownInventoryReferenceError,
        InsufficientStockError,
        SalesOrderConflictError,
    ) as error:
        flash(str(error), "error")
        return redirect(url_for("web.orders_page"))


@web_bp.get("/orders/<int:order_id>")
def order_detail_page(order_id: int):
    return render_template(
        "order_detail.html", order=_sales_order_for_actor(order_id)
    )


@web_bp.post("/orders/<int:order_id>/start-picking")
@roles_required("admin", "manager", "picker")
def start_order_picking_form(order_id: int):
    order = _sales_order_for_actor(order_id)
    try:
        order, changed = SalesOrderService.start_picking(
            order, actor=_current_actor()
        )
        flash(
            "Pick list generated." if changed else "This order is already being picked.",
            "success",
        )
    except (SalesOrderStateError, UnknownInventoryReferenceError) as error:
        flash(str(error), "error")
    return _order_action_redirect(order_id)


@web_bp.post("/orders/<int:order_id>/items/<int:item_id>/pick")
@roles_required("admin", "manager", "picker")
def confirm_order_item_pick_form(order_id: int, item_id: int):
    order = _sales_order_for_actor(order_id)
    try:
        item, changed = SalesOrderService.confirm_item_picked(
            order, item_id, actor=_current_actor()
        )
        flash(
            (
                f"{item.product.sku} marked as picked."
                if changed
                else f"{item.product.sku} was already picked."
            ),
            "success",
        )
    except (SalesOrderStateError, UnknownInventoryReferenceError) as error:
        flash(str(error), "error")
    return _order_action_redirect(order_id)


@web_bp.post("/orders/<int:order_id>/pack")
@roles_required("admin", "manager", "picker")
def pack_order_form(order_id: int):
    order = _sales_order_for_actor(order_id)
    try:
        _, changed = SalesOrderService.confirm_packing(
            order, actor=_current_actor()
        )
        flash(
            "Packing confirmed." if changed else "Packing was already confirmed.",
            "success",
        )
    except SalesOrderStateError as error:
        flash(str(error), "error")
    return _order_action_redirect(order_id)


@web_bp.post("/orders/<int:order_id>/ship")
@roles_required("admin", "manager")
def ship_order_form(order_id: int):
    order = _sales_order_for_actor(order_id)
    try:
        _, changed = SalesOrderService.ship(order, actor=_current_actor())
        flash(
            (
                "Order shipped; reserved stock moved to fulfilled sales."
                if changed
                else "This order was already shipped."
            ),
            "success",
        )
    except SalesOrderStateError as error:
        flash(str(error), "error")
    return redirect(url_for("web.order_detail_page", order_id=order_id))


@web_bp.post("/orders/<int:order_id>/cancel")
@roles_required("admin", "manager")
def cancel_order_form(order_id: int):
    order = _sales_order_for_actor(order_id)
    try:
        _, changed = SalesOrderService.cancel(order, actor=_current_actor())
        flash(
            (
                "Order cancelled and all stock reservations were released."
                if changed
                else "This order was already cancelled."
            ),
            "success",
        )
    except SalesOrderStateError as error:
        flash(str(error), "error")
    return redirect(url_for("web.order_detail_page", order_id=order_id))


@web_bp.get("/returns")
def returns_page():
    actor = _current_actor()
    status = request.args.get("status", "").strip().lower()
    allowed_statuses = {
        "requested",
        "authorized",
        "receiving",
        "completed",
        "rejected",
        "cancelled",
    }
    query = ReturnAuthorization.query.filter_by(workspace_id=actor.workspace_id)
    if status in allowed_statuses:
        query = query.filter_by(status=status)
    return render_template(
        "returns.html",
        returns=query.order_by(
            ReturnAuthorization.created_at.desc(), ReturnAuthorization.id.desc()
        )
        .limit(100)
        .all(),
        selected_status=status if status in allowed_statuses else "",
    )


@web_bp.route("/orders/<int:order_id>/returns/new", methods=["GET", "POST"])
@roles_required("admin", "manager")
def create_return_form(order_id: int):
    order = _sales_order_for_actor(order_id)
    if request.method == "POST":
        try:
            rma, created = ReturnService.create(
                order, _return_form_payload(), actor=_current_actor()
            )
            flash(
                (
                    f"Return {rma.rma_uid[:8]} was requested for review."
                    if created
                    else f"Return {rma.rma_uid[:8]} already exists."
                ),
                "success",
            )
            return redirect(url_for("web.return_detail_page", return_id=rma.id))
        except (
            ValueError,
            UnknownInventoryReferenceError,
            ReturnConflictError,
        ) as error:
            flash(str(error), "error")
    return render_template(
        "return_form.html",
        order=order,
        return_request_id=str(uuid4()),
        reason_labels=RETURN_REASON_LABELS,
        returnable={item.id: returnable_quantity(item) for item in order.items},
    )


@web_bp.get("/returns/<int:return_id>")
def return_detail_page(return_id: int):
    actor = _current_actor()
    rma = _return_for_actor(return_id)
    return render_template(
        "return_detail.html",
        rma=rma,
        serialized=serialize_return(rma),
        locations=InventoryLocation.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        )
        .order_by(InventoryLocation.name)
        .all(),
        disposition_labels=RETURN_DISPOSITION_LABELS,
        receipt_request_ids={item.id: str(uuid4()) for item in rma.items},
    )


@web_bp.post("/returns/<int:return_id>/authorize")
@roles_required("admin", "manager")
def authorize_return_form(return_id: int):
    try:
        _, changed = ReturnService.authorize(
            _return_for_actor(return_id), actor=_current_actor()
        )
        flash(
            "Return authorized for receiving."
            if changed
            else "Return was already authorized.",
            "success",
        )
    except ReturnStateError as error:
        flash(str(error), "error")
    return redirect(url_for("web.return_detail_page", return_id=return_id))


@web_bp.post("/returns/<int:return_id>/reject")
@roles_required("admin", "manager")
def reject_return_form(return_id: int):
    try:
        _, changed = ReturnService.reject(
            _return_for_actor(return_id), actor=_current_actor()
        )
        flash("Return rejected." if changed else "Return was already rejected.", "success")
    except ReturnStateError as error:
        flash(str(error), "error")
    return redirect(url_for("web.return_detail_page", return_id=return_id))


@web_bp.post("/returns/<int:return_id>/cancel")
@roles_required("admin", "manager")
def cancel_return_form(return_id: int):
    try:
        _, changed = ReturnService.cancel(
            _return_for_actor(return_id), actor=_current_actor()
        )
        flash("Return cancelled." if changed else "Return was already cancelled.", "success")
    except ReturnStateError as error:
        flash(str(error), "error")
    return redirect(url_for("web.return_detail_page", return_id=return_id))


@web_bp.post("/returns/<int:return_id>/items/<int:item_id>/receive")
@roles_required("admin", "manager", "picker")
def receive_return_item_form(return_id: int, item_id: int):
    try:
        receipt, created = ReturnService.receive_item(
            _return_for_actor(return_id),
            item_id,
            request.form.to_dict(),
            actor=_current_actor(),
        )
        flash(
            (
                f"Received {number_for_json(receipt.quantity)} units as "
                f"{RETURN_DISPOSITION_LABELS[receipt.disposition].lower()}."
                if created
                else "This receipt was already recorded."
            ),
            "success",
        )
    except (
        ValueError,
        UnknownInventoryReferenceError,
        ReturnConflictError,
        CapacityExceededError,
    ) as error:
        flash(str(error), "error")
    return redirect(url_for("web.return_detail_page", return_id=return_id))


@web_bp.get("/reports")
@roles_required("admin", "manager")
def reports_page():
    actor = _current_actor()
    return render_template(
        "reports.html",
        risk_report=ReportService.risk_report(workspace_id=actor.workspace_id),
        valuation_report=ReportService.valuation_report(
            workspace_id=actor.workspace_id
        ),
        deliveries=AlertDelivery.query.filter_by(workspace_id=actor.workspace_id)
        .order_by(AlertDelivery.created_at.desc(), AlertDelivery.id.desc())
        .limit(20)
        .all(),
        ses_enabled=current_app.config["SES_ENABLED"],
    )


@web_bp.get("/reports/<report_type>.<file_format>")
@roles_required("admin", "manager")
def download_report(report_type: str, file_format: str):
    actor = _current_actor()
    try:
        report = ReportService.build(report_type, workspace_id=actor.workspace_id)
        content, mimetype = ReportExporter.export(report, file_format)
    except ValueError as error:
        abort(404, description=str(error))
    return send_file(
        BytesIO(content),
        mimetype=mimetype,
        as_attachment=True,
        download_name=ReportExporter.filename(report, file_format),
        max_age=0,
    )


@web_bp.post("/reports/alerts/critical")
@roles_required("admin", "manager")
def send_critical_alert_form():
    actor = _current_actor()
    results = _workspace_forecasts(ForecastService.run(), actor.workspace_id)
    delivery = ReportMailer.send_critical_alerts(
        results, workspace_id=actor.workspace_id
    )
    flash(delivery.reason, "success" if delivery.sent else "error")
    return redirect(url_for("web.reports_page"))


@api_bp.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Database readiness check failed")
        return (
            jsonify(
                {
                    "status": "unavailable",
                    "service": "ai-inventory-tracker",
                    "database": "unavailable",
                }
            ),
            503,
        )
    return jsonify(
        {"status": "ok", "service": "ai-inventory-tracker", "database": "ok"}
    )


@web_bp.get("/service-worker.js")
def service_worker():
    response = send_from_directory(current_app.static_folder, "service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@api_bp.get("/dashboard")
@api_read_access
def dashboard_api():
    return jsonify(_dashboard_data())


@api_bp.get("/locations")
@api_read_access
def locations_api():
    include_inactive = request.args.get("include_inactive", "").lower() in {
        "1",
        "true",
        "yes",
    }
    query = InventoryLocation.query
    if not include_inactive:
        query = query.filter_by(is_active=True)
    return jsonify(
        {
            "locations": [
                serialize_location(location)
                for location in query.order_by(InventoryLocation.name).all()
            ]
        }
    )


@api_bp.post("/locations")
def create_location_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        location = LocationService.create(
            request.get_json(silent=True) or {}, actor=_current_actor()
        )
    except ValueError as error:
        if isinstance(error, LocationValidationError):
            return (
                jsonify({"error": "location validation failed", "fields": error.errors}),
                _location_error_status(error),
            )
        return jsonify({"error": str(error)}), 400
    return jsonify({"location": serialize_location(location)}), 201


@api_bp.patch("/locations/<int:location_id>")
def update_location_api(location_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        location = LocationService.update(
            db.get_or_404(InventoryLocation, location_id),
            request.get_json(silent=True) or {},
        )
    except LocationValidationError as error:
        return (
            jsonify({"error": "location validation failed", "fields": error.errors}),
            _location_error_status(error),
        )
    return jsonify({"location": serialize_location(location)})


@api_bp.post("/locations/<int:location_id>/bins")
def create_bin_api(location_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        bin_record = BinService.create(
            db.get_or_404(InventoryLocation, location_id),
            request.get_json(silent=True) or {},
        )
    except LocationValidationError as error:
        return (
            jsonify({"error": "bin validation failed", "fields": error.errors}),
            _location_error_status(error),
        )
    return jsonify({"bin": serialize_bin(bin_record)}), 201


@api_bp.patch("/bins/<int:bin_id>")
def update_bin_api(bin_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        bin_record = BinService.update(
            db.get_or_404(Bin, bin_id), request.get_json(silent=True) or {}
        )
    except LocationValidationError as error:
        return (
            jsonify({"error": "bin validation failed", "fields": error.errors}),
            _location_error_status(error),
        )
    return jsonify({"bin": serialize_bin(bin_record)})


@api_bp.get("/suppliers")
def suppliers_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    actor = _current_actor()
    include_inactive = request.args.get("include_inactive", "").lower() in {
        "1",
        "true",
        "yes",
    }
    query = Supplier.query.filter_by(workspace_id=actor.workspace_id)
    if not include_inactive:
        query = query.filter_by(is_active=True)
    return jsonify(
        {
            "suppliers": [
                serialize_supplier(supplier)
                for supplier in query.order_by(Supplier.name).all()
            ]
        }
    )


@api_bp.post("/suppliers")
def create_supplier_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        supplier = SupplierService.create(
            request.get_json(silent=True) or {}, actor=_current_actor()
        )
    except SupplierValidationError as error:
        return (
            jsonify({"error": "supplier validation failed", "fields": error.errors}),
            _supplier_error_status(error),
        )
    return jsonify({"supplier": serialize_supplier(supplier)}), 201


@api_bp.patch("/suppliers/<int:supplier_id>")
def update_supplier_api(supplier_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        supplier = SupplierService.update(
            _supplier_for_actor(supplier_id),
            request.get_json(silent=True) or {},
            actor=_current_actor(),
        )
    except SupplierValidationError as error:
        return (
            jsonify({"error": "supplier validation failed", "fields": error.errors}),
            _supplier_error_status(error),
        )
    return jsonify({"supplier": serialize_supplier(supplier)})


@api_bp.delete("/suppliers/<int:supplier_id>")
def archive_supplier_api(supplier_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    supplier = SupplierService.archive(
        _supplier_for_actor(supplier_id), actor=_current_actor()
    )
    return jsonify({"supplier": serialize_supplier(supplier)})


@api_bp.post("/suppliers/<int:supplier_id>/restore")
def restore_supplier_api(supplier_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    supplier = SupplierService.restore(
        _supplier_for_actor(supplier_id), actor=_current_actor()
    )
    return jsonify({"supplier": serialize_supplier(supplier)})


@api_bp.get("/products")
@api_read_access
def products_api():
    actor = _current_actor()
    include_archived = request.args.get("include_archived", "").lower() in {
        "1",
        "true",
        "yes",
    }
    query = Product.query.options(
        selectinload(Product.stock_levels).selectinload(StockLevel.location),
        selectinload(Product.stock_levels).selectinload(StockLevel.bin),
        selectinload(Product.preferred_supplier),
    )
    if not include_archived:
        query = query.filter_by(is_active=True)
    return jsonify(
        {
            "products": [
                serialize_product(
                    product,
                    include_sensitive=actor.role != "picker",
                )
                for product in query.order_by(Product.name)
            ]
        }
    )


@api_bp.get("/products/<int:product_id>")
@api_read_access
def product_api(product_id: int):
    actor = _current_actor()
    return jsonify(
        {
            "product": serialize_product(
                db.get_or_404(Product, product_id),
                include_sensitive=actor.role != "picker",
            )
        }
    )


@api_bp.get("/barcodes/lookup")
@session_api_access
def barcode_lookup_api():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "barcode is required"}), 400
    if len(code) > 100:
        return jsonify({"error": "barcode must be 100 characters or fewer"}), 400

    product = Product.query.filter_by(barcode=code, is_active=True).first()
    matched_by = "barcode"
    if product is None:
        product = Product.query.filter_by(sku=code.upper(), is_active=True).first()
        matched_by = "sku"
    if product is None:
        return jsonify({"error": "no active product matches this barcode"}), 404

    serialized = serialize_product(product)
    actor = _current_actor()
    if actor.role == "picker":
        serialized.pop("cost_price", None)
        serialized.pop("preferred_supplier_id", None)
        serialized.pop("supplier", None)
    return jsonify({"matched_by": matched_by, "product": serialized})


@api_bp.post("/products")
def create_product():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    payload = request.get_json(silent=True) or {}
    try:
        product = ProductService.create(
            payload, workspace_id=_current_actor().workspace_id
        )
    except ProductValidationError as error:
        return jsonify({"error": "product validation failed", "fields": error.errors}), _product_error_status(error)
    return jsonify({"product": serialize_product(product)}), 201


@api_bp.patch("/products/<int:product_id>")
def update_product(product_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    product = db.get_or_404(Product, product_id)
    try:
        ProductService.update(
            product,
            request.get_json(silent=True) or {},
            workspace_id=_current_actor().workspace_id,
        )
    except ProductValidationError as error:
        return jsonify({"error": "product validation failed", "fields": error.errors}), _product_error_status(error)
    return jsonify({"product": serialize_product(product)})


@api_bp.delete("/products/<int:product_id>")
def archive_product(product_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    product = ProductService.archive(db.get_or_404(Product, product_id))
    return jsonify({"product": serialize_product(product)})


@api_bp.post("/products/<int:product_id>/restore")
def restore_product(product_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    product = ProductService.restore(db.get_or_404(Product, product_id))
    return jsonify({"product": serialize_product(product)})


@api_bp.post("/products/import")
def import_products():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        content = _uploaded_csv_bytes()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    update_existing = request.form.get("update_existing", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    result = ProductCSVImporter.import_bytes(
        content,
        update_existing=update_existing,
        max_rows=current_app.config["MAX_PRODUCT_CSV_ROWS"],
        workspace_id=_current_actor().workspace_id,
    )
    return jsonify(result.as_dict()), 201 if result.committed else 422


@api_bp.post("/stock/adjustments")
def stock_adjustment():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        stock = InventoryService.adjust_stock(
            request.get_json(silent=True) or {}, actor=_current_actor()
        )
    except (ValueError, UnknownInventoryReferenceError) as error:
        return jsonify({"error": str(error)}), 400
    except (InsufficientStockError, CapacityExceededError) as error:
        return jsonify({"error": str(error)}), 409
    return jsonify(
        {
            "sku": stock.product.sku,
            "location": stock.location.code,
            "bin": stock.bin.code if stock.bin else None,
            "quantity_on_hand": number_for_json(stock.quantity_on_hand),
            "quantity_reserved": number_for_json(stock.quantity_reserved),
            "quantity_available": number_for_json(stock.quantity_available),
        }
    )


@api_bp.get("/transfers")
def transfers_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    transfers = StockTransfer.query.order_by(StockTransfer.created_at.desc()).limit(limit)
    return jsonify({"transfers": [serialize_transfer(item) for item in transfers]})


@api_bp.post("/transfers")
def create_transfer_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        transfer, created = TransferService.transfer(
            request.get_json(silent=True) or {}, actor=_current_actor()
        )
    except (TransferConflictError, InsufficientStockError) as error:
        return jsonify({"error": str(error)}), 409
    except (ValueError, UnknownInventoryReferenceError) as error:
        return jsonify({"error": str(error)}), 400
    return (
        jsonify({"transfer": serialize_transfer(transfer), "created": created}),
        201 if created else 200,
    )


@api_bp.get("/sales-orders")
@api_read_access
def sales_orders_api():
    actor = _current_actor()
    query = SalesOrder.query.filter_by(workspace_id=actor.workspace_id)
    status = request.args.get("status", "").strip().lower()
    if status:
        if status not in {"pending", "picking", "packed", "shipped", "cancelled"}:
            return jsonify({"error": "unsupported sales-order status"}), 400
        query = query.filter_by(status=status)
    limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
    orders = query.order_by(
        SalesOrder.created_at.desc(), SalesOrder.id.desc()
    ).limit(limit)
    return jsonify({"orders": [serialize_order(order) for order in orders]})


@api_bp.post("/sales-orders")
def create_sales_order_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        order, created = SalesOrderService.create(
            request.get_json(silent=True) or {}, actor=_current_actor()
        )
    except SalesOrderPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except (InsufficientStockError, SalesOrderConflictError) as error:
        return jsonify({"error": str(error)}), 409
    except (ValueError, UnknownInventoryReferenceError) as error:
        return jsonify({"error": str(error)}), 400
    return (
        jsonify({"order": serialize_order(order), "created": created}),
        201 if created else 200,
    )


@api_bp.get("/sales-orders/<int:order_id>")
@api_read_access
def sales_order_api(order_id: int):
    return jsonify({"order": serialize_order(_sales_order_for_actor(order_id))})


@api_bp.get("/sales-orders/<int:order_id>/pick-list")
@api_read_access
def sales_order_pick_list_api(order_id: int):
    return jsonify(
        {"pick_list": serialize_pick_list(_sales_order_for_actor(order_id))}
    )


@api_bp.post("/sales-orders/<int:order_id>/start-picking")
def start_sales_order_picking_api(order_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        order, changed = SalesOrderService.start_picking(
            _sales_order_for_actor(order_id), actor=_current_actor()
        )
    except SalesOrderPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except SalesOrderStateError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"order": serialize_order(order), "changed": changed})


@api_bp.post("/sales-orders/<int:order_id>/items/<int:item_id>/pick")
def pick_sales_order_item_api(order_id: int, item_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        item, changed = SalesOrderService.confirm_item_picked(
            _sales_order_for_actor(order_id), item_id, actor=_current_actor()
        )
    except SalesOrderPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except SalesOrderStateError as error:
        return jsonify({"error": str(error)}), 409
    except UnknownInventoryReferenceError as error:
        return jsonify({"error": str(error)}), 404
    return jsonify(
        {
            "item_id": item.id,
            "sku": item.product.sku,
            "picked_quantity": number_for_json(item.picked_quantity),
            "changed": changed,
        }
    )


@api_bp.post("/sales-orders/<int:order_id>/pack")
def pack_sales_order_api(order_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        order, changed = SalesOrderService.confirm_packing(
            _sales_order_for_actor(order_id), actor=_current_actor()
        )
    except SalesOrderPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except SalesOrderStateError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"order": serialize_order(order), "changed": changed})


@api_bp.post("/sales-orders/<int:order_id>/ship")
def ship_sales_order_api(order_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        order, changed = SalesOrderService.ship(
            _sales_order_for_actor(order_id), actor=_current_actor()
        )
    except SalesOrderPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except SalesOrderStateError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"order": serialize_order(order), "changed": changed})


@api_bp.post("/sales-orders/<int:order_id>/cancel")
def cancel_sales_order_api(order_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        order, changed = SalesOrderService.cancel(
            _sales_order_for_actor(order_id), actor=_current_actor()
        )
    except SalesOrderPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except SalesOrderStateError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"order": serialize_order(order), "changed": changed})


@api_bp.get("/returns")
@api_read_access
def returns_api():
    actor = _current_actor()
    query = ReturnAuthorization.query.filter_by(workspace_id=actor.workspace_id)
    status = request.args.get("status", "").strip().lower()
    allowed_statuses = {
        "requested",
        "authorized",
        "receiving",
        "completed",
        "rejected",
        "cancelled",
    }
    if status:
        if status not in allowed_statuses:
            return jsonify({"error": "unsupported return status"}), 400
        query = query.filter_by(status=status)
    limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
    rows = query.order_by(
        ReturnAuthorization.created_at.desc(), ReturnAuthorization.id.desc()
    ).limit(limit)
    return jsonify({"returns": [serialize_return(row) for row in rows]})


@api_bp.post("/sales-orders/<int:order_id>/returns")
def create_return_api(order_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        rma, created = ReturnService.create(
            _sales_order_for_actor(order_id),
            request.get_json(silent=True) or {},
            actor=_current_actor(),
        )
    except ReturnPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except (ReturnStateError, ReturnConflictError) as error:
        return jsonify({"error": str(error)}), 409
    except (ValueError, UnknownInventoryReferenceError) as error:
        return jsonify({"error": str(error)}), 400
    return (
        jsonify({"return": serialize_return(rma), "created": created}),
        201 if created else 200,
    )


@api_bp.get("/returns/<int:return_id>")
@api_read_access
def return_api(return_id: int):
    return jsonify({"return": serialize_return(_return_for_actor(return_id))})


@api_bp.post("/returns/<int:return_id>/authorize")
def authorize_return_api(return_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        rma, changed = ReturnService.authorize(
            _return_for_actor(return_id), actor=_current_actor()
        )
    except ReturnPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except ReturnStateError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"return": serialize_return(rma), "changed": changed})


@api_bp.post("/returns/<int:return_id>/reject")
def reject_return_api(return_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        rma, changed = ReturnService.reject(
            _return_for_actor(return_id), actor=_current_actor()
        )
    except ReturnPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except ReturnStateError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"return": serialize_return(rma), "changed": changed})


@api_bp.post("/returns/<int:return_id>/cancel")
def cancel_return_api(return_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        rma, changed = ReturnService.cancel(
            _return_for_actor(return_id), actor=_current_actor()
        )
    except ReturnPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except ReturnStateError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"return": serialize_return(rma), "changed": changed})


@api_bp.post("/returns/<int:return_id>/items/<int:item_id>/receive")
def receive_return_item_api(return_id: int, item_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        receipt, created = ReturnService.receive_item(
            _return_for_actor(return_id),
            item_id,
            request.get_json(silent=True) or {},
            actor=_current_actor(),
        )
    except ReturnPermissionError as error:
        return jsonify({"error": str(error)}), 403
    except (ReturnStateError, ReturnConflictError, CapacityExceededError) as error:
        return jsonify({"error": str(error)}), 409
    except UnknownInventoryReferenceError as error:
        return jsonify({"error": str(error)}), 404
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return (
        jsonify({"receipt": serialize_return_receipt(receipt), "created": created}),
        201 if created else 200,
    )


@api_bp.get("/audit/movements")
def movements_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
    query = InventoryMovement.query
    movement_type = request.args.get("movement_type", "").strip()
    if movement_type:
        query = query.filter_by(movement_type=movement_type)
    rows = query.order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc()).limit(limit)
    return jsonify({"movements": [serialize_movement(row) for row in rows]})


@api_bp.post("/webhooks/sales")
def sales_webhook():
    """Receive an idempotent sale message from a POS, store, or mobile app."""
    _require_token("X-POS-Token", "POS_WEBHOOK_TOKEN")
    try:
        sale, created = InventoryService.record_sale(
            request.get_json(silent=True) or {}, actor=_current_actor()
        )
    except (ValueError, UnknownInventoryReferenceError) as error:
        return jsonify({"error": str(error)}), 400
    except InsufficientStockError as error:
        return jsonify({"error": str(error)}), 409
    except InventoryConflictError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"sale": serialize_sale(sale), "created": created}), 201 if created else 200


@api_bp.get("/insights")
@api_read_access
def insights_api():
    return jsonify({"insights": [serialize_insight(row) for row in latest_insights()]})


@api_bp.post("/analysis/run")
def run_analysis():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    results = ForecastService.run()
    return jsonify(
        {
            "analyzed": len(results),
            "at_risk": sum(item.recommended_reorder_quantity > 0 for item in results),
            "results": [item.as_dict() for item in results],
        }
    )


@api_bp.get("/reports/<report_type>")
def report_api(report_type: str):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    actor = _current_actor()
    try:
        report = ReportService.build(report_type, workspace_id=actor.workspace_id)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    file_format = request.args.get("format", "json").strip().lower()
    if file_format == "json":
        return jsonify({"report": report.as_dict()})
    try:
        content, mimetype = ReportExporter.export(report, file_format)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return send_file(
        BytesIO(content),
        mimetype=mimetype,
        as_attachment=True,
        download_name=ReportExporter.filename(report, file_format),
        max_age=0,
    )


@api_bp.post("/alerts/critical")
def send_critical_alert_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    actor = _current_actor()
    results = _workspace_forecasts(ForecastService.run(), actor.workspace_id)
    result = ReportMailer.send_critical_alerts(
        results, workspace_id=actor.workspace_id
    )
    return jsonify({"delivery": result.as_dict()}), 200 if result.sent else 202


@api_bp.get("/alerts/deliveries")
def alert_deliveries_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    actor = _current_actor()
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    rows = (
        AlertDelivery.query.filter_by(workspace_id=actor.workspace_id)
        .order_by(AlertDelivery.created_at.desc(), AlertDelivery.id.desc())
        .limit(limit)
        .all()
    )
    return jsonify(
        {
            "deliveries": [
                {
                    "id": row.id,
                    "report_type": row.report_type,
                    "severity": row.severity,
                    "status": row.status,
                    "recipient_count": row.recipient_count,
                    "item_count": row.item_count,
                    "provider_message_id": row.provider_message_id,
                    "detail": row.detail,
                    "created_at": row.created_at.isoformat(),
                    "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                }
                for row in rows
            ]
        }
    )
