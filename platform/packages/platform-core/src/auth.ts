import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import type { Store } from './ports';
import type { Session } from './pipeline';

type Invite = { orgId: string; userId: string; expiresAt: number; used: boolean };
const b64 = (b: Buffer | string) => Buffer.from(b).toString('base64url');
const sha = (s: string) => createHash('sha256').update(s).digest('hex');

/**
 * Passwordless login with no outside service: an operator creates a single-use invite link, redeeming it sets a
 * signed session cookie. Swap for magic-link email (Supabase Auth) once that exists; the Session shape stays the same.
 */
export class InviteAuth {
  constructor(private store: Store, private secret: string, private now: () => number = Date.now) {
    if (secret.length < 32) throw new Error('session secret must be at least 32 characters');
  }
  async createInvite(orgId: string, userId: string, ttlMs = 7 * 86_400_000): Promise<string> {
    const token = randomBytes(32).toString('base64url');
    await this.store.put<Invite>('invites', sha(token), { orgId, userId, expiresAt: this.now() + ttlMs, used: false });
    return token;
  }
  /** Returns a session cookie value, or null if the invite is unknown, used or expired. */
  async redeem(token: string): Promise<string | null> {
    const inv = await this.store.get<Invite>('invites', sha(token));
    if (!inv || inv.used || inv.expiresAt < this.now()) return null;
    await this.store.put('invites', sha(token), { ...inv, used: true });
    return this.sign({ userId: inv.userId, orgId: inv.orgId });
  }
  sign(s: Session, ttlMs = 30 * 86_400_000): string {
    const payload = b64(JSON.stringify({ ...s, exp: this.now() + ttlMs }));
    return `${payload}.${b64(createHmac('sha256', this.secret).update(payload).digest())}`;
  }
  verify(cookie: string | undefined | null): Session | null {
    if (!cookie) return null;
    const [payload, mac] = cookie.split('.');
    if (!payload || !mac) return null;
    const expected = createHmac('sha256', this.secret).update(payload).digest();
    const given = Buffer.from(mac, 'base64url');
    if (given.length !== expected.length || !timingSafeEqual(given, expected)) return null;
    try {
      const p = JSON.parse(Buffer.from(payload, 'base64url').toString());
      if (typeof p.userId !== 'string' || typeof p.orgId !== 'string' || p.exp < this.now()) return null;
      return { userId: p.userId, orgId: p.orgId };
    } catch { return null; }
  }
}
