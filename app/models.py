from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from flask import g, has_request_context
from sqlalchemy import Computed
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import synonym
from werkzeug.security import check_password_hash, generate_password_hash

from app import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Workspace(db.Model):
    __tablename__ = "workspaces"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    business_username = db.Column(
        db.String(63), nullable=False, unique=True, index=True,
        default=lambda: f"workspace-{uuid4().hex[:12]}"
    )
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    users = db.relationship(
        "User", back_populates="workspace", foreign_keys="User._workspace_id"
    )
    memberships = db.relationship(
        "WorkspaceMembership", back_populates="workspace", cascade="all, delete-orphan"
    )
    settings = db.relationship(
        "WorkspaceSetting", back_populates="workspace", uselist=False,
        cascade="all, delete-orphan"
    )
    integrations = db.relationship(
        "WorkspaceIntegration", back_populates="workspace", cascade="all, delete-orphan"
    )
    locations = db.relationship("InventoryLocation", back_populates="workspace")
    suppliers = db.relationship("Supplier", back_populates="workspace")
    products = db.relationship("Product", back_populates="workspace")
    alert_deliveries = db.relationship("AlertDelivery", back_populates="workspace")


class User(db.Model):
    """Named staff account and durable audit identity."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    # Keep the original columns as a durable home-workspace fallback for old
    # audit records and deployments. During a request, the hybrid properties
    # resolve to the membership selected in that browser session.
    _workspace_id = db.Column(
        "workspace_id", db.Integer, db.ForeignKey("workspaces.id"), nullable=False
    )
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255))
    _role = db.Column("role", db.String(50), nullable=False, default="admin")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    email_verified_at = db.Column(db.DateTime(timezone=True))
    mfa_secret_encrypted = db.Column(db.Text)
    mfa_enabled_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    workspace = db.relationship(
        "Workspace", back_populates="users", foreign_keys=[_workspace_id]
    )
    memberships = db.relationship(
        "WorkspaceMembership", back_populates="user", cascade="all, delete-orphan"
    )
    auth_tokens = db.relationship(
        "AuthToken", back_populates="user", cascade="all, delete-orphan"
    )
    mfa_recovery_codes = db.relationship(
        "MFARecoveryCode", back_populates="user", cascade="all, delete-orphan"
    )

    @hybrid_property
    def workspace_id(self) -> int:
        if has_request_context() and getattr(g, "current_user", None) is self:
            return int(getattr(g, "active_workspace_id", self._workspace_id))
        return self._workspace_id

    @workspace_id.setter
    def workspace_id(self, value: int) -> None:
        self._workspace_id = value

    @workspace_id.expression
    def workspace_id(cls):
        return cls._workspace_id

    @hybrid_property
    def role(self) -> str:
        if has_request_context() and getattr(g, "current_user", None) is self:
            return str(getattr(g, "active_workspace_role", self._role))
        return self._role

    @role.setter
    def role(self, value: str) -> None:
        self._role = value

    @role.expression
    def role(cls):
        return cls._role

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(
            self.password_hash, password
        )

    def has_role(self, *roles: str) -> bool:
        return self.is_active and self.role in roles


class WorkspaceMembership(db.Model):
    """A user's role and access state inside one independently isolated tenant."""

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        db.UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),
        db.CheckConstraint(
            "role IN ('admin', 'manager', 'picker')", name="ck_membership_role"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(50), nullable=False, default="picker")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    joined_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    last_accessed_at = db.Column(db.DateTime(timezone=True))

    workspace = db.relationship("Workspace", back_populates="memberships")
    user = db.relationship("User", back_populates="memberships")


class WorkspaceSetting(db.Model):
    __tablename__ = "workspace_settings"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, unique=True
    )
    timezone = db.Column(db.String(64), nullable=False, default="Asia/Kolkata")
    currency = db.Column(db.String(3), nullable=False, default="INR")
    date_format = db.Column(db.String(32), nullable=False, default="DD MMM YYYY")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    workspace = db.relationship("Workspace", back_populates="settings")


class WorkspaceIntegration(db.Model):
    __tablename__ = "workspace_integrations"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "provider", "name", name="uq_workspace_integration"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    provider = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False, default="default")
    config_json = db.Column(db.JSON, nullable=False, default=dict)
    # Secrets stay in the deployment secret store. This field contains only
    # the environment/Secrets Manager reference used to retrieve one.
    secret_reference = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    workspace = db.relationship("Workspace", back_populates="integrations")


class AuthToken(db.Model):
    """Single-use, hashed tokens for verification, recovery, and invitations."""

    __tablename__ = "auth_tokens"
    __table_args__ = (
        db.CheckConstraint(
            "purpose IN ('email_verification', 'password_reset', 'invitation')",
            name="ck_auth_token_purpose",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), index=True)
    email = db.Column(db.String(255))
    purpose = db.Column(db.String(40), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    payload_json = db.Column(db.JSON, nullable=False, default=dict)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    consumed_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    user = db.relationship("User", back_populates="auth_tokens")
    workspace = db.relationship("Workspace")


class LoginAttempt(db.Model):
    """Database-backed throttling shared by every Gunicorn worker."""

    __tablename__ = "login_attempts"

    id = db.Column(db.Integer, primary_key=True)
    attempt_key_hash = db.Column(db.String(64), nullable=False, index=True)
    succeeded = db.Column(db.Boolean, nullable=False, default=False)
    attempted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class MFARecoveryCode(db.Model):
    __tablename__ = "mfa_recovery_codes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=False, unique=True)
    used_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    user = db.relationship("User", back_populates="mfa_recovery_codes")


class Supplier(db.Model):
    __tablename__ = "suppliers"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "name", name="uq_supplier_workspace_name"
        ),
        db.CheckConstraint(
            "lead_time_days >= 0 AND lead_time_days <= 3650",
            name="ck_supplier_lead_time_range",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    name = db.Column(db.String(255), nullable=False)
    contact_email = db.Column(db.String(255))
    contact_phone = db.Column(db.String(50))
    lead_time_days = db.Column(db.Integer, nullable=False, default=3)
    payment_terms = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    # Keep earlier Sprint code and imports compatible while the public supplier
    # contract uses the schema names contact_email/contact_phone.
    email = synonym("contact_email")
    phone = synonym("contact_phone")

    workspace = db.relationship("Workspace", back_populates="suppliers")
    products = db.relationship("Product", back_populates="preferred_supplier")


class AlertDelivery(db.Model):
    """Durable audit record for scheduled and manually-triggered owner alerts."""

    __tablename__ = "alert_deliveries"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('sent', 'skipped', 'failed')",
            name="ck_alert_delivery_status",
        ),
        db.CheckConstraint(
            "recipient_count >= 0 AND item_count >= 0",
            name="ck_alert_delivery_counts_non_negative",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    report_type = db.Column(db.String(40), nullable=False, default="critical_inventory")
    severity = db.Column(db.String(20), nullable=False, default="critical")
    status = db.Column(db.String(20), nullable=False)
    recipient_count = db.Column(db.Integer, nullable=False, default=0)
    item_count = db.Column(db.Integer, nullable=False, default=0)
    provider_message_id = db.Column(db.String(255))
    detail = db.Column(db.String(500))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    sent_at = db.Column(db.DateTime(timezone=True))

    workspace = db.relationship("Workspace", back_populates="alert_deliveries")


class InventoryLocation(db.Model):
    __tablename__ = "inventory_locations"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "code", name="uq_location_workspace_code"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(32), nullable=False, index=True)
    address = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    workspace = db.relationship("Workspace", back_populates="locations")
    bins = db.relationship(
        "Bin", back_populates="location", cascade="all, delete-orphan"
    )
    stock_levels = db.relationship(
        "StockLevel", back_populates="location", cascade="all, delete-orphan"
    )


class Bin(db.Model):
    __tablename__ = "bins"
    __table_args__ = (
        db.UniqueConstraint("location_id", "code", name="uq_bin_location_code"),
        db.CheckConstraint("capacity > 0", name="ck_bin_capacity_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False, index=True
    )
    code = db.Column(db.String(50), nullable=False)
    capacity = db.Column(db.Integer)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    location = db.relationship("InventoryLocation", back_populates="bins")
    stock_levels = db.relationship("StockLevel", back_populates="bin")


class Product(db.Model):
    __tablename__ = "products"

    __table_args__ = (
        db.UniqueConstraint("workspace_id", "sku", name="uq_product_workspace_sku"),
        db.UniqueConstraint(
            "workspace_id", "barcode", name="uq_product_workspace_barcode"
        ),
        db.CheckConstraint("cost_price >= 0", name="ck_product_cost_non_negative"),
        db.CheckConstraint("sell_price >= 0", name="ck_product_sell_non_negative"),
        db.CheckConstraint("reorder_point >= 0", name="ck_product_reorder_non_negative"),
        db.CheckConstraint("safety_stock >= 0", name="ck_product_safety_non_negative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    sku = db.Column(db.String(100), nullable=False, index=True)
    barcode = db.Column(db.String(100), index=True)
    name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(100), nullable=False, default="General")
    unit_of_measure = db.Column(db.String(20), nullable=False, default="unit")
    cost_price = db.Column(db.Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    sell_price = db.Column(db.Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    reorder_point = db.Column(db.Integer, nullable=False, default=0)
    safety_stock = db.Column(db.Integer, nullable=False, default=0)
    preferred_supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"))
    is_perishable = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    archived_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    # Legacy attribute aliases keep the existing forecasting and template code
    # compatible while the public schema uses the clearer Sprint 1 names.
    unit = synonym("unit_of_measure")
    active = synonym("is_active")

    preferred_supplier = db.relationship("Supplier", back_populates="products")
    workspace = db.relationship("Workspace", back_populates="products")
    stock_levels = db.relationship(
        "StockLevel", back_populates="product", cascade="all, delete-orphan"
    )
    unit_conversions = db.relationship(
        "UnitConversion", back_populates="product", cascade="all, delete-orphan"
    )
    inventory_lots = db.relationship("InventoryLot", back_populates="product")


class StockLevel(db.Model):
    __tablename__ = "stock_levels"
    __table_args__ = (
        db.UniqueConstraint(
            "product_id",
            "location_id",
            "position_bin_key",
            name="uq_stock_product_location_position",
        ),
        db.CheckConstraint(
            "quantity_on_hand >= 0", name="ck_stock_on_hand_non_negative"
        ),
        db.CheckConstraint(
            "quantity_reserved >= 0", name="ck_stock_reserved_non_negative"
        ),
        db.CheckConstraint(
            "quantity_reserved <= quantity_on_hand", name="ck_stock_reserved_not_excessive"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    bin_id = db.Column(db.Integer, db.ForeignKey("bins.id"), nullable=True)
    # SQL unique constraints treat NULL values as distinct. The generated key
    # maps an unassigned bin to zero, preventing duplicate location-level rows
    # during concurrent adjustments, transfers, or return receipts.
    position_bin_key = db.Column(
        db.Integer,
        Computed("COALESCE(bin_id, 0)", persisted=False),
    )
    quantity_on_hand = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    quantity_reserved = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    # Compatibility for legacy code. There is still only one physical on-hand
    # column in the database: quantity_on_hand.
    quantity = synonym("quantity_on_hand")

    @property
    def quantity_available(self) -> Decimal:
        return Decimal(self.quantity_on_hand or 0) - Decimal(self.quantity_reserved or 0)

    product = db.relationship("Product", back_populates="stock_levels")
    location = db.relationship("InventoryLocation", back_populates="stock_levels")
    bin = db.relationship("Bin", back_populates="stock_levels")
    order_allocations = db.relationship(
        "SalesOrderAllocation", back_populates="stock_level"
    )


class UnitConversion(db.Model):
    """Product-specific packaging unit expressed in the product's base unit."""

    __tablename__ = "unit_conversions"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "product_id", "unit_code",
            name="uq_conversion_workspace_product_unit",
        ),
        db.CheckConstraint("to_base_factor > 0", name="ck_conversion_factor_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    unit_code = db.Column(db.String(20), nullable=False)
    to_base_factor = db.Column(db.Numeric(18, 6), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    product = db.relationship("Product", back_populates="unit_conversions")


class InventoryLot(db.Model):
    """Expiry-aware lot subledger reconciled with aggregate stock positions."""

    __tablename__ = "inventory_lots"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "product_id", "location_id", "position_bin_key", "lot_number",
            name="uq_lot_workspace_position_number",
        ),
        db.CheckConstraint("quantity_on_hand >= 0", name="ck_lot_quantity_non_negative"),
        db.CheckConstraint(
            "expiry_date IS NULL OR manufactured_at IS NULL OR expiry_date >= manufactured_at",
            name="ck_lot_expiry_after_manufacture",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    bin_id = db.Column(db.Integer, db.ForeignKey("bins.id"))
    position_bin_key = db.Column(
        db.Integer, Computed("COALESCE(bin_id, 0)", persisted=False)
    )
    lot_number = db.Column(db.String(100), nullable=False)
    manufactured_at = db.Column(db.Date)
    expiry_date = db.Column(db.Date, index=True)
    quantity_on_hand = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    product = db.relationship("Product", back_populates="inventory_lots")
    location = db.relationship("InventoryLocation")
    bin = db.relationship("Bin")


class Sale(db.Model):
    __tablename__ = "sales"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "external_id", name="uq_sale_workspace_external_id"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    external_id = db.Column(db.String(128), nullable=False, index=True)
    source = db.Column(db.String(64), nullable=False, default="pos")
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    location = db.relationship("InventoryLocation")
    items = db.relationship(
        "SaleItem", back_populates="sale", cascade="all, delete-orphan"
    )


class SaleItem(db.Model):
    __tablename__ = "sale_items"

    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=True)

    sale = db.relationship("Sale", back_populates="items")
    product = db.relationship("Product")


class InventoryMovement(db.Model):
    __tablename__ = "inventory_movements"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    bin_id = db.Column(db.Integer, db.ForeignKey("bins.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    movement_type = db.Column(db.String(30), nullable=False, default="adjustment")
    quantity_delta = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.String(64), nullable=False)
    reference_type = db.Column(db.String(64))
    reference_id = db.Column(db.String(128))
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    product = db.relationship("Product")
    location = db.relationship("InventoryLocation")
    bin = db.relationship("Bin")
    user = db.relationship("User")


class StockTransfer(db.Model):
    __tablename__ = "stock_transfers"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "external_id", name="uq_transfer_workspace_external_id"
        ),
        db.CheckConstraint("quantity > 0", name="ck_transfer_quantity_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    transfer_uid = db.Column(
        db.String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid4())
    )
    external_id = db.Column(db.String(128), index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    source_location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    destination_location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    source_bin_id = db.Column(db.Integer, db.ForeignKey("bins.id"), nullable=True)
    destination_bin_id = db.Column(db.Integer, db.ForeignKey("bins.id"), nullable=True)
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="completed")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    product = db.relationship("Product")
    source_location = db.relationship(
        "InventoryLocation", foreign_keys=[source_location_id]
    )
    destination_location = db.relationship(
        "InventoryLocation", foreign_keys=[destination_location_id]
    )
    source_bin = db.relationship("Bin", foreign_keys=[source_bin_id])
    destination_bin = db.relationship("Bin", foreign_keys=[destination_bin_id])
    user = db.relationship("User")


class SalesOrder(db.Model):
    """Outbound order whose reservations remain in the stock single source of truth."""

    __tablename__ = "sales_orders"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "external_id", name="uq_sales_order_workspace_external_id"
        ),
        db.CheckConstraint(
            "status IN ('pending', 'picking', 'packed', 'shipped', 'cancelled')",
            name="ck_sales_order_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_uid = db.Column(
        db.String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid4())
    )
    external_id = db.Column(db.String(128), index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=False)
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False, index=True
    )
    channel = db.Column(db.String(50), nullable=False, default="manual")
    status = db.Column(db.String(30), nullable=False, default="pending", index=True)
    customer_reference = db.Column(db.String(128))
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    picking_started_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    packed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    shipped_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    picking_started_at = db.Column(db.DateTime(timezone=True))
    packed_at = db.Column(db.DateTime(timezone=True))
    shipped_at = db.Column(db.DateTime(timezone=True))
    cancelled_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    workspace = db.relationship("Workspace")
    location = db.relationship("InventoryLocation")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    picking_started_by = db.relationship("User", foreign_keys=[picking_started_by_id])
    packed_by = db.relationship("User", foreign_keys=[packed_by_id])
    shipped_by = db.relationship("User", foreign_keys=[shipped_by_id])
    cancelled_by = db.relationship("User", foreign_keys=[cancelled_by_id])
    items = db.relationship(
        "SalesOrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="SalesOrderItem.id",
    )
    return_authorizations = db.relationship(
        "ReturnAuthorization",
        back_populates="sales_order",
        cascade="all, delete-orphan",
        order_by="ReturnAuthorization.created_at.desc()",
    )


class SalesOrderItem(db.Model):
    __tablename__ = "sales_order_items"
    __table_args__ = (
        db.UniqueConstraint("order_id", "product_id", name="uq_order_product"),
        db.CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        db.CheckConstraint(
            "picked_quantity >= 0 AND picked_quantity <= quantity",
            name="ck_order_item_picked_quantity",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(
        db.Integer, db.ForeignKey("sales_orders.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    picked_quantity = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    picked_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    picked_at = db.Column(db.DateTime(timezone=True))

    order = db.relationship("SalesOrder", back_populates="items")
    product = db.relationship("Product")
    picked_by = db.relationship("User", foreign_keys=[picked_by_id])
    allocations = db.relationship(
        "SalesOrderAllocation",
        back_populates="order_item",
        cascade="all, delete-orphan",
        order_by="SalesOrderAllocation.id",
    )


class SalesOrderAllocation(db.Model):
    """Exact stock positions reserved for an order line and shown on its pick list."""

    __tablename__ = "sales_order_allocations"
    __table_args__ = (
        db.UniqueConstraint(
            "order_item_id", "stock_level_id", name="uq_order_item_stock_allocation"
        ),
        db.CheckConstraint(
            "quantity_reserved > 0", name="ck_order_allocation_quantity_positive"
        ),
        db.CheckConstraint(
            "picked_quantity >= 0 AND picked_quantity <= quantity_reserved",
            name="ck_order_allocation_picked_quantity",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_item_id = db.Column(
        db.Integer, db.ForeignKey("sales_order_items.id"), nullable=False, index=True
    )
    stock_level_id = db.Column(
        db.Integer, db.ForeignKey("stock_levels.id"), nullable=False, index=True
    )
    quantity_reserved = db.Column(db.Numeric(12, 2), nullable=False)
    picked_quantity = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )

    order_item = db.relationship("SalesOrderItem", back_populates="allocations")
    stock_level = db.relationship("StockLevel", back_populates="order_allocations")


class PurchaseOrder(db.Model):
    """Inbound supplier order with explicit draft, approval and receipt states."""

    __tablename__ = "purchase_orders"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "external_id", name="uq_purchase_order_workspace_external_id"
        ),
        db.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', "
            "'partially_received', 'received', 'cancelled')",
            name="ck_purchase_order_status",
        ),
        db.CheckConstraint(
            "source IN ('manual', 'ai')", name="ck_purchase_order_source"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    po_uid = db.Column(
        db.String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid4())
    )
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    external_id = db.Column(db.String(128), index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False)
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    status = db.Column(db.String(30), nullable=False, default="draft", index=True)
    source = db.Column(db.String(20), nullable=False, default="manual")
    expected_at = db.Column(db.Date)
    note = db.Column(db.String(500))
    ai_rationale = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime(timezone=True))
    cancelled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    workspace = db.relationship("Workspace")
    supplier = db.relationship("Supplier")
    location = db.relationship("InventoryLocation")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    cancelled_by = db.relationship("User", foreign_keys=[cancelled_by_id])
    items = db.relationship(
        "PurchaseOrderItem", back_populates="purchase_order",
        cascade="all, delete-orphan", order_by="PurchaseOrderItem.id"
    )
    receipts = db.relationship(
        "PurchaseReceipt", back_populates="purchase_order",
        cascade="all, delete-orphan", order_by="PurchaseReceipt.received_at"
    )


class PurchaseOrderItem(db.Model):
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        db.UniqueConstraint(
            "purchase_order_id", "product_id", name="uq_purchase_order_product"
        ),
        db.CheckConstraint(
            "ordered_quantity > 0 AND received_quantity >= 0 "
            "AND received_quantity <= ordered_quantity",
            name="ck_purchase_item_quantities",
        ),
        db.CheckConstraint(
            "conversion_to_base > 0", name="ck_purchase_item_conversion_positive"
        ),
        db.CheckConstraint("unit_cost >= 0", name="ck_purchase_item_cost_non_negative"),
    )

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(
        db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False, index=True
    )
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    ordered_quantity = db.Column(db.Numeric(12, 2), nullable=False)
    received_quantity = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    order_unit = db.Column(db.String(20), nullable=False)
    conversion_to_base = db.Column(db.Numeric(18, 6), nullable=False)
    unit_cost = db.Column(db.Numeric(10, 2), nullable=False, default=Decimal("0.00"))

    purchase_order = db.relationship("PurchaseOrder", back_populates="items")
    product = db.relationship("Product")
    receipt_items = db.relationship("PurchaseReceiptItem", back_populates="purchase_order_item")


class PurchaseReceipt(db.Model):
    __tablename__ = "purchase_receipts"
    __table_args__ = (
        db.UniqueConstraint(
            "purchase_order_id", "external_id", name="uq_purchase_receipt_external_id"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    receipt_uid = db.Column(
        db.String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid4())
    )
    purchase_order_id = db.Column(
        db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False, index=True
    )
    external_id = db.Column(db.String(128), nullable=False)
    received_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    note = db.Column(db.String(255))
    received_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    purchase_order = db.relationship("PurchaseOrder", back_populates="receipts")
    received_by = db.relationship("User")
    items = db.relationship(
        "PurchaseReceiptItem", back_populates="receipt", cascade="all, delete-orphan"
    )


class PurchaseReceiptItem(db.Model):
    __tablename__ = "purchase_receipt_items"
    __table_args__ = (
        db.CheckConstraint("quantity_received > 0", name="ck_receipt_item_quantity_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(
        db.Integer, db.ForeignKey("purchase_receipts.id"), nullable=False, index=True
    )
    purchase_order_item_id = db.Column(
        db.Integer, db.ForeignKey("purchase_order_items.id"), nullable=False
    )
    inventory_lot_id = db.Column(db.Integer, db.ForeignKey("inventory_lots.id"))
    quantity_received = db.Column(db.Numeric(12, 2), nullable=False)
    received_unit = db.Column(db.String(20), nullable=False)
    conversion_to_base = db.Column(db.Numeric(18, 6), nullable=False)

    receipt = db.relationship("PurchaseReceipt", back_populates="items")
    purchase_order_item = db.relationship("PurchaseOrderItem", back_populates="receipt_items")
    inventory_lot = db.relationship("InventoryLot")


class ReturnAuthorization(db.Model):
    """Return merchandise authorization linked to a fulfilled sales order."""

    __tablename__ = "return_authorizations"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id", "external_id", name="uq_return_workspace_external_id"
        ),
        db.CheckConstraint(
            "status IN ('requested', 'authorized', 'receiving', 'completed', "
            "'rejected', 'cancelled')",
            name="ck_return_authorization_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    rma_uid = db.Column(
        db.String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid4())
    )
    external_id = db.Column(db.String(128), index=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    sales_order_id = db.Column(
        db.Integer, db.ForeignKey("sales_orders.id"), nullable=False, index=True
    )
    status = db.Column(db.String(30), nullable=False, default="requested", index=True)
    reason_code = db.Column(db.String(50), nullable=False)
    customer_note = db.Column(db.String(500))
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    authorized_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    rejected_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    completed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    authorized_at = db.Column(db.DateTime(timezone=True))
    rejected_at = db.Column(db.DateTime(timezone=True))
    cancelled_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    workspace = db.relationship("Workspace")
    sales_order = db.relationship("SalesOrder", back_populates="return_authorizations")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    authorized_by = db.relationship("User", foreign_keys=[authorized_by_id])
    rejected_by = db.relationship("User", foreign_keys=[rejected_by_id])
    cancelled_by = db.relationship("User", foreign_keys=[cancelled_by_id])
    completed_by = db.relationship("User", foreign_keys=[completed_by_id])
    items = db.relationship(
        "ReturnItem",
        back_populates="return_authorization",
        cascade="all, delete-orphan",
        order_by="ReturnItem.id",
    )
    events = db.relationship(
        "ReturnEvent",
        back_populates="return_authorization",
        cascade="all, delete-orphan",
        order_by="ReturnEvent.created_at, ReturnEvent.id",
    )


class ReturnItem(db.Model):
    __tablename__ = "return_items"
    __table_args__ = (
        db.UniqueConstraint(
            "return_authorization_id",
            "sales_order_item_id",
            name="uq_return_sales_order_item",
        ),
        db.CheckConstraint(
            "quantity_requested > 0", name="ck_return_item_requested_positive"
        ),
        db.CheckConstraint(
            "quantity_authorized >= 0 AND quantity_authorized <= quantity_requested",
            name="ck_return_item_authorized_range",
        ),
        db.CheckConstraint(
            "quantity_received >= 0 AND quantity_received <= quantity_authorized",
            name="ck_return_item_received_range",
        ),
        db.CheckConstraint(
            "quantity_restocked >= 0 AND quantity_restocked <= quantity_received",
            name="ck_return_item_restocked_range",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    return_authorization_id = db.Column(
        db.Integer,
        db.ForeignKey("return_authorizations.id"),
        nullable=False,
        index=True,
    )
    sales_order_item_id = db.Column(
        db.Integer, db.ForeignKey("sales_order_items.id"), nullable=False, index=True
    )
    quantity_requested = db.Column(db.Numeric(12, 2), nullable=False)
    quantity_authorized = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    quantity_received = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    quantity_restocked = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )

    return_authorization = db.relationship("ReturnAuthorization", back_populates="items")
    sales_order_item = db.relationship("SalesOrderItem")
    receipts = db.relationship(
        "ReturnReceipt",
        back_populates="return_item",
        cascade="all, delete-orphan",
        order_by="ReturnReceipt.created_at, ReturnReceipt.id",
    )


class ReturnReceipt(db.Model):
    """One idempotent physical receipt, supporting partial and mixed dispositions."""

    __tablename__ = "return_receipts"
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_return_receipt_quantity_positive"),
        db.CheckConstraint(
            "disposition IN ('restock', 'damaged')",
            name="ck_return_receipt_disposition",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    receipt_uid = db.Column(
        db.String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid4())
    )
    external_id = db.Column(db.String(128), unique=True, index=True)
    return_item_id = db.Column(
        db.Integer, db.ForeignKey("return_items.id"), nullable=False, index=True
    )
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    disposition = db.Column(db.String(30), nullable=False)
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    bin_id = db.Column(db.Integer, db.ForeignKey("bins.id"))
    received_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    return_item = db.relationship("ReturnItem", back_populates="receipts")
    location = db.relationship("InventoryLocation")
    bin = db.relationship("Bin")
    received_by = db.relationship("User")


class ReturnEvent(db.Model):
    """Append-only workflow audit record, including non-stock return outcomes."""

    __tablename__ = "return_events"

    id = db.Column(db.Integer, primary_key=True)
    return_authorization_id = db.Column(
        db.Integer,
        db.ForeignKey("return_authorizations.id"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event_type = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    return_authorization = db.relationship("ReturnAuthorization", back_populates="events")
    user = db.relationship("User")


class DemandInsight(db.Model):
    __tablename__ = "demand_insights"
    __table_args__ = (
        db.Index(
            "ix_insight_product_location_latest",
            "product_id",
            "location_id",
            "id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    location_id = db.Column(
        db.Integer, db.ForeignKey("inventory_locations.id"), nullable=False
    )
    generated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    daily_demand = db.Column(db.Float, nullable=False, default=0)
    expected_stockout_at = db.Column(db.DateTime(timezone=True), nullable=True)
    recommended_reorder_quantity = db.Column(
        db.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    confidence = db.Column(db.Integer, nullable=False, default=0)
    narrative = db.Column(db.Text)
    factors = db.Column(db.JSON, nullable=False, default=dict)

    product = db.relationship("Product")
    location = db.relationship("InventoryLocation")


class ForecastOutcome(db.Model):
    """Actual demand observed after a persisted forecast horizon."""

    __tablename__ = "forecast_outcomes"
    __table_args__ = (
        db.UniqueConstraint("insight_id", name="uq_forecast_outcome_insight"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    insight_id = db.Column(
        db.Integer, db.ForeignKey("demand_insights.id"), nullable=False
    )
    horizon_days = db.Column(db.Integer, nullable=False, default=7)
    predicted_units = db.Column(db.Numeric(12, 2), nullable=False)
    actual_units = db.Column(db.Numeric(12, 2), nullable=False)
    absolute_error = db.Column(db.Numeric(12, 2), nullable=False)
    absolute_percentage_error = db.Column(db.Float)
    evaluated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    insight = db.relationship("DemandInsight")


class ChatConversation(db.Model):
    __tablename__ = "chat_conversations"

    id = db.Column(db.Integer, primary_key=True)
    conversation_uid = db.Column(
        db.String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid4())
    )
    workspace_id = db.Column(
        db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    user = db.relationship("User")
    messages = db.relationship(
        "ChatMessage", back_populates="conversation", cascade="all, delete-orphan",
        order_by="ChatMessage.created_at, ChatMessage.id"
    )


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"
    __table_args__ = (
        db.CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_message_role"),
    )

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("chat_conversations.id"), nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    context_snapshot = db.Column(db.JSON)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    conversation = db.relationship("ChatConversation", back_populates="messages")
