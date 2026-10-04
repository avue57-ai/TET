import { describe, expect, it } from 'vitest';
import { applyOps, PatchError } from '../src/ops';
import { applyChanges, CAPS } from '../src/validator';
import { sampleSite, sampleAssets } from '../../schemas/src/sample';

describe('applyOps', () => {
  it('replaces, adds, removes and inserts into arrays without mutating the input', () => {
    const doc = { a: { b: [1, 2, 3] }, c: 'x' };
    const out = applyOps(doc, [{ op: 'replace', path: '/c', value: 'y' }, { op: 'add', path: '/a/b/1', value: 9 }, { op: 'add', path: '/a/b/-', value: 4 }, { op: 'remove', path: '/a/b/0' }]);
    expect(out).toEqual({ a: { b: [9, 2, 3, 4] }, c: 'y' });
    expect(doc).toEqual({ a: { b: [1, 2, 3] }, c: 'x' });
  });
  it('throws on missing paths, bad indexes, root patches and prototype keys', () => {
    expect(() => applyOps({}, [{ op: 'replace', path: '/nope', value: 1 }])).toThrow(PatchError);
    expect(() => applyOps({ a: [1] }, [{ op: 'replace', path: '/a/5', value: 1 }])).toThrow(PatchError);
    expect(() => applyOps({}, [{ op: 'add', path: '', value: 1 }])).toThrow(PatchError);
    expect(() => applyOps({}, [{ op: 'add', path: '/__proto__', value: 1 }])).toThrow(PatchError);
  });
});

describe('applyChanges', () => {
  const assets = sampleAssets();
  const hero = 'content/pages/home.json';
  it('applies a valid change and reports a field diff', () => {
    const r = applyChanges(sampleSite(), [{ path: hero, ops: [{ op: 'replace', path: '/sections/0/headline/0', value: 'Luxury Bridal,' }] }], assets);
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.changedPaths).toEqual([hero]);
      expect(r.diff[0]!.fields).toEqual([{ pointer: '/sections/0/headline/0', before: 'For the bride who', after: 'Luxury Bridal,' }]);
    }
  });
  it('refuses code files and traversal', () => {
    for (const path of ['src/pages/[...slug].astro', 'netlify.toml', 'content/../netlify.toml', 'content/pages/home.json/../../x.json'])
      expect(applyChanges(sampleSite(), [{ path, ops: [{ op: 'add', path: '/x', value: 1 }] }], assets).ok).toBe(false);
  });
  it('rejects results that fail the schema, with the offending path', () => {
    const r = applyChanges(sampleSite(), [{ path: hero, ops: [{ op: 'replace', path: '/sections/1/headline', value: '<script>x</script>' }] }], assets);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.errors.join()).toMatch(/home\.json#\/sections\/1\/headline/);
  });
  it('rejects an unknown asset', () => {
    const r = applyChanges(sampleSite(), [{ path: hero, ops: [{ op: 'replace', path: '/sections/0/image/asset', value: 'ast_other_tenant' }] }], assets);
    expect(r.ok).toBe(false);
  });
  it('enforces file and operation caps', () => {
    const many = Array.from({ length: CAPS.maxFiles + 1 }, () => ({ path: hero, ops: [{ op: 'replace' as const, path: '/title', value: 'x' }] }));
    expect(applyChanges(sampleSite(), many, assets).ok).toBe(false);
    const ops = Array.from({ length: CAPS.maxOps + 1 }, () => ({ op: 'replace' as const, path: '/title', value: 'x' }));
    expect(applyChanges(sampleSite(), [{ path: hero, ops }], assets).ok).toBe(false);
  });
  it('creates a page, and protects home and non-page files from delete/create', () => {
    const page = { title: 'Visit', path: '/visit/', meta: { title: 'Visit us', description: 'Find the boutique.' }, header: 'solid', sections: [{ id: 'hours', type: 'hours-location' }] };
    expect(applyChanges(sampleSite(), [{ path: 'content/pages/visit.json', create: page }], assets).ok).toBe(true);
    expect(applyChanges(sampleSite(), [{ path: 'content/pages/home.json', delete: true }], assets).ok).toBe(false);
    expect(applyChanges(sampleSite(), [{ path: 'content/settings/theme.json', delete: true }], assets).ok).toBe(false);
    expect(applyChanges(sampleSite(), [{ path: 'content/settings/other.json', create: {} }], assets).ok).toBe(false);
  });
  it('rejects deleting a page that navigation still links to', () => {
    expect(applyChanges(sampleSite(), [{ path: 'content/pages/about.json', delete: true }], assets).ok).toBe(false);
  });
  it('requires exactly one of ops, create, delete', () => {
    expect(applyChanges(sampleSite(), [{ path: hero }], assets).ok).toBe(false);
  });
});
