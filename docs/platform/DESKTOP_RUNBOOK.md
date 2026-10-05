# Runbook: put the assistant live (for the session that has the Netlify and GitHub logins)

You are the Claude session on the owner's Windows computer that built the La Soirée site. You already have Netlify and GitHub access. The cloud session that wrote the platform could not reach Netlify, Supabase or any secret, so the live steps are yours. Everything below uses accounts that already exist. Nothing needs the owner to click anything unless a step says STOP.

## Rules

- Do not merge the La Soirée pull request. Do not change DNS. Do not upgrade or buy any plan. Do not commit a secret anywhere. Never print a secret value in your replies.
- If a step fails twice, stop, write what failed and the exact error text (without secrets) into the report, and carry on with the steps that do not depend on it.
- Prefer small, reversible actions. The live La Soirée site must keep working at every moment.

## 0. Check your access

```
node -v        # 22 or newer
netlify status # logged in, shows the team
gh auth status # logged in to github.com as avue57-ai (or the owner's account)
```

## 1. Get the code (outside the TET repo)

TET holds private deal data and must never be connected to Netlify. Copy only the platform folder.

```
git clone --depth 1 --branch claude/pensive-hypatia-0ftxet https://github.com/avue57-ai/TET %TEMP%\tet
xcopy /E /I %TEMP%\tet\platform %USERPROFILE%\site-manager-portal
cd %USERPROFILE%\site-manager-portal
npm install
npx vitest run
```

Expected: 9 test files, 88 tests, all passing. If not, stop and report.

## 2. Create the portal site in the existing Netlify team

```
netlify sites:create --name la-soiree-portal
netlify link
```

If the name is taken, add a short suffix. Note the final address, for example `https://la-soiree-portal.netlify.app`. It is called PORTAL below.

## 3. Set the secrets (never commit these)

Generate three random strings of at least 32 characters and set them as secret environment variables on the portal site:

```
SESSION_SECRET   signs login cookies
INTERNAL_SECRET  protects the background function
ADMIN_SECRET     protects the operator endpoints
```

Keep ADMIN_SECRET in a file outside any repo (for example `%USERPROFILE%\.site-manager-admin`) so later onboarding calls can use it.

Then two credentials:

- **GITHUB_TOKEN**: a token that can read and write contents and pull requests on `avue57-ai/la-soiree-bridal` (and the staging repo in step 8). Use `gh auth token` only if nothing narrower is possible. A fine-grained token limited to those repos is better if one already exists. Record which you used in the report; this is the main security shortcut of the zero-new-accounts setup and must be replaced by a one-repo GitHub App token before a second customer is added.
- **AI access**: first check whether Netlify's AI Gateway injects `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` into functions for this site on the current plan (Netlify docs, "AI Gateway"). If yes, set nothing. If no, the portal needs an Anthropic API key. That is the one thing that may need the owner: say so plainly in the report, with the exact place to create it (console.anthropic.com, API keys) and the variable name `ANTHROPIC_API_KEY`.

## 4. Deploy the portal

```
netlify deploy --build --prod
```

Then verify, using ADMIN_SECRET in an `x-admin-secret` header:

```
GET  PORTAL/api/me                      -> 401 with a JSON error (not a Netlify error page)
GET  PORTAL/api/admin/diag              -> 401 without the secret
GET  PORTAL/api/admin/diag?probe=ai     -> env flags true for the three secrets and GITHUB_TOKEN; ai.ok true
```

If `ai.ok` is false and the error mentions credentials, that is the API key issue from step 3. If sync functions time out or the background function is not found, read Netlify's functions docs, fix the file names or settings in `apps/portal/netlify`, and note the change in the report. Things the cloud session could not check and may need adjusting: the `-background` function naming, `external_node_modules = ["sharp"]`, and the response shape of `@netlify/blobs` on the live service.

## 5. Register La Soirée

```
POST PORTAL/api/admin/sites      (x-admin-secret)
{ "id": "la-soiree", "orgId": "la-soiree-org", "name": "La Soirée Bridal",
  "url": "https://la-soiree-bridal.netlify.app",
  "repo": { "owner": "avue57-ai", "repo": "la-soiree-bridal", "netlifySiteName": "la-soiree-bridal" } }
GET PORTAL/api/admin/diag?site=la-soiree   -> github.status 200 and canPush true
```

## 6. Point the site branch at the portal and read the preview

On branch `standard-v1` of `avue57-ai/la-soiree-bridal` (draft pull request 1, already open):

```
node scripts/set-asset-base.mjs PORTAL
npm run build && npm run check
git add -A && git commit -m "Point site at the assistant portal" && git push
```

Netlify builds a Deploy Preview for the pull request at no credit cost. Open it on a desktop size and a phone size and compare with the live site: it should look identical. List any visual difference in the report. Leave the pull request as a draft and unmerged.

## 7. Check how Netlify reports a ready preview

The portal decides a preview is ready when the pull request's head commit has a successful commit status whose context matches `netlify/<site>/deploy-preview`. Look at the real statuses on pull request 1 (`gh api repos/avue57-ai/la-soiree-bridal/commits/<sha>/status`). If the context name differs, change the pattern in `packages/platform-core/src/github.ts` (`previewUrl`), update the test in `test/github.test.ts`, and redeploy. Note the real context name in the report.

## 8. End-to-end test on a staging copy, not the live site

```
gh repo create avue57-ai/la-soiree-staging --private
# push the standard-v1 branch as main of the staging repo
netlify sites:create --name la-soiree-staging   # link it to the staging repo, same build settings
node scripts/set-asset-base.mjs PORTAL           # in the staging repo, then push
POST PORTAL/api/admin/sites { "id": "la-soiree-staging", "orgId": "staging", "name": "La Soirée (staging)",
  "url": "<staging site url>", "repo": { "owner": "avue57-ai", "repo": "la-soiree-staging", "netlifySiteName": "la-soiree-staging" } }
POST PORTAL/api/admin/invites { "orgId": "staging", "userId": "tester" }   -> a link
```

Open the link in a browser, then run the example request with a photo: "Replace the big photo in the first section after the headline with this one and change the headline to Luxury Bridal, Personally Curated." Confirm, in order: a draft branch and pull request appear in the staging repo; the preview becomes ready; the preview shows the change; Approve publishes it to the staging site; Undo restores it within a minute. Also test: a question that should be refused ("add a booking widget") lands as "needs a designer's touch"; an unclear request gets one clarifying question; a second request while one is running is blocked.

Fix anything that breaks (the code is in `packages/platform-core` and `apps/portal`), keep the 88 tests passing, and note each fix.

## 9. Give the owner their link

```
POST PORTAL/api/admin/invites { "orgId": "la-soiree-org", "userId": "owner" }
```

Do not send it to anyone. Put the link in the report so the human can pass it on. It works once, for seven days. The owner's login must not be activated until the pull request is merged and the live site is on Standard v1, otherwise the assistant would be editing a site that does not read its files.

## 10. Report

Write `docs/platform/LIVE_STATUS.md` on branch `claude/pensive-hypatia-0ftxet` of `avue57-ai/TET` (pull first, commit, push). Include: PORTAL address, pass or fail for each step above, every discrepancy you found with evidence and the fix, which GitHub credential and AI route you used, the staging test results, and the exact things that still need a human (the Anthropic key if needed, the pull request merge, DNS, the owner invite).
