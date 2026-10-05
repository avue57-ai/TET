import { describe, expect, it } from 'vitest';
import sharp from 'sharp';
import { handleAdmin } from '../src/admin';
import { AssetService, InMemoryAssetBytes, siteGuard } from '../src/assets';
import { InviteAuth } from '../src/auth';
import { InMemoryStore } from '../src/fakes';

const SECRET = 'a'.repeat(40);
function rig(secret = SECRET) {
  const store = new InMemoryStore(); const auth = new InviteAuth(store, 'b'.repeat(40));
  const assets = new AssetService(store, new InMemoryAssetBytes(), { describe: async () => ({ description: 'd', alt: 'a' }) }, siteGuard(store));
  const call = (path: string, body?: BodyInit, headers: Record<string, string> = { 'x-admin-secret': SECRET }) =>
    handleAdmin(new Request(`https://portal.test/api/admin/${path}`, { method: 'POST', body, headers }), { secret, store, auth, assets, origin: 'https://portal.test' });
  return { store, auth, call };
}
const site = { id: 'la-soiree', orgId: 'la-soiree-org', name: 'La Soirée', url: 'https://la-soiree-bridal.netlify.app', repo: { owner: 'avue57-ai', repo: 'la-soiree-bridal', netlifySiteName: 'la-soiree-bridal' } };

describe('admin', () => {
  it('rejects missing or wrong secrets and disables itself with a short secret', async () => {
    expect((await rig().call('sites', JSON.stringify(site), {})).status).toBe(401);
    expect((await rig().call('sites', JSON.stringify(site), { 'x-admin-secret': 'nope' })).status).toBe(401);
    expect((await rig('short').call('sites', JSON.stringify(site), { 'x-admin-secret': 'short' })).status).toBe(503);
  });
  it('registers a site, then an invite that logs the owner in to that org only', async () => {
    const { call, auth, store } = rig();
    expect((await call('sites', JSON.stringify(site))).status).toBe(200);
    expect((await store.get<any>('sites', 'la-soiree'))!.repo.repo).toBe('la-soiree-bridal');
    const { invite } = (await (await call('invites', JSON.stringify({ orgId: 'la-soiree-org', userId: 'owner' }))).json()) as any;
    const token = new URL(invite).searchParams.get('invite')!;
    expect(auth.verify(await auth.redeem(token))).toEqual({ userId: 'owner', orgId: 'la-soiree-org' });
  });
  it('validates input and refuses to move a site between orgs', async () => {
    const { call } = rig();
    expect((await call('sites', JSON.stringify({ id: 'Bad Id', orgId: 'x', name: 'n' }))).status).toBe(400);
    await call('sites', JSON.stringify(site));
    expect((await call('sites', JSON.stringify({ ...site, orgId: 'someone-else' }))).status).toBe(409);
  });
  it('uploads migration images for a site', async () => {
    const { call } = rig();
    await call('sites', JSON.stringify(site));
    const img = await sharp({ create: { width: 50, height: 50, channels: 3, background: '#fff' } }).jpeg().toBuffer();
    const res = await call('sites/la-soiree/assets', new Uint8Array(img), { 'x-admin-secret': SECRET, 'content-type': 'image/jpeg' });
    expect(res.status).toBe(201);
    expect(((await res.json()) as any).id).toMatch(/^ast_/);
    expect((await call('sites/nope/assets', new Uint8Array(img), { 'x-admin-secret': SECRET })).status).toBe(404);
  });
});
