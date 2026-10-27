/* Utilidades compartilhadas: CSRF + fetch, tema, markdown seguro, toasts. */
const OA = (() => {
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';

  async function api(url, opts = {}) {
    const init = { method: opts.method || 'GET', credentials: 'same-origin', headers: { 'X-CSRFToken': csrf() } };
    if (opts.form) init.body = opts.form;
    else if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    const res = await fetch(url, init);
    if (res.status === 401) { location.href = '/login/'; throw new Error('Sessão expirada.'); }
    let data = {};
    try { data = await res.json(); } catch (_) { /* resposta sem corpo */ }
    if (!res.ok) { const e = new Error(data.error || `Erro ${res.status}`); e.status = res.status; e.data = data; throw e; }
    return data;
  }

  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* Markdown mínimo e seguro: escapa TUDO primeiro, depois aplica poucas regras. */
  function md(src) {
    let s = esc(src); const blocks = [];
    s = s.replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, c) => { blocks.push(`<pre><code>${c.replace(/\n$/, '')}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g, '$1<em>$2</em>');
    s = s.replace(/^#{1,3} (.+)$/gm, '<strong>$1</strong>');
    s = s.replace(/(^|\n)((?:[-*] .+(?:\n|$))+)/g, (_, p, list) =>
      `${p}<ul>${list.trim().split('\n').map(li => `<li>${li.replace(/^[-*] /, '')}</li>`).join('')}</ul>`);
    s = s.replace(/\bhttps:\/\/[^\s<)]+/g, u => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
    s = s.replace(/\n{2,}/g, '</p><p>').replace(/\n/g, '<br>');
    return ('<p>' + s + '</p>').replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[i]).replace(/<p>(<(?:pre|ul)[\s\S]*?<\/(?:pre|ul)>)<\/p>/g, '$1');
  }

  const human = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
  const num = n => Number(n || 0).toLocaleString('pt-BR');

  let toastTimer;
  function toast(msg) {
    let t = document.querySelector('.toast');
    if (!t) { t = document.createElement('div'); t.className = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.textContent = msg; t.classList.remove('hidden');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), 3800);
  }

  function setTheme(t) {
    const eff = t === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : t;
    document.documentElement.dataset.theme = eff; localStorage.setItem('orbit-theme', t);
  }
  function initTheme() {
    document.querySelectorAll('[data-theme-toggle]').forEach(btn => btn.addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      setTheme(next);
      api('/api/profile/', { method: 'POST', body: { theme: next } }).catch(() => {}); // anônimo (login): ignora
    }));
  }
  initTheme();
  return { api, esc, md, human, num, toast, csrf };
})();
