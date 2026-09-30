# Vertex Origination Engine — handoff (2026-09-30)

Read this first in a new session. Branch `claude/festive-albattani-odixx8` in `avue57-ai/TET` holds everything (it continues `claude/focused-volta-qmu6l1`, where the engine was built); `data/vertex.db` is the system of record and is committed. Last updated 2026-09-30 after signal extraction, rescoring and a 5-contact drafting pilot.

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
| Scoring | `vertex/core/scoring.py`, `vertex/ai/score.py`, `config/scoring/weights.yaml` v001 | 238 keepers rescored 2026-09-30 after ownership evidence and signals landed (always pass `--keepers-only`). The 50 targets average 82.8 with 54% coverage, all T2, all still provisional (ownership unknown for 33 of 50) |
| Targets | `vertex/workflows/targets.py`, `vertex targets …` | 50 pre-accepted by the engine (45 core + 5 exploration), stage Qualified/Contact Found; reversible in review Section C |
| Contacts (Apollo) | `vertex/integrations/apollo.py`, `vertex contacts …` | 50 contacts stored; 30 companies have an owner-level primary; 29 primaries have a verified email; 20 companies have a `needs_owner_contact` task |
| Ownership evidence | founder-titled executive rule in `vertex/workflows/enrich.py` | 16 companies now `founder/med`; rescoring pending |
| Signals (WebSearch) | `vertex/integrations/websearch.py`, `vertex/ai/signals.py` | 150 WebSearch results ingested (3 per target; 21 found nothing relevant, mostly name clashes). `vertex signals extract` run: 290 signals for 50 targets (214 safe to cite, 1 rejected for a missing quote). 4 of the 30 primary contacts have no usable hook: Impact Systems, Claimpower, Adroit Infosystems, Hoptek |
| Personalization | `vertex/ai/personalize.py`, prompts `personalize.md`, `copy_critic.md`, sequences in `config/sequences/` | Pilot drafted for 5 contacts (Points North, iCareManager, CHAMPS, Consis, Psyquel): all `needs_edit`, critic 3/5. Facts are clean; the copy restates the product without an investor angle. The other 21 are not drafted until the tone is approved |
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
- Spend so far: Inven ≈ 690 export + 20 AI credits; Apollo ≈ 28 (CRM list pages) + 2 (people search) + 30 (reveals) credits; Claude ≈ $41 (prescreen $20, scoring $16, signals $3, drafting pilot and prompt iterations $2). The `llm_usd_per_run` cap ($15) is not enforced in code for extraction, scoring or drafting; it only gates bridge jobs.
- A fresh cloud container needs `python3 -m venv` and `pip install -e ".[dev]"` before `vertex` works. `claude -p` works there (Sonnet 5.5). Extraction costs about $0.06 per company, scoring $0.06, drafting plus critic about $0.12 per contact.

## Fixed on 2026-09-30
- Both `config/sequences/*.yaml` failed to parse (mixed flow/block syntax in arm A, an unquoted colon in arm B). Campaign build and drafting could not have run. Fixed, with `tests/test_sequences.py`.
- Lint `number_parroted` flagged every "15-minute call" as a parroted number, sending every draft to `needs_edit`. Call lengths are now ignored (`tests/test_lint.py`).
- The copy critic only saw the first hook, so it called the second hook, HQ and description "invented", and it flagged the mandated buyer positioning as a sensitive theme. It now sees all three and exempts the positioning (`tests/test_prompts.py`).
- The writer padded drafts with unsupported claims ("sticky", "rare", "stayed focused for this long") and generic praise. `personalize.md` now forbids both, and the `email3` step purpose no longer asks for a generic model-fit argument.

## Next steps, in order
1. Done: signals extracted and keepers rescored (see "What exists" and "Fixed on 2026-09-30").
2. Get the user's tone sign-off on the 5 pilot drafts (`vertex personalize show <domain>`), then `vertex personalize run b2b_vertical_software --limit 50 --workers 3` for the rest (26 of the 30 primary contacts have a usable hook; about $3).
3. `vertex review render b2b_vertical_software` → open `data/review/<date>.html`, decide, then `vertex review decide E1 E2 --decision approve` (or `vertex review apply data/review/decisions/<date>.json`). In a cloud session the user replies in chat and the engine runs `review decide`.
4. `vertex campaign create b2b_vertical_software --arm linkedin_led` and `--arm email_first`; run `vertex bridge next --connector lemlist` jobs one by one (create → steps → branches), then `vertex campaign finalize <id>`; verify with `vertex campaign show`.
5. `vertex campaign enroll <id>` pushes only approved contacts (gates); `vertex campaign launch <id> --i-have-reviewed` prints the human launch step. Never launch from the engine.
6. After a human launch: `vertex campaign mark-launched <id>`, then `vertex sync` every 4 hours (Routine), replies classified into review Section A.
7. Remaining build: report/analytics, feedback loop, Routines (connectors Inven/Apollo/Lemlist/Granola only), docs, tests.

## Open decisions for the user
- Clear a `.net` mailbox for wave 1, or accept ~10 leads/day on avue@vertex-equity.com.
- Confirm the 50 pre-accepted targets (Section C) and the 23 keepers held because they sit in the Apollo CRM list.
- The 20 targets without an owner-level Apollo contact: source by hand or drop.
- Add the three API keys as environment secrets and allow the three API hosts if the REST path is wanted; otherwise the bridge stays.
- Two targets fail the founded-before-2019 rule: ProScore (founded 2023) and Servos (2019). No founding year was found for Mismo, Dexur or Hoptek. Drop or keep?
- Critic bar: no pilot draft reached 4/5, the "ready to send" bar. Keep the bar so every draft gets a human edit, or accept 3/5 for human review?
- Copy style: some subject lines say "long-term ownership" (Psyquel) and Points North email 3 says "no sale clock". Keep, or keep subjects niche-only and avoid sale wording?
