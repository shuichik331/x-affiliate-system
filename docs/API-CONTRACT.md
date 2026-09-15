# MVP API contract (implementation reference)

Python standard library server, SQLite persistence, plain JS/CSS frontend. Local-only, no live external requests. All dates ISO 8601 UTC strings. Money is integer JPY. API errors `{error: string}` with 400/403/404/409/413/503. All mutations POST JSON and require `X-CSRF-Token` from GET state. UI must use textContent/escaped HTML for user fields.

## GET /api/state

Returns `{csrfToken, config:{mode:'mock',liveEnabled:false}, profile, sources, campaigns, drafts, metrics, analysis, summary, audit}`.

- profile: `{name,bio,audience,niche,tone,pillars}` (pillars is string)
- sources: `[{id,text,url,author,likes,reposts,replies,impressions,topic,collected_at,is_mock}]`
- campaigns: `[{id,name,network,url,affiliate_url,category,reward_yen,status,notes,updated_at,is_mock}]`; status candidate/applied/approved/rejected/paused. Seed campaigns clearly fictional. Manual entries not mock.
- drafts: `[{id,title,text,campaign_id,source_id,status,checks,created_at,updated_at,is_mock}]`; status draft/review/approved/exported. `checks` null or `{passed,weighted_length,issues:[{code,severity:'error'|'warning',message}],checked_at}`. `is_mock` indicates sample-linked content.
- metrics: `[{id,draft_id,impressions,clicks,conversions,revenue_yen,recorded_at}]` unique per draft (upsert cumulative totals, not additive).
- analysis: `[{source_id,engagement_rate,score,pattern,lesson}]` sorted descending score; scores illustrative and transparent, mock data labelled.
- summary: `{sources,campaigns,review,approved,impressions,clicks,conversions,revenue_yen,ctr,cvr,recommendation}` percentages 0–100, denominator zero => null.
- audit: `[{id,action,entity_type,entity_id,created_at}]` latest 30, no secrets/body.

## POST endpoints

All return updated complete state except export.

- `/api/collect` `{query?:string}`: load deterministic mock sources, idempotent; filter by query if supplied. No external requests.
- `/api/sources` `{text,url,author,likes,reposts,replies,impressions,topic}`: manual source; URL HTTPS X/twitter post URL only; integers >=0.
- `/api/profile` `{name,bio,audience,niche,tone,pillars}`: save full profile.
- `/api/campaigns` `{id?:number,name,network,url,affiliate_url,category,reward_yen,status,notes}`: add/update. Require HTTPS URLs if nonempty. Status changed to applied records manual application status only. Changes invalidate related drafts' checks/approval. Approved requires affiliate_url, user manually confirms in UI.
- `/api/drafts/generate` `{campaign_id?:number|null,source_id?:number|null,angle?:string}`: template-based Japanese draft using profile + optional campaign/source; no LLM. Campaign must be approved to generate promotion. Draft with campaign includes `【PR】`, exact affiliate_url, no invented personal experience or factual benefit. For sample content set is_mock true.
- `/api/drafts/save` `{id?:number,title,text,campaign_id?:number|null,source_id?:number|null}`: create/update draft; always reset status=draft/checks=null. Text up to 5000. Manual UI uses same endpoint.
- `/api/drafts/check` `{id:number}`: recompute checks and set review (even if previously approved). Check PR disclosure when campaign linked, campaign approved, exact affiliate URL present, no guaranteed outcomes/false first-person experience, weighted text <=280, nonempty, sample flag warning; missing campaign with promotional-looking text/URL is error. Source facts/rights and truth need human review warning. Warnings do not block, errors block.
- `/api/drafts/approve` `{id:number,confirmed:true}`: recompute checks, require passed=true; explicit human confirmation, set approved. Approval allowed for sample, export remains sample-labelled.
- `/api/drafts/export` `{id:number}`: only approved or exported, recompute gate; return `{text,filename,is_mock}`; for mock append conspicuous `【サンプル・公開不可】` banner before draft. Real content no append. Set exported, no posting.
- `/api/metrics` `{draft_id,impressions,clicks,conversions,revenue_yen}`: cumulative manual totals for approved/exported draft only, integers >=0; conversions<=clicks<=impressions.

## Frontend

7 nav views: overview/dashboard (role progress + recommendations), research (collection/manual sources + scored analysis), profile, campaigns (candidate search/filter + form), studio (template generation + editable drafts), review (checks + explicit human approval + text export), analytics (cumulative record + summary). Show mock/local badges and practical empty/loading/error states. Use full state reload after mutation. External services not connected; never expose keys. No auto publishing or actual application UI. Layout should be polished, Japanese, responsive and accessible.
