import tempfile
import unittest
from pathlib import Path

from app.domain import AppError, Store


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def approved_campaign(self):
        return next(c for c in self.store.state()["campaigns"] if c["status"] == "approved")

    def test_mock_data_is_clear_and_export_is_marked_publication_prohibited(self):
        draft = self.store.state()["drafts"][0]
        self.store.mutate("/api/drafts/check", {"id": draft["id"]})
        self.store.mutate("/api/drafts/approve", {"id": draft["id"], "confirmed": True})
        exported = self.store.mutate("/api/drafts/export", {"id": draft["id"]})
        self.assertTrue(exported["is_mock"])
        self.assertIn("サンプル・公開不可", exported["text"])

    def test_affiliate_draft_requires_disclosure_and_exact_url(self):
        campaign = self.approved_campaign()
        state = self.store.mutate("/api/drafts/save", {
            "title": "危険な下書き", "text": "購入はこちら https://example.com/wrong",
            "campaign_id": campaign["id"], "source_id": None
        })
        draft = state["drafts"][0]
        checked = self.store.mutate("/api/drafts/check", {"id": draft["id"]})
        codes = {x["code"] for x in checked["drafts"][0]["checks"]["issues"]}
        self.assertIn("disclosure_missing", codes)
        self.assertIn("affiliate_url_missing", codes)
        with self.assertRaises(AppError):
            self.store.mutate("/api/drafts/approve", {"id": draft["id"], "confirmed": True})

    def test_edit_invalidates_approval(self):
        draft = self.store.state()["drafts"][0]
        self.store.mutate("/api/drafts/check", {"id": draft["id"]})
        self.store.mutate("/api/drafts/approve", {"id": draft["id"], "confirmed": True})
        state = self.store.mutate("/api/drafts/save", {
            "id": draft["id"], "title": draft["title"], "text": draft["text"] + "\n追記",
            "campaign_id": draft["campaign_id"], "source_id": draft["source_id"]
        })
        changed = next(x for x in state["drafts"] if x["id"] == draft["id"])
        self.assertEqual("draft", changed["status"])
        self.assertIsNone(changed["checks"])

    def test_metrics_are_cumulative_and_consistent(self):
        draft = self.store.state()["drafts"][0]
        self.store.mutate("/api/drafts/check", {"id": draft["id"]})
        self.store.mutate("/api/drafts/approve", {"id": draft["id"], "confirmed": True})
        state = self.store.mutate("/api/metrics", {"draft_id": draft["id"], "impressions": 1000, "clicks": 20, "conversions": 2, "revenue_yen": 1000})
        self.assertEqual(2.0, state["summary"]["ctr"])
        state = self.store.mutate("/api/metrics", {"draft_id": draft["id"], "impressions": 2000, "clicks": 50, "conversions": 5, "revenue_yen": 2500})
        self.assertEqual(1, len(state["metrics"]))
        self.assertEqual(2000, state["summary"]["impressions"])
        with self.assertRaises(AppError):
            self.store.mutate("/api/metrics", {"draft_id": draft["id"], "impressions": 1, "clicks": 2, "conversions": 0, "revenue_yen": 0})

    def test_live_mode_fails_closed(self):
        live = Store(Path(self.temp.name) / "live.sqlite3", mode="live")
        with self.assertRaises(AppError) as error:
            live.mutate("/api/collect", {})
        self.assertEqual(503, error.exception.status)
        live.close()


if __name__ == "__main__":
    unittest.main()
