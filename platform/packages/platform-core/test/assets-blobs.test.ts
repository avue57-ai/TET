import { describe, expect, it } from 'vitest';
import sharp from 'sharp';
import { AssetService, InMemoryAssetBytes, siteGuard, UploadError, MAX_UPLOAD_BYTES } from '../src/assets';
import { NetlifyBlobsStore } from '../src/blobs';
import { InMemoryStore } from '../src/fakes';
import { ForbiddenError, type AssetRecord } from '../src/pipeline';

const alice = { userId: 'u1', orgId: 'orgA' }, mallory = { userId: 'u2', orgId: 'orgB' };
async function rig(describe = async () => ({ description: 'A bride <script>x</script> in lace', alt: 'Bride in lace' })) {
  const store = new InMemoryStore(); const bytes = new InMemoryAssetBytes();
  await store.put('sites', 'siteA', { id: 'siteA', orgId: 'orgA', name: 'A' });
  await store.put('sites', 'siteB', { id: 'siteB', orgId: 'orgB', name: 'B' });
  return { store, bytes, svc: new AssetService(store, bytes, { describe }, siteGuard(store)) };
}
const jpeg = (w: number, h: number, opts: any = {}) => sharp({ create: { width: w, height: h, channels: 3, background: '#aa8866' } }).jpeg(opts).toBuffer();

describe('AssetService.upload', () => {
  it('normalizes: caps size, strips EXIF, fixes rotation, stores a record with sanitized text', async () => {
    const { svc, store, bytes } = await rig();
    const big = await sharp({ create: { width: 6000, height: 3000, channels: 3, background: '#aa8866' } }).withMetadata({ orientation: 6, exif: { IFD0: { Copyright: 'secret-name' } } }).jpeg().toBuffer();
    const r = await svc.upload(alice, 'siteA', big, 'image/jpeg');
    expect(r.id).toMatch(/^ast_[0-9a-f]{12}$/);
    // 6000x3000 with orientation 6 is displayed 3000x6000, then capped to 4000 on the long edge
    expect(Math.max(r.w, r.h)).toBe(4000);
    expect(r.h).toBeGreaterThan(r.w);
    const stored = bytes.m.get(`siteA/${r.id}`)!;
    const meta = await sharp(stored.bytes).metadata();
    expect(meta.exif).toBeUndefined();
    const rec = (await store.get<AssetRecord>('assets:siteA', r.id))!;
    expect(rec.description).not.toMatch(/[<>]/);
    expect(rec.suggestedAlt).toBe('Bride in lace');
  });
  it('keeps transparency for PNG logos', async () => {
    const { svc, bytes } = await rig();
    const png = await sharp({ create: { width: 200, height: 100, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 0.4 } } }).png().toBuffer();
    const r = await svc.upload(alice, 'siteA', png, 'image/png');
    expect(bytes.m.get(`siteA/${r.id}`)!.mime).toBe('image/png');
  });
  it('rejects non-images by content, even with an image content-type', async () => {
    const { svc } = await rig();
    await expect(svc.upload(alice, 'siteA', new TextEncoder().encode('<svg onload=alert(1)>'), 'image/png')).rejects.toThrow(UploadError);
    await expect(svc.upload(alice, 'siteA', new Uint8Array(0), 'image/png')).rejects.toThrow(UploadError);
  });
  it('rejects oversize files and corrupt images', async () => {
    const { svc } = await rig();
    await expect(svc.upload(alice, 'siteA', new Uint8Array(MAX_UPLOAD_BYTES + 1), 'image/jpeg')).rejects.toThrow(/10 MB/);
    const corrupt = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff]), Buffer.from('not really a jpeg')]);
    await expect(svc.upload(alice, 'siteA', corrupt, 'image/jpeg')).rejects.toThrow(UploadError);
  });
  it('still stores the image if alt-text generation fails', async () => {
    const { svc } = await rig(async () => { throw new Error('model down'); });
    const r = await svc.upload(alice, 'siteA', await jpeg(300, 200), 'image/jpeg');
    expect(r.alt).toBe('');
  });
  it("refuses uploads to another org's site", async () => {
    const { svc, bytes } = await rig();
    await expect(svc.upload(mallory, 'siteA', await jpeg(100, 100), 'image/jpeg')).rejects.toThrow(ForbiddenError);
    expect(bytes.m.size).toBe(0);
  });
});

describe('AssetService.serve', () => {
  it('serves an existing asset with immutable caching and nosniff, 404 for wrong site or deleted', async () => {
    const { svc, store } = await rig();
    const r = await svc.upload(alice, 'siteA', await jpeg(100, 100), 'image/jpeg');
    const ok = await svc.serve('siteA', r.id);
    expect(ok.status).toBe(200);
    expect(ok.headers.get('cache-control')).toMatch(/immutable/);
    expect(ok.headers.get('x-content-type-options')).toBe('nosniff');
    expect((await svc.serve('siteB', r.id)).status).toBe(404);
    expect((await svc.serve('siteA', 'ast_nope')).status).toBe(404);
    const rec = (await store.get<AssetRecord>('assets:siteA', r.id))!;
    await store.put('assets:siteA', r.id, { ...rec, deleted: true });
    expect((await svc.serve('siteA', r.id)).status).toBe(404);
  });
});

describe('NetlifyBlobsStore', () => {
  const fake = () => { const m = new Map<string, unknown>(); return { m, s: { get: async (k: string) => (m.has(k) ? structuredClone(m.get(k)) : null), setJSON: async (k: string, v: unknown) => { m.set(k, structuredClone(v)); }, list: async ({ prefix }: { prefix: string }) => ({ blobs: [...m.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })) }) } }; };
  it('round-trips and lists only its own namespace', async () => {
    const { s, m } = fake(); const st = new NetlifyBlobsStore(s);
    await st.put('requests:siteA', 'r1', { n: 1 }); await st.put('requests:siteA', 'r2', { n: 2 }); await st.put('requests:siteB', 'r9', { n: 9 });
    expect(await st.get('requests:siteA', 'r1')).toEqual({ n: 1 });
    expect(await st.get('requests:siteA', 'nope')).toBeUndefined();
    expect((await st.list<{ n: number }>('requests:siteA')).map((x) => x.n).sort()).toEqual([1, 2]);
    expect(await st.list('requests:site')).toEqual([]); // a prefix of another namespace does not leak
    expect([...m.keys()]).toContain('requests/siteA/r1');
  });
});
