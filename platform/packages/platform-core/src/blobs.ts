import type { Store } from './ports';

/** The slice of @netlify/blobs Store we use. Injected so the adapter is testable and swappable. */
export interface BlobStoreLike {
  get(key: string, opts: { type: 'json' }): Promise<any>;
  setJSON(key: string, data: unknown): Promise<unknown>;
  list(opts: { prefix: string }): Promise<{ blobs: { key: string }[] }>;
}
const k = (ns: string, key: string) => `${ns.replace(/[^A-Za-z0-9_.-]+/g, '/')}/${key}`;

/** Store on Netlify Blobs: one blob store, keys shaped ns/key. Tenant scoping is done by callers via ns (e.g. requests:<siteId>). */
export class NetlifyBlobsStore implements Store {
  constructor(private s: BlobStoreLike) {}
  async get<T>(ns: string, key: string) { return ((await this.s.get(k(ns, key), { type: 'json' })) ?? undefined) as T | undefined; }
  async put<T>(ns: string, key: string, value: T) { await this.s.setJSON(k(ns, key), value); }
  async list<T>(ns: string) {
    const prefix = k(ns, '');
    const { blobs } = await this.s.list({ prefix });
    const rows = await Promise.all(blobs.map((b) => this.s.get(b.key, { type: 'json' })));
    return rows.filter((r) => r != null) as T[];
  }
}

export interface BlobBytesLike {
  set(key: string, data: ArrayBuffer | Uint8Array | string, opts?: { metadata?: Record<string, string> }): Promise<unknown>;
  getWithMetadata(key: string, opts: { type: 'arrayBuffer' }): Promise<{ data: ArrayBuffer; metadata: Record<string, any> } | null>;
}
/** Image bytes on Netlify Blobs (a separate blob store from the JSON records). */
export class BlobAssetBytes {
  constructor(private s: BlobBytesLike) {}
  async put(siteId: string, id: string, bytes: Uint8Array, mime: string) { await this.s.set(`${siteId}/${id}`, bytes, { metadata: { mime } }); }
  async get(siteId: string, id: string) {
    const r = await this.s.getWithMetadata(`${siteId}/${id}`, { type: 'arrayBuffer' });
    return r ? { bytes: new Uint8Array(r.data), mime: String(r.metadata?.mime ?? 'application/octet-stream') } : null;
  }
}
