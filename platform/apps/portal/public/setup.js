const $ = (id) => document.getElementById(id);
const out = (v) => { $('out').textContent = typeof v === 'string' ? v : JSON.stringify(v, null, 2); };

async function call(method, path, body) {
  const pw = $('pw').value;
  if (!pw) { out('Type the setup password first.'); return null; }
  const res = await fetch(path, { method, headers: { 'x-admin-secret': pw, ...(body ? { 'content-type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { out('Wrong setup password.'); return null; }
  if (!res.ok) { out(`Error ${res.status}: ${data.error ?? 'request failed'}`); return null; }
  return data;
}

$('check').addEventListener('click', async () => {
  out('Checking…');
  const site = $('chk-site').value.trim();
  const d = await call('GET', `/api/admin/diag?probe=ai${site ? `&site=${encodeURIComponent(site)}` : ''}`);
  if (d) out(d);
});
$('register').addEventListener('click', async () => {
  const d = await call('POST', '/api/admin/sites', {
    id: $('s-id').value.trim(), orgId: $('s-org').value.trim(), name: $('s-name').value.trim(), url: $('s-url').value.trim(),
    repo: { owner: $('s-owner').value.trim(), repo: $('s-repo').value.trim(), netlifySiteName: $('s-netlify').value.trim() },
  });
  if (d) out('Website registered.');
});
$('invite').addEventListener('click', async () => {
  const d = await call('POST', '/api/admin/invites', { orgId: $('i-org').value.trim(), userId: $('i-user').value.trim() });
  if (d) out(`Sign-in link (works once):\n${d.invite}`);
});
