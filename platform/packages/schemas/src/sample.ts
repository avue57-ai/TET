import type { SiteFiles } from './index';

/** Small La Soirée-shaped site used as the template demo and as the test fixture. */
export function sampleSite(): SiteFiles {
  return structuredClone({
    'content/settings/business.json': {
      name: 'La Soirée Bridal', phone: '269-220-0440', phoneHref: 'tel:+12692200440', email: 'hello@lasoireebridal.com',
      address: { street: '8185 Gull Rd', suite: 'Suite 4', city: 'Richland', region: 'MI', zip: '49083' },
      hours: [{ label: 'Tue to Sat', time: '10 am to 7 pm' }, { label: 'Sun and Mon', time: 'Closed', closed: true }],
      priceRange: '$1,500 to $6,000', social: { instagram: 'https://instagram.com/lasoireebridal' },
    },
    'content/settings/navigation.json': {
      header: { left: [{ label: 'Collection', href: '/collection/' }], right: [{ label: 'About', href: '/about/' }], cta: { label: 'Book', href: '/book/' } },
      footer: { tagline: 'A private salon for European gowns.', columns: [{ title: 'Explore', links: [{ label: 'Collection', href: '/collection/' }] }], legal: [{ label: 'Privacy', href: '/about/' }], bottomLine: '© La Soirée Bridal' },
    },
    'content/settings/theme.json': {
      colors: { background: '#f7f3ec', text: '#1d1916', muted: '#6b5f52', accent: '#7a5c2e', accentText: '#ffffff' },
      fonts: { serif: 'Bodoni Moda', sans: 'Jost' }, headerStyle: 'transparent',
    },
    'content/pages/home.json': {
      title: 'Home', path: '/', header: 'transparent',
      meta: { title: 'La Soirée Bridal | Kalamazoo, Michigan Bridal Boutique', description: 'Private bridal salon near Kalamazoo, Michigan. European wedding gowns, by appointment.' },
      sections: [
        { id: 'hero', type: 'hero', variant: 'image', eyebrow: 'Richland, MI · By appointment', headline: ['For the bride who', 'wants something *different.*'], sub: 'European bridal gowns. Private appointments.', image: { asset: 'ast_hero1', alt: 'Bride in a lace gown', w: 2400, h: 1600 }, primaryCta: { label: 'Book an appointment', href: '/book/' } },
        { id: 'statement', type: 'statement', headline: 'Private. Curated. Personal.', body: 'Every appointment is yours alone, with a stylist who knows the collection.' },
        { id: 'reviews', type: 'testimonials', heading: 'In their words', items: [{ quote: 'Best dress shopping experience.', name: 'Tabitha P.', source: 'Google review' }] },
      ],
    },
    'content/pages/about.json': {
      title: 'About', path: '/about/', header: 'solid',
      meta: { title: 'About La Soirée Bridal', description: 'Our story, our designers and our approach to bridal.' },
      sections: [{ id: 'story', type: 'rich-text', heading: 'Our story', body: 'La Soirée opened its doors on September 1, 2022.' }],
    },
    'content/pages/collection.json': {
      title: 'Collection', path: '/collection/', header: 'solid',
      meta: { title: 'The Collection', description: 'Browse European wedding gowns by Eva Lendel, Anna Sposa and more.' },
      sections: [{ id: 'intro', type: 'rich-text', body: 'Browse the gowns we carry.' }],
    },
    'content/pages/book.json': {
      title: 'Book', path: '/book/', header: 'solid',
      meta: { title: 'Book an appointment', description: 'Choose an appointment and book online.' },
      sections: [{ id: 'prices', type: 'pricing-table', heading: 'Appointments', items: [{ name: 'VIP', price: '$125', details: ['2 hours', 'Up to 6 guests'], featured: true }, { name: 'Standard', price: '$50' }] }],
    },
    'src/pages/[...slug].astro': '// code file: not editable by the structured engine',
  });
}
export const sampleAssets = () => new Set(['ast_hero1', 'ast_new1', 'ast_new2']);
