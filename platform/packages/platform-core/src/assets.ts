import { randomBytes } from 'node:crypto';
import sharp, { type OutputInfo } from 'sharp';
import { ForbiddenError, type AssetRecord, type Session } from './pipeline';
import type { Store } from './ports';

export interface AssetBytesStore {
  put(siteId: string, id: string, bytes: Uint8Array, mime: string): Promise<void>;
  get(siteId: string, id: string): Promise<{ bytes: Uint8Array; mime: string } | null>;
}
export interface Describer { describe(bytes: Uint8Array, mime: string): Promise<{ description: string; alt: string }> }

export class UploadError extends Error {}
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const MAX_EDGE = 4000;

function sniff(b: Uint8Array): 'image/jpeg' | 'image/png' | 'image/webp' | null {
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return 'image/png';
  if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 && b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return 'image/webp';
  return null;
}
const clean = (s: string, max: number) => s.replace(/[<>]/g, '').replace(/\s+/g, ' ').trim().slice(0, max);

export class AssetService {
  constructor(private store: Store, private bytes: AssetBytesStore, private describer: Describer, private sites: { exists(s: Session, siteId: string): Promise<void> }) {}

  async upload(s: Session, siteId: string, input: Uint8Array, claimedMime: string) {
    await this.sites.exists(s, siteId);
    if (input.byteLength === 0 || input.byteLength > MAX_UPLOAD_BYTES) throw new UploadError('Images must be under 10 MB.');
    const real = sniff(input);
    if (!real) throw new UploadError('Please upload a JPG, PNG or WebP image.');
    if (claimedMime && !claimedMime.startsWith('image/')) throw new UploadError('Please upload an image.');
    let out: { data: Buffer; info: OutputInfo; mime: string };
    try {
      const img = sharp(input, { limitInputPixels: 50_000_000 });
      const meta = await img.metadata();
      const pipeline = img.rotate().resize({ width: MAX_EDGE, height: MAX_EDGE, fit: 'inside', withoutEnlargement: true });
      const keepAlpha = real !== 'image/jpeg' && meta.hasAlpha;
      const r = await (keepAlpha ? pipeline.png() : pipeline.jpeg({ quality: 85, mozjpeg: true })).toBuffer({ resolveWithObject: true });
      out = { data: r.data, info: r.info, mime: keepAlpha ? 'image/png' : 'image/jpeg' };
    } catch { throw new UploadError('That image could not be read. Try saving it as a JPG and uploading again.'); }

    const id = `ast_${randomBytes(6).toString('hex')}`;
    await this.bytes.put(siteId, id, out.data, out.mime);
    let d = { description: 'An uploaded image', alt: '' };
    try { d = await this.describer.describe(out.data, out.mime); } catch { /* alt text is optional; the owner can ask for it later */ }
    const rec: AssetRecord = { id, siteId, w: out.info.width, h: out.info.height, description: clean(d.description, 300) || 'An uploaded image', suggestedAlt: clean(d.alt, 200) };
    await this.store.put(`assets:${siteId}`, id, rec);
    return { id, w: rec.w, h: rec.h, alt: rec.suggestedAlt };
  }

  /** Public route used by the customer's site and Netlify Image CDN. Serves only assets that exist for that site. */
  async serve(siteId: string, id: string): Promise<Response> {
    const rec = await this.store.get<AssetRecord>(`assets:${siteId}`, id);
    if (!rec || rec.deleted) return new Response('not found', { status: 404 });
    const f = await this.bytes.get(siteId, id);
    if (!f) return new Response('not found', { status: 404 });
    return new Response(f.bytes as BodyInit, { headers: { 'content-type': f.mime, 'cache-control': 'public, max-age=31536000, immutable', 'x-content-type-options': 'nosniff' } });
  }
}

export class InMemoryAssetBytes implements AssetBytesStore {
  m = new Map<string, { bytes: Uint8Array; mime: string }>();
  async put(siteId: string, id: string, bytes: Uint8Array, mime: string) { this.m.set(`${siteId}/${id}`, { bytes, mime }); }
  async get(siteId: string, id: string) { return this.m.get(`${siteId}/${id}`) ?? null; }
}

export function siteGuard(store: Store) {
  return { async exists(s: Session, siteId: string) { const site = await store.get<{ orgId: string }>('sites', siteId); if (!site || site.orgId !== s.orgId) throw new ForbiddenError('site not found'); } };
}
