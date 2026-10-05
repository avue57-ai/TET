// Local demo server: the real portal runtime and UI with in-memory fakes and a scripted model. Run: npx tsx apps/portal/dev/server.ts
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { InMemoryAssetBytes, InMemoryRepo, InMemoryStore, ScriptedLlm, tool } from '../../../packages/platform-core/src/index';
import { sampleSite } from '../../../packages/schemas/src/sample';
import { createRuntime } from '../src/runtime';

const PORT = Number(process.env.PORT ?? 8787);
const env = { SESSION_SECRET: 's'.repeat(40), INTERNAL_SECRET: 'i'.repeat(40), ADMIN_SECRET: 'a'.repeat(40), URL: `http://localhost:${PORT}` };
const store = new InMemoryStore(), repo = new InMemoryRepo(); repo.seed('la-soiree', sampleSite());
const HOME = 'content/pages/home.json';
const llm = new ScriptedLlm([
  [tool('read_content', { path: HOME })],
  [tool('propose_changes', { summary: 'Changed the homepage headline.', changes: [{ path: HOME, ops: [{ op: 'replace', path: '/sections/0/headline', value: ['Luxury Bridal,', 'Personally Curated.'] }] }] })],
  [tool('ask_customer', { question: 'Which photo do you mean, the large one at the top or the one lower down?' })],
]);
let bg: Promise<unknown> = Promise.resolve();
const rt = createRuntime(env, { store, repo, llm, bytes: new InMemoryAssetBytes(), describer: { describe: async () => ({ description: 'A bride in a lace gown', alt: 'Bride in a lace gown' }) },
  fetchBackground: async (_u, init) => { bg = rt.background(new Request('http://x/bg', { method: 'POST', headers: init.headers as any, body: init.body as string })); } });
const MIME: Record<string, string> = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
const PUBLIC = join(import.meta.dirname, '../public');

createServer(async (req, res) => {
  const url = new URL(req.url!, env.URL);
  const chunks: Buffer[] = []; for await (const c of req) chunks.push(c as Buffer);
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/assets/')) {
    const r = await rt.route(new Request(url, { method: req.method, headers: req.headers as any, body: ['GET', 'HEAD'].includes(req.method!) ? undefined : Buffer.concat(chunks) }));
    res.writeHead(r.status, Object.fromEntries(r.headers)); res.end(Buffer.from(await r.arrayBuffer())); return;
  }
  const file = join(PUBLIC, url.pathname === '/' ? 'index.html' : normalize(url.pathname).replace(/^(\.\.[/\\])+/, ''));
  let data: Buffer | null = null; try { data = await readFile(file); } catch { /* not found */ }
  if (!data) { res.writeHead(404); res.end('nf'); return; }
  res.writeHead(200, { 'content-type': MIME[extname(file)] ?? 'text/plain' }); res.end(data);
}).listen(PORT, async () => {
  const admin = (p: string, body: unknown) => rt.route(new Request(`${env.URL}/api/admin/${p}`, { method: 'POST', body: JSON.stringify(body), headers: { 'x-admin-secret': env.ADMIN_SECRET } }));
  await admin('sites', { id: 'la-soiree', orgId: 'la-soiree-org', name: 'La Soirée Bridal', url: `http://localhost:${PORT}/demo-site.html`, repo: { owner: 'o', repo: 'r', netlifySiteName: 'la-soiree' } });
  const { invite } = (await (await admin('invites', { orgId: 'la-soiree-org', userId: 'owner' })).json()) as any;
  console.log(`READY ${invite}`);
  void bg;
});
