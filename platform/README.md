# Site Manager platform

Customers describe a website change in plain language, preview it, and approve it. See `../docs/platform/ARCHITECTURE.md`, `STATUS.md` and `DESKTOP_RUNBOOK.md`.

```
npm install
npx vitest run      # 89 tests
npx tsc --noEmit -p tsconfig.json
npm run demo        # local portal with in-memory fakes and a scripted model at http://localhost:8787
npm run e2e         # browser test against the demo (needs Chromium)
```

- `packages/schemas`: content schemas and the validator every edit must pass.
- `packages/platform-core`: patch operations, the editor loop, the draft/preview/approve/undo pipeline, GitHub and Netlify Blobs clients, uploads, auth and the API.
- `apps/portal`: Netlify functions, the customer screen, and the local demo server.
