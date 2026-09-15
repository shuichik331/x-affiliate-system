import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.domain import (
    AppError,
    DRAFT_STYLES,
    MockSourceProvider,
    RuleBasedAnalyzer,
    RuleBasedDraftGenerator,
    RuleBasedMatcher,
    Store,
    WebIntentPublishProvider,
    buzz_score,
)


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

    def _approve(self, draft_id):
        self.store.mutate("/api/drafts/check", {"id": draft_id})
        return self.store.mutate("/api/drafts/approve", {"id": draft_id, "confirmed": True})

    def _publish(self, draft_id, url="https://x.com/myaccount/status/999", posted_at="2026-09-15T09:00:00Z"):
        state = self.store.mutate("/api/posts/publish", {"draft_id": draft_id, "url": url, "posted_at": posted_at})
        return next(p for p in state["posts"] if p["draft_id"] == draft_id)

    def test_metrics_are_cumulative_and_consistent(self):
        draft = self.store.state()["drafts"][0]
        self._approve(draft["id"])
        post = self._publish(draft["id"])
        state = self.store.mutate("/api/metrics", {"post_id": post["id"], "impressions": 1000, "likes": 30, "reposts": 5, "replies": 2, "link_clicks": 20, "conversions": 2, "revenue_yen": 1000})
        self.assertEqual(2.0, state["summary"]["ctr"])
        state = self.store.mutate("/api/metrics", {"post_id": post["id"], "impressions": 2000, "likes": 60, "reposts": 10, "replies": 4, "link_clicks": 50, "conversions": 5, "revenue_yen": 2500})
        self.assertEqual(1, len(state["metrics"]))
        self.assertEqual(2000, state["summary"]["impressions"])
        with self.assertRaises(AppError):
            self.store.mutate("/api/metrics", {"post_id": post["id"], "impressions": 1, "likes": 0, "reposts": 0, "replies": 0, "link_clicks": 2, "conversions": 0, "revenue_yen": 0})

    def test_publish_requires_approved_or_exported_draft(self):
        draft = self.store.state()["drafts"][0]  # still status='draft'
        with self.assertRaises(AppError) as error:
            self.store.mutate("/api/posts/publish", {"draft_id": draft["id"], "url": "https://x.com/myaccount/status/1", "posted_at": "2026-09-15T09:00:00Z"})
        self.assertEqual(409, error.exception.status)

    def test_publish_intent_url_only_present_for_approved_or_exported_drafts(self):
        state = self.store.state()
        draft = state["drafts"][0]
        self.assertIsNone(draft["publish_intent_url"])  # still status='draft': no "Xで投稿" affordance

        campaign = self.approved_campaign()
        state = self.store.mutate("/api/drafts/save", {
            "title": "禁止表現で失敗する下書き", "text": "購入はこちら https://example.com/wrong",
            "campaign_id": campaign["id"], "source_id": None,
        })
        bad_draft = state["drafts"][0]
        self.store.mutate("/api/drafts/check", {"id": bad_draft["id"]})
        state = self.store.state()
        failed = next(d for d in state["drafts"] if d["id"] == bad_draft["id"])
        self.assertEqual("review", failed["status"])  # checked but failing, never approved
        self.assertIsNone(failed["publish_intent_url"])  # preflight NG: still no intent URL

        approved = self._approve(draft["id"])
        approved_draft = next(d for d in approved["drafts"] if d["id"] == draft["id"])
        self.assertIsNotNone(approved_draft["publish_intent_url"])
        self.assertTrue(approved_draft["publish_intent_url"].startswith("https://x.com/intent/tweet?text="))

    def test_opening_web_intent_alone_does_not_publish(self):
        # There is no API call behind the "Xで投稿" link (it's a plain <a href> the browser opens),
        # so simply having an intent URL available must never create a posts row on its own.
        draft = self.store.state()["drafts"][0]
        state = self._approve(draft["id"])
        approved_draft = next(d for d in state["drafts"] if d["id"] == draft["id"])
        self.assertIsNotNone(approved_draft["publish_intent_url"])
        self.assertEqual([], self.store.state()["posts"])
        self.assertIn(draft["id"], self.store.state()["tasks"]["drafts_pending_publish"])

    def test_double_publish_upserts_instead_of_duplicating(self):
        draft = self.store.state()["drafts"][0]
        self._approve(draft["id"])
        first = self._publish(draft["id"], url="https://x.com/myaccount/status/111")
        second = self._publish(draft["id"], url="https://x.com/myaccount/status/222")
        self.assertEqual(first["id"], second["id"])
        state = self.store.state()
        matching = [p for p in state["posts"] if p["draft_id"] == draft["id"]]
        self.assertEqual(1, len(matching))
        self.assertEqual("https://x.com/myaccount/status/222", matching[0]["url"])

    def test_publish_rejects_invalid_x_url(self):
        draft = self.store.state()["drafts"][0]
        self._approve(draft["id"])
        for bad_url in ("https://example.com/not-x", "not-a-url", "https://x.com/justahandle"):
            with self.assertRaises(AppError):
                self.store.mutate("/api/posts/publish", {"draft_id": draft["id"], "url": bad_url, "posted_at": "2026-09-15T09:00:00Z"})

    def test_published_post_links_campaign_plan_draft_source(self):
        state = self.store.state()
        campaign = self.approved_campaign()
        source = state["sources"][0]
        state = self.store.mutate("/api/plans/generate", {"campaign_id": campaign["id"], "source_id": source["id"], "angle": ""})
        plan = state["plans"][0]
        state = self.store.mutate("/api/drafts/generate-from-plan", {"plan_id": plan["id"]})
        draft = state["drafts"][0]
        self._approve(draft["id"])
        post = self._publish(draft["id"])
        self.assertEqual(draft["id"], post["draft_id"])
        self.assertEqual(plan["id"], post["plan_id"])
        self.assertEqual(campaign["id"], post["campaign_id"])
        self.assertEqual(source["id"], post["source_id"])
        self.assertEqual("published", post["status"])

        state = self.store.mutate("/api/posts/status", {"id": post["id"], "status": "stopped"})
        updated = next(p for p in state["posts"] if p["id"] == post["id"])
        self.assertEqual("stopped", updated["status"])
        with self.assertRaises(AppError):
            self.store.mutate("/api/posts/status", {"id": post["id"], "status": "not-a-status"})

    def test_published_post_snapshots_title_and_text_unaffected_by_later_draft_edits(self):
        draft = self.store.state()["drafts"][0]
        original_text = draft["text"]
        self._approve(draft["id"])
        post = self._publish(draft["id"])
        self.assertEqual(original_text, post["text"])
        self.assertEqual(draft["title"], post["title"])

        # Editing the draft afterward must not retroactively rewrite what was actually posted.
        state = self.store.mutate("/api/drafts/save", {
            "id": draft["id"], "title": "編集後のタイトル", "text": original_text + "\n後から追記",
            "campaign_id": draft["campaign_id"], "source_id": draft["source_id"],
        })
        still_posted = next(p for p in state["posts"] if p["id"] == post["id"])
        self.assertEqual(original_text, still_posted["text"])
        self.assertEqual(draft["title"], still_posted["title"])

    def test_stopped_post_still_counts_toward_pending_metrics_task(self):
        draft = self.store.state()["drafts"][0]
        self._approve(draft["id"])
        post = self._publish(draft["id"])
        state = self.store.mutate("/api/posts/status", {"id": post["id"], "status": "stopped"})
        self.assertIn(post["id"], state["tasks"]["posts_pending_metrics"])

    def test_performance_calculates_ctr_cvr_epc_rpm(self):
        draft = self.store.state()["drafts"][0]
        self._approve(draft["id"])
        post = self._publish(draft["id"])
        state = self.store.mutate("/api/metrics", {"post_id": post["id"], "impressions": 1000, "likes": 40, "reposts": 10, "replies": 5, "link_clicks": 50, "conversions": 5, "revenue_yen": 2500})
        perf = next(p for p in state["performance"] if p["post_id"] == post["id"])
        self.assertEqual(5.0, perf["ctr"])
        self.assertEqual(10.0, perf["cvr"])
        self.assertEqual(5.5, perf["engagement_rate"])
        self.assertEqual(50.0, perf["epc"])
        self.assertEqual(2500.0, perf["rpm"])
        self.assertEqual(post["campaign_id"], perf["campaign_id"])

    def test_performance_handles_zero_revenue(self):
        draft = self.store.state()["drafts"][0]
        self._approve(draft["id"])
        post = self._publish(draft["id"])
        state = self.store.mutate("/api/metrics", {"post_id": post["id"], "impressions": 1000, "likes": 10, "reposts": 2, "replies": 1, "link_clicks": 30, "conversions": 0, "revenue_yen": 0})
        perf = next(p for p in state["performance"] if p["post_id"] == post["id"])
        self.assertEqual(0.0, perf["epc"])
        self.assertEqual(0.0, perf["rpm"])
        self.assertEqual(0.0, perf["cvr"])

    def test_performance_avoids_division_by_zero_when_impressions_or_clicks_are_zero(self):
        draft = self.store.state()["drafts"][0]
        self._approve(draft["id"])
        post = self._publish(draft["id"])
        state = self.store.mutate("/api/metrics", {"post_id": post["id"], "impressions": 0, "likes": 0, "reposts": 0, "replies": 0, "link_clicks": 0, "conversions": 0, "revenue_yen": 0})
        perf = next(p for p in state["performance"] if p["post_id"] == post["id"])
        self.assertIsNone(perf["ctr"])
        self.assertIsNone(perf["cvr"])
        self.assertIsNone(perf["engagement_rate"])
        self.assertIsNone(perf["epc"])
        self.assertIsNone(perf["rpm"])
        self.assertIsNone(state["summary"]["ctr"])

        state = self.store.mutate("/api/metrics", {"post_id": post["id"], "impressions": 500, "likes": 5, "reposts": 1, "replies": 0, "link_clicks": 0, "conversions": 0, "revenue_yen": 0})
        perf = next(p for p in state["performance"] if p["post_id"] == post["id"])
        self.assertEqual(0.0, perf["ctr"])
        self.assertIsNone(perf["cvr"])
        self.assertIsNone(perf["epc"])

    def test_draft_style_variants_produce_distinct_texts(self):
        state = self.store.state()
        campaign = self.approved_campaign()
        source = state["sources"][0]
        state = self.store.mutate("/api/plans/generate", {"campaign_id": campaign["id"], "source_id": source["id"], "angle": ""})
        plan = state["plans"][0]
        texts = {}
        for style in DRAFT_STYLES:
            state = self.store.mutate("/api/drafts/generate-from-plan", {"plan_id": plan["id"], "style": style})
            draft = state["drafts"][0]
            self.assertNotIn(source["text"], draft["text"])
            self.assertIn("【PR】", draft["text"])
            self.assertIn(campaign["affiliate_url"], draft["text"])
            texts[style] = draft["text"]
        self.assertEqual(len(DRAFT_STYLES), len({t for t in texts.values()}))
        with self.assertRaises(AppError):
            self.store.mutate("/api/drafts/generate-from-plan", {"plan_id": plan["id"], "style": "not-a-style"})

    def test_metrics_table_migrates_from_old_draft_based_schema(self):
        db_path = Path(self.temp.name) / "old-metrics.sqlite3"
        conn = sqlite3.connect(str(db_path))
        # meta.initialized=1 so Store skips demo seeding; only drafts/metrics use the pre-posts-feature schema.
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key,value) VALUES('initialized','1')")
        conn.execute("""CREATE TABLE profile (
            id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL, bio TEXT NOT NULL,
            audience TEXT NOT NULL, niche TEXT NOT NULL, tone TEXT NOT NULL, pillars TEXT NOT NULL
        )""")
        conn.execute("INSERT INTO profile(id,name,bio,audience,niche,tone,pillars) VALUES(1,'旧プロフィール','','','','','')")
        conn.execute("""CREATE TABLE drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, text TEXT NOT NULL,
            campaign_id INTEGER, source_id INTEGER, status TEXT NOT NULL, checks TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, is_mock INTEGER NOT NULL
        )""")
        conn.execute("INSERT INTO drafts(id,title,text,campaign_id,source_id,status,checks,created_at,updated_at,is_mock) VALUES(1,'旧下書き','本文です',NULL,NULL,'approved',NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',0)")
        conn.execute("""CREATE TABLE metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL UNIQUE,
            impressions INTEGER NOT NULL, clicks INTEGER NOT NULL, conversions INTEGER NOT NULL,
            revenue_yen INTEGER NOT NULL, recorded_at TEXT NOT NULL
        )""")
        conn.execute("INSERT INTO metrics(draft_id,impressions,clicks,conversions,revenue_yen,recorded_at) VALUES(1,100,10,1,500,'2026-01-01T00:00:00Z')")
        conn.commit()
        conn.close()

        migrated = Store(db_path)
        state = migrated.state()
        self.assertEqual(1, len(state["drafts"]))  # no demo seeding ran; only the pre-existing draft is present
        self.assertEqual([], state["metrics"])  # old draft-linked rows can't map onto a post; table is rebuilt empty
        self.assertIn("migrate_drop_old_metrics", {a["action"] for a in state["audit"]})  # destructive migration is logged, not silent
        draft = state["drafts"][0]
        migrated.mutate("/api/drafts/check", {"id": draft["id"]})
        migrated.mutate("/api/drafts/approve", {"id": draft["id"], "confirmed": True})
        state = migrated.mutate("/api/posts/publish", {"draft_id": draft["id"], "url": "https://x.com/myaccount/status/1", "posted_at": "2026-09-15T09:00:00Z"})
        post = state["posts"][0]
        state = migrated.mutate("/api/metrics", {"post_id": post["id"], "impressions": 100, "likes": 5, "reposts": 1, "replies": 0, "link_clicks": 10, "conversions": 1, "revenue_yen": 500})
        self.assertEqual(1, len(state["metrics"]))
        migrated.close()

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

    def _set_campaign_fields(self, campaign, **overrides):
        payload = {
            "id": campaign["id"], "name": campaign["name"], "network": campaign["network"], "url": campaign["url"],
            "affiliate_url": campaign["affiliate_url"], "category": campaign["category"], "target": campaign["target"],
            "appeal_points": campaign["appeal_points"], "reward_conditions": campaign["reward_conditions"],
            "prohibited_expressions": campaign["prohibited_expressions"], "reward_yen": campaign["reward_yen"],
            "status": campaign["status"], "notes": campaign["notes"],
        }
        payload.update(overrides)
        return next(c for c in self.store.mutate("/api/campaigns", payload)["campaigns"] if c["id"] == campaign["id"])

    def test_plan_generation_excludes_non_approved_campaigns(self):
        state = self.store.state()
        candidate = next(c for c in state["campaigns"] if c["status"] == "candidate")
        source = state["sources"][0]
        with self.assertRaises(AppError) as error:
            self.store.mutate("/api/plans/generate", {"campaign_id": candidate["id"], "source_id": source["id"], "angle": ""})
        self.assertEqual(409, error.exception.status)
        self.assertFalse(any(m["campaign_id"] == candidate["id"] for m in state["matches"]))

    def test_prohibited_expression_is_detected_in_check(self):
        campaign = self.approved_campaign()
        campaign = self._set_campaign_fields(campaign, prohibited_expressions=["今だけ特別価格"])
        state = self.store.mutate("/api/drafts/save", {
            "title": "禁止表現テスト", "text": "【PR】\n今だけ特別価格でご案内。\n" + campaign["affiliate_url"],
            "campaign_id": campaign["id"], "source_id": None,
        })
        draft = state["drafts"][0]
        checked = self.store.mutate("/api/drafts/check", {"id": draft["id"]})
        codes = {i["code"] for i in checked["drafts"][0]["checks"]["issues"]}
        self.assertIn("prohibited_expression", codes)
        self.assertFalse(checked["drafts"][0]["checks"]["passed"])

    def test_plan_to_draft_to_check_chain_does_not_copy_original_text_and_passes(self):
        state = self.store.state()
        campaign = self.approved_campaign()
        source = state["sources"][0]
        state = self.store.mutate("/api/plans/generate", {"campaign_id": campaign["id"], "source_id": source["id"], "angle": ""})
        plan = state["plans"][0]
        self.assertEqual(campaign["id"], plan["campaign_id"])
        self.assertEqual(source["id"], plan["source_id"])
        self.assertIsInstance(plan["match_score"], int)
        for key in ("target", "theme", "hook", "structure", "appeal", "cta"):
            self.assertIn(key, plan)

        state = self.store.mutate("/api/drafts/generate-from-plan", {"plan_id": plan["id"]})
        draft = state["drafts"][0]
        self.assertEqual(plan["id"], draft["plan_id"])
        self.assertNotIn(source["text"], draft["text"])
        self.assertIn("【PR】", draft["text"])
        self.assertIn(campaign["affiliate_url"], draft["text"])

        checked = self.store.mutate("/api/drafts/check", {"id": draft["id"]})
        self.assertTrue(checked["drafts"][0]["checks"]["passed"])

    def test_profile_change_invalidates_draft_generated_from_plan(self):
        campaign = self.approved_campaign()
        source = self.store.state()["sources"][0]
        state = self.store.mutate("/api/plans/generate", {"campaign_id": campaign["id"], "source_id": source["id"], "angle": ""})
        plan = state["plans"][0]
        state = self.store.mutate("/api/drafts/generate-from-plan", {"plan_id": plan["id"]})
        draft = state["drafts"][0]
        self.store.mutate("/api/drafts/check", {"id": draft["id"]})
        self.store.mutate("/api/drafts/approve", {"id": draft["id"], "confirmed": True})

        profile = self.store.state()["profile"]
        state = self.store.mutate("/api/profile", dict(profile, bio=profile["bio"] + "（更新）"))
        changed = next(d for d in state["drafts"] if d["id"] == draft["id"])
        self.assertEqual("draft", changed["status"])
        self.assertIsNone(changed["checks"])

    def test_match_score_shown_in_state_matches_score_saved_on_plan(self):
        # 表示スコアと企画作成時に保存されるスコアが同じ分析結果（同じ audience）から算出されることの回帰テスト。
        campaign = self.approved_campaign()
        source = self.store.state()["sources"][0]
        shown = next(m["score"] for m in self.store.state()["matches"] if m["campaign_id"] == campaign["id"] and m["source_id"] == source["id"])
        state = self.store.mutate("/api/plans/generate", {"campaign_id": campaign["id"], "source_id": source["id"], "angle": ""})
        plan = state["plans"][0]
        self.assertEqual(shown, plan["match_score"])

    def test_match_reasons_use_japanese_appeal_labels_not_raw_keys(self):
        campaign = self.approved_campaign()
        campaign = self._set_campaign_fields(campaign, appeal_points="知らないと損する、今だけの特別な条件です。")
        state = self.store.mutate("/api/sources", {
            "text": "知らないと損する投稿の作り方。今だけ限定で公開します。", "url": "https://x.com/appealtest/status/1",
            "author": "訴求テスト", "likes": 5, "reposts": 1, "replies": 1, "impressions": 100, "topic": campaign["category"],
            "posted_at": "2026-09-14T10:00:00Z", "followers": 100,
        })
        source = state["sources"][0]
        match = next(m for m in state["matches"] if m["campaign_id"] == campaign["id"] and m["source_id"] == source["id"])
        reason_text = " ".join(match["reasons"])
        self.assertIn("共通する訴求パターン", reason_text)
        self.assertNotIn("curiosity", reason_text)
        self.assertNotIn("urgency", reason_text)

    def test_draft_plan_id_cleared_when_reassigned_to_another_campaign(self):
        state = self.store.state()
        campaign_a = self.approved_campaign()
        source = state["sources"][0]
        state = self.store.mutate("/api/plans/generate", {"campaign_id": campaign_a["id"], "source_id": source["id"], "angle": ""})
        plan = state["plans"][0]
        state = self.store.mutate("/api/drafts/generate-from-plan", {"plan_id": plan["id"]})
        draft = state["drafts"][0]
        self.assertEqual(plan["id"], draft["plan_id"])

        # 同じ案件・参考投稿のまま本文だけ編集した場合は plan_id を維持する。
        state = self.store.mutate("/api/drafts/save", {
            "id": draft["id"], "title": draft["title"], "text": draft["text"] + "\n追記",
            "campaign_id": draft["campaign_id"], "source_id": draft["source_id"],
        })
        unchanged = next(d for d in state["drafts"] if d["id"] == draft["id"])
        self.assertEqual(plan["id"], unchanged["plan_id"])

        # 別の案件を新規登録し、そちらへ付け替えると plan_id は外れる。
        state = self.store.mutate("/api/campaigns", {
            "name": "別案件", "network": "デモASP", "url": "https://example.com/other",
            "affiliate_url": "https://example.com/other-aff", "category": "その他", "target": "", "appeal_points": "",
            "reward_conditions": "", "prohibited_expressions": [], "reward_yen": 100, "status": "approved", "notes": "",
        })
        campaign_b = next(c for c in state["campaigns"] if c["name"] == "別案件")
        state = self.store.mutate("/api/drafts/save", {
            "id": draft["id"], "title": draft["title"], "text": draft["text"],
            "campaign_id": campaign_b["id"], "source_id": draft["source_id"],
        })
        reassigned = next(d for d in state["drafts"] if d["id"] == draft["id"])
        self.assertIsNone(reassigned["plan_id"])
        self.assertEqual(campaign_b["id"], reassigned["campaign_id"])


class PureFunctionTest(unittest.TestCase):
    def test_web_intent_url_uses_official_endpoint_and_round_trips_japanese_newlines_symbols(self):
        provider = WebIntentPublishProvider()
        text = "【PR】これは投稿文です。\n改行・記号（！？＆%#）・絵文字🎉も含みます。\nhttps://example.com/mock-affiliate"
        url = provider.build_intent_url(text)
        parsed = urlsplit(url)
        self.assertEqual("https", parsed.scheme)
        self.assertEqual("x.com", parsed.netloc)
        self.assertEqual("/intent/tweet", parsed.path)
        self.assertEqual(text, parse_qs(parsed.query)["text"][0])  # encode/decode round-trip is lossless

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

    def test_matcher_scores_aligned_campaign_higher_than_mismatched(self):
        matcher = RuleBasedMatcher()
        content = {"theme": "節約", "target": "家計を見直したい人", "appeals": ["curiosity", "number"], "structure": "list"}
        profile = {"niche": "節約と家計管理", "pillars": "固定費の見直し、節約術"}
        aligned_campaign = {"category": "節約", "target": "家計を見直したい人", "appeal_points": "知らないと損する固定費の見直し術"}
        mismatched_campaign = {"category": "旅行", "target": "海外旅行が好きな人", "appeal_points": "豪華な特典付きツアー"}
        aligned = matcher.match(aligned_campaign, content, profile)
        mismatched = matcher.match(mismatched_campaign, content, profile)
        self.assertGreater(aligned["score"], mismatched["score"])
        self.assertTrue(0 <= aligned["score"] <= 100)
        self.assertTrue(0 <= mismatched["score"] <= 100)
        self.assertTrue(aligned["reasons"])

    def test_rule_based_draft_generator_produces_distinct_styles(self):
        plan = {"theme": "節約術", "target": "家計を見直したい人", "hook": "statement", "structure": "narrative", "cta": "none"}
        campaign = {"name": "架空案件", "affiliate_url": "https://example.com/aff"}
        profile = {"audience": "家計を見直したい人"}
        gen = RuleBasedDraftGenerator()
        texts = {style: gen.generate(plan, campaign, profile, style=style) for style in DRAFT_STYLES}
        self.assertEqual(len(DRAFT_STYLES), len(set(texts.values())))
        for text in texts.values():
            self.assertIn("【PR】", text)
            self.assertIn(campaign["affiliate_url"], text)
        auto = gen.generate(plan, campaign, profile)  # style省略時は structure から自動選択される
        self.assertIn(auto, texts.values())

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
