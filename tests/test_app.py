import json
import tempfile
import unittest
from pathlib import Path
from io import BytesIO

import app


class SalesAssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "sales.sqlite3"
        app.initialize(app.CSV_PATH, cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def ask(self, q):
        return app.answer(q, self.db)

    def test_total_revenue_is_computed(self):
        result = self.ask("What is the total revenue?")
        self.assertEqual(result["intent"], "total_revenue")
        self.assertGreater(result["supporting_data"]["value"], 0)

    def test_top_product_has_supporting_figures(self):
        result = self.ask("Which product generated the highest revenue?")
        self.assertTrue(result["supporting_data"]["label"])
        self.assertGreater(result["supporting_data"]["revenue"], 0)

    def test_monthly_trend_returns_ordered_months(self):
        months = self.ask("Show monthly revenue trend")["supporting_data"]["months"]
        self.assertGreaterEqual(len(months), 5)
        self.assertEqual(months, sorted(months, key=lambda row: row["month"]))

    def test_region_rank(self):
        result = self.ask("Which region generated the highest sales?")
        self.assertIn(result["supporting_data"]["label"], {"North", "South", "East", "West"})

    def test_month_comparison(self):
        result = self.ask("How did revenue change between January 2026 and February 2026?")
        self.assertEqual(len(result["supporting_data"]["months"]), 2)
        self.assertIsInstance(result["supporting_data"]["change"], float)

    def test_unsupported_question_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ask("Will it rain tomorrow?")

    def test_destructive_question_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ask("Drop table sales")

    def test_api_requires_string_question(self):
        payload=json.dumps({"question": 42}).encode()
        environ={"PATH_INFO":"/api/ask","REQUEST_METHOD":"POST","CONTENT_LENGTH":str(len(payload)),"wsgi.input":BytesIO(payload)}
        response={}
        body=b"".join(app.application(environ,lambda status,headers: response.update(status=status)))
        self.assertTrue(response["status"].startswith("400"))
        self.assertIn("string",json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
