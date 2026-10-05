const $ = (id) => document.getElementById(id);
const post = (path, body) => fetch(path, { method: 'POST', headers: { 'content-type': 'application/json', 'x-sm-csrf': '1' }, body: body === undefined ? undefined : JSON.stringify(body) });
let siteId = null, state = null, attached = [], timer = null, busy = false;

const STATUS = {
  received: 'Working on it…',
  previewing: 'Building your preview. This usually takes about a minute.',
  preview_ready: "Here's a preview of the change.",
  awaiting_operator: "This one needs a designer's touch. We'll have a preview for you within one business day.",
  failed: "Something went wrong on our side, so nothing was changed. We've been notified.",
  published: 'Published. Your website has been updated.',
  discarded: 'Discarded. Nothing was changed.',
};
const short = (v) => { const t = typeof v === 'string' ? v.replace(/\*+/g, '') : String(v ?? ''); return t.length > 70 ? t.slice(0, 67) + '…' : t; };
function describe(f) {
  const where = f.file.replace('content/pages/', '').replace('content/settings/', '').replace('.json', '');
  const isPhoto = /\/(asset|image)(\/|$)/.test(f.pointer);
  const label = f.pointer.split('/').filter((p) => p && !/^\d+$/.test(p)).slice(-1)[0] || 'content';
  if (isPhoto) return `${where}: photo changed`;
  if (f.before === undefined) return `${where}: added ${label} “${short(f.after)}”`;
  if (f.after === undefined) return `${where}: removed ${label}`;
  return `${where}: ${label} “${short(f.before)}” → “${short(f.after)}”`;
}
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

function render() {
  const { site, draft, requests, revisions } = state;
  $('site-name').textContent = site.name;
  const thread = $('thread'); thread.replaceChildren();
  if (requests.length === 0) thread.append(Object.assign(el('li', 'msg bot', 'Hi! Tell me what you would like to change on your website. You can add a photo too.')));
  for (const r of [...requests].reverse()) {
    thread.append(el('li', 'msg me', r.text));
    const b = el('li', 'msg bot');
    if (r.status === 'needs_clarification') b.textContent = r.question;
    else {
      b.append(el('div', r.status === 'published' ? 'status-ok' : r.status === 'failed' ? 'status-bad' : '', STATUS[r.status] ?? ''));
      if (r.summary && ['previewing', 'preview_ready', 'published'].includes(r.status)) {
        b.append(el('div', '', r.summary));
        const ul = el('ul', 'changes'); (r.changedFields ?? []).slice(0, 8).forEach((f) => ul.append(el('li', '', describe(f)))); if (ul.children.length) b.append(ul);
      }
    }
    thread.append(b);
  }
  thread.scrollTop = thread.scrollHeight;

  const ready = draft && requests.some((r) => r.draftId === draft.id && r.status === 'preview_ready') && !requests.some((r) => r.draftId === draft.id && ['received', 'previewing'].includes(r.status));
  $('actions').hidden = !ready;
  const showPreview = draft?.previewUrl && ready;
  const url = showPreview ? draft.previewUrl : site.url;
  $('preview-label').textContent = showPreview ? 'Preview of your change' : 'Your live website';
  if (url && $('frame').dataset.url !== url) { $('frame').src = url; $('frame').dataset.url = url; }
  const open = $('preview-open'); if (url) open.href = url; else open.removeAttribute('href');
  $('undo').hidden = !(revisions.length > 0 && !draft);
  const working = requests.some((r) => ['received', 'previewing'].includes(r.status));
  $('send').disabled = working || busy;
  clearTimeout(timer); if (working) timer = setTimeout(load, 3000);
}

async function load() {
  const res = await fetch(`/api/sites/${encodeURIComponent(siteId)}/state`);
  if (res.status === 401) return showSignin('Your session has ended. Open your invite link again.');
  if (res.ok) { state = await res.json(); render(); }
}
function showSignin(msg) { $('app').hidden = true; $('signin').hidden = false; $('signin-msg').textContent = msg; }
function fail(msg) { const e = $('error'); e.textContent = msg; e.hidden = !msg; }

async function act(path, okMsg) {
  busy = true; fail('');
  try { const r = await post(path); if (!r.ok) fail((await r.json().catch(() => ({}))).error || 'That did not work. Please try again.'); }
  finally { busy = false; await load(); }
}

$('composer').addEventListener('submit', async (e) => {
  e.preventDefault(); fail(''); busy = true; $('send').disabled = true;
  try {
    const r = await post(`/api/sites/${encodeURIComponent(siteId)}/requests`, { text: $('text').value, assetIds: attached.map((a) => a.id) });
    if (!r.ok) return fail((await r.json().catch(() => ({}))).error || 'That did not work. Please try again.');
    $('text').value = ''; attached = []; $('chips').replaceChildren();
  } finally { busy = false; await load(); }
});
$('file').addEventListener('change', async (e) => {
  const f = e.target.files[0]; e.target.value = ''; if (!f) return; fail('');
  const r = await fetch(`/api/sites/${encodeURIComponent(siteId)}/assets`, { method: 'POST', headers: { 'content-type': f.type, 'x-sm-csrf': '1' }, body: f });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return fail(j.error || 'That photo could not be added.');
  attached.push(j);
  const img = el('img'); img.alt = j.alt || 'Uploaded photo'; img.src = `/assets/${encodeURIComponent(siteId)}/${j.id}`; $('chips').append(img);
});
$('approve').addEventListener('click', () => act(`/api/sites/${encodeURIComponent(siteId)}/drafts/${state.draft.id}/approve`));
$('discard').addEventListener('click', () => act(`/api/sites/${encodeURIComponent(siteId)}/drafts/${state.draft.id}/discard`));
$('keep').addEventListener('click', () => $('text').focus());
$('undo').addEventListener('click', () => { if (confirm('Undo the last published change?')) act(`/api/sites/${encodeURIComponent(siteId)}/undo`); });

(async function start() {
  const invite = new URLSearchParams(location.search).get('invite');
  if (invite) {
    const r = await post('/api/login', { invite });
    history.replaceState(null, '', '/');
    if (!r.ok) return showSignin((await r.json().catch(() => ({}))).error || 'This link did not work.');
  }
  const me = await fetch('/api/me');
  if (!me.ok) return showSignin('Open the link we sent you to sign in.');
  const { sites } = await me.json();
  if (!sites.length) return showSignin('No website is connected to your account yet.');
  siteId = sites[0].id; $('signin').hidden = true; $('app').hidden = false; await load();
})();
