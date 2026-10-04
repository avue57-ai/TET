import { StructuredEditor, costUsd, type AssetInfo, type RunUsage } from './engine';
import type { LlmClient, RepoClient, Store } from './ports';

export class ForbiddenError extends Error {}
export class RateLimitError extends Error {}
export class ConflictError extends Error {}

export type Session = { userId: string; orgId: string };
export type Site = { id: string; orgId: string; name: string };
export type AssetRecord = { id: string; siteId: string; w: number; h: number; description: string; suggestedAlt: string; deleted?: boolean };
export type Status = 'received' | 'needs_clarification' | 'awaiting_operator' | 'failed' | 'previewing' | 'preview_ready' | 'published' | 'discarded';
export type ChangedField = { file: string; pointer: string; before: unknown; after: unknown };
export type EditRequest = {
  id: string; siteId: string; draftId: string | null; userId: string; text: string; assetIds: string[]; status: Status;
  summary?: string; changedFields?: ChangedField[]; question?: string; error?: string; createdAt: string;
};
export type Draft = { id: string; siteId: string; status: 'open' | 'published' | 'discarded'; branch: string; prNumber: number | null; previewUrl: string | null };
export type Revision = { id: string; siteId: string; number: number; commitSha: string; summary: string; restoredFrom?: string; createdAt: string };

export type Deps = { store: Store; repo: RepoClient; llm: LlmClient; now?: () => Date; maxEditsPerDay?: number; newId?: () => string };

export class EditService {
  private now: () => Date;
  private maxPerDay: number;
  private newId: () => string;
  constructor(private d: Deps) {
    this.now = d.now ?? (() => new Date());
    this.maxPerDay = d.maxEditsPerDay ?? 30;
    this.newId = d.newId ?? (() => crypto.randomUUID().slice(0, 8));
  }

  /** Same error whether the site is missing or belongs to another org, so existence never leaks. */
  private async siteFor(s: Session, siteId: string): Promise<Site> {
    const site = await this.d.store.get<Site>('sites', siteId);
    if (!site || site.orgId !== s.orgId) throw new ForbiddenError('site not found');
    return site;
  }
  private async audit(s: Session, siteId: string, action: string, target: string) {
    await this.d.store.put('audit', this.newId(), { at: this.now().toISOString(), orgId: s.orgId, userId: s.userId, siteId, action, target });
  }
  private async openDraft(siteId: string) { return (await this.d.store.list<Draft>(`drafts:${siteId}`)).find((x) => x.status === 'open') ?? null; }

  async submit(s: Session, siteId: string, text: string, assetIds: string[] = []): Promise<EditRequest> {
    const site = await this.siteFor(s, siteId);
    const all = await this.d.store.list<EditRequest>(`requests:${siteId}`);
    const dayAgo = this.now().getTime() - 86_400_000;
    if (all.filter((r) => new Date(r.createdAt).getTime() > dayAgo).length >= this.maxPerDay) throw new RateLimitError('daily edit limit reached');

    const assetRecs = (await this.d.store.list<AssetRecord>(`assets:${siteId}`)).filter((a) => !a.deleted);
    for (const id of assetIds) if (!assetRecs.some((a) => a.id === id)) throw new ForbiddenError(`unknown asset ${id}`);
    const assets: AssetInfo[] = assetRecs.filter((a) => assetIds.includes(a.id)).map((a) => ({ id: a.id, description: a.description, w: a.w, h: a.h, suggestedAlt: a.suggestedAlt }));

    let draft = await this.openDraft(siteId);
    const req: EditRequest = { id: this.newId(), siteId, draftId: draft?.id ?? null, userId: s.userId, text, assetIds, status: 'received', createdAt: this.now().toISOString() };
    await this.d.store.put(`requests:${siteId}`, req.id, req);
    await this.audit(s, siteId, 'edit_request.received', req.id);

    const files = await this.d.repo.getFiles(site.id, draft?.branch ?? 'main');
    const recent = all.filter((r) => r.draftId && r.draftId === draft?.id).map((r) => `Owner: ${r.text}`);
    const editor = new StructuredEditor(this.d.llm, (u) => this.logUsage(siteId, req.id, u));
    const out = await editor.run({ files, assets, text, knownAssets: new Set(assetRecs.map((a) => a.id)), recentTurns: recent });

    if (out.kind === 'ask') return this.setStatus(s, req, 'needs_clarification', { question: out.question });
    if (out.kind === 'escalate') return this.setStatus(s, req, 'awaiting_operator', { error: `${out.category}: ${out.reason}` });
    if (out.kind === 'failed') return this.setStatus(s, req, 'failed', { error: out.reason });

    if (!draft) {
      const id = this.newId();
      const branch = `draft/${id}`;
      await this.d.repo.createBranch(site.id, branch, 'main');
      draft = { id, siteId, status: 'open', branch, prNumber: null, previewUrl: null };
      req.draftId = id;
    }
    await this.d.repo.commit(site.id, draft.branch, out.summary, out.changedPaths.map((p) => ({ path: p, content: out.files[p] ?? null })));
    if (draft.prNumber === null) draft.prNumber = (await this.d.repo.openPr(site.id, draft.branch, out.summary, `Owner request: ${text}`)).number;
    await this.d.store.put(`drafts:${siteId}`, draft.id, draft);
    const changedFields = out.diff.flatMap((d) => d.fields.map((f) => ({ file: d.path, ...f })));
    return this.setStatus(s, req, 'previewing', { summary: out.summary, changedFields });
  }

  /** Called by the portal poller or a deploy status webhook. */
  async refreshPreview(s: Session, siteId: string, requestId: string): Promise<EditRequest> {
    const site = await this.siteFor(s, siteId);
    const req = await this.d.store.get<EditRequest>(`requests:${siteId}`, requestId);
    if (!req) throw new ForbiddenError('request not found');
    if (req.status !== 'previewing' || !req.draftId) return req;
    const draft = (await this.d.store.get<Draft>(`drafts:${siteId}`, req.draftId))!;
    const url = await this.d.repo.previewUrl(site.id, draft.prNumber!);
    if (!url) return req;
    draft.previewUrl = url;
    await this.d.store.put(`drafts:${siteId}`, draft.id, draft);
    return this.setStatus(s, req, 'preview_ready', {});
  }

  async approve(s: Session, siteId: string, draftId: string): Promise<Revision> {
    const site = await this.siteFor(s, siteId);
    const draft = await this.d.store.get<Draft>(`drafts:${siteId}`, draftId);
    if (!draft || draft.status !== 'open') throw new ConflictError('no open draft');
    const reqs = (await this.d.store.list<EditRequest>(`requests:${siteId}`)).filter((r) => r.draftId === draftId && ['previewing', 'preview_ready'].includes(r.status));
    if (reqs.length === 0 || reqs.some((r) => r.status !== 'preview_ready')) throw new ConflictError('preview is not ready yet');
    const sha = await this.d.repo.mergePr(site.id, draft.prNumber!);
    const revs = await this.d.store.list<Revision>(`revisions:${siteId}`);
    const rev: Revision = { id: this.newId(), siteId, number: revs.length + 1, commitSha: sha, summary: reqs.map((r) => r.summary).join('; '), createdAt: this.now().toISOString() };
    await this.d.store.put(`revisions:${siteId}`, rev.id, rev);
    draft.status = 'published';
    await this.d.store.put(`drafts:${siteId}`, draft.id, draft);
    for (const r of reqs) await this.setStatus(s, r, 'published', {});
    await this.audit(s, siteId, 'draft.published', draftId);
    return rev;
  }

  async discard(s: Session, siteId: string, draftId: string): Promise<void> {
    const site = await this.siteFor(s, siteId);
    const draft = await this.d.store.get<Draft>(`drafts:${siteId}`, draftId);
    if (!draft || draft.status !== 'open') throw new ConflictError('no open draft');
    if (draft.prNumber !== null) await this.d.repo.closePr(site.id, draft.prNumber);
    draft.status = 'discarded';
    await this.d.store.put(`drafts:${siteId}`, draft.id, draft);
    for (const r of (await this.d.store.list<EditRequest>(`requests:${siteId}`)).filter((r) => r.draftId === draftId && r.status !== 'published')) await this.setStatus(s, r, 'discarded', {});
    await this.audit(s, siteId, 'draft.discarded', draftId);
  }

  /** Reverts the latest revision. Undoing an undo re-applies the change. */
  async undo(s: Session, siteId: string): Promise<Revision> {
    const site = await this.siteFor(s, siteId);
    const revs = (await this.d.store.list<Revision>(`revisions:${siteId}`)).sort((a, b) => b.number - a.number);
    const last = revs[0];
    if (!last) throw new ConflictError('nothing to undo');
    if (await this.openDraft(siteId)) throw new ConflictError('finish or discard the open draft first');
    const sha = await this.d.repo.revertCommit(site.id, last.commitSha, `Undo: ${last.summary}`);
    const rev: Revision = { id: this.newId(), siteId, number: last.number + 1, commitSha: sha, summary: `Undid: ${last.summary}`, restoredFrom: last.id, createdAt: this.now().toISOString() };
    await this.d.store.put(`revisions:${siteId}`, rev.id, rev);
    await this.audit(s, siteId, 'revision.undone', last.id);
    return rev;
  }

  async history(s: Session, siteId: string): Promise<Revision[]> {
    await this.siteFor(s, siteId);
    return (await this.d.store.list<Revision>(`revisions:${siteId}`)).sort((a, b) => b.number - a.number);
  }

  private async setStatus(s: Session, req: EditRequest, status: Status, patch: Partial<EditRequest>) {
    Object.assign(req, patch, { status });
    await this.d.store.put(`requests:${req.siteId}`, req.id, req);
    await this.audit(s, req.siteId, `edit_request.${status}`, req.id);
    return req;
  }
  private async logUsage(siteId: string, requestId: string, u: RunUsage) {
    await this.d.store.put('airuns', this.newId(), { siteId, requestId, model: u.model, usage: u.usage, costUsd: costUsd(u.model, u.usage), at: this.now().toISOString() });
  }
}
