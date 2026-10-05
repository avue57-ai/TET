import type { SiteFiles } from '../../schemas/src/index';
import type { FileWrite, RepoClient } from './ports';

export class GitHubError extends Error { constructor(public status: number, msg: string) { super(msg); } }
export type RepoRef = { owner: string; repo: string; netlifySiteName: string };
export type GitHubOpts = {
  /** Maps our site id to its repo. Throws for unknown sites, which is what keeps one tenant out of another's repo. */
  resolve: (siteId: string) => Promise<RepoRef>;
  /** A token scoped to this one repo (GitHub App installation token, or a fine-grained token in the zero-touch MVP). */
  token: (siteId: string) => Promise<string>;
  fetch?: typeof fetch;
  base?: string;
};

/** RepoClient on the GitHub REST API (Git Data API for commits, so no clone is needed). */
export class GitHubRepoClient implements RepoClient {
  private f: typeof fetch; private base: string;
  constructor(private o: GitHubOpts) { this.f = o.fetch ?? fetch; this.base = o.base ?? 'https://api.github.com'; }

  private async call<T>(siteId: string, method: string, path: string, body?: unknown): Promise<T> {
    const r = await this.o.resolve(siteId);
    const res = await this.f(`${this.base}/repos/${r.owner}/${r.repo}${path}`, {
      method,
      headers: { authorization: `Bearer ${await this.o.token(siteId)}`, accept: 'application/vnd.github+json', 'x-github-api-version': '2022-11-28', 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) throw new GitHubError(res.status, `GitHub ${method} ${path} failed: ${res.status} ${(await res.text()).slice(0, 200)}`);
    return (res.status === 204 ? undefined : await res.json()) as T;
  }
  private enc = (ref: string) => ref.split('/').map(encodeURIComponent).join('/');

  async getFiles(siteId: string, ref: string): Promise<SiteFiles> {
    const tree = await this.call<{ tree: { path: string; type: string; sha: string }[]; truncated?: boolean }>(siteId, 'GET', `/git/trees/${this.enc(ref)}?recursive=1`);
    if (tree.truncated) throw new GitHubError(500, 'repository tree too large');
    const wanted = tree.tree.filter((t) => t.type === 'blob' && t.path.startsWith('content/') && t.path.endsWith('.json'));
    const files: SiteFiles = {};
    for (let i = 0; i < wanted.length; i += 8)
      await Promise.all(wanted.slice(i, i + 8).map(async (t) => {
        const b = await this.call<{ content: string }>(siteId, 'GET', `/git/blobs/${t.sha}`);
        files[t.path] = JSON.parse(Buffer.from(b.content, 'base64').toString('utf8'));
      }));
    return files;
  }
  async createBranch(siteId: string, name: string, from: string): Promise<string> {
    const head = await this.call<{ object: { sha: string } }>(siteId, 'GET', `/git/ref/heads/${this.enc(from)}`);
    await this.call(siteId, 'POST', '/git/refs', { ref: `refs/heads/${name}`, sha: head.object.sha });
    return head.object.sha;
  }
  private async writeTree(siteId: string, parentSha: string, writes: FileWrite[], fixed: { path: string; sha: string | null }[] = []) {
    const parent = await this.call<{ tree: { sha: string } }>(siteId, 'GET', `/git/commits/${parentSha}`);
    const entries = [
      ...writes.map((w) => (w.content === null ? { path: w.path, mode: '100644', type: 'blob', sha: null } : { path: w.path, mode: '100644', type: 'blob', content: JSON.stringify(w.content, null, 2) + '\n' })),
      ...fixed.map((f) => ({ path: f.path, mode: '100644', type: 'blob', sha: f.sha })),
    ];
    return (await this.call<{ sha: string }>(siteId, 'POST', '/git/trees', { base_tree: parent.tree.sha, tree: entries })).sha;
  }
  async commit(siteId: string, branch: string, message: string, writes: FileWrite[]): Promise<string> {
    const head = await this.call<{ object: { sha: string } }>(siteId, 'GET', `/git/ref/heads/${this.enc(branch)}`);
    const tree = await this.writeTree(siteId, head.object.sha, writes);
    const c = await this.call<{ sha: string }>(siteId, 'POST', '/git/commits', { message, tree, parents: [head.object.sha] });
    await this.call(siteId, 'PATCH', `/git/refs/heads/${this.enc(branch)}`, { sha: c.sha, force: false });
    return c.sha;
  }
  async openPr(siteId: string, branch: string, title: string, body: string) {
    const pr = await this.call<{ number: number }>(siteId, 'POST', '/pulls', { title: title.slice(0, 200), head: branch, base: 'main', body });
    return { number: pr.number };
  }
  /** Ready when Netlify's deploy-preview commit status succeeds; the URL follows Netlify's fixed pattern. [verify the status context name on the live account] */
  async previewUrl(siteId: string, n: number): Promise<string | null> {
    const r = await this.o.resolve(siteId);
    const pr = await this.call<{ head: { sha: string } }>(siteId, 'GET', `/pulls/${n}`);
    const st = await this.call<{ statuses: { context: string; state: string }[] }>(siteId, 'GET', `/commits/${pr.head.sha}/status`);
    const ok = st.statuses.some((s) => /^netlify\/.*\/deploy-preview$/.test(s.context) && s.state === 'success');
    return ok ? `https://deploy-preview-${n}--${r.netlifySiteName}.netlify.app` : null;
  }
  async mergePr(siteId: string, n: number): Promise<string> {
    const m = await this.call<{ sha: string; merged: boolean }>(siteId, 'PUT', `/pulls/${n}/merge`, { merge_method: 'squash' });
    if (!m.merged) throw new GitHubError(409, 'merge did not complete');
    return m.sha;
  }
  async closePr(siteId: string, n: number): Promise<void> {
    const pr = await this.call<{ head: { ref: string } }>(siteId, 'PATCH', `/pulls/${n}`, { state: 'closed' });
    await this.call(siteId, 'DELETE', `/git/refs/heads/${this.enc(pr.head.ref)}`).catch(() => {});
  }
  /** Direct commit to main that restores every file the given commit changed to its parent's version. */
  async revertCommit(siteId: string, sha: string, message: string): Promise<string> {
    const c = await this.call<{ parents: { sha: string }[]; files: { filename: string; status: string; previous_filename?: string }[] }>(siteId, 'GET', `/commits/${sha}`);
    const parentSha = c.parents[0]?.sha;
    if (!parentSha) throw new GitHubError(409, 'cannot revert a root commit');
    const parentTree = await this.call<{ tree: { path: string; sha: string }[] }>(siteId, 'GET', `/git/trees/${parentSha}?recursive=1`);
    const at = new Map(parentTree.tree.map((t) => [t.path, t.sha]));
    const fixed: { path: string; sha: string | null }[] = [];
    for (const f of c.files) {
      if (f.status === 'renamed' && f.previous_filename) { fixed.push({ path: f.filename, sha: null }); fixed.push({ path: f.previous_filename, sha: at.get(f.previous_filename) ?? null }); }
      else fixed.push({ path: f.filename, sha: at.get(f.filename) ?? null });
    }
    const head = await this.call<{ object: { sha: string } }>(siteId, 'GET', '/git/ref/heads/main');
    const tree = await this.writeTree(siteId, head.object.sha, [], fixed);
    const nc = await this.call<{ sha: string }>(siteId, 'POST', '/git/commits', { message, tree, parents: [head.object.sha] });
    await this.call(siteId, 'PATCH', '/git/refs/heads/main', { sha: nc.sha, force: false });
    return nc.sha;
  }
}
