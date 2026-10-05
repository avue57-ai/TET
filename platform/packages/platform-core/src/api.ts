import { InviteAuth } from './auth';
import { ConflictError, EditService, ForbiddenError, RateLimitError, type Session } from './pipeline';
import type { Store } from './ports';

export type ApiDeps = {
  svc: EditService; auth: InviteAuth; store: Store;
  /** Starts the AI work without holding the HTTP request open (a Netlify background function in production). */
  kickProcess: (siteId: string, requestId: string) => Promise<void>;
  assets?: { upload(s: Session, siteId: string, bytes: Uint8Array, mime: string): Promise<{ id: string; w: number; h: number; alt: string }> };
  secureCookies?: boolean;
};

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store', ...headers } });

function cookieOf(req: Request, name: string) {
  return req.headers.get('cookie')?.split(/;\s*/).map((c) => c.split('=')).find(([k]) => k === name)?.[1];
}

export async function handleApi(req: Request, d: ApiDeps): Promise<Response> {
  const url = new URL(req.url);
  const parts = url.pathname.replace(/^\/api\/?/, '').split('/').filter(Boolean);
  const method = req.method;
  try {
    if (method !== 'GET' && req.headers.get('x-sm-csrf') !== '1') return json(403, { error: 'missing csrf header' });

    if (method === 'POST' && parts[0] === 'login' && parts.length === 1) {
      const { invite } = (await req.json().catch(() => ({}))) as { invite?: string };
      const cookie = invite ? await d.auth.redeem(invite) : null;
      if (!cookie) return json(401, { error: 'This link is invalid or has already been used. Ask for a new one.' });
      const flags = `HttpOnly; SameSite=Lax; Path=/; Max-Age=${30 * 86400}${d.secureCookies === false ? '' : '; Secure'}`;
      return json(200, { ok: true }, { 'set-cookie': `sm_session=${cookie}; ${flags}` });
    }

    const session = d.auth.verify(cookieOf(req, 'sm_session'));
    if (!session) return json(401, { error: 'Please sign in with your invite link.' });

    if (method === 'GET' && parts[0] === 'me') {
      const sites = (await d.store.list<{ id: string; orgId: string; name: string }>('sites')).filter((s) => s.orgId === session.orgId);
      return json(200, { sites: sites.map((s) => ({ id: s.id, name: s.name })) });
    }
    if (parts[0] === 'sites' && parts[1]) {
      const siteId = parts[1];
      if (method === 'GET' && parts.length === 3 && parts[2] === 'state') return json(200, await d.svc.state(session, siteId));
      if (method === 'POST' && parts[2] === 'requests' && parts.length === 3) {
        const body = (await req.json().catch(() => ({}))) as { text?: string; assetIds?: string[] };
        const r = await d.svc.enqueue(session, siteId, String(body.text ?? ''), Array.isArray(body.assetIds) ? body.assetIds.map(String).slice(0, 10) : []);
        await d.kickProcess(siteId, r.id);
        return json(202, { request: r });
      }
      if (method === 'POST' && parts[2] === 'assets' && parts.length === 3) {
        if (!d.assets) return json(501, { error: 'uploads are not enabled' });
        const mime = req.headers.get('content-type') ?? '';
        const bytes = new Uint8Array(await req.arrayBuffer());
        return json(201, await d.assets.upload(session, siteId, bytes, mime));
      }
      if (method === 'POST' && parts[2] === 'drafts' && parts[3] && parts[4] === 'approve') return json(200, { revision: await d.svc.approve(session, siteId, parts[3]) });
      if (method === 'POST' && parts[2] === 'drafts' && parts[3] && parts[4] === 'discard') { await d.svc.discard(session, siteId, parts[3]); return json(200, { ok: true }); }
      if (method === 'POST' && parts[2] === 'undo' && parts.length === 3) return json(200, { revision: await d.svc.undo(session, siteId) });
      if (method === 'GET' && parts[2] === 'history') return json(200, { revisions: await d.svc.history(session, siteId) });
    }
    return json(404, { error: 'not found' });
  } catch (e) {
    if (e instanceof ForbiddenError) return json(404, { error: 'not found' });
    if (e instanceof RateLimitError) return json(429, { error: (e as Error).message });
    if (e instanceof ConflictError) return json(409, { error: (e as Error).message });
    console.error('api error', (e as Error).stack);
    return json(500, { error: 'Something went wrong. Please try again, or contact us if it keeps happening.' });
  }
}
