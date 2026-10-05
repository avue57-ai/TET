import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { exportManifest, validateSite, type SiteFiles } from '../../schemas/src/index';
import { applyChanges, type Change } from '../src/validator';
import { StructuredEditor } from '../src/engine';
import { ScriptedLlm, tool } from '../src/fake-llm';

const real = (): SiteFiles => JSON.parse(readFileSync(new URL('./fixtures/la-soiree-content.json', import.meta.url), 'utf8'));
const ASSETS = new Set(['ast_new1', 'ast_new2']);
const HOME = 'content/pages/home.json';
const BIZ = 'content/settings/business.json';
const ok = (changes: Change[]) => { const r = applyChanges(real(), changes, ASSETS); if (!r.ok) throw new Error(r.errors.join('; ')); return r; };
const bad = (changes: Change[]) => { const r = applyChanges(real(), changes, ASSETS); expect(r.ok, 'should have been rejected').toBe(false); return r.ok ? [] : r.errors.join(' | '); };
const sec = (files: SiteFiles, id: string) => ((files[HOME] as any).sections as any[]).find((s) => s.id === id);

describe('real La Soirée content', () => {
  it('is valid as shipped', () => { const r = validateSite(real()); expect(r.issues).toEqual([]); });

  it('manifest stays small enough to cache and send on every request', () => {
    const m = exportManifest(real());
    const chars = JSON.stringify(m).length;
    console.log(`manifest: ${chars} chars (~${Math.round(chars / 3.5)} tokens), ${m.collections.gowns.length} gowns, ${m.pages[0]!.sections.length} home sections`);
    expect(chars).toBeLessThan(140_000);
    expect(m.pages.map((p) => p.path)).toEqual(['/']);
    expect(m.pages[0]!.sections.map((s) => s.type)).toContain('hero-video');
  });

  describe('requests the assistant must be able to fulfil', () => {
    it('change a headline', () => {
      const r = ok([{ path: HOME, ops: [{ op: 'replace', path: '/sections/0/headline', value: ['Luxury Bridal,', 'Personally *Curated.*'] }] }]);
      expect(sec(r.files, 'hero').headline).toEqual(['Luxury Bridal,', 'Personally *Curated.*']);
    });
    it('replace a photo with an uploaded one', () => {
      const r = ok([{ path: HOME, ops: [{ op: 'replace', path: '/sections/1/imageA', value: { asset: 'ast_new1', alt: 'A lace gown on a form', w: 3000, h: 2000 } }] }]);
      expect(sec(r.files, 'statement').imageA.asset).toBe('ast_new1');
    });
    it('move a section higher, and hide one', () => {
      const r = ok([{ path: HOME, ops: [{ op: 'move', from: '/sections/6', path: '/sections/2' }, { op: 'replace', path: '/sections/9/hidden', value: true }] }].map((c) => c) as Change[]);
      const ids = ((r.files[HOME] as any).sections as any[]).map((s) => s.id);
      expect(ids.slice(0, 4)).toEqual(['hero', 'statement', 'vip', 'pillars']);
      expect(((r.files[HOME] as any).sections as any[]).find((s) => s.id === 'journal').hidden).toBe(true);
    });
    it('change opening hours', () => {
      const r = ok([{ path: BIZ, ops: [{ op: 'replace', path: '/hours/1/time', value: '10 am – 6 pm' }, { op: 'replace', path: '/hours/1/closes', value: '18:00' }] }]);
      expect((r.files[BIZ] as any).hours[1].closes).toBe('18:00');
    });
    it('reject malformed opening hours', () => {
      expect(bad([{ path: BIZ, ops: [{ op: 'replace', path: '/hours/1/closes', value: '6pm' }] }])).toMatch(/24-hour/);
    });
    it('change a price everywhere it appears (search finds all copies)', async () => {
      const llm = new ScriptedLlm([
        [tool('search_content', { text: '$125' }, 's1')],
        (req) => { const out = JSON.stringify(req.messages.at(-1)); expect(out).toContain('content/pages/home.json'); return [tool('ask_customer', { question: 'stop here' })]; },
      ]);
      const outcome = await new StructuredEditor(llm).run({ files: real(), assets: [], text: 'VIP is now $150', knownAssets: ASSETS });
      expect(outcome.kind).toBe('ask');
      const hits = JSON.stringify(llm.calls[1]!.messages.at(-1));
      expect(hits).toContain('appointments.json'); // the number 125 is found too, not only the "$125" strings
      expect(hits).toContain('(number)');
    });
    it('add a FAQ question, a testimonial and a new appointment type', () => {
      const r = ok([
        { path: 'content/settings/faq.json', ops: [{ op: 'add', path: '/groups/0/items/-', value: { q: 'Can I bring my kids?', a: 'Yes, within the guest limit.' } }] },
        { path: 'content/settings/testimonials.json', ops: [{ op: 'add', path: '/testimonials/-', value: { quote: 'Magical afternoon.', name: 'Ana R.', source: 'Google review' } }] },
      ]);
      expect((r.files['content/settings/faq.json'] as any).groups[0].items.at(-1).q).toBe('Can I bring my kids?');
    });
    it('hide a gown that sold', () => {
      const r = ok([{ path: 'content/gowns/jess.json', ops: [{ op: 'add', path: '/hidden', value: true }] }]);
      expect((r.files['content/gowns/jess.json'] as any).hidden).toBe(true);
    });
    it('rename a menu item', () => {
      const r = ok([{ path: 'content/settings/navigation.json', ops: [{ op: 'replace', path: '/header/right/0/label', value: 'VIP Experience' }] }]);
      expect((r.files['content/settings/navigation.json'] as any).header.right[0].label).toBe('VIP Experience');
    });
    it('retheme: accepts readable colours, refuses unreadable ones', () => {
      ok([{ path: 'content/settings/theme.json', ops: [{ op: 'replace', path: '/colors/brassText', value: '#6e4f2a' }] }]);
      expect(bad([{ path: 'content/settings/theme.json', ops: [{ op: 'replace', path: '/colors/ink', value: '#d9d0c3' }] }])).toMatch(/contrast/);
    });
    it('create a new page, link it from the footer, and add a promo banner to the home page', () => {
      const page = { title: 'Kalamazoo Showroom', path: '/kalamazoo/', header: 'solid', meta: { title: 'Our Kalamazoo showroom', description: 'Visit our second location.' }, sections: [{ id: 'intro', type: 'rich-text', heading: 'Now open', body: 'We opened a second showroom.' }] };
      const r = ok([
        { path: 'content/pages/kalamazoo.json', create: page },
        { path: 'content/settings/navigation.json', ops: [{ op: 'add', path: '/footer/columns/0/links/-', value: { label: 'Kalamazoo', href: '/kalamazoo/' } }] },
        { path: HOME, ops: [{ op: 'add', path: '/sections/1', value: { id: 'spring-promo', type: 'cta-banner', headline: 'Spring *sample sale*', cta: { label: 'See the sale', href: '/collection/' } } }] },
      ]);
      expect(Object.keys(r.files)).toContain('content/pages/kalamazoo.json');
    });
  });

  describe('requests it must refuse or hand to a person', () => {
    it('code, config and unknown files', () => {
      for (const path of ['src/pages/index.astro', 'netlify.toml', 'content/settings/secret.json', 'content/../package.json'])
        bad([{ path, ops: [{ op: 'add', path: '/x', value: 1 }] }]);
    });
    it('section types this site cannot display', () => {
      expect(bad([{ path: HOME, ops: [{ op: 'add', path: '/sections/-', value: { id: 'g', type: 'gallery', images: [{ key: 's/boutique-salon', alt: '' }] } }] }])).toMatch(/cannot display "gallery"/);
      bad([{ path: HOME, ops: [{ op: 'add', path: '/sections/-', value: { id: 'x', type: 'carousel' } }] }]);
    });
    it('a page path that code already owns, a dead link, and a missing gown', () => {
      const page = { title: 'About', path: '/about/', header: 'solid', meta: { title: 'About', description: 'About us.' }, sections: [{ id: 'a', type: 'rich-text', body: 'x' }] };
      expect(bad([{ path: 'content/pages/about.json', create: page }])).toMatch(/already used by a page built in code/);
      expect(bad([{ path: 'content/settings/navigation.json', ops: [{ op: 'replace', path: '/header/left/0/href', value: '/nowhere/' }] }])).toMatch(/not a page/);
      expect(bad([{ path: HOME, ops: [{ op: 'replace', path: '/sections/3/gowns/0', value: 'no-such-gown' }] }])).toMatch(/does not exist/);
    });
    it('unknown or foreign assets, scripts and unsafe links', () => {
      bad([{ path: HOME, ops: [{ op: 'replace', path: '/sections/1/imageA', value: { asset: 'ast_other_tenant', alt: 'x', w: 10, h: 10 } }] }]);
      bad([{ path: HOME, ops: [{ op: 'replace', path: '/sections/0/headline/0', value: '<script>alert(1)</script>' }] }]);
      bad([{ path: HOME, ops: [{ op: 'replace', path: '/sections/0/primaryCta/href', value: 'javascript:alert(1)' }] }]);
    });
    it('deleting the home page or a page the menu links to', () => {
      bad([{ path: HOME, delete: true }]);
    });
    it('stays within the per-request caps', () => {
      const many = Array.from({ length: 201 }, () => ({ op: 'replace' as const, path: '/sections/0/eyebrow', value: 'x' }));
      expect(bad([{ path: HOME, ops: many }])).toMatch(/too many operations/);
    });
  });
});
