import type { SiteFiles } from '../../schemas/src/index';
import type { FileWrite, RepoClient, Store } from './ports';

export class InMemoryStore implements Store {
  private data = new Map<string, Map<string, unknown>>();
  async get<T>(ns: string, key: string) { return structuredClone(this.data.get(ns)?.get(key)) as T | undefined; }
  async put<T>(ns: string, key: string, value: T) { if (!this.data.has(ns)) this.data.set(ns, new Map()); this.data.get(ns)!.set(key, structuredClone(value)); }
  async list<T>(ns: string) { return [...(this.data.get(ns)?.values() ?? [])].map((v) => structuredClone(v) as T); }
}

type Commit = { sha: string; parent: string | null; files: SiteFiles; message: string };
type Pr = { number: number; branch: string; open: boolean; merged: boolean; baseSha: string };
type Repo = { commits: Map<string, Commit>; branches: Map<string, string>; prs: Pr[]; n: number };

/** Git-like fake. Previews become ready only when markPreviewReady is called, as with Netlify. */
export class InMemoryRepo implements RepoClient {
  repos = new Map<string, Repo>();
  private ready = new Set<string>();
  autoPreview = true;
  seed(siteId: string, files: SiteFiles) {
    const sha = 'c0';
    this.repos.set(siteId, { commits: new Map([[sha, { sha, parent: null, files: structuredClone(files), message: 'seed' }]]), branches: new Map([['main', sha]]), prs: [], n: 1 });
  }
  private r(siteId: string) { const r = this.repos.get(siteId); if (!r) throw new Error(`unknown repo for ${siteId}`); return r; }
  private resolve(r: Repo, ref: string) { const sha = r.branches.get(ref) ?? (r.commits.has(ref) ? ref : undefined); if (!sha) throw new Error(`unknown ref ${ref}`); return r.commits.get(sha)!; }
  private add(r: Repo, parent: string, files: SiteFiles, message: string) { const sha = `c${r.n++}`; r.commits.set(sha, { sha, parent, files, message }); return sha; }
  async getFiles(siteId: string, ref: string) { return structuredClone(this.resolve(this.r(siteId), ref).files); }
  async createBranch(siteId: string, name: string, from: string) { const r = this.r(siteId); const c = this.resolve(r, from); r.branches.set(name, c.sha); return c.sha; }
  async commit(siteId: string, branch: string, message: string, writes: FileWrite[]) {
    const r = this.r(siteId); const head = this.resolve(r, branch); const files = structuredClone(head.files);
    for (const w of writes) { if (w.content === null) delete files[w.path]; else files[w.path] = structuredClone(w.content); }
    const sha = this.add(r, head.sha, files, message); r.branches.set(branch, sha); return sha;
  }
  async openPr(siteId: string, branch: string) { const r = this.r(siteId); const number = r.prs.length + 1; r.prs.push({ number, branch, open: true, merged: false, baseSha: r.branches.get('main')! }); if (this.autoPreview) this.ready.add(`${siteId}#${number}`); return { number }; }
  markPreviewReady(siteId: string, n: number) { this.ready.add(`${siteId}#${n}`); }
  async previewUrl(siteId: string, n: number) { return this.ready.has(`${siteId}#${n}`) ? `https://deploy-preview-${n}--${siteId}.netlify.app` : null; }
  async mergePr(siteId: string, n: number) {
    const r = this.r(siteId); const pr = r.prs.find((p) => p.number === n);
    if (!pr || !pr.open) throw new Error('PR not open');
    const main = this.resolve(r, 'main'); const head = this.resolve(r, pr.branch); const base = r.commits.get(pr.baseSha)!;
    const files = structuredClone(main.files);
    for (const p of new Set([...Object.keys(base.files), ...Object.keys(head.files)])) {
      if (JSON.stringify(base.files[p]) === JSON.stringify(head.files[p])) continue;
      if (JSON.stringify(main.files[p]) !== JSON.stringify(base.files[p])) throw new Error(`merge conflict in ${p}`);
      if (head.files[p] === undefined) delete files[p]; else files[p] = structuredClone(head.files[p]);
    }
    const sha = this.add(r, main.sha, files, `squash PR ${n}`); r.branches.set('main', sha); pr.open = false; pr.merged = true; return sha;
  }
  async closePr(siteId: string, n: number) { const pr = this.r(siteId).prs.find((p) => p.number === n); if (pr) pr.open = false; }
  async revertCommit(siteId: string, sha: string, message: string) {
    const r = this.r(siteId); const c = this.resolve(r, sha); const parent = r.commits.get(c.parent!)!; const main = this.resolve(r, 'main'); const files = structuredClone(main.files);
    for (const p of new Set([...Object.keys(parent.files), ...Object.keys(c.files)])) {
      if (JSON.stringify(parent.files[p]) === JSON.stringify(c.files[p])) continue;
      if (parent.files[p] === undefined) delete files[p]; else files[p] = structuredClone(parent.files[p]);
    }
    const nsha = this.add(r, main.sha, files, message); r.branches.set('main', nsha); return nsha;
  }
}
