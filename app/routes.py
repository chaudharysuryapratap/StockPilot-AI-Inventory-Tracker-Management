from __future__ import annotations

import secrets
import re
from html import escape
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
from sqlalchemy import func, or_, text
from sqlalchemy.orm import selectinload
from itsdangerous import BadSignature, URLSafeSerializer

from app import db
from app.models import (
    AlertDelivery,
    AuthToken,
    Bin,
    InventoryLocation,
    InventoryMovement,
    MFARecoveryCode,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    ReturnAuthorization,
    SalesOrder,
    StockLevel,
    StockTransfer,
    Supplier,
    User,
    Workspace,
    WorkspaceIntegration,
    WorkspaceMembership,
    WorkspaceSetting,
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
from app.services.identity import (
    activate_workspace_context,
    ensure_default_identity,
    memberships_for,
    normalize_business_username,
    resolve_actor,
)
from app.services.saas_auth import (
    AuthActionThrottle,
    AuthMailer,
    AuthTokenService,
    LoginThrottle,
    MFAService,
    SignupThrottle,
    WorkspaceService,
    WorkspaceValidationError,
    oidc_secret,
)
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
from app.services.procurement import (
    ProcurementConflictError,
    ProcurementError,
    ProcurementStateError,
    PurchaseOrderService,
    UnitConversionService,
    serialize_purchase_order,
    serialize_receipt,
)
from app.services.intelligence import (
    DashboardChatService,
    ForecastAccuracyService,
    dashboard_context,
    inventory_recommendations,
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
    try:
        membership = activate_workspace_context(
            actor, session.get("active_workspace_id")
        )
    except RuntimeError:
        session.clear()
        return None
    session["active_workspace_id"] = membership.workspace_id
    return actor


def _current_actor() -> User:
    actor = getattr(g, "current_user", None) or _session_user()
    if actor:
        return actor
    try:
        return resolve_actor(
            request.headers.get("X-Actor-Email"), request.headers.get("X-Workspace")
        )
    except ValueError as error:
        abort(403, description=str(error))
    except RuntimeError as error:
        abort(503, description=str(error))


def _validate_csrf() -> None:
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
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


def _page_size() -> int:
    raw_value = request.args.get("limit", "").strip()
    if not raw_value:
        return current_app.config["DEFAULT_PAGE_SIZE"]
    try:
        value = int(raw_value)
    except ValueError:
        abort(400, description="limit must be an integer")
    if value < 1 or value > current_app.config["MAX_PAGE_SIZE"]:
        abort(
            400,
            description=(
                f"limit must be between 1 and "
                f"{current_app.config['MAX_PAGE_SIZE']}"
            ),
        )
    return value


def _cursor_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="stockpilot-page-v1")


def _decode_cursor(kind: str, workspace_id: int) -> tuple[int, str] | None:
    encoded = request.args.get("cursor", "").strip()
    if not encoded:
        return None
    try:
        payload = _cursor_serializer().loads(encoded)
        if (
            payload.get("kind") != kind
            or int(payload.get("workspace_id")) != workspace_id
        ):
            raise ValueError
        direction = str(payload.get("direction", "next"))
        if direction not in {"next", "previous"}:
            raise ValueError
        return int(payload["id"]), direction
    except (BadSignature, KeyError, TypeError, ValueError):
        abort(400, description="invalid or expired pagination cursor")


def _encode_cursor(
    kind: str, workspace_id: int, record_id: int, *, direction: str = "next"
) -> str:
    return _cursor_serializer().dumps(
        {
            "kind": kind,
            "workspace_id": workspace_id,
            "id": record_id,
            "direction": direction,
        }
    )


def _cursor_page(query, model, *, kind: str, workspace_id: int, descending=False):
    """Return a reversible stable keyset page without exposing tenant identifiers."""

    limit = _page_size()
    total = query.order_by(None).count()
    decoded_cursor = _decode_cursor(kind, workspace_id)
    marker, direction = decoded_cursor or (None, "next")
    if marker is not None:
        moving_forward = direction == "next"
        use_less_than = descending == moving_forward
        query = query.filter(model.id < marker if use_less_than else model.id > marker)

    base_order = model.id.desc() if descending else model.id.asc()
    reverse_order = model.id.asc() if descending else model.id.desc()
    rows = query.order_by(
        reverse_order if direction == "previous" else base_order
    ).limit(limit + 1).all()
    has_extra = len(rows) > limit
    rows = rows[:limit]
    if direction == "previous":
        rows.reverse()
        has_previous = has_extra
        has_more = marker is not None
    else:
        has_previous = marker is not None
        has_more = has_extra

    next_cursor = (
        _encode_cursor(kind, workspace_id, rows[-1].id, direction="next")
        if has_more and rows
        else None
    )
    previous_cursor = (
        _encode_cursor(kind, workspace_id, rows[0].id, direction="previous")
        if has_previous and rows
        else None
    )
    args = request.args.to_dict(flat=True)
    args.pop("cursor", None)
    next_url = None
    previous_url = None
    if next_cursor:
        next_url = url_for(request.endpoint, cursor=next_cursor, **args)
    if previous_cursor:
        previous_url = url_for(request.endpoint, cursor=previous_cursor, **args)
    return rows, {
        "total": total,
        "limit": limit,
        "count": len(rows),
        "has_more": has_more,
        "has_previous": has_previous,
        "next_cursor": next_cursor,
        "next_url": next_url,
        "previous_cursor": previous_cursor,
        "previous_url": previous_url,
    }


def _login_attempt_key(identifier: object) -> str:
    return LoginThrottle.key(request.remote_addr, identifier)


def _login_is_rate_limited(identifier: object) -> bool:
    return LoginThrottle.is_limited(request.remote_addr, identifier)


def _record_login_result(identifier: object, *, succeeded: bool) -> None:
    LoginThrottle.record(request.remote_addr, identifier, succeeded=succeeded)


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
        actor = _session_user()
        if actor is not None:
            if (
                current_app.config.get("REQUIRE_EMAIL_VERIFICATION")
                and not actor.email_verified_at
            ):
                return jsonify({"error": "email verification required"}), 403
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
        if (
            current_app.config["STAFF_AUTH_ENABLED"]
            and current_app.config.get("REQUIRE_EMAIL_VERIFICATION")
            and actor is not None
            and not actor.email_verified_at
        ):
            return jsonify({"error": "email verification required"}), 403
        if current_app.config["STAFF_AUTH_ENABLED"] and request.method not in {"GET", "HEAD", "OPTIONS"}:
            _validate_csrf()
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
        activate_workspace_context(
            g.current_user, session.get("active_workspace_id")
        )
        return None

    setup_required = authentication_setup_required()
    if setup_required:
        activate_legacy_credentials()
        setup_required = authentication_setup_required()

    public_endpoints = {
        "web.login",
        "web.signup",
        "web.forgot_password",
        "web.reset_password",
        "web.verify_email",
        "web.accept_invitation",
        "web.mfa_challenge",
        "web.sso_login",
        "web.sso_callback",
        "web.favicon",
        "web.service_worker",
    }
    if request.endpoint in public_endpoints:
        return None
    if g.current_user is None:
        if setup_required and not current_app.config["ALLOW_WEB_SIGNUP"]:
            abort(
                503,
                description=(
                    "administrator setup is required; run "
                    "'flask --app run create-admin' on the server"
                ),
            )
        destination = (
            "web.signup"
            if setup_required
            or (
                request.endpoint == "web.dashboard"
                and current_app.config["ALLOW_WEB_SIGNUP"]
            )
            else "web.login"
        )
        return redirect(url_for(destination, next=request.path))
    if (
        current_app.config.get("REQUIRE_EMAIL_VERIFICATION")
        and not g.current_user.email_verified_at
        and request.endpoint not in {
            "web.verification_pending",
            "web.resend_verification",
            "web.logout",
        }
    ):
        return redirect(url_for("web.verification_pending"))
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
        "allow_signup": current_app.config.get("ALLOW_WEB_SIGNUP", False),
        "asset_version": current_app.config.get("ASSET_VERSION", "20260821.1"),
        "current_workspace": getattr(g, "active_workspace", None),
        "current_membership": getattr(g, "active_membership", None),
        "workspace_memberships": (
            memberships_for(g.current_user)
            if getattr(g, "current_user", None)
            else []
        ),
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


def _dashboard_data(location_id: int | None = None) -> dict:
    actor = _current_actor()
    insight_rows = [
        row for row in latest_insights()
        if row.location.workspace_id == actor.workspace_id
        and (location_id is None or row.location_id == location_id)
    ]
    insights = [serialize_insight(row) for row in insight_rows]
    stock_total_query = (
        db.session.query(func.coalesce(func.sum(StockLevel.quantity_on_hand), 0))
        .join(Product, StockLevel.product_id == Product.id)
        .filter(
            Product.workspace_id == actor.workspace_id,
            Product.is_active.is_(True),
        )
    )
    if location_id is not None:
        stock_total_query = stock_total_query.filter(
            StockLevel.location_id == location_id
        )
    total_units = stock_total_query.scalar()
    at_risk = [
        insight
        for insight in insights
        if insight["recommended_reorder_quantity"] > 0 or _stockout_is_soon(insight)
    ]
    product_count_query = Product.query.filter_by(
        workspace_id=actor.workspace_id, is_active=True
    )
    if location_id is not None:
        product_count_query = product_count_query.join(StockLevel).filter(
            StockLevel.location_id == location_id
        )
    locations = InventoryLocation.query.filter_by(
        workspace_id=actor.workspace_id, is_active=True
    ).order_by(InventoryLocation.name, InventoryLocation.id).all()
    warehouse_summaries = []
    for location in locations:
        serialized = serialize_location(location)
        warehouse_summaries.append(
            {
                "id": location.id,
                "name": location.name,
                "code": location.code,
                "address": location.address,
                "bins": len([item for item in location.bins if item.is_active]),
                "products": db.session.query(func.count(func.distinct(StockLevel.product_id)))
                .filter(StockLevel.location_id == location.id)
                .scalar(),
                "stock": serialized["stock"],
                "risks": len(
                    [
                        insight
                        for insight in insights
                        if insight["location"] == location.code
                        and insight["recommended_reorder_quantity"] > 0
                    ]
                ),
            }
        )
    return {
        "metrics": {
            "active_products": product_count_query.distinct().count(),
            "locations": 1 if location_id is not None else len(locations),
            "total_units": number_for_json(total_units),
            "items_at_risk": len(at_risk),
        },
        "insights": insights,
        "recommendations": inventory_recommendations(
            workspace_id=actor.workspace_id, location_id=location_id
        ),
        "forecast_accuracy": ForecastAccuracyService.summary(
            workspace_id=actor.workspace_id, location_id=location_id
        ),
        "warehouses": [
            {"id": row.id, "name": row.name, "code": row.code}
            for row in locations
        ],
        "warehouse_summaries": warehouse_summaries,
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


def _product_for_actor(product_id: int) -> Product:
    actor = _current_actor()
    product = Product.query.filter_by(
        id=product_id, workspace_id=actor.workspace_id
    ).first()
    if product is None:
        abort(404)
    return product


def _location_for_actor(location_id: int) -> InventoryLocation:
    actor = _current_actor()
    location = InventoryLocation.query.filter_by(
        id=location_id, workspace_id=actor.workspace_id
    ).first()
    if location is None:
        abort(404)
    return location


def _bin_for_actor(bin_id: int) -> Bin:
    actor = _current_actor()
    bin_record = (
        Bin.query.join(InventoryLocation)
        .filter(Bin.id == bin_id, InventoryLocation.workspace_id == actor.workspace_id)
        .first()
    )
    if bin_record is None:
        abort(404)
    return bin_record


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


def _purchase_order_for_actor(order_id: int) -> PurchaseOrder:
    order = db.session.get(PurchaseOrder, order_id)
    if order is None or order.workspace_id != _current_actor().workspace_id:
        abort(404)
    return order


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
    actor = _current_actor()
    warehouses = InventoryLocation.query.filter_by(
        workspace_id=actor.workspace_id, is_active=True
    ).order_by(InventoryLocation.name, InventoryLocation.id).all()
    scope = request.args.get("scope", session.get("dashboard_scope", "all"))
    scope = "warehouse" if scope == "warehouse" else "all"
    requested_id = request.args.get(
        "warehouse_id", session.get("dashboard_warehouse_id", "")
    )
    try:
        warehouse_id = int(requested_id) if requested_id not in (None, "") else None
    except (TypeError, ValueError):
        warehouse_id = None
    selected = next((row for row in warehouses if row.id == warehouse_id), None)
    if scope == "warehouse" and selected is None:
        selected = warehouses[0] if warehouses else None
    selected_id = selected.id if scope == "warehouse" and selected else None
    session["dashboard_scope"] = scope
    if selected:
        session["dashboard_warehouse_id"] = selected.id
    data = _dashboard_data(selected_id)
    return render_template(
        "dashboard.html",
        **data,
        dashboard_scope=scope,
        selected_warehouse=selected,
    )


def _complete_login(actor: User, workspace_id: int | None = None):
    membership = activate_workspace_context(actor, workspace_id)
    session.clear()
    session["user_id"] = actor.id
    session["active_workspace_id"] = membership.workspace_id
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


def _send_auth_link(
    *, recipient: str, subject: str, link: str, explanation: str
) -> bool:
    return AuthMailer.send_link(
        recipient=recipient,
        subject=subject,
        text_body=f"{explanation}\n\n{link}\n\nIf you did not request this, ignore this email.",
        html_body=(
            f"<p>{escape(explanation)}</p><p><a href=\"{escape(link, quote=True)}\">Continue to StockPilot</a></p>"
            "<p>If you did not request this, ignore this email.</p>"
        ),
    )


def _workspace_from_username(value: object) -> Workspace | None:
    username = normalize_business_username(value)
    if not username:
        return None
    return Workspace.query.filter(func.lower(Workspace.business_username) == username).first()


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    if not current_app.config["STAFF_AUTH_ENABLED"]:
        return redirect(url_for("web.dashboard"))
    if getattr(g, "current_user", None):
        return redirect(url_for("web.dashboard"))
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
            business_username=request.form.get("business_username"),
        )
        if actor:
            _record_login_result(identifier, succeeded=True)
            workspace = _workspace_from_username(request.form.get("business_username"))
            membership = activate_workspace_context(
                actor, workspace.id if workspace else None
            )
            if actor.mfa_enabled_at:
                session.clear()
                session["preauth_user_id"] = actor.id
                session["preauth_workspace_id"] = membership.workspace_id
                session["csrf_token"] = secrets.token_urlsafe(32)
                session["mfa_attempts"] = 0
                return redirect(url_for("web.mfa_challenge"))
            _complete_login(actor, membership.workspace_id)
            target = request.args.get("next", "")
            if (
                current_app.config.get("REQUIRE_EMAIL_VERIFICATION")
                and not actor.email_verified_at
            ):
                return redirect(url_for("web.verification_pending"))
            return redirect(_safe_next_target(target) or url_for("web.dashboard"))
        _record_login_result(identifier, succeeded=False)
        flash("Incorrect email or password.", "error")
    return render_template("login.html")


@web_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if not current_app.config["STAFF_AUTH_ENABLED"]:
        return redirect(url_for("web.dashboard"))
    if getattr(g, "current_user", None):
        return redirect(url_for("web.create_workspace_page"))
    if not current_app.config["ALLOW_WEB_SIGNUP"]:
        abort(404)
    if request.method == "POST":
        _validate_csrf()
        if SignupThrottle.is_limited(request.remote_addr):
            abort(429, description="too many account-creation attempts; try again later")
        SignupThrottle.record(request.remote_addr)
        try:
            payload = request.form.to_dict()
            if authentication_setup_required():
                # Modern browser onboarding must supply a complete business and
                # warehouse identity. The workspace_name-only branch remains
                # for the legacy setup contract and CLI-created audit actor.
                if payload.get("business_name") is not None:
                    placeholder = ensure_default_identity()
                    WorkspaceService.validate_identity(
                        payload, existing_workspace=placeholder.workspace
                    )
                actor = UserService.bootstrap_admin(payload)
                workspace = actor.workspace
            else:
                workspace, actor = WorkspaceService.create_for_user(payload)
            raw, _ = AuthTokenService.verification(actor)
            link = url_for("web.verify_email", token=raw, _external=True)
            sent = _send_auth_link(
                recipient=actor.email,
                subject="Verify your StockPilot email",
                link=link,
                explanation=f"Verify your email to finish setting up {workspace.name}.",
            )
            _complete_login(actor, workspace.id)
            if not sent and current_app.config.get("APP_ENV") != "production":
                flash(f"Development verification link: {link}", "success")
            elif not sent and current_app.config.get("REQUIRE_EMAIL_VERIFICATION"):
                flash(
                    "Your account was created, but the verification email could not be sent. "
                    "Use Send a fresh link to try again.",
                    "error",
                )
            flash("Your business workspace and primary warehouse are ready.", "success")
            if current_app.config.get("REQUIRE_EMAIL_VERIFICATION"):
                return redirect(url_for("web.verification_pending"))
            return redirect(url_for("web.dashboard"))
        except (
            AuthenticationError,
            UserValidationError,
            WorkspaceValidationError,
        ) as error:
            db.session.rollback()
            flash(str(error), "error")
    return render_template("signup.html")


@web_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@web_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        _validate_csrf()
        email = str(request.form.get("email") or "").strip().lower()
        if AuthActionThrottle.is_limited(
            request.remote_addr, "password-reset", email
        ):
            abort(429, description="too many password-reset requests; try again later")
        AuthActionThrottle.record(request.remote_addr, "password-reset", email)
        user = User.query.filter_by(email=email, is_active=True).first()
        if user:
            raw, record = AuthTokenService.password_reset(
                user, replace_existing=False
            )
            link = url_for("web.reset_password", token=raw, _external=True)
            sent = _send_auth_link(
                recipient=user.email,
                subject="Reset your StockPilot password",
                link=link,
                explanation="Use this single-use link to reset your StockPilot password.",
            )
            if sent:
                AuthTokenService.activate_replacement(record)
            elif current_app.config.get("APP_ENV") != "production":
                AuthTokenService.activate_replacement(record)
                flash(f"Development reset link: {link}", "success")
            else:
                AuthTokenService.discard(record)
        flash("If that account exists, a password-reset link has been sent.", "success")
        return redirect(url_for("web.login"))
    return render_template("forgot_password.html")


@web_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    record = AuthTokenService.resolve(token, "password_reset")
    if record is None or record.user is None:
        return render_template("auth_token_invalid.html", purpose="password reset"), 400
    if request.method == "POST":
        _validate_csrf()
        password = str(request.form.get("password") or "")
        confirmation = str(request.form.get("password_confirm") or "")
        if len(password) < 10 or len(password) > 128:
            flash("Password must be between 10 and 128 characters.", "error")
        elif password != confirmation:
            flash("Password confirmation does not match.", "error")
        else:
            record.user.set_password(password)
            now = utcnow()
            for active in AuthToken.query.filter_by(
                user_id=record.user_id, purpose="password_reset", consumed_at=None
            ).all():
                active.consumed_at = now
            db.session.commit()
            flash("Your password has been reset. Sign in with the new password.", "success")
            return redirect(url_for("web.login"))
    return render_template("reset_password.html", token=token)


@web_bp.get("/verify-email/<token>")
def verify_email(token: str):
    record = AuthTokenService.resolve(token, "email_verification")
    if record is None or record.user is None:
        return render_template("auth_token_invalid.html", purpose="verification"), 400
    record.user.email_verified_at = utcnow()
    record.consumed_at = utcnow()
    db.session.commit()
    flash("Email verified. Your account is fully active.", "success")
    return redirect(url_for("web.dashboard") if _session_user() else url_for("web.login"))


@web_bp.get("/verification-pending")
def verification_pending():
    actor = _session_user()
    if actor is None:
        return redirect(url_for("web.login"))
    if actor.email_verified_at:
        return redirect(url_for("web.dashboard"))
    return render_template("verification_pending.html")


@web_bp.post("/verification/resend")
def resend_verification():
    actor = _current_actor()
    throttle_identifier = f"user:{actor.id}"
    if AuthActionThrottle.is_limited(
        request.remote_addr, "verification-resend", throttle_identifier
    ):
        abort(429, description="too many verification requests; try again later")
    AuthActionThrottle.record(
        request.remote_addr, "verification-resend", throttle_identifier
    )
    raw, record = AuthTokenService.verification(actor, replace_existing=False)
    link = url_for("web.verify_email", token=raw, _external=True)
    sent = _send_auth_link(
        recipient=actor.email,
        subject="Verify your StockPilot email",
        link=link,
        explanation="Verify your email to continue using StockPilot.",
    )
    if sent:
        AuthTokenService.activate_replacement(record)
        flash("A fresh verification link has been sent.", "success")
    elif current_app.config.get("APP_ENV") != "production":
        AuthTokenService.activate_replacement(record)
        flash(f"Development verification link: {link}", "success")
    else:
        AuthTokenService.discard(record)
        flash(
            "The verification email could not be sent. Please try again shortly.",
            "error",
        )
    return redirect(url_for("web.verification_pending"))


@web_bp.route("/mfa/challenge", methods=["GET", "POST"])
def mfa_challenge():
    user = db.session.get(User, session.get("preauth_user_id"))
    if user is None or not user.is_active or not user.mfa_enabled_at:
        session.clear()
        return redirect(url_for("web.login"))
    if request.method == "POST":
        _validate_csrf()
        session["mfa_attempts"] = int(session.get("mfa_attempts", 0)) + 1
        if session["mfa_attempts"] > 8:
            session.clear()
            abort(429, description="too many MFA attempts; sign in again")
        if MFAService.verify(user, request.form.get("code")):
            workspace_id = session.get("preauth_workspace_id")
            _complete_login(user, workspace_id)
            return redirect(url_for("web.dashboard"))
        flash("That authenticator or recovery code is incorrect.", "error")
    return render_template("mfa_challenge.html")


@web_bp.route("/security", methods=["GET", "POST"])
def security_page():
    actor = _current_actor()
    recovery_codes = session.pop("new_recovery_codes", None)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "enable_mfa":
            secret = MFAService.decrypt(actor)
            try:
                recovery_codes = MFAService.enable(actor, secret, request.form.get("code"))
                session["new_recovery_codes"] = recovery_codes
                flash("Multi-factor authentication is enabled.", "success")
                return redirect(url_for("web.security_page"))
            except ValueError as error:
                flash(str(error), "error")
        elif action == "disable_mfa":
            if not actor.check_password(str(request.form.get("password") or "")):
                flash("Password is incorrect.", "error")
            elif not MFAService.verify(actor, request.form.get("code")):
                flash("Authenticator or recovery code is incorrect.", "error")
            else:
                actor.mfa_secret_encrypted = None
                actor.mfa_enabled_at = None
                MFARecoveryCode.query.filter_by(user_id=actor.id).delete()
                db.session.commit()
                flash("Multi-factor authentication has been disabled.", "success")
                return redirect(url_for("web.security_page"))
        elif action == "change_password":
            current_password = str(request.form.get("current_password") or "")
            new_password = str(request.form.get("new_password") or "")
            confirmation = str(request.form.get("new_password_confirm") or "")
            if not actor.check_password(current_password):
                flash("Current password is incorrect.", "error")
            elif len(new_password) < 10 or len(new_password) > 128:
                flash("Password must be between 10 and 128 characters.", "error")
            elif new_password != confirmation:
                flash("Password confirmation does not match.", "error")
            else:
                actor.set_password(new_password)
                now = utcnow()
                for active in AuthToken.query.filter_by(
                    user_id=actor.id,
                    purpose="password_reset",
                    consumed_at=None,
                ).all():
                    active.consumed_at = now
                db.session.commit()
                session.clear()
                flash(
                    "Your password has been changed. Sign in again with your new password.",
                    "success",
                )
                return redirect(url_for("web.login"))
    if not actor.mfa_enabled_at and not actor.mfa_secret_encrypted:
        actor.mfa_secret_encrypted = MFAService.encrypt(MFAService.generate_secret())
        db.session.commit()
    secret = MFAService.decrypt(actor) if not actor.mfa_enabled_at else None
    return render_template(
        "security.html",
        secret=secret,
        provisioning_uri=(MFAService.provisioning_uri(actor, secret) if secret else None),
        recovery_codes=recovery_codes,
    )


@web_bp.route("/profile", methods=["GET", "POST"])
def profile_settings_page():
    actor = _current_actor()
    if request.method == "POST":
        previous_email = actor.email
        try:
            UserService.update(
                actor,
                {
                    "name": request.form.get("name"),
                    "email": request.form.get("email"),
                },
                acting_user=actor,
            )
            if actor.email != previous_email:
                actor.email_verified_at = None
                db.session.commit()
                raw_token, _ = AuthTokenService.verification(actor)
                verification_link = url_for(
                    "web.verify_email", token=raw_token, _external=True
                )
                _send_auth_link(
                    recipient=actor.email,
                    subject="Verify your updated StockPilot email",
                    link=verification_link,
                    explanation="Confirm the new email address for your StockPilot profile.",
                )
                flash("Profile updated. Verify your new email address.", "success")
            else:
                flash("Profile settings were updated.", "success")
            return redirect(url_for("web.profile_settings_page"))
        except UserValidationError as error:
            flash(str(error), "error")
    return render_template("profile_settings.html")


@web_bp.route("/workspaces/new", methods=["GET", "POST"])
@roles_required("admin")
def create_workspace_page():
    flash("Business accounts use one workspace. Add warehouses here instead.", "success")
    return redirect(url_for("web.locations_page"))


@web_bp.post("/workspaces/<int:workspace_id>/switch")
def switch_workspace(workspace_id: int):
    abort(404)


@web_bp.route("/workspace/settings", methods=["GET", "POST"])
@roles_required("admin")
def workspace_settings_page():
    actor = _current_actor()
    workspace = g.active_workspace
    settings = workspace.settings or WorkspaceSetting(workspace=workspace)
    integration = WorkspaceIntegration.query.filter_by(
        workspace_id=workspace.id, provider="oidc", name="default"
    ).first()
    if request.method == "POST":
        name = str(request.form.get("business_name") or "").strip()
        username = normalize_business_username(request.form.get("business_username"))
        if not name or len(name) > 255:
            flash("Business name is required and must be 255 characters or fewer.", "error")
        elif len(username) < 3:
            flash("Business username must be at least 3 characters.", "error")
        elif Workspace.query.filter(
            func.lower(Workspace.business_username) == username,
            Workspace.id != workspace.id,
        ).first():
            flash("That business username is already in use.", "error")
        else:
            workspace.name = name
            workspace.business_username = username
            settings.timezone = str(request.form.get("timezone") or "Asia/Kolkata")[:64]
            settings.currency = str(request.form.get("currency") or "INR").upper()[:3]
            db.session.add(settings)
            issuer = str(request.form.get("oidc_issuer") or "").strip().rstrip("/")
            client_id = str(request.form.get("oidc_client_id") or "").strip()
            if issuer or client_id:
                integration = integration or WorkspaceIntegration(
                    workspace=workspace, provider="oidc", name="default"
                )
                default_role = request.form.get("oidc_default_role", "picker")
                if default_role not in ROLES:
                    default_role = "picker"
                integration.config_json = {
                    "issuer": issuer,
                    "client_id": client_id,
                    "auto_provision": "oidc_auto_provision" in request.form,
                    "default_role": default_role,
                }
                integration.secret_reference = str(
                    request.form.get("oidc_secret_reference") or ""
                ).strip()[:255]
                integration.is_active = "oidc_enabled" in request.form
                db.session.add(integration)
            elif integration:
                integration.is_active = False
            db.session.commit()
            flash("Business settings were updated.", "success")
            return redirect(url_for("web.workspace_settings_page"))
    return render_template(
        "workspace_settings.html",
        workspace=workspace,
        settings=settings,
        integration=integration,
    )


@web_bp.post("/workspace/invitations")
@roles_required("admin")
def invite_workspace_user():
    actor = _current_actor()
    try:
        raw, invitation = WorkspaceService.invite(
            workspace=g.active_workspace,
            acting_user=actor,
            email=request.form.get("email"),
            role=request.form.get("role"),
        )
        link = url_for("web.accept_invitation", token=raw, _external=True)
        sent = _send_auth_link(
            recipient=invitation.email,
            subject=f"You’re invited to {g.active_workspace.name} on StockPilot",
            link=link,
            explanation=(
                f"An administrator invited you as "
                f"{invitation.payload_json.get('role', 'picker').title()}."
            ),
        )
        if sent:
            AuthTokenService.activate_replacement(invitation)
        elif current_app.config.get("APP_ENV") != "production":
            AuthTokenService.activate_replacement(invitation)
            flash(f"Development invitation link: {link}", "success")
        else:
            AuthTokenService.discard(invitation)
        if sent or current_app.config.get("APP_ENV") != "production":
            flash(f"Invitation issued to {invitation.email}.", "success")
        else:
            flash("The invitation email could not be sent.", "error")
    except WorkspaceValidationError as error:
        flash(str(error), "error")
    return redirect(url_for("web.users_page"))


@web_bp.route("/invitations/<token>", methods=["GET", "POST"])
def accept_invitation(token: str):
    record = AuthTokenService.resolve(token, "invitation")
    if record is None or record.workspace is None:
        return render_template("auth_token_invalid.html", purpose="invitation"), 400
    actor = _session_user()
    if request.method == "POST":
        _validate_csrf()
        try:
            user = WorkspaceService.accept_invitation(
                record, request.form.to_dict(), current_user=actor
            )
            _complete_login(user, record.workspace_id)
            flash(f"You now have access to {record.workspace.name}.", "success")
            return redirect(url_for("web.dashboard"))
        except (WorkspaceValidationError, UserValidationError) as error:
            flash(str(error), "error")
    return render_template("accept_invitation.html", invitation=record, token=token, actor=actor)


def _oidc_client(workspace: Workspace, integration: WorkspaceIntegration):
    try:
        from authlib.integrations.flask_client import OAuth
    except ImportError as error:
        raise RuntimeError("Authlib must be installed to use SSO") from error
    config = integration.config_json or {}
    issuer = str(config.get("issuer") or "").rstrip("/")
    client_id = str(config.get("client_id") or "")
    client_secret = oidc_secret(integration)
    if not issuer or not client_id or not client_secret:
        raise RuntimeError("SSO issuer, client ID, or secret reference is incomplete")
    oauth = current_app.extensions.get("stockpilot_oauth")
    if oauth is None:
        oauth = OAuth(current_app)
        current_app.extensions["stockpilot_oauth"] = oauth
    name = f"workspace_{workspace.id}"
    client = oauth.create_client(name)
    if client is None:
        client = oauth.register(
            name=name,
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url=f"{issuer}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    return client


@web_bp.get("/auth/sso/<business_username>")
def sso_login(business_username: str):
    if not current_app.config.get("OIDC_ENABLED"):
        abort(404)
    workspace = _workspace_from_username(business_username)
    integration = (
        WorkspaceIntegration.query.filter_by(
            workspace_id=workspace.id if workspace else -1,
            provider="oidc",
            name="default",
            is_active=True,
        ).first()
    )
    if workspace is None or integration is None:
        flash("SSO is not configured for that business.", "error")
        return redirect(url_for("web.login"))
    try:
        client = _oidc_client(workspace, integration)
        session["sso_workspace_id"] = workspace.id
        return client.authorize_redirect(url_for("web.sso_callback", _external=True))
    except RuntimeError as error:
        current_app.logger.warning("SSO initiation failed: %s", error)
        flash("SSO is temporarily unavailable for this business.", "error")
        return redirect(url_for("web.login"))


@web_bp.get("/auth/sso/callback")
def sso_callback():
    workspace = db.session.get(Workspace, session.get("sso_workspace_id"))
    integration = WorkspaceIntegration.query.filter_by(
        workspace_id=workspace.id if workspace else -1,
        provider="oidc",
        name="default",
        is_active=True,
    ).first()
    if workspace is None or integration is None:
        abort(400, description="SSO session expired")
    try:
        client = _oidc_client(workspace, integration)
        token = client.authorize_access_token()
        userinfo = token.get("userinfo") or client.userinfo(token=token)
    except Exception as error:
        current_app.logger.warning("SSO callback failed: %s", error)
        flash("SSO sign-in could not be completed.", "error")
        return redirect(url_for("web.login"))
    email = str(userinfo.get("email") or "").strip().lower()
    if not email or userinfo.get("email_verified") is False:
        abort(403, description="SSO provider did not return a verified email")
    user = User.query.filter_by(email=email).first()
    membership = (
        WorkspaceMembership.query.filter_by(
            workspace_id=workspace.id, user_id=user.id, is_active=True
        ).first()
        if user
        else None
    )
    config = integration.config_json or {}
    if membership is None and config.get("auto_provision"):
        if user is None:
            user = User(
                workspace=workspace,
                name=str(userinfo.get("name") or email.split("@")[0])[:255],
                email=email,
                role=str(config.get("default_role") or "picker"),
                is_active=True,
                email_verified_at=utcnow(),
            )
            db.session.add(user)
            db.session.flush()
        membership = WorkspaceMembership(
            workspace=workspace,
            user=user,
            role=str(config.get("default_role") or "picker"),
            is_active=True,
        )
        db.session.add(membership)
        db.session.commit()
    if user is None or membership is None:
        abort(403, description="This SSO account has not been invited")
    _complete_login(user, workspace.id)
    return redirect(url_for("web.dashboard"))


@web_bp.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users_page():
    actor = _current_actor()
    if request.method == "POST":
        try:
            user = UserService.create(
                request.form.to_dict(), workspace=g.active_workspace
            )
            flash(f"{user.name} was added as {ROLE_LABELS[user.role]}.", "success")
            return redirect(url_for("web.users_page"))
        except UserValidationError as error:
            flash(str(error), "error")
    membership_query = (
        WorkspaceMembership.query.filter_by(workspace_id=actor.workspace_id)
        .join(User)
        .options(selectinload(WorkspaceMembership.user))
    )
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip().lower()
    if q:
        membership_query = membership_query.filter(
            or_(User.name.ilike(f"%{q}%"), User.email.ilike(f"%{q}%"))
        )
    if role in ROLES:
        membership_query = membership_query.filter(WorkspaceMembership.role == role)
    memberships, pagination = _cursor_page(
        membership_query,
        WorkspaceMembership,
        kind="memberships",
        workspace_id=actor.workspace_id,
    )
    return render_template(
        "users.html",
        memberships=memberships,
        pagination=pagination,
        q=q,
        role_filter=role,
        invitations=AuthToken.query.filter_by(
            workspace_id=actor.workspace_id,
            purpose="invitation",
            consumed_at=None,
        ).order_by(AuthToken.created_at.desc()).limit(20).all(),
        roles=ROLE_LABELS,
    )


@web_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_user_form(user_id: int):
    actor = _current_actor()
    user = db.get_or_404(User, user_id)
    membership = WorkspaceMembership.query.filter_by(
        user_id=user.id, workspace_id=actor.workspace_id
    ).first()
    if membership is None:
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
    return render_template(
        "user_form.html", user=user, membership=membership, roles=ROLE_LABELS
    )


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
    actor = _current_actor()
    include_archived = request.args.get("include_archived", "").lower() in {
        "1",
        "true",
        "yes",
    }
    query = Product.query.filter_by(workspace_id=actor.workspace_id).options(
        selectinload(Product.stock_levels).selectinload(StockLevel.location),
        selectinload(Product.preferred_supplier),
    )
    if not include_archived:
        query = query.filter_by(is_active=True)
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(
            or_(
                Product.name.ilike(f"%{q}%"),
                Product.sku.ilike(f"%{q}%"),
                Product.barcode.ilike(f"%{q}%"),
                Product.category.ilike(f"%{q}%"),
            )
        )
    products, pagination = _cursor_page(
        query, Product, kind="products", workspace_id=actor.workspace_id
    )
    return render_template(
        "products.html",
        products=products,
        pagination=pagination,
        q=q,
        include_archived=include_archived,
    )


@web_bp.get("/manage")
@roles_required("admin", "manager")
def manage_page():
    actor = _current_actor()
    catalogue_limit = current_app.config["MAX_PAGE_SIZE"]
    return render_template(
        "manage.html",
        products=Product.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        ).order_by(Product.name).limit(catalogue_limit).all(),
        suppliers=Supplier.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        )
        .order_by(Supplier.name)
        .limit(catalogue_limit)
        .all(),
        locations=InventoryLocation.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        )
        .order_by(InventoryLocation.name)
        .limit(catalogue_limit)
        .all(),
        bins=Bin.query.join(InventoryLocation).filter(
            InventoryLocation.workspace_id == actor.workspace_id,
            Bin.is_active.is_(True),
        ).order_by(Bin.code).limit(catalogue_limit).all(),
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
        else url_for("web.purchase_orders_page", _anchor="catalogue-step")
        if target == "purchasing"
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
    query = Supplier.query.filter_by(workspace_id=actor.workspace_id).options(
        selectinload(Supplier.products)
    )
    if not include_inactive:
        query = query.filter_by(is_active=True)
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(
            or_(
                Supplier.name.ilike(f"%{q}%"),
                Supplier.contact_email.ilike(f"%{q}%"),
                Supplier.contact_phone.ilike(f"%{q}%"),
            )
        )
    suppliers, pagination = _cursor_page(
        query, Supplier, kind="suppliers", workspace_id=actor.workspace_id
    )
    return render_template(
        "suppliers.html",
        suppliers=suppliers,
        pagination=pagination,
        q=q,
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
@web_bp.post("/warehouses")
@roles_required("admin")
def add_location_form():
    try:
        location = LocationService.create(request.form.to_dict(), actor=_current_actor())
        flash(f"{location.name} was added.", "success")
    except LocationValidationError as error:
        flash(str(error), "error")
    return redirect(url_for("web.locations_page"))


@web_bp.get("/locations")
@web_bp.get("/warehouses")
def locations_page():
    actor = _current_actor()
    query = InventoryLocation.query.filter_by(
        workspace_id=actor.workspace_id
    ).options(selectinload(InventoryLocation.bins))
    q = request.args.get("q", "").strip()
    state = request.args.get("state", "all").strip().lower()
    if q:
        query = query.filter(
            or_(
                InventoryLocation.name.ilike(f"%{q}%"),
                InventoryLocation.code.ilike(f"%{q}%"),
                InventoryLocation.address.ilike(f"%{q}%"),
            )
        )
    if state in {"active", "inactive"}:
        query = query.filter_by(is_active=state == "active")
    locations, pagination = _cursor_page(
        query,
        InventoryLocation,
        kind="locations",
        workspace_id=actor.workspace_id,
    )
    return render_template(
        "locations.html",
        locations=locations,
        warehouse_stats={row.id: serialize_location(row) for row in locations},
        pagination=pagination,
        q=q,
        state_filter=state,
    )


@web_bp.route("/locations/<int:location_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_location_form(location_id: int):
    location = _location_for_actor(location_id)
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
@roles_required("admin")
def add_bin_form(location_id: int):
    location = _location_for_actor(location_id)
    try:
        bin_record = BinService.create(location, request.form.to_dict())
        flash(f"Bin {location.code}/{bin_record.code} was added.", "success")
    except LocationValidationError as error:
        flash(str(error), "error")
    return redirect(url_for("web.locations_page"))


@web_bp.route("/bins/<int:bin_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_bin_form(bin_id: int):
    bin_record = _bin_for_actor(bin_id)
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
    return_to = payload.pop("return_to", "")
    payload["is_perishable"] = "is_perishable" in request.form
    try:
        product = ProductService.create(
            payload, workspace_id=_current_actor().workspace_id
        )
        flash(
            f"{product.name} was added. It is ready to include in a purchase order."
            if return_to == "purchasing"
            else f"{product.name} was added. Add its starting stock below.",
            "success",
        )
    except ProductValidationError as error:
        flash(str(error), "error")
    return redirect(
        url_for("web.purchase_orders_page", _anchor="catalogue-step")
        if return_to == "purchasing"
        else url_for("web.manage_page")
    )


@web_bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "manager")
def edit_product_form(product_id: int):
    product = _product_for_actor(product_id)
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
        .limit(current_app.config["MAX_PAGE_SIZE"])
        .all(),
    )


@web_bp.post("/products/<int:product_id>/archive")
@roles_required("admin", "manager")
def archive_product_form(product_id: int):
    product = _product_for_actor(product_id)
    ProductService.archive(product)
    flash(f"{product.name} was archived. Its history and stock were preserved.", "success")
    return redirect(url_for("web.products_page", include_archived=1))


@web_bp.post("/products/archive-selected")
@roles_required("admin", "manager")
def archive_selected_products_form():
    actor = _current_actor()
    selected_ids: set[int] = set()
    for raw_id in request.form.getlist("product_ids"):
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if product_id > 0:
            selected_ids.add(product_id)

    if not selected_ids:
        flash("Select at least one active product to remove.", "error")
        return redirect(url_for("web.products_page"))

    products = (
        Product.query.filter(
            Product.workspace_id == actor.workspace_id,
            Product.id.in_(selected_ids),
            Product.is_active.is_(True),
        )
        .order_by(Product.name, Product.id)
        .all()
    )
    if not products:
        flash("None of the selected products are active in this business.", "error")
        return redirect(url_for("web.products_page"))

    for product in products:
        ProductService.archive(product, commit=False)
    db.session.commit()

    count = len(products)
    flash(
        f"{count} product{'s' if count != 1 else ''} removed from active inventory. "
        "Their stock and history were preserved.",
        "success",
    )
    return redirect(url_for("web.products_page"))


@web_bp.post("/products/<int:product_id>/restore")
@roles_required("admin", "manager")
def restore_product_form(product_id: int):
    product = _product_for_actor(product_id)
    ProductService.restore(product)
    flash(f"{product.name} was restored.", "success")
    return redirect(url_for("web.products_page", include_archived=1))


@web_bp.post("/manage/products/import")
@roles_required("admin", "manager")
def import_products_form():
    return_to = request.form.get("return_to", "")
    return_url = (
        url_for("web.purchase_orders_page", _anchor="catalogue-step")
        if return_to == "purchasing"
        else url_for("web.manage_page")
    )
    try:
        content = _uploaded_csv_bytes()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(return_url)
    result = ProductCSVImporter.import_bytes(
        content,
        update_existing="update_existing" in request.form,
        max_rows=current_app.config["MAX_PRODUCT_CSV_ROWS"],
        workspace_id=_current_actor().workspace_id,
        actor=_current_actor(),
    )
    if result.committed:
        flash(
            f"CSV import complete: {result.created} products created, "
            f"{result.updated} product details updated, and "
            f"{result.inventory_updated} inventory balances updated.",
            "success",
        )
    else:
        preview = "; ".join(
            f"row {item['row']}: "
            + ", ".join(f"{field} {message}" for field, message in item["errors"].items())
            for item in result.errors[:5]
        )
        suffix = "" if len(result.errors) <= 5 else f"; plus {len(result.errors) - 5} more"
        flash(
            f"CSV import failed; nothing was imported. {preview}{suffix}", "error"
        )
    return redirect(return_url)


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
    actor = _current_actor()
    query = StockTransfer.query.filter_by(workspace_id=actor.workspace_id).options(
        selectinload(StockTransfer.product),
        selectinload(StockTransfer.source_location),
        selectinload(StockTransfer.destination_location),
        selectinload(StockTransfer.source_bin),
        selectinload(StockTransfer.destination_bin),
        selectinload(StockTransfer.user),
    )
    q = request.args.get("q", "").strip()
    if q:
        query = query.join(Product).filter(
            or_(
                Product.name.ilike(f"%{q}%"),
                Product.sku.ilike(f"%{q}%"),
                StockTransfer.transfer_uid.ilike(f"%{q}%"),
                StockTransfer.external_id.ilike(f"%{q}%"),
            )
        )
    transfers, pagination = _cursor_page(
        query,
        StockTransfer,
        kind="transfers",
        workspace_id=actor.workspace_id,
        descending=True,
    )
    return render_template(
        "transfers.html",
        products=Product.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        ).order_by(Product.name).limit(current_app.config["MAX_PAGE_SIZE"]).all(),
        locations=InventoryLocation.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        )
        .order_by(InventoryLocation.name)
        .limit(current_app.config["MAX_PAGE_SIZE"])
        .all(),
        transfers=transfers,
        pagination=pagination,
        q=q,
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
    query = SalesOrder.query.filter_by(workspace_id=actor.workspace_id).options(
        selectinload(SalesOrder.items),
        selectinload(SalesOrder.location),
        selectinload(SalesOrder.created_by),
    )
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip().lower()
    if q:
        query = query.filter(
            or_(
                SalesOrder.customer_reference.ilike(f"%{q}%"),
                SalesOrder.order_uid.ilike(f"%{q}%"),
                SalesOrder.external_id.ilike(f"%{q}%"),
            )
        )
    if status in {"pending", "picking", "packed", "shipped", "cancelled"}:
        query = query.filter_by(status=status)
    orders, pagination = _cursor_page(
        query,
        SalesOrder,
        kind="sales-orders",
        workspace_id=actor.workspace_id,
        descending=True,
    )
    return render_template(
        "orders.html",
        orders=orders,
        pagination=pagination,
        q=q,
        status_filter=status,
        products=Product.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        ).order_by(Product.name).limit(current_app.config["MAX_PAGE_SIZE"]).all(),
        locations=InventoryLocation.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        )
        .order_by(InventoryLocation.name)
        .limit(current_app.config["MAX_PAGE_SIZE"])
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
    query = ReturnAuthorization.query.filter_by(
        workspace_id=actor.workspace_id
    ).options(
        selectinload(ReturnAuthorization.sales_order),
        selectinload(ReturnAuthorization.created_by),
    )
    if status in allowed_statuses:
        query = query.filter_by(status=status)
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(
            or_(
                ReturnAuthorization.rma_uid.ilike(f"%{q}%"),
                ReturnAuthorization.external_id.ilike(f"%{q}%"),
                ReturnAuthorization.reason_code.ilike(f"%{q}%"),
            )
        )
    returns, pagination = _cursor_page(
        query,
        ReturnAuthorization,
        kind="returns",
        workspace_id=actor.workspace_id,
        descending=True,
    )
    return render_template(
        "returns.html",
        returns=returns,
        pagination=pagination,
        q=q,
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
        .limit(current_app.config["MAX_PAGE_SIZE"])
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
@roles_required("admin", "manager", "viewer")
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
@roles_required("admin", "manager", "viewer")
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


@web_bp.get("/templates/products-import.csv")
@roles_required("admin", "manager")
def download_products_import_template():
    return send_file(
        BytesIO(ProductCSVImporter.template_bytes()),
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="StockPilot-product-import-template.csv",
        max_age=0,
    )


@web_bp.post("/reports/alerts/critical")
@roles_required("admin", "manager")
def send_critical_alert_form():
    actor = _current_actor()
    results = ForecastService.run(workspace_id=actor.workspace_id)
    delivery = ReportMailer.send_critical_alerts(
        results, workspace_id=actor.workspace_id
    )
    flash(delivery.reason, "success" if delivery.sent else "error")
    return redirect(url_for("web.reports_page"))


@web_bp.get("/purchase-orders")
@roles_required("admin", "manager")
def purchase_orders_page():
    actor = _current_actor()
    query = PurchaseOrder.query.filter_by(workspace_id=actor.workspace_id).options(
        selectinload(PurchaseOrder.supplier),
        selectinload(PurchaseOrder.location),
        selectinload(PurchaseOrder.items).selectinload(PurchaseOrderItem.product),
    )
    status = request.args.get("status", "").strip().lower()
    allowed_statuses = {
        "draft", "pending_approval", "approved", "partially_received", "received", "cancelled"
    }
    if status in allowed_statuses:
        query = query.filter_by(status=status)
    q = request.args.get("q", "").strip()
    if q:
        query = query.join(Supplier).join(InventoryLocation).filter(
            or_(
                PurchaseOrder.po_uid.ilike(f"%{q}%"),
                PurchaseOrder.external_id.ilike(f"%{q}%"),
                Supplier.name.ilike(f"%{q}%"),
                InventoryLocation.code.ilike(f"%{q}%"),
            )
        )
    orders, pagination = _cursor_page(
        query,
        PurchaseOrder,
        kind="purchase-orders",
        workspace_id=actor.workspace_id,
        descending=True,
    )
    open_po_total = PurchaseOrder.query.filter(
        PurchaseOrder.workspace_id == actor.workspace_id,
        PurchaseOrder.status.in_(("draft", "pending_approval", "approved", "partially_received")),
    ).count()
    return render_template(
        "purchase_orders.html",
        orders=orders,
        pagination=pagination,
        q=q,
        selected_status=status if status in allowed_statuses else "",
        open_po_total=open_po_total,
        suppliers=Supplier.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        ).order_by(Supplier.name).limit(current_app.config["MAX_PAGE_SIZE"]).all(),
        locations=InventoryLocation.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        ).order_by(InventoryLocation.name).limit(current_app.config["MAX_PAGE_SIZE"]).all(),
        products=Product.query.filter_by(
            workspace_id=actor.workspace_id, is_active=True
        ).order_by(Product.name).limit(current_app.config["MAX_PAGE_SIZE"]).all(),
        bins=Bin.query.join(InventoryLocation).filter(
            InventoryLocation.workspace_id == actor.workspace_id,
            InventoryLocation.is_active.is_(True),
            Bin.is_active.is_(True),
        ).order_by(InventoryLocation.code, Bin.code).limit(
            current_app.config["MAX_PAGE_SIZE"]
        ).all(),
        recommendations=inventory_recommendations(workspace_id=actor.workspace_id),
        accuracy=ForecastAccuracyService.summary(workspace_id=actor.workspace_id),
    )


@web_bp.post("/purchase-orders")
@roles_required("admin", "manager")
def create_purchase_order_form():
    try:
        order = PurchaseOrderService.create(
            {
                "supplier_id": request.form.get("supplier_id"),
                "location_id": request.form.get("location_id"),
                "expected_at": request.form.get("expected_at"),
                "note": request.form.get("note"),
                "items": [
                    {
                        "sku": request.form.get("sku"),
                        "quantity": request.form.get("quantity"),
                        "unit": request.form.get("unit"),
                        "unit_cost": request.form.get("unit_cost"),
                    }
                ],
            },
            actor=_current_actor(),
        )
        flash(f"Draft purchase order {order.po_uid[:8]} was created.", "success")
    except ProcurementError as error:
        flash(str(error), "error")
    return redirect(url_for("web.purchase_orders_page"))


@web_bp.post("/purchase-orders/ai-draft")
@roles_required("admin", "manager")
def generate_purchase_order_drafts_form():
    try:
        orders = PurchaseOrderService.draft_from_recommendations(actor=_current_actor())
        if orders:
            flash(f"Created {len(orders)} AI-assisted purchase-order draft(s).", "success")
        else:
            flash("No forecast recommendations currently qualify for a draft.", "error")
    except ProcurementError as error:
        flash(str(error), "error")
    return redirect(url_for("web.purchase_orders_page"))


@web_bp.post("/purchase-orders/<int:order_id>/approve")
@roles_required("admin", "manager")
def approve_purchase_order_form(order_id: int):
    try:
        PurchaseOrderService.approve(
            _purchase_order_for_actor(order_id), actor=_current_actor()
        )
        flash("Purchase order approved and ready to receive.", "success")
    except ProcurementError as error:
        flash(str(error), "error")
    return redirect(url_for("web.purchase_orders_page"))


@web_bp.post("/purchase-orders/<int:order_id>/receive")
@roles_required("admin", "manager")
def receive_purchase_order_form(order_id: int):
    try:
        _, created = PurchaseOrderService.receive(
            _purchase_order_for_actor(order_id),
            {
                "external_receipt_id": request.form.get("external_receipt_id")
                or f"WEB-{uuid4()}",
                "items": [
                    {
                        "item_id": request.form.get("item_id"),
                        "quantity": request.form.get("quantity"),
                        "unit": request.form.get("unit"),
                        "bin_code": request.form.get("bin_code"),
                        "lot_number": request.form.get("lot_number"),
                        "manufactured_at": request.form.get("manufactured_at"),
                        "expiry_date": request.form.get("expiry_date"),
                    }
                ],
            },
            actor=_current_actor(),
        )
        flash("Receipt posted." if created else "Receipt was already posted.", "success")
    except (ProcurementError, ProcurementConflictError, ProcurementStateError) as error:
        flash(str(error), "error")
    return redirect(url_for("web.purchase_orders_page"))


@web_bp.post("/products/<int:product_id>/unit-conversions")
@roles_required("admin", "manager")
def create_unit_conversion_form(product_id: int):
    actor = _current_actor()
    product = Product.query.filter_by(
        id=product_id, workspace_id=actor.workspace_id
    ).first_or_404()
    try:
        UnitConversionService.define(
            product,
            request.form.get("unit_code"),
            request.form.get("to_base_factor"),
            actor=actor,
        )
        flash("Unit conversion saved.", "success")
    except ProcurementError as error:
        flash(str(error), "error")
    return redirect(url_for("web.purchase_orders_page"))


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


@api_bp.get("/workspaces/availability")
def workspace_username_availability():
    username = normalize_business_username(request.args.get("username"))
    valid = bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", username))
    available = valid and Workspace.query.filter(
        func.lower(Workspace.business_username) == username
    ).first() is None
    return jsonify(
        {"username": username, "valid": valid, "available": available}
    )


@web_bp.get("/favicon.ico")
def favicon():
    return send_from_directory(
        current_app.static_folder, "stockpilot-icon.svg", mimetype="image/svg+xml"
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
    actor = _current_actor()
    warehouse_id = request.args.get("warehouse_id", type=int)
    if warehouse_id is not None and InventoryLocation.query.filter_by(
        id=warehouse_id, workspace_id=actor.workspace_id
    ).first() is None:
        abort(404)
    return jsonify(_dashboard_data(warehouse_id))


@api_bp.get("/locations")
@api_read_access
def locations_api():
    actor = _current_actor()
    include_inactive = request.args.get("include_inactive", "").lower() in {
        "1",
        "true",
        "yes",
    }
    query = InventoryLocation.query.filter_by(workspace_id=actor.workspace_id)
    if not include_inactive:
        query = query.filter_by(is_active=True)
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(
            or_(
                InventoryLocation.name.ilike(f"%{q}%"),
                InventoryLocation.code.ilike(f"%{q}%"),
                InventoryLocation.address.ilike(f"%{q}%"),
            )
        )
    locations, pagination = _cursor_page(
        query,
        InventoryLocation,
        kind="api-locations",
        workspace_id=actor.workspace_id,
    )
    return jsonify(
        {
            "locations": [serialize_location(location) for location in locations],
            "pagination": pagination,
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
            _location_for_actor(location_id),
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
            _location_for_actor(location_id),
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
            _bin_for_actor(bin_id), request.get_json(silent=True) or {}
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
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(
            or_(
                Supplier.name.ilike(f"%{q}%"),
                Supplier.contact_email.ilike(f"%{q}%"),
                Supplier.contact_phone.ilike(f"%{q}%"),
            )
        )
    suppliers, pagination = _cursor_page(
        query, Supplier, kind="api-suppliers", workspace_id=actor.workspace_id
    )
    return jsonify(
        {
            "suppliers": [serialize_supplier(supplier) for supplier in suppliers],
            "pagination": pagination,
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
    query = Product.query.filter_by(workspace_id=actor.workspace_id).options(
        selectinload(Product.stock_levels).selectinload(StockLevel.location),
        selectinload(Product.stock_levels).selectinload(StockLevel.bin),
        selectinload(Product.preferred_supplier),
        selectinload(Product.unit_conversions),
    )
    if not include_archived:
        query = query.filter_by(is_active=True)
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(
            or_(
                Product.name.ilike(f"%{q}%"),
                Product.sku.ilike(f"%{q}%"),
                Product.barcode.ilike(f"%{q}%"),
                Product.category.ilike(f"%{q}%"),
            )
        )
    products, pagination = _cursor_page(
        query, Product, kind="api-products", workspace_id=actor.workspace_id
    )
    return jsonify(
        {
            "products": [
                serialize_product(
                    product,
                    include_sensitive=actor.role != "picker",
                )
                for product in products
            ],
            "pagination": pagination,
        }
    )


@api_bp.get("/products/<int:product_id>")
@api_read_access
def product_api(product_id: int):
    actor = _current_actor()
    return jsonify(
        {
            "product": serialize_product(
                _product_for_actor(product_id),
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

    actor = _current_actor()
    product = Product.query.filter_by(
        workspace_id=actor.workspace_id, barcode=code, is_active=True
    ).first()
    matched_by = "barcode"
    if product is None:
        product = Product.query.filter_by(
            workspace_id=actor.workspace_id, sku=code.upper(), is_active=True
        ).first()
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
    product = _product_for_actor(product_id)
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
    product = ProductService.archive(_product_for_actor(product_id))
    return jsonify({"product": serialize_product(product)})


@api_bp.post("/products/<int:product_id>/restore")
def restore_product(product_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    product = ProductService.restore(_product_for_actor(product_id))
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
        actor=_current_actor(),
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
    actor = _current_actor()
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    transfers = StockTransfer.query.filter_by(
        workspace_id=actor.workspace_id
    ).order_by(StockTransfer.created_at.desc()).limit(limit)
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
    q = request.args.get("q", "").strip()
    if q:
        query = query.filter(
            or_(
                SalesOrder.customer_reference.ilike(f"%{q}%"),
                SalesOrder.order_uid.ilike(f"%{q}%"),
                SalesOrder.external_id.ilike(f"%{q}%"),
            )
        )
    orders, pagination = _cursor_page(
        query,
        SalesOrder,
        kind="api-sales-orders",
        workspace_id=actor.workspace_id,
        descending=True,
    )
    return jsonify(
        {"orders": [serialize_order(order) for order in orders], "pagination": pagination}
    )


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
    actor = _current_actor()
    limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
    query = InventoryMovement.query.join(InventoryLocation).filter(
        InventoryLocation.workspace_id == actor.workspace_id
    )
    movement_type = request.args.get("movement_type", "").strip()
    if movement_type:
        query = query.filter(InventoryMovement.movement_type == movement_type)
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


@api_bp.get("/purchase-orders")
@api_read_access
def purchase_orders_api():
    actor = _current_actor()
    query = PurchaseOrder.query.filter_by(workspace_id=actor.workspace_id)
    status = request.args.get("status", "").strip().lower()
    if status:
        query = query.filter_by(status=status)
    rows = query.order_by(
        PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc()
    ).limit(200).all()
    return jsonify({"purchase_orders": [serialize_purchase_order(row) for row in rows]})


@api_bp.post("/purchase-orders")
def create_purchase_order_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        order = PurchaseOrderService.create(
            request.get_json(silent=True) or {}, actor=_current_actor()
        )
    except ProcurementConflictError as error:
        return jsonify({"error": str(error)}), 409
    except ProcurementError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"purchase_order": serialize_purchase_order(order)}), 201


@api_bp.post("/purchase-orders/ai-drafts")
def generate_purchase_order_drafts_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        orders = PurchaseOrderService.draft_from_recommendations(actor=_current_actor())
    except ProcurementError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(
        {"purchase_orders": [serialize_purchase_order(order) for order in orders]}
    ), 201 if orders else 200


@api_bp.get("/purchase-orders/<int:order_id>")
@api_read_access
def purchase_order_api(order_id: int):
    return jsonify(
        {"purchase_order": serialize_purchase_order(_purchase_order_for_actor(order_id))}
    )


@api_bp.post("/purchase-orders/<int:order_id>/approve")
def approve_purchase_order_api(order_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        order = PurchaseOrderService.approve(
            _purchase_order_for_actor(order_id), actor=_current_actor()
        )
    except ProcurementStateError as error:
        return jsonify({"error": str(error)}), 409
    except ProcurementError as error:
        return jsonify({"error": str(error)}), 403
    return jsonify({"purchase_order": serialize_purchase_order(order)})


@api_bp.post("/purchase-orders/<int:order_id>/receipts")
def receive_purchase_order_api(order_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    try:
        receipt, created = PurchaseOrderService.receive(
            _purchase_order_for_actor(order_id),
            request.get_json(silent=True) or {},
            actor=_current_actor(),
        )
    except (ProcurementStateError, ProcurementConflictError) as error:
        return jsonify({"error": str(error)}), 409
    except ProcurementError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"receipt": serialize_receipt(receipt), "created": created}), 201 if created else 200


@api_bp.post("/products/<int:product_id>/unit-conversions")
def create_unit_conversion_api(product_id: int):
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    actor = _current_actor()
    try:
        conversion = UnitConversionService.define(
            _product_for_actor(product_id),
            (request.get_json(silent=True) or {}).get("unit_code"),
            (request.get_json(silent=True) or {}).get("to_base_factor"),
            actor=actor,
        )
    except ProcurementError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(
        {
            "conversion": {
                "id": conversion.id,
                "product_id": conversion.product_id,
                "unit_code": conversion.unit_code,
                "to_base_factor": number_for_json(conversion.to_base_factor),
                "base_unit": conversion.product.unit_of_measure,
            }
        }
    ), 201


@api_bp.get("/recommendations/inventory")
@api_read_access
def inventory_recommendations_api():
    return jsonify(
        inventory_recommendations(workspace_id=_current_actor().workspace_id)
    )


@api_bp.post("/forecast-accuracy/evaluate")
def evaluate_forecast_accuracy_api():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    actor = _current_actor()
    outcomes = ForecastAccuracyService.evaluate(workspace_id=actor.workspace_id)
    return jsonify(
        {
            "evaluated": len(outcomes),
            "summary": ForecastAccuracyService.summary(
                workspace_id=actor.workspace_id
            ),
        }
    )


@api_bp.get("/forecast-accuracy")
@api_read_access
def forecast_accuracy_api():
    return jsonify(
        ForecastAccuracyService.summary(workspace_id=_current_actor().workspace_id)
    )


@api_bp.post("/assistant/chat")
@session_api_access
def dashboard_chat_api():
    payload = request.get_json(silent=True) or {}
    try:
        conversation, response = DashboardChatService.ask(
            payload.get("question"),
            actor=_current_actor(),
            conversation_id=payload.get("conversation_id"),
            location_id=payload.get("warehouse_id"),
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(
        {
            "conversation_id": conversation.id,
            "answer": response.content,
            "context": response.context_snapshot,
        }
    )


@api_bp.get("/assistant/context")
@api_read_access
def dashboard_assistant_context_api():
    actor = _current_actor()
    return jsonify(
        dashboard_context(
            workspace_id=actor.workspace_id,
            location_id=request.args.get("warehouse_id", type=int),
        )
    )


@api_bp.get("/insights")
@api_read_access
def insights_api():
    actor = _current_actor()
    return jsonify(
        {
            "insights": [
                serialize_insight(row)
                for row in latest_insights()
                if row.location.workspace_id == actor.workspace_id
            ]
        }
    )


@api_bp.post("/analysis/run")
def run_analysis():
    _require_token("X-Internal-Token", "INTERNAL_API_TOKEN")
    actor = _current_actor()
    ForecastAccuracyService.evaluate(workspace_id=actor.workspace_id)
    results = ForecastService.run(workspace_id=actor.workspace_id)
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
    results = ForecastService.run(workspace_id=actor.workspace_id)
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
