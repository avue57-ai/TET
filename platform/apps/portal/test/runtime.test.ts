import { describe, expect, it } from 'vitest';
import sharp from 'sharp';
import { InMemoryAssetBytes, InMemoryRepo, InMemoryStore, ScriptedLlm, tool } from '../../../packages/platform-core/src/index';
import { sampleSite } from '../../../packages/schemas/src/sample';
import { createRuntime } from '../src/runtime';

const env = { SESSION_SECRET: 's'.repeat(40), INTERNAL_SECRET: 'i'.repeat(40), ADMIN_SECRET: 'a'.repeat(40), URL: 'https://portal.test' };
const HOME = 'content/pages/home.json';

async function boot() {
  const store = new InMemoryStore(), repo = new InMemoryRepo(); repo.seed('la-soiree', sampleSite());
  const llm = new ScriptedLlm([[tool('propose_changes', { summary: 'Updated the statement.', changes: [{ path: HOME, ops: [{ op: 'replace', path: '/sections/1/headline', value: 'Private. Personal.' }] }] })]]);
  const calls: { url: string; init: RequestInit }[] = [];
  const rt = createRuntime(env, { store, repo, llm, bytes: new InMemoryAssetBytes(), describer: { describe: async () => ({ description: 'A gown', alt: 'A gown' }) }, fetchBackground: async (url, init) => { calls.push({ url, init }); } });
  const admin = (path: string, body: BodyInit, ct = 'application/json') => rt.route(new Request(`https://portal.test/api/admin/${path}`, { method: 'POST', body, headers: { 'x-admin-secret': env.ADMIN_SECRET, 'content-type': ct } }));
  await admin('sites', JSON.stringify({ id: 'la-soiree', orgId: 'la-soiree-org', name: 'La Soirée', url: 'https://la-soiree-bridal.netlify.app', repo: { owner: 'avue57-ai', repo: 'la-soiree-bridal', netlifySiteName: 'la-soiree-bridal' } }));
  const { invite } = (await (await admin('invites', JSON.stringify({ orgId: 'la-soiree-org', userId: 'owner' }))).json()) as any;
  const login = await rt.route(new Request('https://portal.test/api/login', { method: 'POST', body: JSON.stringify({ invite: new URL(invite).searchParams.get('invite') }), headers: { 'x-sm-csrf': '1' } }));
  const cookie = /sm_session=([^;]+)/.exec(login.headers.get('set-cookie')!)![1]!;
  const api = (method: string, path: string, body?: unknown, extra: Record<string, string> = {}) => rt.route(new Request(`https://portal.test${path}`, { method, body: body === undefined ? undefined : JSON.stringify(body), headers: { cookie: `sm_session=${cookie}`, ...(method === 'GET' ? {} : { 'x-sm-csrf': '1' }), ...extra } }));
  return { rt, api, calls, repo, admin, cookie };
}

describe('portal runtime', () => {
  it('onboard, login, upload, request, background process, preview, approve, undo', async () => {
    const { rt, api, calls, repo, admin } = await boot();

    const img = await sharp({ create: { width: 800, height: 600, channels: 3, background: '#cc9' } }).jpeg().toBuffer();
    const up = await rt.route(new Request('https://portal.test/api/sites/la-soiree/assets', { method: 'POST', body: new Uint8Array(img), headers: { 'content-type': 'image/jpeg', 'x-sm-csrf': '1', cookie: '' } }));
    expect(up.status).toBe(401); // uploads need a session

    const created = await api('POST', '/api/sites/la-soiree/requests', { text: 'Shorten the statement headline' });
    expect(created.status).toBe(202);
    expect(calls[0]!.url).toBe('https://portal.test/.netlify/functions/process-background');
    expect((calls[0]!.init.headers as any)['x-internal-secret']).toBe(env.INTERNAL_SECRET);

    // the background function does the AI work
    const body = JSON.parse(String(calls[0]!.init.body));
    expect((await rt.background(new Request('https://x', { method: 'POST', body: JSON.stringify(body), headers: { 'x-internal-secret': 'wrong' } }))).status).toBe(401);
    expect((await rt.background(new Request('https://x', { method: 'POST', body: JSON.stringify(body), headers: { 'x-internal-secret': env.INTERNAL_SECRET } }))).status).toBe(200);

    const st: any = await (await api('GET', '/api/sites/la-soiree/state')).json();
    expect(st.site.url).toBe('https://la-soiree-bridal.netlify.app');
    expect(st.requests[0]).toMatchObject({ status: 'preview_ready', summary: 'Updated the statement.' });
    expect(st.draft.previewUrl).toContain('deploy-preview-1--');

    expect((await api('POST', `/api/sites/la-soiree/drafts/${st.draft.id}/approve`)).status).toBe(200);
    expect(((await repo.getFiles('la-soiree', 'main'))[HOME] as any).sections[1].headline).toBe('Private. Personal.');
    expect((await api('POST', '/api/sites/la-soiree/undo')).status).toBe(200);
    expect(((await repo.getFiles('la-soiree', 'main'))[HOME] as any).sections[1].headline).toBe('Private. Curated. Personal.');
    void admin;
  });

  it('serves uploaded assets publicly, only at the right site and id shape', async () => {
    const { rt, admin } = await boot();
    const img = await sharp({ create: { width: 64, height: 64, channels: 3, background: '#fff' } }).jpeg().toBuffer();
    const up: any = await (await admin('sites/la-soiree/assets', new Uint8Array(img), 'image/jpeg')).json();
    const ok = await rt.route(new Request(`https://portal.test/assets/la-soiree/${up.id}`));
    expect(ok.status).toBe(200);
    expect(ok.headers.get('content-type')).toBe('image/jpeg');
    expect((await rt.route(new Request(`https://portal.test/assets/other-site/${up.id}`))).status).toBe(404);
    expect((await rt.route(new Request('https://portal.test/assets/la-soiree/..%2Fsecret'))).status).toBe(404);
  });

  it('keeps the admin API closed without the secret and unknown paths 404', async () => {
    const { rt } = await boot();
    expect((await rt.route(new Request('https://portal.test/api/admin/sites', { method: 'POST', body: '{}' }))).status).toBe(401);
    expect((await rt.route(new Request('https://portal.test/whatever'))).status).toBe(404);
  });

  it('diagnostics need the admin secret and never reveal values', async () => {
    const { rt } = await boot();
    expect((await rt.route(new Request('https://portal.test/api/admin/diag'))).status).toBe(401);
    const res = await rt.route(new Request('https://portal.test/api/admin/diag?probe=ai', { headers: { 'x-admin-secret': env.ADMIN_SECRET } }));
    const body = await res.json() as any;
    expect(body.env).toMatchObject({ SESSION_SECRET: true, GITHUB_TOKEN: false, ANTHROPIC_API_KEY: false });
    expect(body.sites[0]).toMatchObject({ id: 'la-soiree', repo: 'avue57-ai/la-soiree-bridal' });
    expect(body.ai.ok).toBe(true);
    expect(JSON.stringify(body)).not.toContain(env.SESSION_SECRET);
  });

  it('runs from a single master secret and uses it as the operator password', async () => {
    const store = new InMemoryStore(), repo = new InMemoryRepo(); repo.seed('la-soiree', sampleSite());
    const one = { SM_SECRET: 'm'.repeat(48), URL: 'https://portal.test' };
    const calls: any[] = [];
    const rt = createRuntime(one, { store, repo, llm: new ScriptedLlm([[tool('ask_customer', { question: 'q?' })]]), bytes: new InMemoryAssetBytes(), describer: { describe: async () => ({ description: 'd', alt: 'a' }) }, fetchBackground: async (u, i) => { calls.push({ u, i }); } });
    const admin = (p: string, body: unknown) => rt.route(new Request(`https://portal.test/api/admin/${p}`, { method: 'POST', body: JSON.stringify(body), headers: { 'x-admin-secret': one.SM_SECRET } }));
    expect((await rt.route(new Request('https://portal.test/api/admin/diag', { headers: { 'x-admin-secret': 'wrong'.repeat(10) } }))).status).toBe(401);
    expect((await admin('sites', { id: 'la-soiree', orgId: 'o', name: 'n', repo: { owner: 'a', repo: 'b', netlifySiteName: 'c' } })).status).toBe(200);
    const { invite } = (await (await admin('invites', { orgId: 'o', userId: 'u' })).json()) as any;
    const login = await rt.route(new Request('https://portal.test/api/login', { method: 'POST', body: JSON.stringify({ invite: new URL(invite).searchParams.get('invite') }), headers: { 'x-sm-csrf': '1' } }));
    expect(login.status).toBe(200);
    // the derived internal key protects the background function, and the master secret itself is not that key
    const cookie = /sm_session=([^;]+)/.exec(login.headers.get('set-cookie')!)![1]!;
    await rt.route(new Request('https://portal.test/api/sites/la-soiree/requests', { method: 'POST', body: JSON.stringify({ text: 'hi' }), headers: { cookie: `sm_session=${cookie}`, 'x-sm-csrf': '1' } }));
    expect(calls[0].i.headers['x-internal-secret']).not.toBe(one.SM_SECRET);
    expect(calls[0].i.headers['x-internal-secret']).toMatch(/^[0-9a-f]{64}$/);
    expect(() => createRuntime({ SM_SECRET: 'short' }, { store, repo, llm: new ScriptedLlm([]), bytes: new InMemoryAssetBytes(), describer: { describe: async () => ({ description: '', alt: '' }) } })).toThrow(/at least 32/);
  });

  it('refuses to start without its secrets', () => {
    expect(() => createRuntime({ SESSION_SECRET: 'x'.repeat(40) }, { store: new InMemoryStore(), bytes: new InMemoryAssetBytes(), repo: new InMemoryRepo(), llm: new ScriptedLlm([]), describer: { describe: async () => ({ description: '', alt: '' }) } })).toThrow(/INTERNAL_SECRET/);
  });
});
