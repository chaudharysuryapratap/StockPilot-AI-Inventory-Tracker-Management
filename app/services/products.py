from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping

from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Product, Supplier, utcnow


SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._/-]*$")
UNIT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9 ._/-]*$")


def number_for_json(value: Decimal | int | float | None) -> int | float:
    """Return predictable JSON numbers instead of Decimal strings."""
    if value is None:
        return 0
    decimal_value = Decimal(str(value))
    if decimal_value == decimal_value.to_integral_value():
        return int(decimal_value)
    return float(decimal_value)


class ProductValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]):
        self.errors = dict(errors)
        super().__init__("; ".join(f"{field}: {message}" for field, message in self.errors.items()))


def _text(
    value: object,
    *,
    field_name: str,
    max_length: int,
    required: bool = True,
) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        if required:
            raise ValueError("is required")
        return None
    if len(normalized) > max_length:
        raise ValueError(f"must be {max_length} characters or fewer")
    return normalized


def _money(value: object, field_name: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    if isinstance(value, bool):
        raise ValueError("must be a non-negative amount with at most 2 decimals")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError("must be a non-negative amount with at most 2 decimals") from error
    if not amount.is_finite() or amount < 0 or amount > Decimal("99999999.99"):
        raise ValueError("must be between 0 and 99999999.99")
    if amount.as_tuple().exponent < -2:
        raise ValueError("must have at most 2 decimal places")
    return amount.quantize(Decimal("0.01"))


def _nonnegative_int(value: object, field_name: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise ValueError("must be a non-negative whole number")
    normalized = str(value).strip()
    if not re.fullmatch(r"\+?\d+", normalized):
        raise ValueError("must be a non-negative whole number")
    try:
        number = int(normalized)
    except (TypeError, ValueError) as error:
        raise ValueError("must be a non-negative whole number") from error
    if number < 0:
        raise ValueError("must be a non-negative whole number")
    return number


def _boolean(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "n", "off"}:
        return False
    raise ValueError("must be true or false")


class ProductService:
    """Validated product catalogue operations shared by forms, APIs, and CSV import."""

    FIELD_ALIASES = {
        "unit": "unit_of_measure",
        "active": "is_active",
        "supplier_id": "preferred_supplier_id",
    }

    @staticmethod
    def _read(payload: Mapping[str, object], field_name: str) -> tuple[object, bool]:
        if field_name in payload:
            return payload[field_name], True
        for alias, canonical in ProductService.FIELD_ALIASES.items():
            if canonical == field_name and alias in payload:
                return payload[alias], True
        return None, False

    @staticmethod
    def validate(
        payload: Mapping[str, object],
        *,
        partial: bool = False,
        current_product: Product | None = None,
        workspace_id: int | None = None,
    ) -> dict:
        if not isinstance(payload, Mapping):
            raise ProductValidationError({"payload": "must be an object"})

        errors: dict[str, str] = {}
        values: dict[str, object] = {}

        specifications = {
            "sku": (100, True),
            "name": (255, True),
            "category": (100, True),
            "unit_of_measure": (20, True),
            "barcode": (100, False),
        }
        defaults = {"category": "General", "unit_of_measure": "unit", "barcode": None}

        for field_name, (max_length, required) in specifications.items():
            raw, supplied = ProductService._read(payload, field_name)
            if not supplied and not partial:
                raw = defaults.get(field_name)
                supplied = field_name in defaults
            if not supplied:
                if not partial and required:
                    errors[field_name] = "is required"
                continue
            try:
                value = _text(
                    raw,
                    field_name=field_name,
                    max_length=max_length,
                    required=required,
                )
                if field_name == "sku" and value:
                    value = value.upper()
                    if not SKU_PATTERN.fullmatch(value):
                        raise ValueError(
                            "may contain only letters, numbers, dots, slashes, underscores, and hyphens"
                        )
                if field_name == "unit_of_measure" and value and not UNIT_PATTERN.fullmatch(value):
                    raise ValueError("contains unsupported characters")
                values[field_name] = value
            except ValueError as error:
                errors[field_name] = str(error)

        numeric_defaults = {
            "cost_price": "0.00",
            "sell_price": "0.00",
            "reorder_point": 0,
            "safety_stock": 0,
        }
        for field_name, default in numeric_defaults.items():
            raw, supplied = ProductService._read(payload, field_name)
            if not supplied and not partial:
                raw, supplied = default, True
            if not supplied:
                continue
            try:
                values[field_name] = (
                    _money(raw, field_name)
                    if field_name in {"cost_price", "sell_price"}
                    else _nonnegative_int(raw, field_name)
                )
            except ValueError as error:
                errors[field_name] = str(error)

        raw_perishable, supplied_perishable = ProductService._read(payload, "is_perishable")
        if not supplied_perishable and not partial:
            raw_perishable, supplied_perishable = False, True
        if supplied_perishable:
            try:
                values["is_perishable"] = _boolean(raw_perishable, "is_perishable")
            except ValueError as error:
                errors["is_perishable"] = str(error)

        raw_supplier, supplied_supplier = ProductService._read(payload, "preferred_supplier_id")
        if supplied_supplier:
            if raw_supplier in (None, ""):
                values["preferred_supplier_id"] = None
            else:
                try:
                    supplier_id = int(str(raw_supplier).strip())
                except (TypeError, ValueError):
                    errors["preferred_supplier_id"] = "must be a valid supplier ID"
                else:
                    supplier = (
                        db.session.get(Supplier, supplier_id) if supplier_id > 0 else None
                    )
                    if supplier is None or not supplier.is_active:
                        errors["preferred_supplier_id"] = "was not found"
                    elif workspace_id is not None and supplier.workspace_id != workspace_id:
                        errors["preferred_supplier_id"] = "was not found"
                    else:
                        values["preferred_supplier_id"] = supplier_id
        elif not partial:
            values["preferred_supplier_id"] = None

        sku = values.get("sku")
        if sku:
            query = Product.query.filter_by(sku=sku)
            if workspace_id is not None:
                query = query.filter_by(workspace_id=workspace_id)
            if current_product is not None:
                query = query.filter(Product.id != current_product.id)
            if query.first():
                errors["sku"] = "already exists"

        barcode = values.get("barcode")
        if barcode:
            query = Product.query.filter_by(barcode=barcode)
            if workspace_id is not None:
                query = query.filter_by(workspace_id=workspace_id)
            if current_product is not None:
                query = query.filter(Product.id != current_product.id)
            if query.first():
                errors["barcode"] = "already exists"

        if errors:
            raise ProductValidationError(errors)
        return values

    @staticmethod
    def create(
        payload: Mapping[str, object],
        *,
        commit: bool = True,
        workspace_id: int | None = None,
    ) -> Product:
        values = ProductService.validate(payload, workspace_id=workspace_id)
        if workspace_id is None:
            raise ProductValidationError({"workspace_id": "is required"})
        values["workspace_id"] = workspace_id
        product = Product(**values)
        db.session.add(product)
        if commit:
            ProductService._commit()
        return product

    @staticmethod
    def update(
        product: Product,
        payload: Mapping[str, object],
        *,
        commit: bool = True,
        workspace_id: int | None = None,
    ) -> Product:
        values = ProductService.validate(
            payload,
            partial=True,
            current_product=product,
            workspace_id=workspace_id,
        )
        if not values:
            raise ProductValidationError({"payload": "include at least one editable field"})
        for field_name, value in values.items():
            setattr(product, field_name, value)
        product.updated_at = utcnow()
        if commit:
            ProductService._commit()
        return product

    @staticmethod
    def archive(product: Product, *, commit: bool = True) -> Product:
        product.is_active = False
        product.archived_at = product.archived_at or utcnow()
        product.updated_at = utcnow()
        if commit:
            ProductService._commit()
        return product

    @staticmethod
    def restore(product: Product, *, commit: bool = True) -> Product:
        product.is_active = True
        product.archived_at = None
        product.updated_at = utcnow()
        if commit:
            ProductService._commit()
        return product

    @staticmethod
    def _commit() -> None:
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise ProductValidationError(
                {"product": "conflicts with an existing SKU or barcode"}
            ) from error


def serialize_product(product: Product, *, include_sensitive: bool = True) -> dict:
    stock_rows = []
    total_on_hand = Decimal("0")
    total_reserved = Decimal("0")
    for level in product.stock_levels:
        on_hand = Decimal(level.quantity_on_hand or 0)
        reserved = Decimal(level.quantity_reserved or 0)
        total_on_hand += on_hand
        total_reserved += reserved
        stock_row = {
            "location": level.location.code,
            "quantity_on_hand": number_for_json(on_hand),
            "quantity_reserved": number_for_json(reserved),
            "quantity_available": number_for_json(on_hand - reserved),
        }
        if level.bin is not None:
            stock_row["bin"] = level.bin.code
        stock_rows.append(stock_row)
    data = {
        "id": product.id,
        "sku": product.sku,
        "barcode": product.barcode,
        "name": product.name,
        "category": product.category,
        "unit_of_measure": product.unit_of_measure,
        "unit_conversions": [
            {
                "unit_code": conversion.unit_code,
                "to_base_factor": number_for_json(conversion.to_base_factor),
            }
            for conversion in product.unit_conversions
        ],
        "cost_price": number_for_json(product.cost_price),
        "sell_price": number_for_json(product.sell_price),
        "reorder_point": product.reorder_point,
        "safety_stock": product.safety_stock,
        "is_perishable": product.is_perishable,
        "preferred_supplier_id": product.preferred_supplier_id,
        "supplier": product.preferred_supplier.name if product.preferred_supplier else None,
        "is_active": product.is_active,
        "archived_at": product.archived_at.isoformat() if product.archived_at else None,
        "stock": stock_rows,
        "totals": {
            "quantity_on_hand": number_for_json(total_on_hand),
            "quantity_reserved": number_for_json(total_reserved),
            "quantity_available": number_for_json(total_on_hand - total_reserved),
        },
    }
    if not include_sensitive:
        data.pop("cost_price", None)
        data.pop("preferred_supplier_id", None)
        data.pop("supplier", None)
    return data


@dataclass
class ProductImportResult:
    created: int = 0
    updated: int = 0
    rows_read: int = 0
    committed: bool = False
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "rows_read": self.rows_read,
            "committed": self.committed,
            "errors": self.errors,
        }


class ProductCSVImporter:
    HEADER_ALIASES = {"unit": "unit_of_measure", "supplier_id": "preferred_supplier_id"}
    IMPORTABLE_FIELDS = {
        "sku",
        "barcode",
        "name",
        "category",
        "unit_of_measure",
        "cost_price",
        "sell_price",
        "reorder_point",
        "safety_stock",
        "is_perishable",
        "preferred_supplier_id",
    }

    @staticmethod
    def import_bytes(
        content: bytes,
        *,
        update_existing: bool = False,
        max_rows: int = 1000,
        workspace_id: int | None = None,
    ) -> ProductImportResult:
        result = ProductImportResult()
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            result.errors.append({"row": 1, "errors": {"file": "must be UTF-8 encoded"}})
            return result

        try:
            reader = csv.DictReader(io.StringIO(text, newline=""))
            original_headers = reader.fieldnames or []
        except csv.Error as error:
            result.errors.append({"row": 1, "errors": {"file": f"invalid CSV: {error}"}})
            return result

        canonical_headers: dict[str, str] = {}
        for header in original_headers:
            normalized = (header or "").strip().lower()
            canonical_headers[header] = ProductCSVImporter.HEADER_ALIASES.get(
                normalized, normalized
            )
        header_values = set(canonical_headers.values())
        missing_headers = {"sku", "name"} - header_values
        if missing_headers:
            result.errors.append(
                {
                    "row": 1,
                    "errors": {
                        "headers": "missing required columns: "
                        + ", ".join(sorted(missing_headers))
                    },
                }
            )
            return result

        seen_skus: set[str] = set()
        seen_barcodes: set[str] = set()
        pending_created = 0
        pending_updated = 0

        try:
            for row_number, raw_row in enumerate(reader, start=2):
                if row_number - 1 > max_rows:
                    result.errors.append(
                        {
                            "row": row_number,
                            "errors": {"file": f"contains more than {max_rows} data rows"},
                        }
                    )
                    break
                payload = {
                    canonical_headers[header]: (value or "").strip()
                    for header, value in raw_row.items()
                    if header in canonical_headers
                    and canonical_headers[header] in ProductCSVImporter.IMPORTABLE_FIELDS
                }
                if not any(payload.values()):
                    continue
                result.rows_read += 1
                row_errors: dict[str, str] = {}
                sku = str(payload.get("sku", "")).strip().upper()
                barcode = str(payload.get("barcode", "")).strip()
                if sku and sku in seen_skus:
                    row_errors["sku"] = "is duplicated within the CSV"
                if barcode and barcode in seen_barcodes:
                    row_errors["barcode"] = "is duplicated within the CSV"
                seen_skus.add(sku)
                if barcode:
                    seen_barcodes.add(barcode)

                if row_errors:
                    result.errors.append({"row": row_number, "errors": row_errors})
                    continue

                existing_query = Product.query.filter_by(sku=sku)
                if workspace_id is not None:
                    existing_query = existing_query.filter_by(workspace_id=workspace_id)
                existing = existing_query.first() if sku else None
                try:
                    if existing and update_existing:
                        ProductService.update(
                            existing,
                            payload,
                            commit=False,
                            workspace_id=workspace_id,
                        )
                        pending_updated += 1
                    else:
                        ProductService.create(
                            payload,
                            commit=False,
                            workspace_id=workspace_id,
                        )
                        pending_created += 1
                except ProductValidationError as error:
                    result.errors.append({"row": row_number, "errors": error.errors})
        except csv.Error as error:
            result.errors.append(
                {"row": result.rows_read + 2, "errors": {"file": f"invalid CSV: {error}"}}
            )

        if result.errors:
            db.session.rollback()
            return result

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            result.errors.append(
                {
                    "row": 1,
                    "errors": {"file": "an SKU or barcode conflicts with existing data"},
                }
            )
            return result

        result.created = pending_created
        result.updated = pending_updated
        result.committed = True
        return result
