import { describe, expect, it } from 'vitest';
import { GitHubRepoClient, GitHubError } from '../src/github';

/** Minimal fake of the GitHub REST endpoints the client uses, with real git-like state. */
function fakeGitHub(initial: Record<string, string>) {
  const blobs = new Map<string, string>(); const trees = new Map<string, Record<string, string>>(); const commits = new Map<string, { tree: string; parents: string[]; message: string }>();
  const refs = new Map<string, string>(); const pulls: any[] = []; const statuses = new Map<string, any[]>(); const log: string[] = [];
  let n = 0; const id = (p: string) => `${p}${++n}`;
  const putBlob = (c: string) => { const sha = id('b'); blobs.set(sha, c); return sha; };
  const mkTree = (files: Record<string, string>) => { const sha = id('t'); trees.set(sha, files); return sha; };
  const root = id('c'); commits.set(root, { tree: mkTree(Object.fromEntries(Object.entries(initial).map(([p, c]) => [p, putBlob(c)]))), parents: [], message: 'seed' }); refs.set('main', root);
  const fetchFn = (async (url: string, init: any) => {
    const u = new URL(url); const path = u.pathname.replace('/repos/o/r', ''); const method = init.method; const body = init.body ? JSON.parse(init.body) : undefined;
    log.push(`${method} ${path}`);
    if (init.headers.authorization !== 'Bearer tok') return new Response('bad token', { status: 401 });
    const ok = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s });
    let m: RegExpExecArray | null;
    if ((m = /^\/git\/trees\/(.+)$/.exec(path)) && method === 'GET') { const ref = decodeURIComponent(m[1]!); const c = commits.get(refs.get(ref) ?? ref)!; const t = trees.get(c.tree)!; return ok({ tree: Object.entries(t).map(([p, sha]) => ({ path: p, type: 'blob', sha })) }); }
    if ((m = /^\/git\/blobs\/(.+)$/.exec(path))) return ok({ content: Buffer.from(blobs.get(m[1]!)!).toString('base64') });
    if ((m = /^\/git\/ref\/heads\/(.+)$/.exec(path))) return refs.has(decodeURIComponent(m[1]!)) ? ok({ object: { sha: refs.get(decodeURIComponent(m[1]!)) } }) : new Response('nf', { status: 404 });
    if (path === '/git/refs' && method === 'POST') { refs.set(body.ref.replace('refs/heads/', ''), body.sha); return ok({}, 201); }
    if ((m = /^\/git\/commits\/(.+)$/.exec(path)) && method === 'GET') { const c = commits.get(m[1]!)!; return ok({ tree: { sha: c.tree }, parents: c.parents.map((sha) => ({ sha })) }); }
    if (path === '/git/trees' && method === 'POST') { const next = { ...trees.get(body.base_tree)! }; for (const e of body.tree) { if (e.sha === null) delete next[e.path]; else next[e.path] = e.content !== undefined ? putBlob(e.content) : e.sha; } return ok({ sha: mkTree(next) }, 201); }
    if (path === '/git/commits' && method === 'POST') { const sha = id('c'); commits.set(sha, { tree: body.tree, parents: body.parents, message: body.message }); return ok({ sha }, 201); }
    if ((m = /^\/git\/refs\/heads\/(.+)$/.exec(path)) && method === 'PATCH') { refs.set(decodeURIComponent(m[1]!), body.sha); return ok({}); }
    if ((m = /^\/git\/refs\/heads\/(.+)$/.exec(path)) && method === 'DELETE') { refs.delete(decodeURIComponent(m[1]!)); return new Response(null, { status: 204 }); }
    if (path === '/pulls' && method === 'POST') { pulls.push({ number: pulls.length + 1, head: { ref: body.head, sha: refs.get(body.head) } }); return ok({ number: pulls.length }, 201); }
    if ((m = /^\/pulls\/(\d+)\/merge$/.exec(path))) { const pr = pulls[+m[1]! - 1]; const headC = commits.get(refs.get(pr.head.ref)!)!; const sha = id('c'); commits.set(sha, { tree: headC.tree, parents: [refs.get('main')!], message: 'squash' }); refs.set('main', sha); return ok({ sha, merged: true }); }
    if ((m = /^\/pulls\/(\d+)$/.exec(path)) && method === 'GET') { const pr = pulls[+m[1]! - 1]; return ok({ head: { sha: refs.get(pr.head.ref) } }); }
    if ((m = /^\/pulls\/(\d+)$/.exec(path)) && method === 'PATCH') return ok({ head: { ref: pulls[+m[1]! - 1].head.ref } });
    if ((m = /^\/commits\/([^/]+)\/status$/.exec(path))) return ok({ statuses: statuses.get(m[1]!) ?? [] });
    if ((m = /^\/commits\/(.+)$/.exec(path))) { const c = commits.get(m[1]!)!; const a = trees.get(commits.get(c.parents[0]!)!.tree)!, b = trees.get(c.tree)!; const files = [...new Set([...Object.keys(a), ...Object.keys(b)])].filter((p) => a[p] !== b[p]).map((p) => ({ filename: p, status: a[p] === undefined ? 'added' : b[p] === undefined ? 'removed' : 'modified' })); return ok({ parents: c.parents.map((sha) => ({ sha })), files }); }
    return new Response(`unhandled ${method} ${path}`, { status: 500 });
  }) as unknown as typeof fetch;
  const read = (ref: string) => { const t = trees.get(commits.get(refs.get(ref)!)!.tree)!; return Object.fromEntries(Object.entries(t).map(([p, s]) => [p, blobs.get(s)!])); };
  return { fetchFn, refs, statuses, log, read };
}

const seed = { 'content/pages/home.json': JSON.stringify({ title: 'Home' }), 'content/settings/x.json': JSON.stringify({ a: 1 }), 'src/pages/index.astro': '<h1/>', 'README.md': 'hi' };
function client(gh: ReturnType<typeof fakeGitHub>, token = 'tok') {
  return new GitHubRepoClient({ resolve: async (id) => { if (id !== 'siteA') throw new Error('unknown site'); return { owner: 'o', repo: 'r', netlifySiteName: 'la-soiree' }; }, token: async () => token, fetch: gh.fetchFn });
}

describe('GitHubRepoClient', () => {
  it('reads only content/*.json at a ref', async () => {
    const gh = fakeGitHub(seed);
    const files = await client(gh).getFiles('siteA', 'main');
    expect(Object.keys(files).sort()).toEqual(['content/pages/home.json', 'content/settings/x.json']);
    expect(files['content/pages/home.json']).toEqual({ title: 'Home' });
  });
  it('branch, commit, PR, preview readiness, squash merge', async () => {
    const gh = fakeGitHub(seed); const c = client(gh);
    await c.createBranch('siteA', 'draft/d1', 'main');
    const sha = await c.commit('siteA', 'draft/d1', 'Edit home', [{ path: 'content/pages/home.json', content: { title: 'New' } }, { path: 'content/settings/x.json', content: null }]);
    expect(gh.refs.get('draft/d1')).toBe(sha);
    expect(JSON.parse(gh.read('draft/d1')['content/pages/home.json']!)).toEqual({ title: 'New' });
    expect(gh.read('draft/d1')['content/settings/x.json']).toBeUndefined();
    expect(gh.read('draft/d1')['src/pages/index.astro']).toBe('<h1/>'); // code untouched
    const pr = await c.openPr('siteA', 'draft/d1', 'Edit home', 'body');
    expect(await c.previewUrl('siteA', pr.number)).toBeNull();
    gh.statuses.set(sha, [{ context: 'netlify/la-soiree/deploy-preview', state: 'pending' }]);
    expect(await c.previewUrl('siteA', pr.number)).toBeNull();
    gh.statuses.set(sha, [{ context: 'netlify/la-soiree/deploy-preview', state: 'success' }]);
    expect(await c.previewUrl('siteA', pr.number)).toBe('https://deploy-preview-1--la-soiree.netlify.app');
    const merged = await c.mergePr('siteA', pr.number);
    expect(gh.refs.get('main')).toBe(merged);
  });
  it('reverts a merged change back to the prior content, leaving unrelated files alone', async () => {
    const gh = fakeGitHub(seed); const c = client(gh);
    await c.createBranch('siteA', 'draft/d1', 'main');
    await c.commit('siteA', 'draft/d1', 'e', [{ path: 'content/pages/home.json', content: { title: 'New' } }]);
    const pr = await c.openPr('siteA', 'draft/d1', 't', 'b'); const merged = await c.mergePr('siteA', pr.number);
    const rev = await c.revertCommit('siteA', merged, 'Undo');
    expect(gh.refs.get('main')).toBe(rev);
    expect(JSON.parse(gh.read('main')['content/pages/home.json']!)).toEqual({ title: 'Home' });
    expect(gh.read('main')['README.md']).toBe('hi');
  });
  it('closing a PR deletes its draft branch', async () => {
    const gh = fakeGitHub(seed); const c = client(gh);
    await c.createBranch('siteA', 'draft/d2', 'main'); const pr = await c.openPr('siteA', 'draft/d2', 't', 'b');
    await c.closePr('siteA', pr.number);
    expect(gh.refs.has('draft/d2')).toBe(false);
  });
  it('never calls GitHub for an unknown site and surfaces API errors', async () => {
    const gh = fakeGitHub(seed);
    await expect(client(gh).getFiles('siteB', 'main')).rejects.toThrow('unknown site');
    expect(gh.log.length).toBe(0);
    await expect(client(gh, 'wrong').getFiles('siteA', 'main')).rejects.toThrow(GitHubError);
  });
});
