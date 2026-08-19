from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.ai import AiService
from app.importer import import_data
from app.metrics import DateRange, MetricsService


ROOT = Path(__file__).resolve().parents[1]


class MonekiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database_path = Path(cls.temp_dir.name) / "test.db"
        cls.import_result = import_data(ROOT / "data", cls.database_path)
        cls.metrics = MetricsService(cls.database_path)
        cls.assistant = AiService(cls.metrics)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_import_quarantines_dirty_rows_and_normalizes_currency(self):
        self.assertEqual(self.import_result["raw_rows"], 12131)
        self.assertEqual(self.import_result["accepted_rows"], 11823)
        self.assertEqual(self.import_result["rejected_rows"], 308)
        self.assertEqual(self.import_result["reason_counts"]["missing_or_invalid_amount"], 120)
        self.assertEqual(self.import_result["reason_counts"]["duplicate"], 78)
        self.assertEqual(self.import_result["price_mismatch_rows"], 0)

    def test_dashboard_uses_decimal_money_and_distinct_orders(self):
        period = DateRange("2026-05-01", "2026-07-31")
        overview = self.metrics.overview(period)
        self.assertEqual(overview["revenue"], "425180.00")
        self.assertEqual(overview["order_count"], 11823)
        self.assertEqual(overview["aov"], "35.96")

    def test_daily_endpoint_contract_fills_every_date(self):
        rows = self.metrics.daily(DateRange("2026-06-01", "2026-06-30"))
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["date"], "2026-06-01")
        self.assertEqual(rows[-1]["date"], "2026-06-30")
        self.assertTrue(all("revenue" in row and "order_count" in row and "aov" in row for row in rows))

    def test_top_product_is_joined_from_dimension_table(self):
        rows = self.metrics.top_products(DateRange("2026-06-01", "2026-06-30"))
        self.assertEqual(rows[0]["product_id"], "P06")
        self.assertEqual(rows[0]["product_name"], "牛肉poke")
        self.assertEqual(rows[0]["revenue"], "13524.00")

    def test_store_category_query_requires_store_join(self):
        rows = self.metrics.store_category_revenue(DateRange("2026-05-01", "2026-07-31"))
        self.assertEqual(rows[0]["category"], "日料")
        self.assertEqual(rows[0]["revenue"], "88718.00")

    def test_ai_answer_number_equals_query_tool_result(self):
        result = self.assistant.ask("牛肉poke 六月卖了多少钱？")
        expected = self.metrics.product_revenue("P06", DateRange("2026-06-01", "2026-06-30"))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["evidence"]["value"], expected["revenue"])
        self.assertIn("¥13524.00", result["answer"])
        self.assertEqual(result["tool_call"]["name"], "query_product_revenue")

    def test_follow_up_inherits_product_and_replaces_month(self):
        first = self.assistant.ask("牛肉poke 六月卖了多少钱？")
        follow_up = self.assistant.ask("那五月呢？", first["session_id"])
        expected = self.metrics.product_revenue("P06", DateRange("2026-05-01", "2026-05-31"))
        self.assertEqual(follow_up["evidence"]["value"], expected["revenue"])
        self.assertIn("2026-05-01 至 2026-05-31", follow_up["answer"])

    def test_unsupported_question_is_not_invented(self):
        result = self.assistant.ask("天气怎么样？")
        self.assertEqual(result["status"], "not_answerable")
        self.assertIsNone(result["evidence"])
        self.assertNotIn("¥", result["answer"])


if __name__ == "__main__":
    unittest.main()

