import { timingSafeEqual } from 'node:crypto';
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
  const auth = new InviteAuth(store, need(env, 'SESSION_SECRET'));
  const svc = new EditService({ store, repo, llm, maxEditsPerDay: Number(env.MAX_EDITS_PER_DAY ?? 30) });
  const assets = new AssetService(store, bytes, describer, siteGuard(store));
  const internal = need(env, 'INTERNAL_SECRET');
  const send = o.fetchBackground ?? ((url, init) => fetch(url, init));

  return {
    async route(req: Request): Promise<Response> {
      const url = new URL(req.url);
      const origin = env.URL ?? url.origin;
      if (url.pathname.startsWith('/api/admin/')) return handleAdmin(req, { secret: env.ADMIN_SECRET ?? '', store, auth, assets, origin });
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
