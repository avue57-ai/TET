import { describe, expect, it } from 'vitest';
import { validateSite, exportManifest, contrastRatio, Page } from '../src/index';
import { sampleSite, sampleAssets } from '../src/sample';

describe('validateSite', () => {
  it('accepts the sample site', () => {
    const r = validateSite(sampleSite(), { knownAssets: sampleAssets() });
    expect(r.issues).toEqual([]);
    expect(r.ok).toBe(true);
  });
  it('rejects HTML in text fields', () => {
    const s = sampleSite() as any;
    s['content/pages/home.json'].sections[1].headline = '<b>Hi</b>';
    expect(validateSite(s).ok).toBe(false);
  });
  it('rejects unknown section types and extra keys', () => {
    const s = sampleSite() as any;
    s['content/pages/home.json'].sections.push({ id: 'x', type: 'carousel' });
    expect(validateSite(s).ok).toBe(false);
    const t = sampleSite() as any;
    t['content/pages/home.json'].sections[1].script = 'alert(1)';
    expect(validateSite(t).ok).toBe(false);
  });
  it('rejects duplicate section ids and duplicate page paths', () => {
    const s = sampleSite() as any;
    s['content/pages/home.json'].sections[1].id = 'hero';
    expect(validateSite(s).issues.some((i) => /duplicate section id/.test(i.message))).toBe(true);
    const t = sampleSite() as any;
    t['content/pages/about.json'].path = '/';
    expect(validateSite(t).issues.some((i) => /duplicate page path/.test(i.message))).toBe(true);
  });
  it('flags nav links to missing pages', () => {
    const s = sampleSite() as any;
    s['content/settings/navigation.json'].header.left[0].href = '/nope/';
    expect(validateSite(s).issues.some((i) => /not a page/.test(i.message))).toBe(true);
  });
  it('flags missing assets', () => {
    const s = sampleSite() as any;
    s['content/pages/home.json'].sections[0].image.asset = 'ast_gone';
    expect(validateSite(s, { knownAssets: sampleAssets() }).issues.some((i) => /does not exist/.test(i.message))).toBe(true);
  });
  it('enforces WCAG AA contrast in the theme', () => {
    const s = sampleSite() as any;
    s['content/settings/theme.json'].colors.text = '#e8e0d0';
    expect(validateSite(s).issues.some((i) => /contrast/.test(i.message))).toBe(true);
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 0);
  });
  it('rejects unsafe links', () => {
    const s = sampleSite() as any;
    s['content/pages/home.json'].sections[0].primaryCta.href = 'javascript:alert(1)';
    expect(validateSite(s).ok).toBe(false);
  });
});

describe('exportManifest', () => {
  it('outlines pages and sections and carries JSON schemas', () => {
    const m = exportManifest(sampleSite());
    expect(m.pages.map((p) => p.path).sort()).toEqual(['/', '/about/', '/book/', '/collection/']);
    const home = m.pages.find((p) => p.path === '/')!;
    expect(home.sections.map((s) => s.type)).toEqual(['hero', 'statement', 'testimonials']);
    expect(m.schemas.section).toBeTruthy();
    expect(m.files).not.toContain('src/pages/[...slug].astro');
  });
  it('page schema parses a valid page', () => {
    expect(Page.safeParse((sampleSite() as any)['content/pages/about.json']).success).toBe(true);
  });
});
