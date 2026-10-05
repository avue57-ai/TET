# Runbook: put the assistant live

**Read "Path B" first.** The first attempt found the session has a signed-in browser (Netlify and GitHub) but no `netlify` or `gh` command-line tools, and it may not type secrets or tokens into any field. Path B is built for that. Path A is the original command-line version, kept for a computer where those tools are logged in.

## Path B: browser only, two pasted values

Who does what: the session in the owner's browser does every click and reads every page. The owner (a human at the keyboard) does exactly these four things, because they involve secrets or consent screens: (1) creates one GitHub token, (2) pastes two values into Netlify's environment-variable page, (3) approves any GitHub or Netlify permission screen that appears, (4) types the setup password into the portal's setup page. Everything else is automatic.

### B1. Two empty private repositories (browser, no secrets)

In the signed-in browser, create two **empty private** repositories under `avue57-ai` at github.com/new, with no README, no .gitignore, no licence:

- `site-manager` (the portal)
- `la-soiree-staging` (a private test copy of the migrated website)

Then tell the cloud session (the one that wrote the platform) the two names are ready. It will attach both and push the code:
- `site-manager` gets the contents of `platform/` as its root (so Netlify finds `netlify.toml` there).
- `la-soiree-staging` gets the `standard-v1` branch of `la-soiree-bridal` as its `main`.

### B2. Netlify: two sites from Git (browser)

In Netlify (team already signed in), choose Add new project, then Import an existing project, then GitHub:
- Import `site-manager`. Name it `la-soiree-portal` (add a suffix if taken). The build settings come from `netlify.toml`; change nothing. Do not deploy yet.
- Import `la-soiree-staging`. Name it `la-soiree-staging`. Build settings come from its `netlify.toml`.
Approve the GitHub permission screen so Netlify can see only these two repositories. Do not grant access to `TET`.

### B3. The owner pastes two values (secrets)

1. **Create a GitHub token.** github.com/settings/personal-access-tokens/new. Name: `site-manager`. Expiry: 90 days. Repository access: only `la-soiree-bridal` and `la-soiree-staging`. Permissions: Contents read and write, Pull requests read and write (Metadata read is automatic). Copy the token.
2. **Pick a setup password**: any long random string of at least 32 characters (a password manager's generator is ideal). Save it somewhere safe. This is the only operator password.
3. In Netlify, on the `la-soiree-portal` site, open Site configuration, Environment variables, and add:
   - `SM_SECRET` = the setup password (mark as secret)
   - `GITHUB_TOKEN` = the token (mark as secret)
4. **AI access.** First try without an Anthropic key: Netlify may supply one through its AI Gateway on the current plan. If the setup check in B5 says the AI is not reachable, add `ANTHROPIC_API_KEY` (create one at console.anthropic.com, API keys) the same way. That is the only other value that can ever be needed.

Never paste any of these into a chat.

### B4. Deploy (browser)

Trigger the deploy of `la-soiree-portal` and of `la-soiree-staging`. Wait for both to show Published. Note the portal address, called PORTAL below.

### B5. Setup page (owner types the password)

Open `PORTAL/setup.html`. Type the setup password into the page yourself. Then, in order:
1. **Check setup** (leave the site id blank): the three secret flags should be true and `ai.ok` true.
2. **Register a website** with these values: site id `la-soiree-staging`, customer id `staging`, name `La Soirée (staging)`, live address = the staging Netlify address, GitHub owner `avue57-ai`, repository `la-soiree-staging`, Netlify site name = the staging site's name. Click Register.
3. **Check setup** with site id `la-soiree-staging`: `github.status` should be 200 and `canPush` true.
4. **Create link** for customer id `staging`, person `tester`. The page shows a sign-in link (works once).

### B6. Point the staging site at the portal, then test (session, browser)

In the cloud session's push access to `la-soiree-staging`, the cloud session runs `node scripts/set-asset-base.mjs PORTAL` and pushes. (If the cloud session cannot, the browser session edits `content/settings/site.json` `assets.base` and the `netlify.toml` block as that script does, through GitHub's web editor.) Wait for the staging site to rebuild.

Open the sign-in link and run these on the staging site, in order, noting pass or fail:
1. "Change the main headline to Luxury Bridal, Personally Curated." A preview should appear within a few minutes showing the change. Approve and publish. The staging site updates. Undo. It reverts.
2. The same with a photo: "Replace the first big photo with this one" and attach a photo.
3. "Add a booking widget": should be answered as needing a designer's touch.
4. "Change the photo": should ask one clarifying question.
5. Send a second request while one is running: should be blocked politely.

### B7. Confirm Netlify's preview status name

Open the pull request the assistant created in `la-soiree-staging`. Note the exact name of Netlify's deploy-preview check or status. The portal expects a status named like `netlify/<site>/deploy-preview`. If it differs, tell the cloud session; it will change `packages/platform-core/src/github.ts`, run the tests, and push.

### B8. Report

Write what passed and failed, with exact error text and no secrets, to `docs/platform/LIVE_STATUS.md` (the cloud session can do this if you paste the results to it).

### What is left for the live La Soirée site

Not part of this runbook, and never automatic: merging draft pull request 1 on `la-soiree-bridal`, running `node scripts/set-asset-base.mjs PORTAL` there, registering the live site, and the DNS cutover. The owner's own sign-in link is created only after the live site is on Standard v1.

---

## Path A: command line (original)

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

Expected: 9 test files, 89 tests, all passing. If not, stop and report.

## 2. Create the portal site in the existing Netlify team

```
netlify sites:create --name la-soiree-portal
netlify link
```

If the name is taken, add a short suffix. Note the final address, for example `https://la-soiree-portal.netlify.app`. It is called PORTAL below.

## 3. Set the secrets (never commit these)

Generate one random string of at least 32 characters and set it as a secret environment variable `SM_SECRET` on the portal site. It is the operator password, and the cookie-signing and internal keys are derived from it. (`SESSION_SECRET`, `INTERNAL_SECRET` and `ADMIN_SECRET` still work if you prefer to set them separately.) Keep it in a file outside any repo so later onboarding calls can use it, or use `PORTAL/setup.html` instead of calling the admin API by hand.

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
