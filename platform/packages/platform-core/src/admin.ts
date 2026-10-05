import { timingSafeEqual } from 'node:crypto';
import type { AssetService } from './assets';
import { UploadError } from './assets';
import type { InviteAuth } from './auth';
import type { Site } from './pipeline';
import type { Store } from './ports';

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });
const same = (a: string, b: string) => { const x = Buffer.from(a), y = Buffer.from(b); return x.length === y.length && timingSafeEqual(x, y); };
const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;

/**
 * Operator endpoints, used from the command line during onboarding. Guarded by one secret header.
 * POST /api/admin/sites                 {id, orgId, name, repo:{owner,repo,netlifySiteName}, url}
 * POST /api/admin/invites               {orgId, userId}  -> {invite}
 * POST /api/admin/sites/:id/assets      raw image bytes   -> {id, w, h, alt}   (migration uploads)
 */
export async function handleAdmin(req: Request, d: { secret: string; store: Store; auth: InviteAuth; assets: AssetService; origin: string }): Promise<Response> {
  if (d.secret.length < 32) return json(503, { error: 'admin disabled: ADMIN_SECRET must be at least 32 characters' });
  if (!same(req.headers.get('x-admin-secret') ?? '', d.secret)) return json(401, { error: 'unauthorized' });
  const parts = new URL(req.url).pathname.replace(/^\/api\/admin\/?/, '').split('/').filter(Boolean);
  try {
    if (req.method === 'POST' && parts[0] === 'sites' && parts.length === 1) {
      const b = (await req.json()) as Partial<Site> & { url?: string };
      if (!b.id || !ID.test(b.id) || !b.orgId || !ID.test(b.orgId) || !b.name || !b.repo?.owner || !b.repo.repo || !b.repo.netlifySiteName) return json(400, { error: 'id, orgId, name and repo{owner,repo,netlifySiteName} are required; ids are lowercase letters, numbers, dashes' });
      const existing = await d.store.get<Site>('sites', b.id);
      if (existing && existing.orgId !== b.orgId) return json(409, { error: 'site id belongs to another org' });
      await d.store.put('sites', b.id, { id: b.id, orgId: b.orgId, name: b.name, repo: b.repo } satisfies Site);
      if (b.url) await d.store.put('siteurls', b.id, { url: b.url });
      return json(200, { ok: true });
    }
    if (req.method === 'POST' && parts[0] === 'invites' && parts.length === 1) {
      const b = (await req.json()) as { orgId?: string; userId?: string };
      if (!b.orgId || !b.userId || !ID.test(b.orgId) || !ID.test(b.userId)) return json(400, { error: 'orgId and userId required' });
      const token = await d.auth.createInvite(b.orgId, b.userId);
      return json(200, { invite: `${d.origin}/?invite=${token}` });
    }
    if (req.method === 'POST' && parts[0] === 'sites' && parts[1] && parts[2] === 'assets' && parts.length === 3) {
      const site = await d.store.get<Site>('sites', parts[1]);
      if (!site) return json(404, { error: 'unknown site' });
      const r = await d.assets.upload({ userId: 'operator', orgId: site.orgId }, site.id, new Uint8Array(await req.arrayBuffer()), req.headers.get('content-type') ?? '');
      return json(201, r);
    }
    return json(404, { error: 'not found' });
  } catch (e) {
    if (e instanceof UploadError) return json(400, { error: e.message });
    console.error('admin error', (e as Error).stack);
    return json(500, { error: 'failed' });
  }
}
