import tempfile
import unittest
from pathlib import Path

from app.domain import AppError, MockSourceProvider, RuleBasedAnalyzer, Store, buzz_score


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

    def test_collection_settings_save_and_validation(self):
        state = self.store.mutate("/api/collection-settings", {
            "keywords": ["比較", "情報"], "genre": "比較", "watched_accounts": ["架空の比較ノート"], "period_days": 14,
        })
        self.assertEqual(["比較", "情報"], state["collection_settings"]["keywords"])
        self.assertEqual(14, state["collection_settings"]["period_days"])
        with self.assertRaises(AppError):
            self.store.mutate("/api/collection-settings", {"keywords": "not-a-list", "genre": "", "watched_accounts": [], "period_days": 7})
        with self.assertRaises(AppError):
            self.store.mutate("/api/collection-settings", {"keywords": [], "genre": "", "watched_accounts": [], "period_days": 999})

    def test_source_manual_entry_holds_followers_and_posted_at(self):
        state = self.store.mutate("/api/sources", {
            "text": "手入力のテスト投稿です。", "url": "https://x.com/testuser/status/12345", "author": "テストユーザー",
            "likes": 10, "reposts": 2, "replies": 1, "impressions": 500, "topic": "テスト",
            "posted_at": "2026-09-14T10:00:00Z", "followers": 1000,
        })
        added = state["sources"][0]
        self.assertEqual("2026-09-14T10:00:00Z", added["posted_at"])
        self.assertEqual(1000, added["author_followers"])
        with self.assertRaises(AppError):
            self.store.mutate("/api/sources", dict(
                text="t", url="https://x.com/u/status/1", author="a", likes=0, reposts=0, replies=0,
                impressions=0, topic="", posted_at="not-a-date", followers=0,
            ))

    def test_analysis_includes_buzz_score_and_content_fields(self):
        analysis = self.store.state()["analysis"][0]
        for key in ("buzz_score", "hook", "structure", "cta", "theme", "target", "appeals", "length"):
            self.assertIn(key, analysis)

    def test_generate_from_source_only_does_not_copy_original_text(self):
        state = self.store.state()
        source = state["sources"][0]
        generated = self.store.mutate("/api/drafts/generate", {"source_id": source["id"], "campaign_id": None, "angle": ""})
        draft = generated["drafts"][0]
        self.assertNotIn(source["text"], draft["text"])
        self.assertLessEqual(len(draft["text"]), 5000)
        self.assertTrue(draft["text"].strip())

    def test_generate_with_campaign_and_source_still_passes_check(self):
        campaign = self.approved_campaign()
        source = self.store.state()["sources"][0]
        state = self.store.mutate("/api/drafts/generate", {"campaign_id": campaign["id"], "source_id": source["id"], "angle": ""})
        draft = state["drafts"][0]
        checked = self.store.mutate("/api/drafts/check", {"id": draft["id"]})
        self.assertTrue(checked["drafts"][0]["checks"]["passed"])
        self.assertNotIn(source["text"], draft["text"])


class PureFunctionTest(unittest.TestCase):
    def test_buzz_score_rewards_reach_relative_to_followers(self):
        small_account = buzz_score(likes=50, reposts=10, replies=5, impressions=5000, followers=1000)
        large_account = buzz_score(likes=50, reposts=10, replies=5, impressions=5000, followers=100000)
        self.assertGreater(small_account, large_account)

    def test_buzz_score_handles_zero_followers_and_impressions(self):
        self.assertEqual(0.0, buzz_score(0, 0, 0, 0, 0))

    def test_rule_based_analyzer_classifies_question_hook_and_cta(self):
        analysis = RuleBasedAnalyzer().analyze("これって知らないと損しませんか？\nフォローして続きをお待ちください。", topic="節約", audience="")
        self.assertEqual("question", analysis["hook"])
        self.assertEqual("follow", analysis["cta"])
        self.assertIn("curiosity", analysis["appeals"])
        self.assertEqual("節約", analysis["theme"])

    def test_mock_source_provider_filters_by_genre_and_keyword(self):
        provider = MockSourceProvider()
        all_sources = provider.fetch({"keywords": [], "genre": "", "watched_accounts": []})
        filtered = provider.fetch({"keywords": [], "genre": "比較", "watched_accounts": []})
        self.assertLess(len(filtered), len(all_sources))
        self.assertTrue(all(s["topic"] == "比較" for s in filtered))
        by_keyword = provider.fetch({"keywords": ["存在しないキーワードxyz"], "genre": "", "watched_accounts": []})
        self.assertEqual([], by_keyword)


if __name__ == "__main__":
    unittest.main()
