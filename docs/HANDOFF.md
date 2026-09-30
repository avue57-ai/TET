# Vertex Origination Engine — handoff (2026-09-30)

Read this first in a new session. Branch `claude/focused-volta-qmu6l1` in `avue57-ai/TET` holds everything; `data/vertex.db` is the system of record and is committed.

## Hard rules (do not relax)
- The Microsoft 365 / MGT Outlook connector is never used, for anything. `.claude/settings.json` denies it at the harness level. Gmail is not part of outreach either.
- The engine never launches a campaign or sends a message. Lemlist launch/send tools are denied in `.claude/settings.json`; `vertex campaign launch` only prints the human step.
- Every outreach message is human-approved on the review page before a lead is pushed to Lemlist. Enrollment requires an approving decision row (eight code gates in `vertex/core/gates.py`).
- Buyer stays anonymous in copy. No age, retirement, succession, exit, health, or sell-intent language (lint in `vertex/ai/qa.py`, list in `config/banned_phrases.txt`).
- The user does not want permission prompts. The allowlist in `.claude/settings.json` covers the connector tools the engine uses.

## What exists
| Layer | Where | State |
|---|---|---|
| Thesis (4 verticals) | `config/theses/b2b_vertical_software.yaml` | compiled → 12 Inven queries, wave 1 run |
| Universe | `companies` (527), `thesis_companies` | 402 screened, 125 hard-excluded |
| Prescreen judge (Claude) | `vertex/ai/screen.py` | 401 judged: 237 keep, 163 drop, 1 unclear; keeper rates in `vertex prescreen rates` |
| Scoring | `vertex/core/scoring.py`, `vertex/ai/score.py`, `config/scoring/weights.yaml` v001 | 238 keepers scored with LLM components; all provisional (ownership was unknown), scores 71–88, avg coverage 43% |
| Targets | `vertex/workflows/targets.py`, `vertex targets …` | 50 pre-accepted by the engine (45 core + 5 exploration), stage Qualified/Contact Found; reversible in review Section C |
| Contacts (Apollo) | `vertex/integrations/apollo.py`, `vertex contacts …` | 50 contacts stored; 30 companies have an owner-level primary; 29 primaries have a verified email; 20 companies have a `needs_owner_contact` task |
| Ownership evidence | founder-titled executive rule in `vertex/workflows/enrich.py` | 16 companies now `founder/med`; rescoring pending |
| Signals (WebSearch) | `vertex/integrations/websearch.py`, `vertex/ai/signals.py` | 150 search jobs planned (3 per target); results land in `data/inbox/<job>.json` via subagents; ingest with `vertex bridge sweep --connector websearch`; extraction (`vertex signals extract`) not yet run |
| Personalization | `vertex/ai/personalize.py`, prompts `personalize.md`, `copy_critic.md`, sequences in `config/sequences/` | built, not yet run |
| Suppression | `vertex/workflows/suppression.py`, `vertex suppression …` | 210 Lemlist unsubscribes/bounces, 2,731 Apollo CRM accounts (existing-relationship holds), 43 Granola meetings (title match), never-contact domains; per-target Lemlist lookups + `deduplicate=true` at push replace the bulk lead export (CSV endpoint is not reachable through the connector) |
| Gates + enrollment + campaign build | `vertex/core/gates.py`, `vertex/workflows/campaign.py`, `vertex campaign …` | built, no Lemlist campaign created yet |
| Review page | `vertex/workflows/review.py`, `vertex/templates/review_page.html.j2`, `vertex review render/apply/decide` | renders to `data/review/<date>.html` (sections A–D, decisions JSON) |
| Reply classification + sync | `vertex/ai/classify.py`, `vertex/workflows/sync.py`, `vertex sync`, `vertex replies …` | built; not yet exercised; 38 labelled replies in `tests/fixtures/replies_calibration.jsonl` |
| Bridge (connector calls) | `vertex bridge next/complete/sweep/run`, `docs/BRIDGE.md` (to write) | Python plans every call; Claude runs the MCP tool and drops the raw result in `data/inbox/<job>.json`; large results are auto-saved by the harness under `~/.claude/projects/.../tool-results/` and can be copied straight in |

Not built yet: analytics/report (`vertex report`), feedback loop (`vertex feedback propose/apply`), Routine runbooks, README/ARCHITECTURE/BRIDGE docs, more tests.

## Environment facts
- Network policy blocks api.lemlist.com, api.apollo.io, api.inven.ai from the container, and no provider keys exist in the session, so every connector call goes through the MCP bridge. REST backends are implemented (`vertex/integrations/base.py`) and activate when `LEMLIST_API_KEY`, `APOLLO_API_KEY`, `INVEN_API_KEY` exist and the hosts are allowed.
- Apollo people search returns organization names, not domains; ingest matches names to the searched domains. Apollo masks last names until `bulk_match`.
- Lemlist `add_leads_to_campaign` with `deduplicate=true` reports cross-campaign duplicates; the engine records them as prior outreach.
- Mailbox headroom: avue@vertex-equity.com is capped at 50/day and already serves two running legacy campaigns; settings allocate 10/day to the engine. The `.net` mailboxes are disabled in `config/settings.yaml` until the user clears one.
- Spend so far: Inven ≈ 690 export + 20 AI credits; Apollo ≈ 28 (CRM list pages) + 2 (people search) + 30 (reveals) credits; Claude ≈ $32 (prescreen $20, scoring $11).

## Next steps, in order
1. `vertex bridge sweep --connector websearch` (ingests any WebSearch files not yet ingested), then `vertex signals extract b2b_vertical_software --limit 50 --workers 4`.
2. `vertex score b2b_vertical_software --rescore --workers 4` so ownership evidence and signals update the scores (cached prompts cost nothing when material is unchanged).
3. `vertex personalize run b2b_vertical_software --limit 50 --workers 3` → `vertex review render b2b_vertical_software` → open `data/review/<date>.html`, decide, `vertex review apply data/review/decisions/<date>.json` (or `vertex review decide E1 E2 --decision approve`).
4. `vertex campaign create b2b_vertical_software --arm linkedin_led` and `--arm email_first`; run `vertex bridge next --connector lemlist` jobs one by one (create → steps → branches), then `vertex campaign finalize <id>`; verify with `vertex campaign show`.
5. `vertex campaign enroll <id>` pushes only approved contacts (gates); `vertex campaign launch <id> --i-have-reviewed` prints the human launch step. Never launch from the engine.
6. After a human launch: `vertex campaign mark-launched <id>`, then `vertex sync` every 4 hours (Routine), replies classified into review Section A.
7. Remaining build: report/analytics, feedback loop, Routines (connectors Inven/Apollo/Lemlist/Granola only), docs, tests.

## Open decisions for the user
- Clear a `.net` mailbox for wave 1, or accept ~10 leads/day on avue@vertex-equity.com.
- Confirm the 50 pre-accepted targets (Section C) and the 23 keepers held because they sit in the Apollo CRM list.
- The 20 targets without an owner-level Apollo contact: source by hand or drop.
- Add the three API keys as environment secrets and allow the three API hosts if the REST path is wanted; otherwise the bridge stays.
