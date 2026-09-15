"""SQLite-backed local workflow. This module never performs network requests.

The character counter deliberately overestimates some URLs and emoji sequences.
It is a preflight aid, not an implementation of X's publishing API.
"""

import contextlib
import datetime
import json
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlsplit


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


CAMPAIGN_STATUSES = {"candidate", "applied", "approved", "rejected", "paused"}
MAX_NUMBER = 10 ** 12
URL_RE = re.compile(r"https?://[^\s<>\"'【】「」『』（）]+", re.IGNORECASE)
PROMOTION_RE = re.compile(
    r"(?:【\s*PR\s*】|#PR\b|アフィリエイト|購入はこちら|お申し込み|申込はこちら|"
    r"登録はこちら|おすすめ商品|割引コード|クーポン|購入リンク|成果報酬|https?://)",
    re.IGNORECASE,
)
GUARANTEE_RE = re.compile(
    r"(?:必ず.{0,12}(?:儲|稼|痩|やせ|増え|成功|治|効果)|"
    r"絶対.{0,12}(?:儲|稼|痩|やせ|増え|成功|治|効果)|"
    r"確実に.{0,12}(?:儲|稼|痩|やせ|増え|成功|治|効果)|"
    r"(?:100|１００)\s*[%％].{0,12}(?:保証|成功|稼|効果|治)|"
    r"元本保証|損しない|誰でも.{0,12}(?:月収|万円|稼)|"
    r"guaranteed\s+(?:profit|returns?|results?|income)|risk[- ]free\s+(?:profit|returns?))",
    re.IGNORECASE,
)
EXPERIENCE_RE = re.compile(
    r"(?:使ってみ(?:た|ました)|試してみ(?:た|ました)|飲んでみ(?:た|ました)|"
    r"(?:買い|購入し|使い|試し|稼ぎ)ました|愛用|"
    r"(?:私|僕|俺|わたし|自分)(?:は|が|も).{0,35}(?:使|買|試|稼|痩|やせ|治|効果)|"
    r"I\s+(?:tried|bought|used|earned|made\s+money))",
    re.IGNORECASE,
)
DISCLOSURE_RE = re.compile(r"(?:【\s*PR\s*】|#PR\b|【広告】|広告[：:]|広告投稿)", re.IGNORECASE)


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(payload, key, limit, required=True, default="", allow_empty=True):
    if key not in payload:
        if required:
            raise AppError("必須項目が不足しています: " + key)
        value = default
    else:
        value = payload[key]
    if not isinstance(value, str):
        raise AppError(key + " は文字列で入力してください。")
    if len(value) > limit:
        raise AppError(key + " が長すぎます（上限 " + str(limit) + " 文字）。")
    # Reject invalid Unicode / control bytes before SQLite or the HTTP encoder sees them.
    if any((ord(char) < 32 and char not in "\n\t\r") or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise AppError(key + " に使用できない制御文字が含まれています。")
    value = value.strip()
    if not allow_empty and not value:
        raise AppError(key + " を入力してください。")
    return value


def _integer(payload, key, required=True, nullable=False):
    if key not in payload:
        if required:
            raise AppError("必須項目が不足しています: " + key)
        return None
    value = payload[key]
    if nullable and value is None:
        return None
    if type(value) is not int or value < 0 or value > MAX_NUMBER:
        raise AppError(key + " は 0〜1兆の整数で入力してください。")
    return value


def _id(payload, key="id", required=True, nullable=False):
    value = _integer(payload, key, required, nullable)
    if value is not None and value < 1:
        raise AppError(key + " は正の整数で指定してください。")
    return value


def _keys(payload, allowed):
    if not isinstance(payload, dict):
        raise AppError("JSON オブジェクトで送信してください。")
    if set(payload) - set(allowed):
        raise AppError("未対応の入力項目があります。")


def _url(value, key, source=False):
    if not value:
        if source:
            raise AppError("参照する X 投稿の HTTPS URL を入力してください。")
        return value
    if any(char.isspace() for char in value) or "\\" in value:
        raise AppError(key + " は空白やバックスラッシュを含まない HTTPS URL にしてください。")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AppError(key + " の URL が正しくありません。")
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise AppError(key + " は認証情報を含まない HTTPS URL にしてください。")
    if port not in (None, 443):
        raise AppError(key + " は標準 HTTPS ポートを使用してください。")
    if source:
        allowed_hosts = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
        post_path = r"/(?:[A-Za-z0-9_]{1,15}/status|i/web/status)/[0-9]+/?"
        if parsed.hostname.lower() not in allowed_hosts or not re.fullmatch(post_path, parsed.path):
            raise AppError("参照 URL は x.com または twitter.com の個別投稿 URL にしてください。")
    return value


ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$")


def _iso_datetime(payload, key, required=True):
    value = _text(payload, key, 40, required=required, allow_empty=not required)
    if not value:
        return ""
    if not ISO_DATETIME_RE.match(value):
        raise AppError(key + " は ISO 8601 UTC 形式（例: 2026-09-15T10:00:00Z）で入力してください。")
    return value


def _string_list(payload, key, max_items, max_len):
    if key not in payload:
        raise AppError("必須項目が不足しています: " + key)
    value = payload[key]
    if not isinstance(value, list) or len(value) > max_items:
        raise AppError(key + " は " + str(max_items) + " 件以内のリストで指定してください。")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise AppError(key + " の各項目は文字列で入力してください。")
        item = item.strip()
        if len(item) > max_len:
            raise AppError(key + " の各項目が長すぎます（上限 " + str(max_len) + " 文字）。")
        if item:
            result.append(item)
    return result


def _unit_weight(text):
    total = 0
    for char in text:
        code = ord(char)
        total += 1 if (code <= 0x10FF or 0x2000 <= code <= 0x200D or 0x2010 <= code <= 0x201F or 0x2032 <= code <= 0x2037) else 2
    return total


def weighted_length(text):
    """Conservative X-like estimate: CJK=2; URL=max(23, original weight).

    Emoji sequences are counted by code point, so this can reject content X
    would accept. Never silently truncate a draft or an affiliate URL.
    """
    total = 0
    offset = 0
    for match in URL_RE.finditer(text):
        total += _unit_weight(text[offset:match.start()])
        total += max(23, _unit_weight(match.group(0)))
        offset = match.end()
    return total + _unit_weight(text[offset:])


MOCK_SOURCES = [
    {"key": "research-checklist", "text": "【架空サンプル】情報を集めるときの3項目：出典、更新日、利用条件。投稿構成を比較するためのデモです。", "url": "https://example.com/mock-posts/research-checklist", "author": "架空の情報整理アカウント", "likes": 180, "reposts": 42, "replies": 18, "impressions": 6000, "topic": "情報整理", "followers": 8200, "posted_hours_ago": 6},
    {"key": "question", "text": "【架空サンプル】新しいツールを選ぶとき、最初に何を確認しますか？ 読者への問いかけを含むデモ投稿です。", "url": "https://example.com/mock-posts/question", "author": "架空のツール研究室", "likes": 90, "reposts": 12, "replies": 36, "impressions": 4000, "topic": "ツール選び", "followers": 3100, "posted_hours_ago": 30},
    {"key": "comparison", "text": "【架空サンプル】選択肢を比較する観点：費用、対象者、利用条件。数字はすべて検証用の架空値です。", "url": "https://example.com/mock-posts/comparison", "author": "架空の比較ノート", "likes": 260, "reposts": 80, "replies": 20, "impressions": 12000, "topic": "比較", "followers": 15400, "posted_hours_ago": 50},
    {"key": "planning", "text": "【架空サンプル】投稿前に、誰に向けて何を伝えるかを一文に整理する。短い導入の構成を試すデモです。", "url": "https://example.com/mock-posts/planning", "author": "架空の投稿編集室", "likes": 64, "reposts": 14, "replies": 6, "impressions": 3000, "topic": "投稿企画", "followers": 2400, "posted_hours_ago": 12},
]


# --- バズスコア: フォロワー数に対する伸びの目安 -----------------------------
# 重みはここだけを見れば調整できるように定数へ切り出している。
BUZZ_SCORE_WEIGHTS = {"like": 1.0, "repost": 2.0, "reply": 1.5}


def buzz_score(likes, reposts, replies, impressions, followers):
    """フォロワー数に対する反応の大きさを表す参考スコア（絶対値の反応数だけでは大アカウントが常に上位になるため）。

    reach_multiplier: 表示回数がフォロワー数の何倍に達したか（フォロワー外への拡散の目安）
    engagement_rate:   重み付き反応 ÷ 表示回数（反応の質の目安）
    score = reach_multiplier × (1 + engagement_rate)
    """
    followers_safe = max(followers, 1)
    impressions_safe = max(impressions, 0)
    weighted_engagement = (
        likes * BUZZ_SCORE_WEIGHTS["like"]
        + reposts * BUZZ_SCORE_WEIGHTS["repost"]
        + replies * BUZZ_SCORE_WEIGHTS["reply"]
    )
    reach_multiplier = impressions_safe / followers_safe
    engagement_rate = (weighted_engagement / impressions_safe) if impressions_safe else 0.0
    return round(reach_multiplier * (1 + engagement_rate), 2)


# --- 投稿内容の分析: ルールベース（将来 LLM 実装へ差し替え可能な構造） -------
HOOK_PATTERNS = (
    ("question", re.compile(r"[?？]")),
    ("number", re.compile(r"[0-9０-９]+\s*(?:つ|個|選|ステップ|項目)")),
    ("warning", re.compile(r"(?:注意|危険|やってはいけない|NG|失敗)")),
    ("claim", re.compile(r"(?:結論|断言|実は|答えは)")),
)
STRUCTURE_PATTERNS = (
    ("question", re.compile(r"[?？]")),
    ("list", re.compile(r"(?:項目|観点|：|1\.|①|・)")),
)
CTA_PATTERNS = (
    ("follow", re.compile(r"フォロー")),
    ("reply", re.compile(r"(?:リプ|コメント)(?:で|欄)")),
    ("save", re.compile(r"保存")),
    ("link", re.compile(r"https?://")),
)
APPEAL_PATTERNS = {
    "number": re.compile(r"[0-9０-９]+\s*(?:つ|個|%|％|万|円)"),
    "authority": re.compile(r"(?:年収|経歴|資格|専門家|プロ)"),
    "empathy": re.compile(r"(?:わかる|あるある|私も|共感)"),
    "urgency": re.compile(r"(?:今すぐ|今だけ|残り|締切|損する)"),
    "curiosity": re.compile(r"(?:知らないと|実は|意外)"),
}


def _classify(text, patterns, default):
    for name, pattern in patterns:
        if pattern.search(text):
            return name
    return default


class ContentAnalyzer:
    """将来 LLM ベースの分析に差し替えるための共通インターフェース。"""

    def analyze(self, text, topic="", audience=""):
        raise NotImplementedError


class RuleBasedAnalyzer(ContentAnalyzer):
    """正規表現によるルールベース分析。初期版の既定実装。"""

    def analyze(self, text, topic="", audience=""):
        head = text.strip().splitlines()[0] if text.strip() else ""
        hook = _classify(head, HOOK_PATTERNS, "statement")
        structure = _classify(text, STRUCTURE_PATTERNS, "narrative")
        cta = _classify(text, CTA_PATTERNS, "none")
        appeals = [name for name, pattern in APPEAL_PATTERNS.items() if pattern.search(text)]
        target = audience.strip() or ((topic + "に関心がある人") if topic else "未設定")
        return {
            "hook": hook,
            "structure": structure,
            "cta": cta,
            "theme": topic or "未分類",
            "target": target,
            "appeals": appeals,
            "length": weighted_length(text),
        }


DEFAULT_ANALYZER = RuleBasedAnalyzer()

# 投稿案生成で使うテンプレート。参考にするのは分類ラベルのみで、原文は一切使わない。
HOOK_TEMPLATES = {
    "question": "{theme}について、こう感じたことはありませんか？",
    "number": "{theme}を整理する3つの視点。",
    "warning": "{theme}で見落としがちな注意点があります。",
    "claim": "{theme}について、先に要点を書きます。",
    "statement": "{theme}について、今日は要点を整理します。",
}
STRUCTURE_BODIES = {
    "list": "{audience}向けに、押さえておきたい点を整理します。\n・出典と更新日を確認する\n・対象者と条件を確認する\n・自分の言葉で言い換える",
    "question": "{audience}にとって何が一番気になるポイントか、一つずつ確認していきます。",
    "narrative": "{audience}向けに、要点を一つに絞って深掘りします。",
}
CTA_TEMPLATES = {
    "follow": "続きを見逃したくない方はフォローしてお待ちください。",
    "reply": "気になる点があれば、リプライで教えてください。",
    "save": "後で見返せるよう、保存しておくのがおすすめです。",
    "link": "詳しい内容はリンク先で確認してください。",
    "none": "参考になれば、気軽に反応してください。",
}


def build_reference_draft(profile, theme, analysis):
    """バズ投稿の構成・フック・訴求パターン(分類ラベル)だけを参考に新規本文を組み立てる。原文は使用しない。"""
    hook = HOOK_TEMPLATES.get(analysis["hook"], HOOK_TEMPLATES["statement"]).format(theme=theme)
    audience = profile.get("audience") or "読者"
    body = STRUCTURE_BODIES.get(analysis["structure"], STRUCTURE_BODIES["narrative"]).format(audience=audience)
    cta = CTA_TEMPLATES.get(analysis["cta"], CTA_TEMPLATES["none"])
    return hook + "\n" + body + "\n" + cta


# --- 情報収集プロバイダー: 将来 X API へ差し替え可能な構造 -------------------
class SourceProvider:
    """情報収集の抽象化。実接続の可否は Store 側の mode で fail-closed に制御する。"""

    def fetch(self, settings):
        raise NotImplementedError


class MockSourceProvider(SourceProvider):
    """架空サンプルのみを返す。外部通信は一切行わない。"""

    def fetch(self, settings):
        keywords = settings.get("keywords") or []
        genre = settings.get("genre") or ""
        accounts = settings.get("watched_accounts") or []
        results = []
        for source in MOCK_SOURCES:
            haystack = (source["text"] + " " + source["author"] + " " + source["topic"]).casefold()
            if keywords and not any(k.casefold() in haystack for k in keywords):
                continue
            if genre and genre.casefold() != source["topic"].casefold():
                continue
            if accounts and not any(a.casefold() in source["author"].casefold() for a in accounts):
                continue
            results.append(source)
        return results


class Store:
    def __init__(self, db_path, mode="mock"):
        if mode not in {"mock", "live"}:
            raise AppError("運転モードは mock または live を指定してください。")
        self.mode = mode
        self.provider = MockSourceProvider() if mode == "mock" else None
        self.analyzer = DEFAULT_ANALYZER
        self.db_path = str(db_path)
        self._anchor = None
        self._uri = self.db_path == ":memory:"
        if self._uri:
            self._location = "file:x-affiliate-" + uuid.uuid4().hex + "?mode=memory&cache=shared"
            self._anchor = sqlite3.connect(self._location, uri=True)
        else:
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._location = str(Path(self.db_path).expanduser())
        self._initialize()

    @contextlib.contextmanager
    def _connection(self):
        connection = sqlite3.connect(self._location, timeout=10, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self):
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    def _initialize(self):
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL, bio TEXT NOT NULL,
                    audience TEXT NOT NULL, niche TEXT NOT NULL, tone TEXT NOT NULL, pillars TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, seed_key TEXT UNIQUE, text TEXT NOT NULL,
                    url TEXT NOT NULL, author TEXT NOT NULL, likes INTEGER NOT NULL CHECK(likes>=0),
                    reposts INTEGER NOT NULL CHECK(reposts>=0), replies INTEGER NOT NULL CHECK(replies>=0),
                    impressions INTEGER NOT NULL CHECK(impressions>=0), topic TEXT NOT NULL,
                    posted_at TEXT NOT NULL DEFAULT '', author_followers INTEGER NOT NULL DEFAULT 0,
                    collected_at TEXT NOT NULL, is_mock INTEGER NOT NULL CHECK(is_mock IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS collection_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), keywords TEXT NOT NULL,
                    genre TEXT NOT NULL, watched_accounts TEXT NOT NULL,
                    period_days INTEGER NOT NULL CHECK(period_days BETWEEN 1 AND 365)
                );
                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, network TEXT NOT NULL,
                    url TEXT NOT NULL, affiliate_url TEXT NOT NULL, category TEXT NOT NULL,
                    reward_yen INTEGER NOT NULL CHECK(reward_yen>=0),
                    status TEXT NOT NULL CHECK(status IN ('candidate','applied','approved','rejected','paused')),
                    notes TEXT NOT NULL, updated_at TEXT NOT NULL, is_mock INTEGER NOT NULL CHECK(is_mock IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, text TEXT NOT NULL,
                    campaign_id INTEGER REFERENCES campaigns(id), source_id INTEGER REFERENCES sources(id),
                    status TEXT NOT NULL CHECK(status IN ('draft','review','approved','exported')),
                    checks TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    is_mock INTEGER NOT NULL CHECK(is_mock IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL UNIQUE REFERENCES drafts(id),
                    impressions INTEGER NOT NULL CHECK(impressions>=0), clicks INTEGER NOT NULL CHECK(clicks>=0),
                    conversions INTEGER NOT NULL CHECK(conversions>=0), revenue_yen INTEGER NOT NULL CHECK(revenue_yen>=0),
                    recorded_at TEXT NOT NULL, CHECK(conversions<=clicks AND clicks<=impressions)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entity_type TEXT NOT NULL,
                    entity_id INTEGER, created_at TEXT NOT NULL
                );
            """)
            self._migrate(connection)
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM collection_settings WHERE id=1").fetchone() is None:
                connection.execute(
                    "INSERT INTO collection_settings(id,keywords,genre,watched_accounts,period_days) VALUES(1,'[]','','[]',7)"
                )
            if connection.execute("SELECT value FROM meta WHERE key='initialized'").fetchone():
                return
            connection.execute(
                "INSERT INTO profile(id,name,bio,audience,niche,tone,pillars) VALUES(1,?,?,?,?,?,?)",
                ("情報整理ノート（設定例）", "情報の出典と条件を確認して、わかりやすく整理します。", "情報を整理したい人", "情報整理", "落ち着いた、わかりやすい日本語", "出典の確認、選択肢の比較、振り返り"),
            )
            self._insert_sources(connection, MockSourceProvider().fetch({"keywords": [], "genre": "", "watched_accounts": []}))
            timestamp = now()
            campaign_id = connection.execute(
                "INSERT INTO campaigns(name,network,url,affiliate_url,category,reward_yen,status,notes,updated_at,is_mock) VALUES(?,?,?,?,?,?,?,?,?,1)",
                ("架空の整理ノート", "デモASP（架空）", "https://example.com/mock-product", "https://example.com/mock-affiliate", "情報整理", 500, "approved", "架空案件です。承認済みの画面を試すためのデモで、実際の申請・提携はありません。", timestamp),
            ).lastrowid
            connection.execute(
                "INSERT INTO campaigns(name,network,url,affiliate_url,category,reward_yen,status,notes,updated_at,is_mock) VALUES(?,?,?,?,?,?,?,?,?,1)",
                ("架空の学習ガイド", "サンプルASP（架空）", "https://example.com/mock-guide", "", "学習", 800, "candidate", "候補の比較・申請状況の管理を試すための架空案件です。", timestamp),
            )
            source_id = connection.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
            self._insert_draft(connection, "整理ノートの紹介（サンプル）", "【PR】\n架空の整理ノートのご案内。\n内容・条件はリンク先でご確認ください。\nhttps://example.com/mock-affiliate", campaign_id, source_id, True)
            connection.execute("INSERT INTO meta(key,value) VALUES('initialized','1')")
            self._audit(connection, "seed_demo", "system", None)

    @staticmethod
    def _migrate(connection):
        # Adds columns introduced after the initial release without disturbing existing local databases.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(sources)")}
        if "posted_at" not in columns:
            connection.execute("ALTER TABLE sources ADD COLUMN posted_at TEXT NOT NULL DEFAULT ''")
        if "author_followers" not in columns:
            connection.execute("ALTER TABLE sources ADD COLUMN author_followers INTEGER NOT NULL DEFAULT 0")

    def _audit(self, connection, action, entity_type, entity_id):
        # Persist only controlled metadata: never a body, URL, key or user input.
        connection.execute("INSERT INTO audit(action,entity_type,entity_id,created_at) VALUES(?,?,?,?)", (action, entity_type, entity_id, now()))

    @staticmethod
    def _record(connection, table, record_id):
        # table names come only from fixed internal call sites, never payloads.
        row = connection.execute("SELECT * FROM " + table + " WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise AppError("指定したデータが見つかりません。", 404)
        return dict(row)

    @staticmethod
    def _rows(connection, table, ordering):
        result = []
        for row in connection.execute("SELECT * FROM " + table + " ORDER BY " + ordering):
            item = dict(row)
            item.pop("seed_key", None)
            if "is_mock" in item:
                item["is_mock"] = bool(item["is_mock"])
            if "checks" in item:
                item["checks"] = json.loads(item["checks"]) if item["checks"] else None
            result.append(item)
        return result

    def state(self):
        with self._connection() as connection:
            connection.execute("BEGIN")
            return self._state(connection)

    def _state(self, connection):
        profile = dict(connection.execute("SELECT * FROM profile WHERE id=1").fetchone())
        profile.pop("id")
        sources = self._rows(connection, "sources", "id DESC")
        campaigns = self._rows(connection, "campaigns", "id DESC")
        drafts = self._rows(connection, "drafts", "id DESC")
        metrics = self._rows(connection, "metrics", "recorded_at DESC, id DESC")
        audit = [dict(row) for row in connection.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 30")]
        collection_settings = self._collection_settings(connection)
        analysis = []
        for source in sources:
            impressions = source["impressions"]
            interactions = source["likes"] + source["reposts"] + source["replies"]
            weighted = source["likes"] + source["reposts"] * 2 + source["replies"] * 1.5
            if "？" in source["text"] or "?" in source["text"]:
                pattern, lesson = "問いかけ", "読者が答えられる問いの置き方を検討。反応との因果関係は、この数字だけでは判断できません。"
            elif any(word in source["text"] for word in ("項目", "観点", "：", "1.", "①")):
                pattern, lesson = "要点・比較", "要点を先に示す構成を検討。原文を転載せず、自分のテーマと根拠で作成してください。"
            else:
                pattern, lesson = "短い導入", "一つのテーマに絞る構成を検討。表示数や投稿条件の違いも確認してください。"
            content = self.analyzer.analyze(source["text"], source["topic"], "")
            analysis.append({
                "source_id": source["id"],
                "engagement_rate": round(interactions / impressions * 100, 2) if impressions else None,
                "score": round(weighted / impressions * 100, 2) if impressions else 0,
                "buzz_score": buzz_score(source["likes"], source["reposts"], source["replies"], impressions, source["author_followers"]),
                "pattern": pattern, "lesson": lesson,
                "hook": content["hook"], "structure": content["structure"], "cta": content["cta"],
                "theme": content["theme"], "target": content["target"], "appeals": content["appeals"], "length": content["length"],
            })
        analysis.sort(key=lambda item: (-item["buzz_score"], item["source_id"]))
        totals = {key: sum(item[key] for item in metrics) for key in ("impressions", "clicks", "conversions", "revenue_yen")}
        ctr = round(totals["clicks"] / totals["impressions"] * 100, 2) if totals["impressions"] else None
        cvr = round(totals["conversions"] / totals["clicks"] * 100, 2) if totals["clicks"] else None
        if not metrics:
            recommendation = "承認した投稿の成果を手入力すると、改善の確認点を表示します。サンプルの数字は実績と区別してください。"
        elif not totals["impressions"]:
            recommendation = "表示回数が 0 のため率を計算できません。集計期間と入力元を確認してください。"
        elif not totals["clicks"]:
            recommendation = "クリックはまだ 0 件です。読者・投稿内容・紹介先の関連性を一つずつ確認してください。"
        elif not totals["conversions"]:
            recommendation = "クリックはありますが成果は 0 件です。案件の対象者・条件と投稿の説明が一致するか確認してください。"
        else:
            recommendation = "同じ集計期間で投稿を比較し、導入や構成を一つずつ変更して確認しましょう。少数の成果から因果関係は断定できません。"
        summary = dict(totals, sources=len(sources), campaigns=len(campaigns), review=sum(item["status"] == "review" for item in drafts), approved=sum(item["status"] in {"approved", "exported"} for item in drafts), ctr=ctr, cvr=cvr, recommendation=recommendation)
        return {"profile": profile, "sources": sources, "campaigns": campaigns, "drafts": drafts, "metrics": metrics, "analysis": analysis, "summary": summary, "audit": audit, "collection_settings": collection_settings}

    def _collection_settings(self, connection):
        row = dict(connection.execute("SELECT * FROM collection_settings WHERE id=1").fetchone())
        row.pop("id")
        row["keywords"] = json.loads(row["keywords"])
        row["watched_accounts"] = json.loads(row["watched_accounts"])
        return row

    @staticmethod
    def _insert_sources(connection, rows):
        timestamp = now()
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        for source in rows:
            posted_at = (now_dt - datetime.timedelta(hours=source.get("posted_hours_ago", 0))).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            connection.execute(
                "INSERT OR IGNORE INTO sources(seed_key,text,url,author,likes,reposts,replies,impressions,author_followers,topic,posted_at,collected_at,is_mock) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (source["key"], source["text"], source["url"], source["author"], source["likes"], source["reposts"], source["replies"], source["impressions"], source.get("followers", 0), source["topic"], posted_at, timestamp),
            )

    def _collect(self, connection, settings):
        # Demo seeding (Store.__init__) always uses fixture data regardless of mode; only this
        # on-demand path (the /api/collect API) is fail-closed for a non-mock provider.
        if self.provider is None:
            raise AppError("実接続は未実装です。収集には mock モードを使用してください。", 503)
        self._insert_sources(connection, self.provider.fetch(settings))

    def _links(self, connection, payload):
        campaign_id = _id(payload, "campaign_id", required=False, nullable=True)
        source_id = _id(payload, "source_id", required=False, nullable=True)
        campaign = self._record(connection, "campaigns", campaign_id) if campaign_id is not None else None
        source = self._record(connection, "sources", source_id) if source_id is not None else None
        return campaign_id, source_id, campaign, source

    @staticmethod
    def _invalidate(connection, campaign_id=None):
        if campaign_id is None:
            connection.execute("UPDATE drafts SET status='draft',checks=NULL,updated_at=?", (now(),))
        else:
            connection.execute("UPDATE drafts SET status='draft',checks=NULL,updated_at=? WHERE campaign_id=?", (now(), campaign_id))

    def _insert_draft(self, connection, title, text, campaign_id, source_id, is_mock):
        timestamp = now()
        return connection.execute(
            "INSERT INTO drafts(title,text,campaign_id,source_id,status,checks,created_at,updated_at,is_mock) VALUES(?,?,?,?,'draft',NULL,?,?,?)",
            (title, text, campaign_id, source_id, timestamp, timestamp, int(bool(is_mock))),
        ).lastrowid

    def _checks(self, connection, draft):
        text = draft["text"]
        length = weighted_length(text)
        issues = []

        def issue(code, severity, message):
            issues.append({"code": code, "severity": severity, "message": message})

        if not text.strip():
            issue("empty_text", "error", "投稿本文が空です。")
        if length > 280:
            issue("length_exceeded", "error", "保守的な文字数換算が 280 を超えています。本文を短くしてください。")
        campaign = self._record(connection, "campaigns", draft["campaign_id"]) if draft["campaign_id"] is not None else None
        source = self._record(connection, "sources", draft["source_id"]) if draft["source_id"] is not None else None
        if campaign:
            if campaign["status"] != "approved":
                issue("campaign_not_approved", "error", "案件が提携承認済みではありません。申請状況を確認してください。")
            if not DISCLOSURE_RE.search(text):
                issue("disclosure_missing", "error", "広告であることが明確になるよう、本文に【PR】を付けてください。")
            # Compare complete URL tokens, not substrings inside an unrelated URL.
            urls = [match.group(0) for match in URL_RE.finditer(text)]
            expected = campaign["affiliate_url"]
            if not expected or not any(url == expected or url.rstrip("。！？、.,!;") == expected for url in urls):
                issue("affiliate_url_missing", "error", "案件に登録した正確なアフィリエイト URL が本文にありません。")
        elif PROMOTION_RE.search(text):
            issue("unassigned_promotion", "error", "広告表現や URL を含む投稿は、承認済みの案件を紐付けて内容・リンクを確認してください。")
        if GUARANTEE_RE.search(text):
            issue("guaranteed_outcome", "error", "成果・効果を保証する表現が含まれています。保証や断定を削除してください。")
        if EXPERIENCE_RE.search(text):
            issue("unverified_experience", "error", "体験談らしい表現が含まれています。この初期版では体験の根拠を確認できないため、事実を確認して表現を修正してください。")
        if draft["is_mock"] or (campaign and campaign["is_mock"]) or (source and source["is_mock"]):
            issue("sample_content", "warning", "架空サンプルを含む投稿です。練習専用で、公開に使わないでください。")
        issue("manual_fact_check", "warning", "人による確認が必要です。事実・価格・条件・広告表示・誤解を招く表現を確認してください。")
        if source:
            issue("source_rights_check", "warning", "参照元の利用条件・引用範囲・出典を確認してください。反応数は真偽や利用許可の証明になりません。")
        issue("length_estimate", "warning", "文字数は保守的な概算です。日本語等は 2、URL は最低 23、絵文字はコードポイントごとに換算します。公開時は X の入力欄でも確認してください。")
        return {"passed": not any(item["severity"] == "error" for item in issues), "weighted_length": length, "issues": issues, "checked_at": now()}

    def mutate(self, path, payload):
        if not isinstance(payload, dict):
            raise AppError("JSON オブジェクトで送信してください。")
        with self._connection() as connection:
            # All reads, validation, writes and approval gates share one transaction.
            connection.execute("BEGIN IMMEDIATE")
            result = self._mutate(connection, path, payload)
            return result if result is not None else self._state(connection)

    def _mutate(self, connection, path, payload):
        if path == "/api/collect":
            _keys(payload, {"query"})
            query = _text(payload, "query", 200, required=False)
            settings = self._collection_settings(connection)
            if query:
                settings = dict(settings, keywords=settings["keywords"] + [query])
            self._collect(connection, settings)
            self._audit(connection, "collect_mock", "source", None)
        elif path == "/api/collection-settings":
            _keys(payload, {"keywords", "genre", "watched_accounts", "period_days"})
            keywords = _string_list(payload, "keywords", 10, 50)
            genre = _text(payload, "genre", 50)
            watched_accounts = _string_list(payload, "watched_accounts", 10, 50)
            period_days = _integer(payload, "period_days")
            if not 1 <= period_days <= 365:
                raise AppError("収集対象期間は 1〜365 日で指定してください。")
            connection.execute(
                "UPDATE collection_settings SET keywords=?,genre=?,watched_accounts=?,period_days=? WHERE id=1",
                (json.dumps(keywords, ensure_ascii=False), genre, json.dumps(watched_accounts, ensure_ascii=False), period_days),
            )
            self._audit(connection, "save_collection_settings", "collection_settings", 1)
        elif path == "/api/sources":
            _keys(payload, {"text", "url", "author", "likes", "reposts", "replies", "impressions", "topic", "posted_at", "followers"})
            text = _text(payload, "text", 5000, allow_empty=False)
            url = _url(_text(payload, "url", 2048, allow_empty=False), "url", source=True)
            author = _text(payload, "author", 100, allow_empty=False)
            topic = _text(payload, "topic", 100)
            posted_at = _iso_datetime(payload, "posted_at")
            numbers = [_integer(payload, key) for key in ("likes", "reposts", "replies", "impressions", "followers")]
            record_id = connection.execute(
                "INSERT INTO sources(text,url,author,likes,reposts,replies,impressions,author_followers,topic,posted_at,collected_at,is_mock) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                (text, url, author, numbers[0], numbers[1], numbers[2], numbers[3], numbers[4], topic, posted_at, now()),
            ).lastrowid
            self._audit(connection, "add_source", "source", record_id)
        elif path == "/api/profile":
            fields = {"name": 80, "bio": 500, "audience": 200, "niche": 100, "tone": 100, "pillars": 1000}
            _keys(payload, fields)
            values = [_text(payload, key, limit) for key, limit in fields.items()]
            connection.execute("UPDATE profile SET name=?,bio=?,audience=?,niche=?,tone=?,pillars=? WHERE id=1", values)
            self._invalidate(connection)
            self._audit(connection, "save_profile_invalidate_drafts", "profile", 1)
        elif path == "/api/campaigns":
            _keys(payload, {"id", "name", "network", "url", "affiliate_url", "category", "reward_yen", "status", "notes"})
            record_id = _id(payload, required=False)
            if record_id is not None:
                self._record(connection, "campaigns", record_id)
            name = _text(payload, "name", 120, allow_empty=False)
            network = _text(payload, "network", 100)
            url = _url(_text(payload, "url", 2048), "url")
            affiliate_url = _url(_text(payload, "affiliate_url", 2048), "affiliate_url")
            category = _text(payload, "category", 100)
            reward_yen = _integer(payload, "reward_yen")
            status = _text(payload, "status", 20, allow_empty=False)
            notes = _text(payload, "notes", 3000)
            if status not in CAMPAIGN_STATUSES:
                raise AppError("案件のステータスが正しくありません。")
            if status == "approved" and not affiliate_url:
                raise AppError("提携承認済みにするには、発行されたアフィリエイト URL を登録してください。")
            values = (name, network, url, affiliate_url, category, reward_yen, status, notes, now())
            if record_id is None:
                record_id = connection.execute("INSERT INTO campaigns(name,network,url,affiliate_url,category,reward_yen,status,notes,updated_at,is_mock) VALUES(?,?,?,?,?,?,?,?,?,0)", values).lastrowid
                action = "add_campaign"
            else:
                connection.execute("UPDATE campaigns SET name=?,network=?,url=?,affiliate_url=?,category=?,reward_yen=?,status=?,notes=?,updated_at=? WHERE id=?", values + (record_id,))
                self._invalidate(connection, record_id)
                action = "save_campaign_invalidate_drafts"
            self._audit(connection, action, "campaign", record_id)
        elif path == "/api/drafts/generate":
            _keys(payload, {"campaign_id", "source_id", "angle"})
            if self.mode != "mock":
                raise AppError("実接続は未実装です。テンプレート作成には mock モードを使用してください。", 503)
            campaign_id, source_id, campaign, source = self._links(connection, payload)
            angle = _text(payload, "angle", 160, required=False)
            profile = dict(connection.execute("SELECT * FROM profile WHERE id=1").fetchone())
            theme = angle or (source["topic"] if source else "") or profile["niche"] or "情報の確認"
            # 参考にするのは分類ラベル（hook/structure/cta）だけで、原文の文字列は生成に使わない。
            content = self.analyzer.analyze(source["text"], source["topic"], profile["audience"]) if source else None
            if campaign:
                if campaign["status"] != "approved":
                    raise AppError("広告投稿の作成には、提携承認済みの案件を選んでください。", 409)
                hook = HOOK_TEMPLATES.get(content["hook"], HOOK_TEMPLATES["statement"]).format(theme=theme) if content else ""
                text = "【PR】\n" + (hook + "\n" if hook else "") + campaign["name"] + "のご案内。\n内容・条件はリンク先でご確認ください。\n" + campaign["affiliate_url"]
                title = campaign["name"] + "の紹介案"
            elif source:
                text = build_reference_draft(profile, theme, content)
                title = (theme + "の投稿案（" + source["author"] + "の構成を参考）")[:160]
            else:
                audience = profile["audience"] or "読者"
                text = "今日のテーマ：" + theme + "\n" + audience + "向けに、情報の出典・更新日・条件を確認して整理します。\n気になる点を一つ選び、次の投稿で深掘りします。"
                title = theme + "の投稿案"
            is_mock = bool((campaign and campaign["is_mock"]) or (source and source["is_mock"]))
            record_id = self._insert_draft(connection, title[:160], text, campaign_id, source_id, is_mock)
            self._audit(connection, "generate_template", "draft", record_id)
        elif path == "/api/drafts/save":
            _keys(payload, {"id", "title", "text", "campaign_id", "source_id"})
            record_id = _id(payload, required=False)
            old = self._record(connection, "drafts", record_id) if record_id is not None else None
            title = _text(payload, "title", 160, allow_empty=False)
            text = _text(payload, "text", 5000)
            campaign_id, source_id, campaign, source = self._links(connection, payload)
            # Keep sample provenance even if a user later removes its links.
            is_mock = bool((old and old["is_mock"]) or (campaign and campaign["is_mock"]) or (source and source["is_mock"]))
            if old:
                connection.execute("UPDATE drafts SET title=?,text=?,campaign_id=?,source_id=?,status='draft',checks=NULL,updated_at=?,is_mock=? WHERE id=?", (title, text, campaign_id, source_id, now(), int(is_mock), record_id))
            else:
                record_id = self._insert_draft(connection, title, text, campaign_id, source_id, is_mock)
            self._audit(connection, "save_draft", "draft", record_id)
        elif path in {"/api/drafts/check", "/api/drafts/approve", "/api/drafts/export"}:
            _keys(payload, {"id", "confirmed"} if path.endswith("/approve") else {"id"})
            record_id = _id(payload)
            draft = self._record(connection, "drafts", record_id)
            if path.endswith("/approve") and payload.get("confirmed") is not True:
                raise AppError("内容・根拠・条件を人が確認したチェックが必要です。")
            if path.endswith("/export") and draft["status"] not in {"approved", "exported"}:
                raise AppError("書き出すには、最新の内容を確認して承認してください。", 409)
            checks = self._checks(connection, draft)
            if not path.endswith("/check") and not checks["passed"]:
                raise AppError("検品に未解決のエラーがあります。再検品して修正してください。", 409)
            status = "review" if path.endswith("/check") else "approved" if path.endswith("/approve") else "exported"
            connection.execute("UPDATE drafts SET status=?,checks=?,updated_at=? WHERE id=?", (status, json.dumps(checks, ensure_ascii=False), now(), record_id))
            self._audit(connection, "check_draft" if status == "review" else "approve_draft" if status == "approved" else "export_draft", "draft", record_id)
            if status == "exported":
                is_mock = bool(draft["is_mock"]) or any(issue["code"] == "sample_content" for issue in checks["issues"])
                prefix = "【サンプル・公開不可】\n以下は架空データを使った練習用の投稿です。公開に使わないでください。\n\n" if is_mock else ""
                return {"text": prefix + draft["text"], "filename": ("sample-" if is_mock else "") + "draft-" + str(record_id) + ".txt", "is_mock": is_mock}
        elif path == "/api/metrics":
            _keys(payload, {"draft_id", "impressions", "clicks", "conversions", "revenue_yen"})
            draft_id = _id(payload, "draft_id")
            draft = self._record(connection, "drafts", draft_id)
            if draft["status"] not in {"approved", "exported"}:
                raise AppError("成果は承認済み・書き出し済みの投稿に入力してください。", 409)
            values = [_integer(payload, key) for key in ("impressions", "clicks", "conversions", "revenue_yen")]
            if not values[2] <= values[1] <= values[0]:
                raise AppError("成果件数 ≦ クリック数 ≦ 表示回数になるよう入力してください。")
            existing = connection.execute("SELECT id FROM metrics WHERE draft_id=?", (draft_id,)).fetchone()
            if existing:
                record_id = existing[0]
                connection.execute("UPDATE metrics SET impressions=?,clicks=?,conversions=?,revenue_yen=?,recorded_at=? WHERE id=?", tuple(values) + (now(), record_id))
            else:
                record_id = connection.execute("INSERT INTO metrics(draft_id,impressions,clicks,conversions,revenue_yen,recorded_at) VALUES(?,?,?,?,?,?)", (draft_id,) + tuple(values) + (now(),)).lastrowid
            self._audit(connection, "upsert_metrics", "metric", record_id)
        else:
            raise AppError("API が見つかりません。", 404)
