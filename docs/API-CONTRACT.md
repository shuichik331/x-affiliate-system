# MVP API contract (implementation reference)

Python standard library server, SQLite persistence, plain JS/CSS frontend. Local-only, no live external requests. All dates ISO 8601 UTC strings. Money is integer JPY. API errors `{error: string}` with 400/403/404/409/413/503. All mutations POST JSON and require `X-CSRF-Token` from GET state. UI must use textContent/escaped HTML for user fields.

## GET /api/state

Returns `{csrfToken, config:{mode:'mock',liveEnabled:false}, profile, sources, campaigns, drafts, metrics, analysis, summary, audit, collection_settings}`.

- profile: `{name,bio,audience,niche,tone,pillars}` (pillars is string)
- sources: `[{id,text,url,author,likes,reposts,replies,impressions,author_followers,topic,posted_at,collected_at,is_mock}]`. `posted_at` ISO 8601 UTC (may be `''` for rows created before this field existed). `author_followers` integer >=0.
- collection_settings: `{keywords:[string],genre,watched_accounts:[string],period_days}`. Singleton, defaults `{keywords:[],genre:'',watched_accounts:[],period_days:7}`. Consumed by the source provider (see Providers below), not sent to any external API.
- campaigns: `[{id,name,network,url,affiliate_url,category,reward_yen,status,notes,updated_at,is_mock}]`; status candidate/applied/approved/rejected/paused. Seed campaigns clearly fictional. Manual entries not mock.
- drafts: `[{id,title,text,campaign_id,source_id,status,checks,created_at,updated_at,is_mock}]`; status draft/review/approved/exported. `checks` null or `{passed,weighted_length,issues:[{code,severity:'error'|'warning',message}],checked_at}`. `is_mock` indicates sample-linked content.
- metrics: `[{id,draft_id,impressions,clicks,conversions,revenue_yen,recorded_at}]` unique per draft (upsert cumulative totals, not additive).
- analysis: `[{source_id,engagement_rate,score,buzz_score,pattern,lesson,hook,structure,cta,theme,target,appeals:[string],length}]` sorted descending `buzz_score`. `buzz_score` (see `buzz_score()` in `app/domain.py`) weighs reach against `author_followers`, not raw counts — a smaller account with the same reach multiplier scores higher than a larger one. `hook`/`structure`/`cta`/`theme`/`target`/`appeals`/`length` come from `RuleBasedAnalyzer` (see Content analysis below); all illustrative, mock data labelled.
- summary: `{sources,campaigns,review,approved,impressions,clicks,conversions,revenue_yen,ctr,cvr,recommendation}` percentages 0–100, denominator zero => null.
- audit: `[{id,action,entity_type,entity_id,created_at}]` latest 30, no secrets/body.

## POST endpoints

All return updated complete state except export.

- `/api/collect` `{query?:string}`: fetch sources through the active `SourceProvider` (mock-only for now — see Providers) using the saved `collection_settings`, plus `query` appended as a one-off extra keyword (not persisted). Idempotent (`INSERT OR IGNORE`). No external requests.
- `/api/collection-settings` `{keywords:[string]≤10×≤50chars, genre:string≤50chars, watched_accounts:[string]≤10×≤50chars, period_days:int 1-365}`: replace the saved collection settings used by `/api/collect`.
- `/api/sources` `{text,url,author,likes,reposts,replies,impressions,followers,topic,posted_at}`: manual source; URL HTTPS X/twitter post URL only; integers >=0 (`followers` maps to the response's `author_followers`); `posted_at` required, ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`).
- `/api/profile` `{name,bio,audience,niche,tone,pillars}`: save full profile.
- `/api/campaigns` `{id?:number,name,network,url,affiliate_url,category,reward_yen,status,notes}`: add/update. Require HTTPS URLs if nonempty. Status changed to applied records manual application status only. Changes invalidate related drafts' checks/approval. Approved requires affiliate_url, user manually confirms in UI.
- `/api/drafts/generate` `{campaign_id?:number|null,source_id?:number|null,angle?:string}`: template-based Japanese draft using profile + optional campaign/source; no LLM. Campaign must be approved to generate promotion. Draft with campaign includes `【PR】`, exact affiliate_url, no invented personal experience or factual benefit. When `source_id` is set, the draft's hook/body/CTA are built from that source's `RuleBasedAnalyzer` **category labels only** (`build_reference_draft()`) — the source's original text is never copied into the generated draft. For sample content set is_mock true.
- `/api/drafts/save` `{id?:number,title,text,campaign_id?:number|null,source_id?:number|null}`: create/update draft; always reset status=draft/checks=null. Text up to 5000. Manual UI uses same endpoint.
- `/api/drafts/check` `{id:number}`: recompute checks and set review (even if previously approved). Check PR disclosure when campaign linked, campaign approved, exact affiliate URL present, no guaranteed outcomes/false first-person experience, weighted text <=280, nonempty, sample flag warning; missing campaign with promotional-looking text/URL is error. Source facts/rights and truth need human review warning. Warnings do not block, errors block.
- `/api/drafts/approve` `{id:number,confirmed:true}`: recompute checks, require passed=true; explicit human confirmation, set approved. Approval allowed for sample, export remains sample-labelled.
- `/api/drafts/export` `{id:number}`: only approved or exported, recompute gate; return `{text,filename,is_mock}`; for mock append conspicuous `【サンプル・公開不可】` banner before draft. Real content no append. Set exported, no posting.
- `/api/metrics` `{draft_id,impressions,clicks,conversions,revenue_yen}`: cumulative manual totals for approved/exported draft only, integers >=0; conversions<=clicks<=impressions.

## Providers (`app/domain.py`)

`SourceProvider` is an abstract `fetch(settings) -> [source dict]`. `MockSourceProvider` (the only implementation today) filters the fixed `MOCK_SOURCES` fixture by `keywords`/`genre`/`watched_accounts`; it performs no network I/O. `Store.provider` is `MockSourceProvider()` when `mode == "mock"` and `None` otherwise — `/api/collect` fails closed (503) whenever `provider is None`. A future `XApiSourceProvider` (read-only) can implement the same `fetch(settings)` signature and be wired into `Store.__init__` without changing `_mutate` or the frontend. Initial demo seeding (`Store._initialize`) always uses `MockSourceProvider` directly regardless of `mode`, so constructing a `Store(mode="live")` still succeeds — only the on-demand `/api/collect` call is blocked.

## Content analysis (`app/domain.py`)

`ContentAnalyzer.analyze(text, topic, audience) -> {hook,structure,cta,theme,target,appeals,length}` is the pluggable interface; `RuleBasedAnalyzer` (regex-based, `Store.analyzer` default) is the only implementation today. A future LLM-backed analyzer can implement the same signature and be swapped into `Store.__init__`. Draft generation (`build_reference_draft`) consumes only the returned category labels (e.g. `hook: "question"`) via `HOOK_TEMPLATES`/`STRUCTURE_BODIES`/`CTA_TEMPLATES`, never the source's raw text — this is how "reference this post's structure" avoids copying.

## Frontend

7 nav views: overview/dashboard (①→⑦ flow at a glance + recommendations), research (①collection settings + manual sources, ②buzz-scored analysis with hook/structure/CTA/appeals + "generate from this structure"), profile, campaigns (candidate search/filter + form), studio (template generation + editable drafts), review (checks + explicit human approval + text export), analytics (cumulative record + summary). Show mock/local badges and practical empty/loading/error states. Use full state reload after mutation. External services not connected; never expose keys. No auto publishing or actual application UI. Layout should be polished, Japanese, responsive and accessible.
