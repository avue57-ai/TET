# Build status (2026-10-05)

**Update (setup without typed secrets):** the portal now creates its own GitHub app (one-repository, short-lived tokens, via GitHub's confirm-and-install screens) and signs the operator in with a code printed in the Netlify function log. No environment variables, command-line tools or pasted tokens are needed. 105 tests pass, and the full setup flow passes 16 checks in a real browser with GitHub simulated. See `DESKTOP_RUNBOOK.md`.

**Live attempt, 2026-10-05:** the session on the owner's computer ran the tests, confirmed the draft pull request's Deploy Preview built and the live site is up, then stopped: it has a signed-in browser but no command-line tools, and it may not type secrets. The setup was redesigned (see the update above) so it no longer needs any.

## Built and tested in the cloud session

| Piece | Where | Evidence |
|---|---|---|
| Content schemas, derived from La Soirée's real files | `platform/packages/schemas` | All 94 real content files validate; negative tests reject bad data |
| Edit validator: patch operations, caps, path protection, WCAG contrast, link and reference checks | `platform/packages/platform-core/src/{ops,validator}.ts` | 20 unit tests plus 19 realistic customer requests run against La Soirée's real content |
| AI editor loop: classify, read, search, propose, ask, escalate; frozen cached prompt; priced usage log | `engine.ts`, `prompts.ts`, `anthropic.ts` | Scripted-model tests; request shape asserted (Opus 5.5, adaptive thinking, auto tool choice, 1-hour cache) |
| Draft, preview, approve, undo pipeline, one open draft, daily cap, audit rows | `pipeline.ts` | 17 tests including cross-tenant isolation |
| GitHub client (Git Data API), Netlify Blobs store, image upload with EXIF strip and alt text, public asset route | `github.ts`, `blobs.ts`, `assets.ts` | Tests against a fake GitHub and fake blob store |
| Invite-link login, signed cookies, CSRF header, API, admin onboarding, diagnostics | `auth.ts`, `api.ts`, `admin.ts`, `apps/portal/src/runtime.ts` | 22 tests |
| Customer screen: preview plus chat, upload, approve, discard, undo | `apps/portal/public` | 18 checks in real Chromium on desktop and 390 px phone; found and fixed one layout bug |
| La Soirée migrated to Site Standard v1 | `avue57-ai/la-soiree-bridal`, branch `standard-v1`, draft PR 1 | 117 built files identical to the previous build (apart from one theme style tag and 289 bytes of new CSS); site checks pass; bad edits fail the build |

105 tests in 10 files. Typecheck clean.

## Not done, and why

| Item | Why | Next step |
|---|---|---|
| Live portal on Netlify | The cloud session cannot reach Netlify; the desktop session cannot enter secrets | `DESKTOP_RUNBOOK.md` (browser only, no secrets typed) |
| Real-model behaviour | No Anthropic key in the cloud session, so the editor loop has only run against a scripted model | Runbook step 4 (`diag?probe=ai`) and step 8; then build an eval set from real runs |
| Netlify preview detection | Depends on the exact commit-status name Netlify posts; unverified | Runbook step 7 |
| Admin console screens | Operators use the admin API and diagnostics for now | Build after the pilot shows what operators need |
| Code agent for bespoke requests | Needs the live portal and an Anthropic account; the request is routed to an operator queue (status `awaiting_operator`) until then | Phase 6 of the plan |
| Other pages as data (About, Experience, VIP, Book, Contact, Collection, Designers, FAQ frame, Privacy) and journal articles | Each is a custom layout; moving them is mechanical but each needs its own parity check | One page at a time, same parity method |
| Prices and policies typed into prose (home VIP teaser, FAQ answers, appointment bullet lines) | Free text in content files | The assistant is told to search every occurrence before changing a fact; the search also finds numbers |
| One-repo GitHub credential | Needs an owner click-through to register a GitHub App | Replace the shortcut token before a second customer |
| Supabase, Resend, GitHub Team plan | Deferred by choice (zero new accounts) | Swap in at the triggers listed in the architecture document |

## Human steps that cannot be removed

1. An Anthropic API key, only if Netlify's AI Gateway does not supply one on the account's plan.
2. Merging the La Soirée pull request, and the DNS cutover. Both change the live customer site.
