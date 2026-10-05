import { createHmac, timingSafeEqual } from 'node:crypto';
import Anthropic from '@anthropic-ai/sdk';
import { getStore } from '@netlify/blobs';
import {
  AnthropicDescriber, AnthropicLlmClient, AssetService, EditService, GitHubRepoClient, InviteAuth, NetlifyBlobsStore, BlobAssetBytes,
  handleAdmin, handleApi, siteGuard, type AssetBytesStore, type Describer, type EditRequest, type LlmClient, type RepoClient, type Site, type Store,
} from '../../../packages/platform-core/src/index';

type Env = Record<string, string | undefined>;
export type Overrides = { store: Store; bytes: AssetBytesStore; repo: RepoClient; llm: LlmClient; describer: Describer; fetchBackground: (url: string, init: RequestInit) => Promise<unknown> };

const need = (env: Env, k: string) => { const v = env[k]; if (!v) throw new Error(`missing environment variable ${k}`); return v; };
const same = (a: string, b: string) => { const x = Buffer.from(a), y = Buffer.from(b); return x.length === y.length && timingSafeEqual(x, y); };

/** Wires the platform from environment variables. Overrides exist for tests. */
export function createRuntime(env: Env, o: Partial<Overrides> = {}) {
  const store = o.store ?? new NetlifyBlobsStore(getStore('sm') as any);
  const bytes = o.bytes ?? new BlobAssetBytes(getStore('sm-assets') as any);
  const anthropic = () => new Anthropic(); // reads ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL (Netlify AI Gateway injects both) [verify]
  const llm = o.llm ?? new AnthropicLlmClient(anthropic());
  const describer = o.describer ?? new AnthropicDescriber(anthropic());
  const repo = o.repo ?? new GitHubRepoClient({
    resolve: async (siteId) => { const s = await store.get<Site>('sites', siteId); if (!s?.repo) throw new Error('unknown site'); return s.repo; },
    token: async () => need(env, 'GITHUB_TOKEN'),
  });
  // One master secret (SM_SECRET) is enough: it is the operator password, and the cookie-signing and internal keys are derived from it.
  // Individual SESSION_SECRET, INTERNAL_SECRET and ADMIN_SECRET still win if set.
  const master = env.SM_SECRET ?? '';
  if (master && master.length < 32) throw new Error('SM_SECRET must be at least 32 characters');
  const derive = (label: string) => createHmac('sha256', master).update(`site-manager:${label}`).digest('hex');
  const pick = (name: string, label: string) => env[name] ?? (master ? (label === 'admin' ? master : derive(label)) : need(env, name));
  const sessionSecret = pick('SESSION_SECRET', 'session');
  const internal = pick('INTERNAL_SECRET', 'internal');
  const adminSecret = pick('ADMIN_SECRET', 'admin');
  const auth = new InviteAuth(store, sessionSecret);
  const svc = new EditService({ store, repo, llm, maxEditsPerDay: Number(env.MAX_EDITS_PER_DAY ?? 30) });
  const assets = new AssetService(store, bytes, describer, siteGuard(store));
  const send = o.fetchBackground ?? ((url, init) => fetch(url, init));

  /** Setup check for operators. Reports which settings exist (never their values) and probes GitHub and the AI model. */
  async function diag(req: Request, url: URL): Promise<Response> {
    const secret = adminSecret;
    if (secret.length < 32 || !same(req.headers.get('x-admin-secret') ?? '', secret)) return new Response(JSON.stringify({ error: 'unauthorized' }), { status: 401, headers: { 'content-type': 'application/json' } });
    const out: Record<string, unknown> = {
      env: Object.fromEntries(['SM_SECRET', 'SESSION_SECRET', 'INTERNAL_SECRET', 'ADMIN_SECRET', 'GITHUB_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'URL'].map((k) => [k, Boolean(env[k])])),
      sites: (await store.list<Site>('sites')).map((x) => ({ id: x.id, org: x.orgId, repo: x.repo ? `${x.repo.owner}/${x.repo.repo}` : null })),
    };
    const siteId = url.searchParams.get('site');
    const site = siteId ? await store.get<Site>('sites', siteId) : undefined;
    if (site?.repo) {
      try {
        const r = await (o.fetchBackground ? Promise.resolve(null) : fetch(`https://api.github.com/repos/${site.repo.owner}/${site.repo.repo}`, { headers: { authorization: `Bearer ${need(env, 'GITHUB_TOKEN')}`, accept: 'application/vnd.github+json' } }));
        out.github = r ? { status: r.status, scopes: r.headers.get('x-oauth-scopes'), canPush: r.status === 200 ? ((await r.json()) as any).permissions?.push ?? null : null } : 'skipped in tests';
      } catch (e) { out.github = `error: ${(e as Error).message}`; }
    }
    if (url.searchParams.get('probe') === 'ai') {
      try { const r = await llm.complete({ system: 'Reply with the single word ok.', messages: [{ role: 'user', content: 'ping' }], tools: [] }); out.ai = { ok: true, model: r.model, tokens: r.usage }; }
      catch (e) { out.ai = { ok: false, error: String((e as Error).message).slice(0, 200) }; }
    }
    return new Response(JSON.stringify(out, null, 2), { headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });
  }

  return {
    async route(req: Request): Promise<Response> {
      const url = new URL(req.url);
      const origin = env.URL ?? url.origin;
      if (url.pathname === '/api/admin/diag' && req.method === 'GET') return diag(req, url);
      if (url.pathname.startsWith('/api/admin/')) return handleAdmin(req, { secret: adminSecret, store, auth, assets, origin });
      const m = /^\/assets\/([a-z0-9-]+)\/(ast_[a-f0-9]+)$/.exec(url.pathname);
      if (m && req.method === 'GET') return assets.serve(m[1]!, m[2]!);
      if (url.pathname.startsWith('/api/'))
        return handleApi(req, {
          svc, auth, store, assets,
          secureCookies: url.protocol === 'https:',
          kickProcess: async (siteId, requestId) => {
            // The AI loop can take over a minute, longer than a normal function may run, so hand it to the background function.
            await send(`${origin}/.netlify/functions/process-background`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-internal-secret': internal }, body: JSON.stringify({ siteId, requestId }) });
          },
        });
      return new Response('not found', { status: 404 });
    },
    async background(req: Request): Promise<Response> {
      if (!same(req.headers.get('x-internal-secret') ?? '', internal)) return new Response('unauthorized', { status: 401 });
      const { siteId, requestId } = (await req.json()) as { siteId: string; requestId: string };
      const r = await store.get<EditRequest>(`requests:${siteId}`, requestId);
      const site = await store.get<Site>('sites', siteId);
      if (!r || !site) return new Response('not found', { status: 404 });
      await svc.process({ userId: r.userId, orgId: site.orgId }, siteId, requestId);
      return new Response('done', { status: 200 });
    },
  };
}

let cached: ReturnType<typeof createRuntime> | null = null;
export const runtime = () => (cached ??= createRuntime(process.env));
