/* Shared helpers. Small on purpose — every page is server-rendered HTML plus
   a handful of fetch calls, so there is no framework to learn or upgrade. */

const API = {
  async get(url) { return API._go(url, { method: 'GET' }); },
  async post(url, data) {
    return API._go(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data || {})
    });
  },
  async del(url) { return API._go(url, { method: 'DELETE' }); },
  async upload(url, formData) {
    return API._go(url, { method: 'POST', body: formData });
  },
  async _go(url, opts) {
    const r = await fetch(url, opts);
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    let data;
    try { data = await r.json(); }
    catch (e) { throw new Error(`The server replied with ${r.status}.`); }
    if (!r.ok || data.ok === false) {
      const e = new Error(data.error || `Request failed (${r.status})`);
      e.payload = data;
      throw e;
    }
    return data;
  }
};

function toast(msg, kind) {
  let wrap = document.querySelector('.toast-wrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'toast-wrap';
    document.body.appendChild(wrap);
  }
  const t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  wrap.appendChild(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 4000);
}

/* Money with tabular figures and parentheses for negatives — the convention
   finance people read fastest. */
function money(v, currency) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (isNaN(n)) return '—';
  const s = Math.abs(n).toLocaleString('en-US',
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const sym = currency === 'CAD' ? 'CA$' : currency === 'EUR' ? '€'
            : currency === 'GBP' ? '£' : '$';
  return n < 0 ? `(${sym}${s})` : `${sym}${s}`;
}

function moneyCell(v, currency) {
  const n = Number(v);
  const cls = (!isNaN(n) && n < 0) ? 'num neg' : 'num';
  return `<td class="${cls}">${money(v, currency)}</td>`;
}

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function dateStr(s) {
  if (!s) return '—';
  const d = new Date(s);
  if (isNaN(d)) return esc(String(s).slice(0, 10));
  return d.toISOString().slice(0, 10);
}

function ago(s) {
  if (!s) return '';
  const then = new Date(s), now = new Date();
  if (isNaN(then)) return '';
  const secs = Math.floor((now - then) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} h ago`;
  const d = Math.floor(secs / 86400);
  if (d < 30) return `${d} day${d > 1 ? 's' : ''} ago`;
  return then.toISOString().slice(0, 10);
}

const VERDICT_CLASS = {
  matched: 'ok', matched_no_doc: 'ok', paid: '', credit: 'info',
  amount_mismatch: 'bad', statement_only: 'bad', qbo_only: 'bad',
  missing_bill: 'warn', missing_invoice: 'warn',
  missing_statement: 'warn', invoice_only: 'warn'
};

function verdictPill(verdict, label) {
  return `<span class="pill ${VERDICT_CLASS[verdict] || ''}">${esc(label || verdict)}</span>`;
}

/* A modal built from a string, removed on close. No library, no leaks. */
function modal(title, bodyHtml, footHtml, opts) {
  opts = opts || {};
  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = `
    <div class="modal ${opts.wide ? 'wide' : ''}">
      <div class="modal-head">
        <h3>${esc(title)}</h3>
        <button class="x" data-close>&times;</button>
      </div>
      <div class="modal-body">${bodyHtml}</div>
      ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ''}
    </div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener('click', e => {
    if (e.target === back || e.target.hasAttribute('data-close')) close();
  });
  document.addEventListener('keydown', function onKey(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); }
  });
  back.close = close;
  return back;
}

/* Keep a button honest while its request is in flight, so nobody
   double-submits a payment-adjacent action. */
async function busy(btn, fn) {
  if (!btn) return fn();
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> working';
  try { return await fn(); }
  finally { btn.disabled = false; btn.innerHTML = original; }
}

function val(id) {
  const el = document.getElementById(id);
  if (!el) return '';
  return el.type === 'checkbox' ? (el.checked ? 1 : 0) : el.value.trim();
}

function qs(name, dflt) {
  return new URLSearchParams(location.search).get(name) || dflt || '';
}
