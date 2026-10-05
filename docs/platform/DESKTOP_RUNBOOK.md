# Runbook: set up the assistant

For a Claude session that has a signed-in browser (Netlify and GitHub), such as the La Soirée session on the owner's computer. The owner approves prompts. Nothing here needs a command-line tool, a pasted token, or an environment variable.

## What changed from the earlier runbook

The portal now makes its own secrets. It creates a private, one-repository GitHub app through GitHub's confirm-and-install screens and keeps the keys itself. It signs the operator in with a short code printed in the Netlify function log, which only someone with access to the Netlify site can read. So the only things a human may ever need to type are the sign-in code (not a credential, and the session may type it) and, only if Netlify's AI service is not available, an Anthropic API key into the portal's own setup page.

## Who does what

- **The session:** every click and every page read.
- **The owner:** says yes to prompts, confirms on GitHub's screens (GitHub may ask for the account password or a security code: only the owner types those), and types an Anthropic API key into the setup page if one turns out to be needed.
- **The cloud session that wrote the code:** pushes any fix and points the staging site at the portal. Tell it the portal address when you have it.

Never paste a token, key or code into a chat. Use only the setup page.

## 1. Two Netlify sites (skip anything that already exists)

Import from GitHub (Add new project, Import an existing project, GitHub):

- `avue57-ai/site-manager`, named `la-soiree-portal` (add a suffix if the name is taken). Leave build settings alone. Add no environment variables.
- `avue57-ai/la-soiree-staging`, named `la-soiree-staging`. Leave build settings alone.

If Netlify says a repository is not visible, use its link to adjust GitHub access and tick only these two repositories, not `TET`. Deploy both and wait for Published. The portal address is called PORTAL below. If a build fails, copy the first error lines and tell the cloud session.

## 2. Sign in to the setup page

Open `PORTAL/setup.html`. A sign-in code has just been printed in the portal's log.

In Netlify, open the portal site, then Logs, then Functions, then the function `api`. Find the newest line `SETUP SIGN-IN CODE: XXXX-XXXX`. If no line appears, reload `setup.html` once and refresh the log. Type that code into the setup page. It works once and lasts 15 minutes. After five wrong tries it is replaced by a new one.

## 3. Connect GitHub (the owner confirms)

Click Connect GitHub (leave the organization empty for a personal account). GitHub shows "Create GitHub App": the owner confirms (and enters a password or code if asked). GitHub then shows Install: choose Only select repositories, tick `la-soiree-staging`, and Install. The browser returns to the setup page saying GitHub is connected.

To allow more sites later (for example `la-soiree-bridal` once its pull request is merged): GitHub, Settings, Applications, Installed GitHub Apps, the app, Configure, add the repository.

## 4. Register the staging website

On the setup page, step 3 is prefilled for staging. Set Live address to the real staging address from Netlify and click Register website.

## 5. Check everything

Click Check setup with site id `la-soiree-staging`. Expect: `github.installed` true, `repoAccess.status` 200 with `canPush` true, `aiProbe.ok` true.

If `aiProbe.ok` is false and the error mentions credentials, Netlify's AI service is not available for this site. The owner creates an API key at console.anthropic.com (API keys) and types it into step 2 of the setup page, then Save key and Check again.

## 6. Point the staging site at the portal

Tell the cloud session the PORTAL address. It runs `node scripts/set-asset-base.mjs PORTAL` in `la-soiree-staging` and pushes; Netlify rebuilds staging. Wait for it to publish.

## 7. Test as a customer, on staging only

On the setup page, step 5, Create link for customer `staging`, person `tester`. Open the link. Run, in order, and note pass or fail:

1. "Change the main headline to Luxury Bridal, Personally Curated." A preview appears within a few minutes. Approve and publish: staging updates. Undo: it reverts.
2. The same with a photo: "Replace the first big photo with this one", attaching a photo.
3. "Add a booking widget": should answer that it needs a designer's touch.
4. "Change the photo": should ask one clarifying question.
5. Send a second request while one is running: should be blocked politely.

Open the pull request the assistant made in `la-soiree-staging` and note the exact name of Netlify's deploy-preview check. The portal expects `netlify/<site>/deploy-preview`. If it differs, tell the cloud session.

## 8. Report

Send the cloud session the pass or fail of each step with exact error text (no secrets). It fixes anything in the code and records the result.

## Not part of this runbook, and never automatic

Merging draft pull request 1 on `la-soiree-bridal`, running `set-asset-base` there, registering the live site and installing the GitHub app on it, and the DNS cutover. The owner's own sign-in link is created only after the live site is on Standard v1.
