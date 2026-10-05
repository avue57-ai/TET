export type Op = { op: 'add' | 'replace' | 'remove' | 'move'; path: string; value?: unknown; from?: string };
export class PatchError extends Error {}

function parse(pointer: string): string[] {
  if (pointer === '') return [];
  if (!pointer.startsWith('/')) throw new PatchError(`bad pointer "${pointer}"`);
  return pointer.slice(1).split('/').map((s) => s.replace(/~1/g, '/').replace(/~0/g, '~'));
}

/** Applies a JSON-patch subset (add, replace, remove) to a deep copy of doc. */
export function applyOps<T>(doc: T, ops: Op[]): T {
  let root: any = structuredClone(doc);
  for (const op0 of ops) {
    let op = op0;
    if (op0.op === 'move') {
      if (typeof op0.from !== 'string') throw new PatchError('move needs a from path');
      let cur: any = root;
      for (const k of parse(op0.from)) { if (cur == null || typeof cur !== 'object' || !(k in cur)) throw new PatchError(`path not found: ${op0.from}`); cur = cur[k]; }
      root = applyOps(root, [{ op: 'remove', path: op0.from }]);
      op = { op: 'add', path: op0.path, value: structuredClone(cur) };
    }
    const keys = parse(op.path);
    if (keys.length === 0) throw new PatchError('cannot patch the document root; use create');
    let parent = root;
    for (const k of keys.slice(0, -1)) {
      if (parent == null || typeof parent !== 'object' || !(k in parent)) throw new PatchError(`path not found: ${op.path}`);
      parent = parent[k];
    }
    const last = keys[keys.length - 1]!;
    if (Array.isArray(parent)) {
      const isEnd = last === '-';
      const idx = isEnd ? parent.length : Number(last);
      if (!Number.isInteger(idx) || idx < 0 || idx > parent.length || (op.op !== 'add' && idx >= parent.length)) throw new PatchError(`bad array index in ${op.path}`);
      if (op.op === 'add') parent.splice(idx, 0, op.value);
      else if (op.op === 'replace') parent[idx] = op.value;
      else parent.splice(idx, 1);
    } else if (parent && typeof parent === 'object') {
      if (last === '__proto__' || last === 'constructor' || last === 'prototype') throw new PatchError(`forbidden key in ${op.path}`);
      if (op.op === 'add') parent[last] = op.value;
      else if (op.op === 'replace') parent[last] = op.value; // replacing a key that is not there yet simply sets it
      else if (!(last in parent)) throw new PatchError(`path not found: ${op.path}`);
      else delete parent[last];
    } else throw new PatchError(`path not found: ${op.path}`);
  }
  return root;
}

export type FieldDiff = { pointer: string; before: unknown; after: unknown };
function flatten(v: unknown, p = '', out: Record<string, unknown> = {}) {
  if (Array.isArray(v)) v.forEach((x, i) => flatten(x, `${p}/${i}`, out));
  else if (v && typeof v === 'object') Object.entries(v).forEach(([k, x]) => flatten(x, `${p}/${k}`, out));
  else out[p] = v;
  return out;
}
export function diffDocs(before: unknown, after: unknown, limit = 60): FieldDiff[] {
  const a = before === undefined ? {} : flatten(before), b = after === undefined ? {} : flatten(after);
  const out: FieldDiff[] = [];
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) if (JSON.stringify(a[k]) !== JSON.stringify(b[k])) out.push({ pointer: k, before: a[k], after: b[k] });
  return out.slice(0, limit);
}
