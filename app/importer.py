from __future__ import annotations

import csv
import hashlib
import json
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .database import connect, initialize


SALES_HEADERS = ["order_id", "date", "store_id", "product_id", "qty", "amount", "payment"]
STORE_HEADERS = ["store_id", "store_name", "category", "district"]
PRODUCT_HEADERS = ["product_id", "product_name", "product_category", "unit_price"]
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y")


class DataImportError(RuntimeError):
    pass


def normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def normalize_id(value: Any) -> str:
    return normalize_text(value).upper()


def parse_date(value: Any) -> str | None:
    text = normalize_text(value)
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_cents(value: Any) -> int:
    text = normalize_text(value).replace(",", "")
    for symbol in ("¥", "￥", "RMB", "CNY"):
        text = text.replace(symbol, "")
    if not text:
        raise InvalidOperation("empty amount")
    decimal_value = Decimal(text)
    return int((decimal_value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def source_fingerprint(data_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in ("stores.csv", "products.csv", "sales.csv"):
        path = data_dir / name
        if not path.exists():
            raise DataImportError(f"Missing required source file: {path}")
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def read_csv(path: Path, headers: list[str]) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != headers:
            raise DataImportError(
                f"Unexpected columns in {path.name}: {reader.fieldnames}; expected {headers}"
            )
        return list(reader)


def _dimension_rows(data_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stores: list[dict[str, Any]] = []
    products: list[dict[str, Any]] = []
    seen_stores: dict[str, dict[str, Any]] = {}
    seen_products: dict[str, dict[str, Any]] = {}

    for raw in read_csv(data_dir / "stores.csv", STORE_HEADERS):
        row = {
            "store_id": normalize_id(raw["store_id"]),
            "store_name": normalize_text(raw["store_name"]),
            "category": normalize_text(raw["category"]),
            "district": normalize_text(raw["district"]),
        }
        if not all(row.values()):
            raise DataImportError(f"Incomplete store dimension row: {raw}")
        existing = seen_stores.get(row["store_id"])
        if existing and existing != row:
            raise DataImportError(f"Conflicting store_id: {row['store_id']}")
        seen_stores[row["store_id"]] = row

    for raw in read_csv(data_dir / "products.csv", PRODUCT_HEADERS):
        try:
            price = parse_cents(raw["unit_price"])
        except InvalidOperation as error:
            raise DataImportError(f"Invalid product price: {raw}") from error
        row = {
            "product_id": normalize_id(raw["product_id"]),
            "product_name": normalize_text(raw["product_name"]),
            "product_category": normalize_text(raw["product_category"]),
            "unit_price_cents": price,
        }
        if not row["product_id"] or not row["product_name"] or not row["product_category"]:
            raise DataImportError(f"Incomplete product dimension row: {raw}")
        existing = seen_products.get(row["product_id"])
        if existing and existing != row:
            raise DataImportError(f"Conflicting product_id: {row['product_id']}")
        seen_products[row["product_id"]] = row

    stores.extend(seen_stores.values())
    products.extend(seen_products.values())
    return stores, products


def import_data(data_dir: Path, database_path: Path) -> dict[str, Any]:
    fingerprint = source_fingerprint(data_dir)
    stores, products = _dimension_rows(data_dir)
    sales = read_csv(data_dir / "sales.csv", SALES_HEADERS)
    store_ids = {row["store_id"] for row in stores}
    product_map = {row["product_id"]: row for row in products}
    reasons: Counter[str] = Counter()
    accepted = 0
    price_mismatch_rows = 0
    seen: set[tuple[Any, ...]] = set()

    connection = connect(database_path)
    initialize(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table in ("sales_rejected", "sales_clean", "sales_raw", "stores", "products", "import_runs"):
            connection.execute(f"DELETE FROM {table}")

        connection.executemany(
            "INSERT INTO stores(store_id, store_name, category, district) VALUES (:store_id, :store_name, :category, :district)",
            stores,
        )
        connection.executemany(
            "INSERT INTO products(product_id, product_name, product_category, unit_price_cents) VALUES (:product_id, :product_name, :product_category, :unit_price_cents)",
            products,
        )

        for source_line, raw in enumerate(sales, start=2):
            raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
            connection.execute(
                """INSERT INTO sales_raw(source_line, order_id, sale_date, store_id, product_id, qty, amount, payment, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_line,
                    raw.get("order_id"),
                    raw.get("date"),
                    raw.get("store_id"),
                    raw.get("product_id"),
                    raw.get("qty"),
                    raw.get("amount"),
                    raw.get("payment"),
                    raw_json,
                ),
            )

            row: dict[str, Any] = {
                "order_id": normalize_text(raw.get("order_id")),
                "sale_date": parse_date(raw.get("date")),
                "store_id": normalize_id(raw.get("store_id")),
                "product_id": normalize_id(raw.get("product_id")),
                "payment": normalize_text(raw.get("payment")),
            }
            row_reasons: list[str] = []
            if not row["order_id"]:
                row_reasons.append("missing_order_id")
            if not row["sale_date"]:
                row_reasons.append("invalid_date")
            try:
                row["qty"] = int(normalize_text(raw.get("qty")))
                if row["qty"] <= 0:
                    row_reasons.append("nonpositive_qty")
            except ValueError:
                row["qty"] = None
                row_reasons.append("invalid_qty")
            try:
                row["amount_cents"] = parse_cents(raw.get("amount"))
                if row["amount_cents"] < 0:
                    row_reasons.append("negative_amount")
            except InvalidOperation:
                row["amount_cents"] = None
                row_reasons.append("missing_or_invalid_amount")
            if row["store_id"] not in store_ids:
                row_reasons.append("unknown_store")
            if row["product_id"] not in product_map:
                row_reasons.append("unknown_product")

            if not row_reasons:
                canonical = (
                    row["order_id"],
                    row["sale_date"],
                    row["store_id"],
                    row["product_id"],
                    row["qty"],
                    row["amount_cents"],
                    row["payment"],
                )
                if canonical in seen:
                    row_reasons.append("duplicate")
                else:
                    seen.add(canonical)

            if row_reasons:
                reasons.update(row_reasons)
                connection.execute(
                    "INSERT INTO sales_rejected(source_line, reason_codes, normalized_json, raw_json) VALUES (?, ?, ?, ?)",
                    (
                        source_line,
                        json.dumps(row_reasons, ensure_ascii=False),
                        json.dumps(row, ensure_ascii=False, sort_keys=True),
                        raw_json,
                    ),
                )
                continue

            expected = product_map[row["product_id"]]["unit_price_cents"] * row["qty"]
            if expected != row["amount_cents"]:
                price_mismatch_rows += 1
            connection.execute(
                """INSERT INTO sales_clean(source_line, order_id, sale_date, store_id, product_id, qty, amount_cents, payment)
                   VALUES (:source_line, :order_id, :sale_date, :store_id, :product_id, :qty, :amount_cents, :payment)""",
                {**row, "source_line": source_line},
            )
            accepted += 1

        result = {
            "source_fingerprint": fingerprint,
            "raw_rows": len(sales),
            "accepted_rows": accepted,
            "rejected_rows": len(sales) - accepted,
            "reason_counts": dict(sorted(reasons.items())),
            "price_mismatch_rows": price_mismatch_rows,
        }
        connection.execute(
            """INSERT INTO import_runs(imported_at, source_fingerprint, raw_rows, accepted_rows, rejected_rows, reason_counts, price_mismatch_rows)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                fingerprint,
                result["raw_rows"],
                result["accepted_rows"],
                result["rejected_rows"],
                json.dumps(result["reason_counts"], ensure_ascii=False, sort_keys=True),
                price_mismatch_rows,
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('source_fingerprint', ?)",
            (fingerprint,),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_imported(data_dir: Path, database_path: Path, force: bool = False) -> dict[str, Any]:
    fingerprint = source_fingerprint(data_dir)
    if not force and database_path.exists():
        connection = connect(database_path)
        initialize(connection)
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'source_fingerprint'"
        ).fetchone()
        latest = connection.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
        connection.close()
        if row and row["value"] == fingerprint and latest:
            result = dict(latest)
            result["reason_counts"] = json.loads(result["reason_counts"])
            return result
    return import_data(data_dir, database_path)

