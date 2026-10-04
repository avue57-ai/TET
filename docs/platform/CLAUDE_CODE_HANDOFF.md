# Claude Code handoff prompt: Site Manager MVP

Give the text below to Claude Code in a fresh session opened on the new `avue57-ai/site-manager` repo. It assumes `docs/platform/ARCHITECTURE.md` has been copied into that repo.

---


You are the lead engineer building **Site Manager**, a multi-tenant SaaS that lets non-technical small-business owners change the websites we built for them by describing the change in plain language, previewing it, and approving it to production. You have full autonomy over implementation details. The architecture is decided; do not re-litigate it.

### 0. Read first, in this order
1. `docs/platform/ARCHITECTURE.md` in this repo — the full architecture, data model, security model, Site Standard v1, and the phase plan. Treat §6 (stack), §7 (Site Standard), §8 (AI engines), §9 (data model) and §10 (security) as fixed requirements.
2. The reference site `https://github.com/avue57-ai/la-soiree-bridal` (clone it read-only). Read `package.json`, `netlify.toml`, `public/admin/config.yml`, `src/data/*.js`, `src/lib/img.js`, `src/components/Pic.astro`, `src/pages/index.astro`, `scripts/*.mjs`, and sample files in `content/`. This is the site you will migrate in Phase 1 and the shape your `site-kit` must be able to express.
3. The Anthropic docs for the Messages API (structured outputs, strict tools, prompt caching), the TypeScript SDK, and Managed Agents (`platform.claude.com/docs/en/managed-agents/*`). Never write Anthropic SDK code from memory; verify every method against the docs.

### 1. Fixed decisions (do not change)
- **Monorepo** (pnpm workspaces, TypeScript strict everywhere): `apps/portal` (Next.js App Router; customer portal + admin + API routes + Netlify Functions/Background Functions), `packages/site-kit` (Astro components, Zod schemas, build/check/manifest scripts), `packages/schemas` (shared types), `packages/platform-core` (engines, GitHub/Netlify/Storage clients, job handlers — pure TS, no framework imports), `templates/site` (the site template), `tooling/provision-site`, `tooling/migrate-assets`, `docs/`.
- **Hosting:** the portal deploys to Netlify (team `platform`). Customer sites each get their own Netlify project in a *separate* team `customer-sites`, Git-connected to their own private GitHub repo in the sites organisation, production from `main`, Deploy Previews on PRs.
- **Data/auth/storage:** Supabase (Postgres with RLS on every tenant table, Auth magic links via Resend custom SMTP, Storage bucket `site-assets` with per-site prefixes, Realtime for job status). Free tier for now; a daily scheduled function pings the DB so it never pauses.
- **Source of truth per site:** its Git repo. Nothing ever writes to `main` except a PR merge performed by the platform GitHub App after customer approval. Every change happens on `draft/<draft_id>`.
- **Images:** never committed to site repos. Originals go to Supabase Storage; sites render them through Netlify Image CDN (`/.netlify/images?url=…`) with `remote_images` restricted to that site's prefix. Content stores `{asset, alt, w, h}`.
- **AI engines:** Engine A (structured) = Anthropic Messages API, model `claude-opus-5-5`, adaptive thinking, `output_config.effort: "medium"`, strict tools, structured outputs, prompt caching with 1-hour TTL on the system prompt + site manifest. Engine B (code) = Anthropic Managed Agents behind a `CodeAgentRunner` interface, with a Claude Agent SDK sandbox implementation as fallback. Engine B output is **operator-reviewed** before any customer sees it (feature flag `code_agent.customer_visible = false`).
- **Protections that are not optional:** schema validation before every commit; `npm run build` (which runs validate → manifest → astro build → check) must pass before a preview is shown; customer approval before production; every production state recorded as a `revision`; one-click undo via Netlify deploy restore + revert commit; per-request caps (≤ 20 files, ≤ 200 patch ops, AI budget), per-org daily edit cap, one open draft per site.
- **Tenant isolation:** RLS by org; per-job GitHub installation tokens scoped with `repository_ids` to one repo (1-hour lifetime); Netlify calls scoped by `sites.netlify_site_id`; Engine A tools are closures over `site_id`; Engine B sessions get one repo, `limited` networking (`allow_package_managers: true`), no web tools, no platform secrets, a dollar budget.
- **Free-tier rules:** see ARCHITECTURE.md §6. Record Netlify credit usage daily; alert at 200 credits. Do not upgrade any plan yourself; tell the owner when a trigger is hit.

### 2. Conventions
- Zod schemas are the single definition of every content shape and every API payload; derive TypeScript types from them. Export JSON Schema from Zod for the site manifest and for Anthropic tool `input_schema` (with `additionalProperties: false` + `required` so `strict: true` works).
- Every external system sits behind an interface in `platform-core` (`RepoClient`, `DeployClient`, `AssetStore`, `JobRunner`, `CodeAgentRunner`, `Mailer`) with a real implementation and an in-memory fake for tests.
- Every job is idempotent and resumable (state in Postgres, `FOR UPDATE SKIP LOCKED`, exponential backoff, max 3 attempts, then `failed` + operator alert). Background function payloads carry IDs only.
- Every state transition of `edit_requests`, `drafts`, `deployments`, `revisions` writes an `audit_log` row. Every Anthropic call writes an `ai_runs` row with model, tokens (input/cache read/cache write/output), cost, latency, request id.
- Prompts are files in `packages/platform-core/src/ai/prompts/*.md`, loaded at build time, with a stable, byte-identical system prefix (no timestamps, sorted JSON) so caching works; verify `cache_read_input_tokens > 0` in a test against the recorded fixture.
- Untrusted text (customer messages, site content, image descriptions, build logs) is always placed in the user turn, never in the system prompt, and tool results are treated as data.
- Secrets only in Netlify environment variables and Supabase vault; never in repos, prompts, logs, or client bundles. `.env.example` lists every variable with a one-line purpose.
- Tests: Vitest unit tests for schemas/engines/state machines; RLS tests that run SQL as two different users; recorded-fixture tests for GitHub/Netlify clients; Playwright e2e for the portal; an engine eval set (`packages/platform-core/eval/requests.jsonl`, ≥ 40 cases) scored on correct target, schema validity, no unrelated changes, asks-when-ambiguous. CI (GitHub Actions) runs lint, typecheck, unit, RLS, eval (recorded) on every PR.
- Commits: small, conventional messages, one phase per PR into this repo's `main`; never force-push.

### 3. What to build, in order (definitions of done are in ARCHITECTURE.md §12 — meet them literally)
**Phase 0 — Foundations.** Monorepo, CI, `.env.example`, docs copied in. *Owner actions you must request once, then wait:* create (or let you create) the private repo `avue57-ai/site-manager`; create the Netlify team `customer-sites`; create the Supabase project; create the Resend account; register the GitHub App on the sites organisation (permissions: contents rw, pull_requests rw, metadata r, administration rw; webhook off) and install it; create a Netlify personal access token; provide the Anthropic API key. Put all values in `.env` locally and in Netlify env for the portal.

**Phase 1 — Site Standard v1 + `site-kit` + La Soirée migration.** Build the section catalogue (§7.3) with Zod schemas and Astro components, `Pic` with Image CDN + legacy-key support, `Base` layout driven by `seo.json`/`theme.json`, the `validate-content → export-manifest → astro build → check` pipeline, `data-sm-path` attributes + postMessage bridge in preview context, `templates/site` that builds a demo site with 0 check problems, `docs/STANDARD.md`. Then migrate La Soirée **on a branch of its own repo** (`standard-v1`), following §7.8: every hardcoded string into `content/pages/*.json`, nav/footer/theme/meta into settings, collections renamed generically, `tooling/migrate-assets` uploads `media-source/**` to Storage and rewrites every reference, remove committed derivatives. Open a PR on the La Soirée repo and verify the Netlify Deploy Preview: 0 check problems, visual parity on the 16 key pages (take Playwright screenshots of the production site and the preview at 390 px and 1440 px and compare by eye; list every difference in the PR), Lighthouse ≥ 95 mobile on home/collection/gown. **Do not merge that PR yourself** — the owner decides when the live site switches.

**Phase 2 — Platform core.** Supabase migrations for §9 + RLS + RLS tests; Auth; portal shell; `platform-core` clients with fakes; jobs table + Background Function runner; keep-alive + credit-usage scheduled function; `provision-site` CLI; Sentry. Prove it by provisioning a throwaway site from the template end to end, then deleting it.

**Phase 3 — Structured edit pipeline.** State machine; classifier + Engine A with the tool set in §8.1; validator with caps; draft/branch/PR management; deploy webhook handler; approve (squash-merge) and undo (restore + revert, with the ≥ 90-day rebuild fallback); the single portal screen (chat, Realtime status, preview iframe, Approve & Publish / Keep editing / Discard / Undo, History); customer emails. Build the eval set from realistic requests against the migrated La Soirée manifest and reach ≥ 90%. Run the §5 example on a staging copy of La Soirée (provision one from the migrated branch; do not touch the live site).

**Phase 4 — Assets.** Upload (presigned), processing job (sharp: EXIF, HEIC→JPEG, ≤ 4000 px, dims, dominant colour), Claude vision description + alt, chat attachment flow, replace/delete, orphan sweeper, `remote_images` enforcement test.

**Phase 5 — Admin console & operations.** Sites/health, requests with filters and cost, conversation viewer, AI usage by org/day, deployments, errors; actions: retry, fail, take over, release to customer, restore revision; operator queue for `needs_code` / `awaiting_operator`; daily cap + per-request budget enforcement; `docs/RUNBOOK.md`; admin alert emails.

**Phase 6 — Code agent (operator-reviewed).** `CodeAgentRunner` interface; Managed Agents implementation (agent + environment provisioned once by a guarded setup script that stores IDs; sessions with the repo resource, `limited` networking, budget, `initial_events`; webhook handler); `.claude/skills/site-standard/SKILL.md` in the template; Agent SDK fallback runner; routing from `escalate`/`needs_code` to the runner with results landing in the operator queue; flag stays off for customers. Prove with three scripted tasks on the staging site.

**Phase 7 — Pilot.** Write `docs/PILOT_CHECKLIST.md` for onboarding La Soirée's owner (account, first login, three guided requests). Stop and hand over.

### 4. Guardrails
- Never push to any customer site's `main` directly, never merge the La Soirée migration PR, never delete a Netlify deploy or a Storage object that a revision references, never disable RLS, never put a token in a prompt or a log, never widen a GitHub token beyond one repo, never let Engine B output reach a customer while the flag is off.
- When a Netlify, GitHub, Supabase or Anthropic behaviour differs from ARCHITECTURE.md, trust the live API and docs, implement what works, and record the discrepancy in `docs/DECISIONS.md` with date and evidence.
- Ask the owner only for: credentials, account/team creation, DNS, the La Soirée cutover, and plan upgrades. Everything else is your call; prefer the simplest option that meets the definition of done.
- If a phase's test gate cannot be met, do not move on; write down exactly what fails and why, and propose the smallest change that would unblock it.

### 5. Reporting
End each phase with a short written status in `docs/STATUS.md`: what was built, test output summary (counts and the command to reproduce), measured numbers (eval pass rate, median AI cost per edit from `ai_runs`, preview build time), what was deferred and why, and the exact owner actions needed next. Keep it under one page per phase.

