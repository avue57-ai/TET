import { EDITABLE_PREFIX, schemaForPath, validateSite, type SiteFiles } from '../../schemas/src/index';
import { applyOps, diffDocs, PatchError, type FieldDiff, type Op } from './ops';

export type Change = { path: string; ops?: Op[]; create?: unknown; delete?: boolean };
export const CAPS = { maxFiles: 20, maxOps: 200 };

export type ApplyResult =
  | { ok: true; files: SiteFiles; changedPaths: string[]; diff: { path: string; fields: FieldDiff[] }[] }
  | { ok: false; errors: string[] };

const SAFE_PATH = /^content\/[A-Za-z0-9\-_\/]+\.json$/;

export function applyChanges(files: SiteFiles, changes: Change[], knownAssets?: Set<string>): ApplyResult {
  const errors: string[] = [];
  if (!Array.isArray(changes) || changes.length === 0) return { ok: false, errors: ['no changes proposed'] };
  if (changes.length > CAPS.maxFiles) return { ok: false, errors: [`too many files (${changes.length} > ${CAPS.maxFiles})`] };
  const opCount = changes.reduce((n, c) => n + (c.ops?.length ?? 1), 0);
  if (opCount > CAPS.maxOps) return { ok: false, errors: [`too many operations (${opCount} > ${CAPS.maxOps})`] };

  const next: SiteFiles = structuredClone(files);
  const seen = new Set<string>();
  for (const c of changes) {
    if (typeof c.path !== 'string' || !c.path.startsWith(EDITABLE_PREFIX) || c.path.includes('..') || !SAFE_PATH.test(c.path)) { errors.push(`path not editable: ${String(c.path)}`); continue; }
    if (!schemaForPath(c.path)) { errors.push(`no schema for ${c.path}`); continue; }
    if (seen.has(c.path)) { errors.push(`path listed twice: ${c.path}`); continue; }
    seen.add(c.path);
    const kinds = [c.ops !== undefined, c.create !== undefined, c.delete === true].filter(Boolean).length;
    if (kinds !== 1) { errors.push(`${c.path}: give exactly one of ops, create or delete`); continue; }
    try {
      if (c.delete) {
        if (!(c.path in next)) throw new PatchError('file does not exist');
        if (c.path === 'content/pages/home.json' || !c.path.startsWith('content/pages/')) throw new PatchError('only non-home pages can be deleted');
        delete next[c.path];
      } else if (c.create !== undefined) {
        if (c.path in next) throw new PatchError('file already exists; use ops');
        if (!c.path.startsWith('content/pages/')) throw new PatchError('only pages can be created');
        next[c.path] = c.create;
      } else {
        if (!(c.path in next)) throw new PatchError('file does not exist');
        next[c.path] = applyOps(next[c.path], c.ops!);
      }
    } catch (e) { errors.push(`${c.path}: ${(e as Error).message}`); }
  }
  if (errors.length) return { ok: false, errors };

  const v = validateSite(next, { knownAssets: knownAssets });
  if (!v.ok) return { ok: false, errors: v.issues.map((i) => `${i.path}: ${i.message}`).slice(0, 20) };
  const diff = [...seen].map((path) => ({ path, fields: diffDocs(files[path], next[path]) }));
  return { ok: true, files: next, changedPaths: [...seen], diff };
}
