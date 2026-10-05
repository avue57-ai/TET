import { describe, expect, it } from 'vitest';
import { handleApi } from '../src/api';
import { InviteAuth } from '../src/auth';
import { EditService } from '../src/pipeline';
import { InMemoryRepo, InMemoryStore } from '../src/fakes';
import { ScriptedLlm, tool } from '../src/fake-llm';
import { sampleSite } from '../../schemas/src/sample';

const SECRET = 'x'.repeat(40);
const HOME = 'content/pages/home.json';
const change = [[tool('propose_changes', { summary: 'Changed the headline.', changes: [{ path: HOME, ops: [{ op: 'replace', path: '/sections/1/headline', value: 'Private. Personal.' }] }] })]];

async function rig(clock = { t: 1_800_000_000_000 }) {
  const store = new InMemoryStore(), repo = new InMemoryRepo();
  repo.seed('siteA', sampleSite()); repo.seed('siteB', sampleSite());
  await store.put('sites', 'siteA', { id: 'siteA', orgId: 'orgA', name: 'La Soirée' });
  await store.put('sites', 'siteB', { id: 'siteB', orgId: 'orgB', name: 'Other' });
  const svc = new EditService({ store, repo, llm: new ScriptedLlm(change), now: () => new Date(clock.t) });
  const auth = new InviteAuth(store, SECRET, () => clock.t);
  const pending: Promise<unknown>[] = [];
  const deps = { svc, auth, store, secureCookies: false, kickProcess: async (siteId: string, rid: string) => { pending.push(svc.process({ userId: 'u1', orgId: 'orgA' }, siteId, rid)); } };
  const call = (method: string, path: string, opts: { body?: unknown; cookie?: string; csrf?: boolean } = {}) =>
    handleApi(new Request(`https://portal.test${path}`, { method, body: opts.body === undefined || method === 'GET' ? undefined : JSON.stringify(opts.body), headers: { ...(opts.cookie ? { cookie: `sm_session=${opts.cookie}` } : {}), ...(method !== 'GET' && opts.csrf !== false ? { 'x-sm-csrf': '1' } : {}) } }), deps);
  const login = async (orgId = 'orgA', userId = 'u1') => {
    const token = await auth.createInvite(orgId, userId);
    const res = await call('POST', '/api/login', { body: { invite: token } });
    return { res, token, cookie: /sm_session=([^;]+)/.exec(res.headers.get('set-cookie') ?? '')?.[1] };
  };
  return { call, login, auth, pending, clock, svc };
}

describe('login', () => {
  it('redeems an invite once and sets an HttpOnly cookie', async () => {
    const { login, call } = await rig();
    const { res, token, cookie } = await login();
    expect(res.status).toBe(200);
    expect(res.headers.get('set-cookie')).toMatch(/HttpOnly/);
    expect(cookie).toBeTruthy();
    expect((await call('POST', '/api/login', { body: { invite: token } })).status).toBe(401);
  });
  it('rejects expired invites, tampered cookies and missing sessions', async () => {
    const r = await rig();
    const token = await r.auth.createInvite('orgA', 'u1', 1000);
    r.clock.t += 5000;
    expect((await r.call('POST', '/api/login', { body: { invite: token } })).status).toBe(401);
    const { cookie } = await r.login();
    const [p, m] = cookie!.split('.');
    const forged = Buffer.from(JSON.stringify({ userId: 'u1', orgId: 'orgB', exp: r.clock.t + 1e9 })).toString('base64url');
    expect((await r.call('GET', '/api/me', { cookie: `${forged}.${m}` })).status).toBe(401);
    expect((await r.call('GET', '/api/me', { cookie: `${p}.AAAA` })).status).toBe(401);
    expect((await r.call('GET', '/api/me')).status).toBe(401);
  });
  it('expires sessions after 30 days', async () => {
    const r = await rig();
    const { cookie } = await r.login();
    expect((await r.call('GET', '/api/me', { cookie })).status).toBe(200);
    r.clock.t += 31 * 86_400_000;
    expect((await r.call('GET', '/api/me', { cookie })).status).toBe(401);
  });
  it('refuses short secrets', () => { expect(() => new InviteAuth(new InMemoryStore(), 'short')).toThrow(); });
});

describe('customer flow over the API', () => {
  it('request, preview, approve, history, undo', async () => {
    const r = await rig();
    const { cookie } = await r.login();
    const me: any = await (await r.call('GET', '/api/me', { cookie })).json();
    expect(me.sites).toEqual([{ id: 'siteA', name: 'La Soirée' }]);

    const created = await r.call('POST', '/api/sites/siteA/requests', { cookie, body: { text: 'Shorten the statement headline' } });
    expect(created.status).toBe(202);
    await Promise.all(r.pending);

    const st: any = await (await r.call('GET', '/api/sites/siteA/state', { cookie })).json();
    expect(st.requests[0].status).toBe('preview_ready');
    expect(st.draft.previewUrl).toMatch(/^https:\/\/deploy-preview-1--siteA/);

    const ok = await r.call('POST', `/api/sites/siteA/drafts/${st.draft.id}/approve`, { cookie });
    expect(ok.status).toBe(200);
    const hist: any = await (await r.call('GET', '/api/sites/siteA/history', { cookie })).json();
    expect(hist.revisions.length).toBe(1);
    expect((await r.call('POST', '/api/sites/siteA/undo', { cookie })).status).toBe(200);
  });
  it('blocks a second request while one is running, and bad input', async () => {
    const r = await rig();
    const { cookie } = await r.login();
    r.svc.process = async () => ({}) as any; // keep the first request in "received"
    await r.call('POST', '/api/sites/siteA/requests', { cookie, body: { text: 'one' } });
    expect((await r.call('POST', '/api/sites/siteA/requests', { cookie, body: { text: 'two' } })).status).toBe(409);
    expect((await r.call('POST', '/api/sites/siteA/requests', { cookie, body: { text: '   ' } })).status).toBe(409);
  });
  it('requires the csrf header on writes', async () => {
    const r = await rig();
    const { cookie } = await r.login();
    expect((await r.call('POST', '/api/sites/siteA/requests', { cookie, csrf: false, body: { text: 'x' } })).status).toBe(403);
  });
});

describe('API tenant isolation', () => {
  it("returns 404 for another org's site on every route", async () => {
    const r = await rig();
    const { cookie } = await r.login('orgA', 'u1');
    for (const [m, p] of [['GET', '/api/sites/siteB/state'], ['POST', '/api/sites/siteB/requests'], ['POST', '/api/sites/siteB/undo'], ['GET', '/api/sites/siteB/history'], ['POST', '/api/sites/siteB/drafts/d1/approve'], ['POST', '/api/sites/siteB/drafts/d1/discard'], ['GET', '/api/sites/nope/state']] as const) {
      const res = await r.call(m, p, { cookie, body: { text: 'hi' } });
      expect(res.status, `${m} ${p}`).toBe(404);
    }
  });
  it('does not list other orgs sites', async () => {
    const r = await rig();
    const { cookie } = await r.login('orgB', 'u9');
    const me: any = await (await r.call('GET', '/api/me', { cookie })).json();
    expect(me.sites.map((s: any) => s.id)).toEqual(['siteB']);
  });
});
