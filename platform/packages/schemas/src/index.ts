import { z } from 'zod';

// ---------- primitives ----------
const plainText = (max: number) =>
  z.string().max(max).refine((s) => !/[<>]/.test(s), 'HTML is not allowed; use plain text or the limited markdown (*italic*, **bold**)');

export const Href = z
  .string()
  .regex(/^(\/[A-Za-z0-9\-._~\/?#=&%]*|https:\/\/[^\s]+|tel:\+?[0-9]+|mailto:[^\s@]+@[^\s@]+)$/, 'must be a site path, https URL, tel: or mailto: link');
export const Link = z.object({ label: plainText(60), href: Href }).strict();
export const ImageRef = z
  .object({
    asset: z.string().regex(/^ast_[A-Za-z0-9]+$/, 'must be an asset id like ast_123'),
    alt: plainText(200),
    w: z.number().int().positive(),
    h: z.number().int().positive(),
    focal: z.object({ x: z.number().min(0).max(1), y: z.number().min(0).max(1) }).strict().optional(),
  })
  .strict();
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

export const Section = z.discriminatedUnion('type', [
  HeroSection, StatementSection, FeatureGridSection, RichTextSection, CtaBannerSection,
  TestimonialsSection, GallerySection, PricingTableSection, FaqSection, HoursLocationSection,
]);
export type Section = z.infer<typeof Section>;

// ---------- pages and settings ----------
export const Page = z
  .object({
    title: plainText(120),
    path: z.string().regex(/^\/([a-z0-9\-]+\/)*$/, 'page path must look like / or /about/'),
    meta: z.object({ title: plainText(70), description: plainText(165), ogAsset: z.string().regex(/^ast_[A-Za-z0-9]+$/).optional() }).strict(),
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

export const Business = z
  .object({
    name: plainText(80),
    phone: plainText(30),
    phoneHref: z.string().regex(/^tel:\+?[0-9]+$/),
    email: z.string().email(),
    address: z.object({ street: plainText(80), suite: plainText(40).optional(), city: plainText(60), region: z.string().length(2), zip: z.string().regex(/^[0-9]{5}(-[0-9]{4})?$/) }).strict(),
    hours: z
      .array(z.object({ label: plainText(60), time: plainText(60), closed: z.boolean().optional() }).strict())
      .max(10),
    priceRange: plainText(60).optional(),
    social: z.record(z.string(), z.string().url()).default({}),
  })
  .strict();

export const Navigation = z
  .object({
    header: z.object({ left: z.array(Link).max(6), right: z.array(Link).max(6), cta: Link.optional() }).strict(),
    footer: z
      .object({
        tagline: plainText(200),
        columns: z.array(z.object({ title: plainText(40), links: z.array(Link).max(10) }).strict()).max(4),
        legal: z.array(Link).max(5),
        bottomLine: plainText(160),
      })
      .strict(),
  })
  .strict();

const Hex = z.string().regex(/^#[0-9a-fA-F]{6}$/, 'must be a 6-digit hex colour like #f7f3ec');
export const Theme = z
  .object({
    colors: z.object({ background: Hex, text: Hex, muted: Hex, accent: Hex, accentText: Hex }).strict(),
    fonts: z.object({ serif: plainText(60), sans: plainText(60) }).strict(),
    headerStyle: z.enum(['transparent', 'solid']),
  })
  .strict();

function luminance(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0]! + 0.7152 * c[1]! + 0.0722 * c[2]!;
}
export function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

// ---------- site tree validation ----------
export type SiteFiles = Record<string, unknown>;
export type Issue = { path: string; message: string };

export function schemaForPath(path: string): z.ZodType | null {
  if (path === 'content/settings/business.json') return Business;
  if (path === 'content/settings/navigation.json') return Navigation;
  if (path === 'content/settings/theme.json') return Theme;
  if (/^content\/pages\/[a-z0-9\-]+\.json$/.test(path)) return Page;
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
    const { background, text, muted, accent, accentText } = theme.data.colors;
    for (const [name, a, b, min] of [['text on background', text, background, 4.5], ['muted text on background', muted, background, 4.5], ['accent text on accent', accentText, accent, 4.5]] as const)
      if (contrastRatio(a, b) < min) issues.push({ path: 'content/settings/theme.json#/colors', message: `${name} fails WCAG AA contrast (${contrastRatio(a, b).toFixed(2)} < ${min})` });
  }
  const nav = Navigation.safeParse(files['content/settings/navigation.json']);
  if (nav.success) {
    const links = [...nav.data.header.left, ...nav.data.header.right, ...(nav.data.header.cta ? [nav.data.header.cta] : []), ...nav.data.footer.columns.flatMap((c) => c.links), ...nav.data.footer.legal];
    for (const l of links) if (l.href.startsWith('/') && !paths.has(l.href.split(/[?#]/)[0]!)) issues.push({ path: 'content/settings/navigation.json', message: `link "${l.label}" points to ${l.href}, which is not a page` });
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
  return {
    standard: '1.0',
    files: Object.keys(files).filter((p) => p.startsWith(EDITABLE_PREFIX)).sort(),
    pages,
    schemas: { section: z.toJSONSchema(Section), page: z.toJSONSchema(Page), business: z.toJSONSchema(Business), navigation: z.toJSONSchema(Navigation), theme: z.toJSONSchema(Theme) },
  };
}
