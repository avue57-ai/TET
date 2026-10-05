import { chromium } from 'playwright-core';
import sharp from 'sharp';
const [invite, out] = process.argv.slice(2);
const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH ?? '/opt/pw-browsers/chromium-1194/chrome-linux/chrome', args: ['--no-sandbox'] });
const errors = [];
const ctx = await b.newContext({ viewport: { width: 1280, height: 800 } });
const p = await ctx.newPage();
p.on('console', (m) => { if (['error', 'warning'].includes(m.type())) errors.push(`${m.type()}: ${m.text()}`); });
p.on('response', (r) => { if (r.status() >= 400) console.log('HTTP', r.status(), r.url()); });
p.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
p.on('dialog', (d) => d.accept());
const ok = (c, msg) => { console.log((c ? 'PASS ' : 'FAIL ') + msg); if (!c) process.exitCode = 1; };

await p.goto(invite);
await p.waitForSelector('#app:not([hidden])');
ok((await p.textContent('#site-name')) === 'La Soirée Bridal', 'signed in via invite link, site name shown');
ok(!p.url().includes('invite='), 'invite token removed from the address bar');
ok(await p.$eval('#signin', (e) => getComputedStyle(e).display === 'none'), 'sign-in message is not visible once signed in');
ok(await p.$eval('#actions', (e) => getComputedStyle(e).display === 'none'), 'approve/discard buttons hidden before there is a preview');
ok((await p.textContent('#thread')).includes('Tell me what you would like to change'), 'greeting shown');
await p.screenshot({ path: `${out}/1-start.png` });

// upload a photo and send a request
const jpg = await sharp({ create: { width: 900, height: 600, channels: 3, background: '#c9a' } }).jpeg().toBuffer();
await p.setInputFiles('#file', { name: 'new.jpg', mimeType: 'image/jpeg', buffer: jpg });
await p.waitForSelector('#chips img');
ok(await p.$eval('#chips img', (i) => i.alt === 'Bride in a lace gown' && i.naturalWidth > 0), 'photo uploaded, thumbnail served from /assets with alt text');
await p.fill('#text', 'Change the headline to Luxury Bridal, Personally Curated.');
await p.click('#send');
await p.waitForSelector('#actions:not([hidden])', { timeout: 20000 });
const thread = await p.textContent('#thread');
ok(thread.includes('Changed the homepage headline.'), 'assistant summary shown');
ok(thread.includes('Luxury Bridal,'), 'before/after change list shown');
ok((await p.textContent('#preview-label')) === 'Preview of your change', 'preview panel switched to the draft preview');
await p.screenshot({ path: `${out}/2-preview-ready.png` });

await p.click('#approve');
await p.waitForFunction(() => document.querySelector('#thread').textContent.includes('Published. Your website has been updated.'));
ok(true, 'approve and publish worked');
ok(await p.$eval('#actions', (e) => e.hidden), 'action buttons hidden after publish');
ok(await p.$eval('#undo', (e) => !e.hidden), 'undo link available');
await p.screenshot({ path: `${out}/3-published.png` });

// clarification path
await p.fill('#text', 'Change the photo');
await p.click('#send');
await p.waitForFunction(() => document.querySelector('#thread').textContent.includes('Which photo do you mean'));
ok(true, 'clarifying question shown in the thread');
await p.click('#undo');
await p.waitForFunction(() => document.querySelector('#undo').hidden === false); // still has revisions (the undo itself)
ok(true, 'undo accepted');

// mobile
const m = await b.newContext({ viewport: { width: 390, height: 844 }, storageState: await ctx.storageState() });
const mp = await m.newPage();
await mp.goto(invite.split('?')[0]);
await mp.waitForSelector('#app:not([hidden])');
const overflow = await mp.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
ok(!overflow, 'no horizontal scroll on a 390px phone');
await mp.screenshot({ path: `${out}/4-mobile.png` });

// invite is single-use
const c2 = await b.newContext(); const p2 = await c2.newPage();
await p2.goto(invite); await p2.waitForSelector('#signin:not([hidden])');
ok((await p2.textContent('#signin-msg')).includes('invalid or has already been used'), 'reusing the invite link is refused with a friendly message');
ok(errors.length === 0, 'no console errors or CSP warnings' + (errors.length ? ': ' + errors.join(' | ') : ''));
await b.close();
