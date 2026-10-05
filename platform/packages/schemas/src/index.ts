import { z } from 'zod';

// ---------- primitives ----------
const plainText = (max: number) =>
  z.string().max(max).refine((s) => !/[<>]/.test(s), 'HTML is not allowed; use plain text or the limited markdown (*italic*, **bold**)');

export const Href = z
  .string()
  .regex(/^(\/[A-Za-z0-9\-._~\/?#=&%]*|https:\/\/[^\s]+|tel:\+?[0-9]+|mailto:[^\s@]+@[^\s@]+)$/, 'must be a site path, https URL, tel: or mailto: link');
export const Link = z.object({ label: plainText(60), href: Href }).strict();
export const AssetImage = z
  .object({
    asset: z.string().regex(/^ast_[A-Za-z0-9]+$/, 'must be an asset id like ast_123'),
    alt: plainText(200),
    w: z.number().int().positive(),
    h: z.number().int().positive(),
    focal: z.object({ x: z.number().min(0).max(1), y: z.number().min(0).max(1) }).strict().optional(),
    pos: z.string().regex(/^\d{1,3}% \d{1,3}%$/).optional(),
  })
  .strict();
/** An image already in the site's build pipeline (media-source key such as s/boutique-salon). Alt may be empty for decorative images. */
export const LocalImage = z
  .object({ key: z.string().regex(/^(s|g|blog|u)\/[A-Za-z0-9._-]+$/, 'must be an image key like s/boutique-salon'), alt: plainText(200), pos: z.string().regex(/^\d{1,3}% \d{1,3}%$/).optional() })
  .strict();
export const ImageRef = z.union([AssetImage, LocalImage]);
export const Cta = z.object({ label: plainText(60), href: Href, track: z.string().regex(/^[a-z_]{1,40}$/).optional() }).strict();
/** Headline as lines; *word* renders as emphasis. */
const Lines = z.array(plainText(80)).min(1).max(4);
const Id = z.string().regex(/^[a-z0-9][a-z0-9-]{0,63}$/, 'section id must be lowercase letters, numbers, dashes');
const base = { id: Id, hidden: z.boolean().optional(), anchor: Id.optional() };

// ---------- sections (kit catalogue, subset for v1) ----------
export const HeroSection = z
  .object({
    ...base,
    type: z.literal('hero'),
    variant: z.enum(['image', 'video', 'split']).default('image'),
    eyebrow: plainText(80).optional(),
    headline: z.array(plainText(80)).min(1).max(4),
    sub: plainText(240).optional(),
    image: ImageRef.optional(),
    primaryCta: Link.optional(),
    secondaryCta: Link.optional(),
  })
  .strict();
export const StatementSection = z
  .object({ ...base, type: z.literal('statement'), eyebrow: plainText(80).optional(), headline: plainText(160), body: plainText(1200), image: ImageRef.optional() })
  .strict();
export const FeatureGridSection = z
  .object({
    ...base,
    type: z.literal('feature-grid'),
    heading: plainText(120).optional(),
    items: z.array(z.object({ title: plainText(80), body: plainText(400), image: ImageRef.optional() }).strict()).min(1).max(6),
  })
  .strict();
export const RichTextSection = z.object({ ...base, type: z.literal('rich-text'), heading: plainText(120).optional(), body: plainText(6000) }).strict();
export const CtaBannerSection = z
  .object({ ...base, type: z.literal('cta-banner'), headline: plainText(120), body: plainText(300).optional(), cta: Link, image: ImageRef.optional() })
  .strict();
export const TestimonialsSection = z
  .object({
    ...base,
    type: z.literal('testimonials'),
    heading: plainText(120).optional(),
    items: z.array(z.object({ quote: plainText(600), name: plainText(80), source: plainText(80).optional() }).strict()).min(1).max(12),
  })
  .strict();
export const GallerySection = z
  .object({ ...base, type: z.literal('gallery'), variant: z.enum(['mosaic', 'grid', 'masonry']).default('grid'), heading: plainText(120).optional(), images: z.array(ImageRef).min(1).max(40) })
  .strict();
export const PricingTableSection = z
  .object({
    ...base,
    type: z.literal('pricing-table'),
    heading: plainText(120).optional(),
    items: z
      .array(z.object({ name: plainText(80), price: plainText(40), details: z.array(plainText(160)).max(8).optional(), featured: z.boolean().optional() }).strict())
      .min(1)
      .max(12),
  })
  .strict();
export const FaqSection = z
  .object({ ...base, type: z.literal('faq'), heading: plainText(120).optional(), items: z.array(z.object({ q: plainText(200), a: plainText(1500) }).strict()).min(1).max(40) })
  .strict();
export const HoursLocationSection = z.object({ ...base, type: z.literal('hours-location'), heading: plainText(120).optional(), note: plainText(400).optional() }).strict();

// ---- sections extracted from the La Soirée homepage (Standard v1 reference site) ----
const eyebrow = plainText(80);
export const HeroVideoSection = z.object({ ...base, type: z.literal('hero-video'), eyebrow, headline: Lines, sub: plainText(240), primaryCta: Cta, secondaryCta: Cta }).strict();
export const StatementSplitSection = z.object({ ...base, type: z.literal('statement-split'), eyebrow, headline: Lines, body: z.array(plainText(800)).min(1).max(4), link: Cta, imageA: ImageRef, imageB: ImageRef }).strict();
export const PillarsSection = z
  .object({ ...base, type: z.literal('pillars'), eyebrow, headline: Lines, items: z.array(z.object({ numeral: plainText(6), title: plainText(40), body: plainText(240), image: ImageRef }).strict()).min(1).max(6) })
  .strict();
export const CollectionRailSection = z
  .object({ ...base, type: z.literal('collection-rail'), eyebrow, headline: Lines, body: plainText(400), railLabel: plainText(60), gowns: z.array(z.string().regex(/^[a-z0-9-]+$/)).min(1).max(24), cta: Cta })
  .strict();
export const DesignerIndexSection = z.object({ ...base, type: z.literal('designer-index'), eyebrow, headline: Lines, body: plainText(300) }).strict();
export const AppointmentFeatureSection = z
  .object({
    ...base, type: z.literal('appointment-feature'), eyebrow, headline: Lines, lede: plainText(400),
    facts: z.array(z.object({ big: plainText(8), unit: plainText(12).optional(), label: plainText(40) }).strict()).min(1).max(4),
    primaryCta: Cta, secondaryCta: Cta, image: ImageRef,
  })
  .strict();
export const VipTeaserSection = z
  .object({
    ...base, type: z.literal('vip-teaser'), eyebrow, headline: Lines, lede: plainText(400),
    facts: z.array(z.object({ term: plainText(40), detail: plainText(60) }).strict()).min(1).max(6),
    cta: Cta, backgroundImage: ImageRef, image: ImageRef,
  })
  .strict();
export const ProofSection = z.object({ ...base, type: z.literal('proof'), eyebrow, srHeading: plainText(120), image: ImageRef, instagramLabel: plainText(60) }).strict();
export const SalonMosaicSection = z
  .object({ ...base, type: z.literal('salon-mosaic'), eyebrow, headline: Lines, body: plainText(400), images: z.array(ImageRef).length(5), footNote: plainText(160), link: Cta })
  .strict();
export const JournalTeaserSection = z.object({ ...base, type: z.literal('journal-teaser'), eyebrow, headline: Lines, allLink: Cta, posts: z.array(z.string().regex(/^[a-z0-9-]+$/)).min(1).max(6) }).strict();
export const FinalCtaSection = z
  .object({ ...base, type: z.literal('final-cta'), eyebrow, headline: Lines, image: ImageRef, primaryCta: Cta, secondaryCta: Cta })
  .strict();

export const Section = z.discriminatedUnion('type', [
  HeroSection, StatementSection, FeatureGridSection, RichTextSection, CtaBannerSection,
  TestimonialsSection, GallerySection, PricingTableSection, FaqSection, HoursLocationSection,
  HeroVideoSection, StatementSplitSection, PillarsSection, CollectionRailSection, DesignerIndexSection,
  AppointmentFeatureSection, VipTeaserSection, ProofSection, SalonMosaicSection, JournalTeaserSection, FinalCtaSection,
]);
export type Section = z.infer<typeof Section>;

// ---------- pages and settings ----------
export const Page = z
  .object({
    title: plainText(120),
    path: z.string().regex(/^\/([a-z0-9\-]+\/)*$/, 'page path must look like / or /about/'),
    meta: z.object({ title: plainText(70), description: plainText(165), ogAsset: z.string().regex(/^ast_[A-Za-z0-9]+$/).optional(), ogKey: z.string().regex(/^(s|g|blog|u)\/[A-Za-z0-9._-]+$/).optional() }).strict(),
    header: z.enum(['transparent', 'solid']).default('solid'),
    sections: z.array(Section).max(40),
  })
  .strict()
  .superRefine((p, ctx) => {
    const seen = new Set<string>();
    p.sections.forEach((s, i) => {
      if (seen.has(s.id)) ctx.addIssue({ code: 'custom', path: ['sections', i, 'id'], message: `duplicate section id "${s.id}"` });
      seen.add(s.id);
    });
  });
export type Page = z.infer<typeof Page>;

const Day = z.enum(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']);
const Clock = z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$/, 'use 24-hour time like 10:00');
const Url = z.string().url().max(400);
export const Business = z
  .object({
    name: plainText(80), legalName: plainText(80).optional(), url: Url.optional(),
    phone: plainText(30), phoneHref: z.string().regex(/^tel:\+?[0-9]+$/), email: z.string().email(),
    address: z.object({ street: plainText(80), suite: plainText(40).optional(), city: plainText(60), region: z.string().length(2), regionLong: plainText(40).optional(), zip: z.string().regex(/^[0-9]{5}(-[0-9]{4})?$/), country: z.string().length(2).optional() }).strict(),
    geo: z.object({ lat: z.number().min(-90).max(90), lng: z.number().min(-180).max(180) }).strict().optional(),
    mapsUrl: Url.optional(), opened: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
    hours: z.array(z.object({ label: plainText(60), time: plainText(60), days: z.array(Day).optional(), opens: Clock.optional(), closes: Clock.optional(), closed: z.boolean().optional() }).strict()).max(10),
    priceRange: plainText(60).optional(),
    booking: z.object({ page: Href, squareServices: Url, squareWidget: Url, manage: Url }).strict().optional(),
    shop: z.record(z.string(), Url).optional(),
    social: z.record(z.string(), z.string().max(200)).default({}).superRefine((o, ctx) => {
      for (const [k, v] of Object.entries(o)) if (!k.endsWith('Handle') && !/^https:\/\//.test(v)) ctx.addIssue({ code: 'custom', path: [k], message: 'social links must be https URLs' });
    }),
    ga4: z.string().regex(/^G-[A-Z0-9]+$/).optional(),
  })
  .strict();

export const Navigation = z
  .object({
    header: z.object({ left: z.array(Link).max(6), right: z.array(Link).max(6), cta: Link.optional(), menuCtaLabel: plainText(40).optional() }).strict(),
    menu: z.object({ before: z.array(Link).max(4), after: z.array(Link).max(4) }).strict().optional(),
    footer: z
      .object({
        tagline: plainText(200),
        columns: z.array(z.object({ title: plainText(40), links: z.array(Link).max(10), withContact: z.boolean().optional() }).strict()).max(4),
        legal: z.array(Link).max(5),
        bottomLine: plainText(160),
      })
      .strict(),
  })
  .strict();

const Hex = z.string().regex(/^#[0-9a-fA-F]{6}$/, 'must be a 6-digit hex colour like #f7f3ec');
/** Colour tokens of the La Soirée design system (global.css :root). Fonts and type scale stay in code. */
export const Theme = z
  .object({ colors: z.object({ ivory: Hex, cream: Hex, sand: Hex, champagne: Hex, stone: Hex, muted: Hex, ink: Hex, charcoal: Hex, brass: Hex, brassText: Hex }).strict() })
  .strict();

function luminance(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0]! + 0.7152 * c[1]! + 0.0722 * c[2]!;
}
export function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

// ---------- collections (content/gowns, designers and settings files) ----------
const Slug = z.string().regex(/^[a-z0-9-]+$/);
const MediaPath = z.string().regex(/^\/media-source\/(gowns|site|blog|uploads)\/[A-Za-z0-9._-]+$/, 'must be a /media-source/... photo path');
export const Gown = z
  .object({
    name: plainText(60), designer: Slug,
    silhouette: z.enum(['Ball Gown', 'A-Line', 'Mermaid', 'Sheath']),
    neckline: z.enum(['', 'Sweetheart', 'V-Neck', 'Scoop', 'Square', 'Illusion', 'Straight']).optional(),
    images: z.array(MediaPath).min(1).max(12),
    silhouetteSource: z.enum(['site', 'visual']).optional(), legacyUrl: z.string().max(200).optional(),
    order: z.number().int().min(0).max(999).optional(), hidden: z.boolean().optional(),
  })
  .strict();
export const Designer = z
  .object({
    name: plainText(60), origin: plainText(80), founded: z.union([z.number().int().min(1800).max(2100), z.literal('')]).optional(),
    price: plainText(40), plus: z.boolean(), line: plainText(160), intro: plainText(800), note: plainText(300).optional(),
    hero: MediaPath, feature: z.array(MediaPath).min(1).max(3), order: z.number().int().min(0).max(99),
  })
  .strict();
export const Appointments = z
  .object({
    appointments: z.array(z.object({
      id: Slug, name: plainText(60), square: plainText(100), price: z.number().int().min(0).max(5000), minutes: z.number().int().min(5).max(480),
      guests: z.number().int().min(0).max(30).nullable(), lines: z.array(plainText(120)).max(8), featured: z.boolean().optional(),
    }).strict()).max(12),
  })
  .strict();
export const Faq = z.object({ groups: z.array(z.object({ group: plainText(80), id: Slug, items: z.array(z.object({ q: plainText(200), a: plainText(1500) }).strict()).min(1).max(40) }).strict()).max(12) }).strict();
export const Testimonials = z.object({ testimonials: z.array(z.object({ quote: plainText(600), name: plainText(60), source: plainText(60) }).strict()).max(30) }).strict();
export const SECTION_TYPES = ['hero','statement','feature-grid','rich-text','cta-banner','testimonials','gallery','pricing-table','faq','hours-location','hero-video','statement-split','pillars','collection-rail','designer-index','appointment-feature','vip-teaser','proof','salon-mosaic','journal-teaser','final-cta'] as const;
/** content/settings/site.json: what this site's code can render and where its uploaded images are served from. */
export const SiteConfig = z
  .object({
    standard: z.literal('1.0'),
    sections: z.array(z.enum(SECTION_TYPES)).min(1),
    assets: z.object({ siteId: z.string().regex(/^[a-z0-9-]+$/), base: z.union([z.literal(''), z.string().url()]) }).strict(),
  })
  .strict();
/** Paths of pages rendered by site code rather than by content/pages/*.json. Links to these are valid. */
export const Routes = z.object({ routes: z.array(z.string().regex(/^\/([a-z0-9\-]+\/)*$/)).max(200) }).strict();

// ---------- site tree validation ----------
export type SiteFiles = Record<string, unknown>;
export type Issue = { path: string; message: string };

export function schemaForPath(path: string): z.ZodType | null {
  if (path === 'content/settings/business.json') return Business;
  if (path === 'content/settings/navigation.json') return Navigation;
  if (path === 'content/settings/theme.json') return Theme;
  if (path === 'content/settings/appointments.json') return Appointments;
  if (path === 'content/settings/faq.json') return Faq;
  if (path === 'content/settings/testimonials.json') return Testimonials;
  if (path === 'content/settings/routes.json') return Routes;
  if (path === 'content/settings/site.json') return SiteConfig;
  if (/^content\/pages\/[a-z0-9\-]+\.json$/.test(path)) return Page;
  if (/^content\/gowns\/[a-z0-9\-]+\.json$/.test(path)) return Gown;
  if (/^content\/designers\/[a-z0-9\-]+\.json$/.test(path)) return Designer;
  return null;
}
export const EDITABLE_PREFIX = 'content/';

export function collectAssets(node: unknown, out: Set<string>) {
  if (Array.isArray(node)) node.forEach((n) => collectAssets(n, out));
  else if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) {
      if ((k === 'asset' || k === 'ogAsset') && typeof v === 'string') out.add(v);
      else collectAssets(v, out);
    }
  }
}

export function validateSite(files: SiteFiles, opts: { knownAssets?: Set<string> } = {}): { ok: boolean; issues: Issue[] } {
  const issues: Issue[] = [];
  const paths = new Set<string>();
  for (const [path, doc] of Object.entries(files)) {
    if (!path.startsWith(EDITABLE_PREFIX)) continue;
    const schema = schemaForPath(path);
    if (!schema) { issues.push({ path, message: 'unknown content file (no schema)' }); continue; }
    const r = schema.safeParse(doc);
    if (!r.success) for (const i of r.error.issues) issues.push({ path: `${path}#/${i.path.join('/')}`, message: i.message });
    if (r.success && path.startsWith('content/pages/')) {
      const page = r.data as Page;
      if (paths.has(page.path)) issues.push({ path, message: `duplicate page path ${page.path}` });
      paths.add(page.path);
    }
  }
  const theme = Theme.safeParse(files['content/settings/theme.json']);
  if (theme.success) {
    const c = theme.data.colors;
    for (const [name, a, b, min] of [['ink on ivory', c.ink, c.ivory, 4.5], ['muted text on ivory', c.muted, c.ivory, 4.5], ['accent text on ivory', c.brassText, c.ivory, 4.5], ['ivory on charcoal', c.ivory, c.charcoal, 4.5]] as const)
      if (contrastRatio(a, b) < min) issues.push({ path: 'content/settings/theme.json#/colors', message: `${name} fails WCAG AA contrast (${contrastRatio(a, b).toFixed(2)} < ${min})` });
  }
  const nav = Navigation.safeParse(files['content/settings/navigation.json']);
  if (nav.success) {
    const links = [...nav.data.header.left, ...nav.data.header.right, ...(nav.data.header.cta ? [nav.data.header.cta] : []), ...nav.data.footer.columns.flatMap((c) => c.links), ...nav.data.footer.legal];
    const routes = new Set<string>((Routes.safeParse(files['content/settings/routes.json']).data?.routes) ?? []);
    for (const l of links) { const p = l.href.split(/[?#]/)[0]!; if (l.href.startsWith('/') && !paths.has(p) && !routes.has(p)) issues.push({ path: 'content/settings/navigation.json', message: `link "${l.label}" points to ${l.href}, which is not a page` }); }
  }
  const cfg = SiteConfig.safeParse(files['content/settings/site.json']);
  const routeSet = new Set<string>((Routes.safeParse(files['content/settings/routes.json']).data?.routes) ?? []);
  for (const [p, doc] of Object.entries(files)) {
    if (!/^content\/pages\//.test(p) || !Page.safeParse(doc).success) continue;
    const page = doc as Page;
    if (page.path !== '/' && routeSet.has(page.path)) issues.push({ path: p, message: `page path ${page.path} is already used by a page built in code` });
    if (cfg.success) for (const sec of page.sections) if (!(cfg.data.sections as readonly string[]).includes(sec.type)) issues.push({ path: `${p}#/sections/${sec.id}`, message: `this site cannot display "${sec.type}" sections` });
  }
  // cross-references between collections
  const designers = new Set(Object.keys(files).map((p) => /^content\/designers\/([a-z0-9-]+)\.json$/.exec(p)?.[1]).filter(Boolean) as string[]);
  const gowns = new Set(Object.keys(files).map((p) => /^content\/gowns\/([a-z0-9-]+)\.json$/.exec(p)?.[1]).filter(Boolean) as string[]);
  for (const [p, doc] of Object.entries(files)) {
    if (/^content\/gowns\//.test(p) && Gown.safeParse(doc).success && !designers.has((doc as { designer: string }).designer)) issues.push({ path: p, message: `designer "${(doc as { designer: string }).designer}" does not exist` });
    if (/^content\/pages\//.test(p) && Page.safeParse(doc).success)
      for (const sec of (doc as Page).sections) if (sec.type === 'collection-rail') for (const g of sec.gowns) if (!gowns.has(g)) issues.push({ path: `${p}#/sections/${sec.id}/gowns`, message: `gown "${g}" does not exist` });
  }
  if (opts.knownAssets) {
    const used = new Set<string>();
    collectAssets(files, used);
    for (const a of used) if (!opts.knownAssets.has(a)) issues.push({ path: 'content/', message: `asset ${a} does not exist` });
  }
  return { ok: issues.length === 0, issues };
}

// ---------- manifest the AI reads ----------
const short = (v: unknown) => (typeof v === 'string' ? (v.length > 80 ? v.slice(0, 77) + '...' : v) : undefined);
export function exportManifest(files: SiteFiles) {
  const pages = Object.entries(files)
    .filter(([p]) => /^content\/pages\//.test(p))
    .map(([file, doc]) => {
      const p = doc as Page;
      return {
        file, path: p.path, title: p.title,
        sections: p.sections.map((s, index) => ({
          index, id: s.id, type: s.type, hidden: !!s.hidden,
          fields: Object.fromEntries(Object.entries(s).filter(([k]) => !['id', 'type', 'hidden'].includes(k)).map(([k, v]) => [k, short(v) ?? (Array.isArray(v) ? `[${v.length} items]` : typeof v === 'object' && v ? '{...}' : v)])),
        })),
      };
    });
  const idx = (re: RegExp, pick: (d: any) => unknown) => Object.entries(files).flatMap(([p, d]) => { const m = re.exec(p); return m ? [{ slug: m[1], file: p, ...(pick(d) as object) }] : []; });
  return {
    standard: '1.0',
    collections: {
      gowns: idx(/^content\/gowns\/([a-z0-9-]+)\.json$/, (d) => ({ name: d.name, designer: d.designer, silhouette: d.silhouette, hidden: !!d.hidden, photos: d.images?.length })),
      designers: idx(/^content\/designers\/([a-z0-9-]+)\.json$/, (d) => ({ name: d.name, price: d.price })),
    },
    files: Object.keys(files).filter((p) => p.startsWith(EDITABLE_PREFIX)).sort(),
    pages,
    schemas: { section: z.toJSONSchema(Section), page: z.toJSONSchema(Page), business: z.toJSONSchema(Business), navigation: z.toJSONSchema(Navigation), theme: z.toJSONSchema(Theme) },
  };
}
