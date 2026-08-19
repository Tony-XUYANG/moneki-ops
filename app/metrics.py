from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from .database import connect


def money(cents: int | None) -> str:
    return f"{Decimal(cents or 0) / 100:.2f}"


def ratio(numerator: int, denominator: int) -> str:
    if not denominator:
        return "0.00"
    return str(
        (Decimal(numerator) / Decimal(denominator) / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def percent_change(current: int, previous: int) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 2)


def parse_iso(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid ISO date: {value}") from error


@dataclass(frozen=True)
class DateRange:
    start: str
    end: str

    @classmethod
    def validated(cls, start: str, end: str) -> "DateRange":
        start_date = parse_iso(start)
        end_date = parse_iso(end)
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        if (end_date - start_date).days > 366:
            raise ValueError("date range cannot exceed 367 days")
        return cls(start=start_date.isoformat(), end=end_date.isoformat())

    @property
    def previous(self) -> "DateRange":
        start = parse_iso(self.start)
        end = parse_iso(self.end)
        days = (end - start).days + 1
        previous_end = start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=days - 1)
        return DateRange(previous_start.isoformat(), previous_end.isoformat())


class MetricsService:
    def __init__(self, database_path):
        self.database_path = database_path

    def _connection(self):
        return connect(self.database_path)

    @staticmethod
    def _store_clause(store_ids: Iterable[str] | None) -> tuple[str, list[str]]:
        ids = sorted({value.strip().upper() for value in store_ids or [] if value.strip()})
        if not ids:
            return "", []
        placeholders = ",".join("?" for _ in ids)
        return f" AND s.store_id IN ({placeholders})", ids

    def date_bounds(self) -> dict[str, str]:
        connection = self._connection()
        row = connection.execute(
            "SELECT MIN(sale_date) AS min_date, MAX(sale_date) AS max_date FROM sales_clean"
        ).fetchone()
        connection.close()
        if not row or not row["min_date"]:
            raise RuntimeError("No clean sales data is available")
        return {"min_date": row["min_date"], "max_date": row["max_date"]}

    def stores(self) -> list[dict[str, Any]]:
        connection = self._connection()
        rows = connection.execute(
            "SELECT store_id, store_name, category, district FROM stores ORDER BY store_id"
        ).fetchall()
        connection.close()
        return [dict(row) for row in rows]

    def products(self) -> list[dict[str, Any]]:
        connection = self._connection()
        rows = connection.execute(
            "SELECT product_id, product_name, product_category, unit_price_cents FROM products ORDER BY product_id"
        ).fetchall()
        connection.close()
        return [
            {**dict(row), "unit_price": money(row["unit_price_cents"])} for row in rows
        ]

    def validate_stores(self, store_ids: Iterable[str] | None) -> list[str]:
        ids = sorted({value.strip().upper() for value in store_ids or [] if value.strip()})
        if not ids:
            return []
        available = {row["store_id"] for row in self.stores()}
        unknown = sorted(set(ids) - available)
        if unknown:
            raise ValueError(f"Unknown store_ids: {', '.join(unknown)}")
        return ids

    def _aggregate(self, period: DateRange, store_ids: Iterable[str] | None = None) -> dict[str, int]:
        store_clause, store_params = self._store_clause(store_ids)
        connection = self._connection()
        row = connection.execute(
            f"""SELECT COALESCE(SUM(s.amount_cents), 0) AS revenue_cents,
                       COUNT(DISTINCT s.store_id || ':' || s.order_id) AS order_count
                FROM sales_clean s
                WHERE s.sale_date BETWEEN ? AND ? {store_clause}""",
            [period.start, period.end, *store_params],
        ).fetchone()
        connection.close()
        return {"revenue_cents": row["revenue_cents"], "order_count": row["order_count"]}

    def overview(self, period: DateRange, store_ids: Iterable[str] | None = None) -> dict[str, Any]:
        stores = self.validate_stores(store_ids)
        current = self._aggregate(period, stores)
        previous_period = period.previous
        previous = self._aggregate(previous_period, stores)
        current_aov_cents = round(current["revenue_cents"] / current["order_count"]) if current["order_count"] else 0
        previous_aov_cents = round(previous["revenue_cents"] / previous["order_count"]) if previous["order_count"] else 0
        return {
            "period": {"start": period.start, "end": period.end},
            "previous_period": {"start": previous_period.start, "end": previous_period.end},
            "store_ids": stores,
            "revenue": money(current["revenue_cents"]),
            "revenue_cents": current["revenue_cents"],
            "order_count": current["order_count"],
            "aov": ratio(current["revenue_cents"], current["order_count"]),
            "changes": {
                "revenue_pct": percent_change(current["revenue_cents"], previous["revenue_cents"]),
                "orders_pct": percent_change(current["order_count"], previous["order_count"]),
                "aov_pct": percent_change(current_aov_cents, previous_aov_cents),
            },
        }

    def daily(self, period: DateRange, store_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        stores = self.validate_stores(store_ids)
        store_clause, store_params = self._store_clause(stores)
        connection = self._connection()
        rows = connection.execute(
            f"""SELECT s.sale_date,
                       SUM(s.amount_cents) AS revenue_cents,
                       COUNT(DISTINCT s.store_id || ':' || s.order_id) AS order_count
                FROM sales_clean s
                WHERE s.sale_date BETWEEN ? AND ? {store_clause}
                GROUP BY s.sale_date ORDER BY s.sale_date""",
            [period.start, period.end, *store_params],
        ).fetchall()
        connection.close()
        by_date = {row["sale_date"]: row for row in rows}
        current = parse_iso(period.start)
        end = parse_iso(period.end)
        result: list[dict[str, Any]] = []
        while current <= end:
            row = by_date.get(current.isoformat())
            revenue_cents = row["revenue_cents"] if row else 0
            order_count = row["order_count"] if row else 0
            result.append(
                {
                    "date": current.isoformat(),
                    "revenue": money(revenue_cents),
                    "revenue_cents": revenue_cents,
                    "order_count": order_count,
                    "aov": ratio(revenue_cents, order_count),
                }
            )
            current += timedelta(days=1)
        return result

    def top_products(
        self, period: DateRange, store_ids: Iterable[str] | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        stores = self.validate_stores(store_ids)
        store_clause, store_params = self._store_clause(stores)
        connection = self._connection()
        rows = connection.execute(
            f"""SELECT p.product_id, p.product_name, p.product_category,
                       SUM(s.qty) AS qty, SUM(s.amount_cents) AS revenue_cents,
                       COUNT(DISTINCT s.store_id || ':' || s.order_id) AS order_count
                FROM sales_clean s
                JOIN products p ON p.product_id = s.product_id
                WHERE s.sale_date BETWEEN ? AND ? {store_clause}
                GROUP BY p.product_id, p.product_name, p.product_category
                ORDER BY revenue_cents DESC, p.product_id ASC LIMIT ?""",
            [period.start, period.end, *store_params, limit],
        ).fetchall()
        connection.close()
        return [
            {
                "rank": index,
                **dict(row),
                "revenue": money(row["revenue_cents"]),
            }
            for index, row in enumerate(rows, start=1)
        ]

    def store_comparison(self, period: DateRange) -> list[dict[str, Any]]:
        connection = self._connection()
        rows = connection.execute(
            """SELECT st.store_id, st.store_name, st.category, st.district,
                      COALESCE(SUM(s.amount_cents), 0) AS revenue_cents,
                      COUNT(DISTINCT s.order_id) AS order_count
               FROM stores st
               LEFT JOIN sales_clean s ON s.store_id = st.store_id AND s.sale_date BETWEEN ? AND ?
               GROUP BY st.store_id, st.store_name, st.category, st.district
               ORDER BY revenue_cents DESC, st.store_id""",
            (period.start, period.end),
        ).fetchall()
        connection.close()
        return [
            {
                **dict(row),
                "revenue": money(row["revenue_cents"]),
                "aov": ratio(row["revenue_cents"], row["order_count"]),
            }
            for row in rows
        ]

    def store_category_revenue(self, period: DateRange) -> list[dict[str, Any]]:
        connection = self._connection()
        rows = connection.execute(
            """SELECT st.category, SUM(s.amount_cents) AS revenue_cents,
                      COUNT(DISTINCT s.store_id || ':' || s.order_id) AS order_count
               FROM sales_clean s JOIN stores st ON st.store_id = s.store_id
               WHERE s.sale_date BETWEEN ? AND ?
               GROUP BY st.category ORDER BY revenue_cents DESC, st.category""",
            (period.start, period.end),
        ).fetchall()
        connection.close()
        return [
            {**dict(row), "revenue": money(row["revenue_cents"])} for row in rows
        ]

    def find_product(self, name: str) -> list[dict[str, Any]]:
        needle = name.strip().casefold().replace(" ", "")
        return [
            row
            for row in self.products()
            if needle in row["product_name"].casefold().replace(" ", "")
            or row["product_name"].casefold().replace(" ", "") in needle
        ]

    def product_revenue(self, product_id: str, period: DateRange) -> dict[str, Any] | None:
        connection = self._connection()
        row = connection.execute(
            """SELECT p.product_id, p.product_name, p.product_category,
                      COALESCE(SUM(s.amount_cents), 0) AS revenue_cents,
                      COALESCE(SUM(s.qty), 0) AS qty,
                      COUNT(DISTINCT s.store_id || ':' || s.order_id) AS order_count
               FROM products p LEFT JOIN sales_clean s
                 ON s.product_id = p.product_id AND s.sale_date BETWEEN ? AND ?
               WHERE p.product_id = ? GROUP BY p.product_id, p.product_name, p.product_category""",
            (period.start, period.end, product_id.upper()),
        ).fetchone()
        connection.close()
        if not row:
            return None
        return {**dict(row), "revenue": money(row["revenue_cents"])}

    def monthly_aov(self, end_date: str | None = None, months: int = 3) -> list[dict[str, Any]]:
        bounds = self.date_bounds()
        end = parse_iso(end_date or bounds["max_date"])
        first_month = date(end.year, end.month, 1)
        month_starts: list[date] = []
        cursor = first_month
        for _ in range(months):
            month_starts.append(cursor)
            cursor = date(cursor.year - 1, 12, 1) if cursor.month == 1 else date(cursor.year, cursor.month - 1, 1)
        result: list[dict[str, Any]] = []
        for start in reversed(month_starts):
            last = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
            if last > end:
                last = end
            aggregate = self._aggregate(DateRange(start.isoformat(), last.isoformat()))
            result.append(
                {
                    "month": start.strftime("%Y-%m"),
                    "start": start.isoformat(),
                    "end": last.isoformat(),
                    "revenue": money(aggregate["revenue_cents"]),
                    "order_count": aggregate["order_count"],
                    "aov": ratio(aggregate["revenue_cents"], aggregate["order_count"]),
                }
            )
        return result

    def quality(self) -> dict[str, Any]:
        connection = self._connection()
        run = connection.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
        samples = connection.execute(
            "SELECT source_line, reason_codes, normalized_json FROM sales_rejected ORDER BY source_line LIMIT 8"
        ).fetchall()
        connection.close()
        if not run:
            raise RuntimeError("No import run found")
        return {
            "imported_at": run["imported_at"],
            "source_fingerprint": run["source_fingerprint"],
            "raw_rows": run["raw_rows"],
            "accepted_rows": run["accepted_rows"],
            "rejected_rows": run["rejected_rows"],
            "acceptance_rate": round(run["accepted_rows"] / run["raw_rows"] * 100, 2),
            "reason_counts": json.loads(run["reason_counts"]),
            "price_mismatch_rows": run["price_mismatch_rows"],
            "samples": [
                {
                    "source_line": row["source_line"],
                    "reason_codes": json.loads(row["reason_codes"]),
                    "normalized": json.loads(row["normalized_json"]),
                }
                for row in samples
            ],
        }

