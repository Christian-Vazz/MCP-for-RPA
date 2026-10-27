/* Dashboard: busca /api/dashboard/ (AJAX) e atualiza a cada 6 s enquanto a aba está visível. */
(() => {
  const $ = s => document.querySelector(s);
  const { api, esc, human, num } = OA;
  let metric = 'runs', last = null;
  const set = (id, v) => { const n = document.getElementById(id); if (n) n.textContent = v; };

  function render(d) {
    last = d;
    set('provider', d.provider === 'mock' ? 'Modo demonstração' : `Modelo: ${d.model}`);
    set('k-req', `${num(d.today.requests)} / ${num(d.today.limit)}`);
    set('k-tok', num(d.today.tokens)); set('k-tok-sub', `${num(d.totals.tokens)} no total`);
    set('k-act', num(d.today.actions)); set('k-act-sub', `${num(d.totals.actions)} no total`);
    const pct = d.today.limit ? Math.min(100, Math.round(100 * d.today.requests / d.today.limit)) : 0;
    set('limit-pct', `${pct}% utilizado`);
    const lb = $('#limit-bar'); lb.className = 'bar' + (pct >= 90 ? ' bad' : pct >= 70 ? ' warn' : ''); lb.firstElementChild.style.width = pct + '%';

    set('k-cache', `${d.totals.cache_hit_rate}%`); set('k-saved', num(d.totals.tokens_saved));
    set('k-lat', d.totals.avg_seconds ? `${d.totals.avg_seconds} s` : '–'); set('k-err', `${d.totals.error_rate}%`);
    set('k-fb', `${d.feedback.up} 👍 · ${d.feedback.down} 👎`);

    drawChart(d.series);

    const tools = $('#tools'); tools.innerHTML = '';
    const max = Math.max(1, ...d.tools.map(t => t.count));
    if (!d.tools.length) tools.innerHTML = '<div class="empty">Nenhuma ferramenta usada ainda.</div>';
    d.tools.forEach(t => tools.insertAdjacentHTML('beforeend', `<div class="r"><b title="${esc(t.tool)}">${esc(t.tool)}</b>
      <div class="bar ${t.failures ? 'warn' : ''}"><i style="width:${Math.round(100 * t.count / max)}%"></i></div>
      <small>${t.count}${t.failures ? ` · <span class="err">${t.failures} falha(s)</span>` : ''} · ${t.avg_ms} ms</small></div>`));

    const w = d.workspace, wp = Math.min(100, Math.round(100 * w.bytes / w.quota_bytes));
    const wb = $('#ws-bar'); wb.className = 'bar' + (wp > 90 ? ' bad' : wp > 70 ? ' warn' : ''); wb.firstElementChild.style.width = wp + '%';
    set('ws-files', `${w.files} de ${w.max_files}`); set('ws-bytes', `${human(w.bytes)} de ${human(w.quota_bytes)}`);
    set('ws-ext', w.by_ext.map(e => `${e.ext} (${e.count})`).join(', ') || '–');

    const k = d.knowledge;
    set('kb-docs', num(k.documents)); set('kb-chunks', num(k.chunks)); set('kb-mem', num(k.memories));
    set('kb-dim', `${k.dimension} · ${k.backend === 'hash' ? 'hashing local' : 'sentence-transformers'}`); set('kb-bytes', human(k.vector_bytes));
    set('kb-types', k.by_type.map(t => `${t.type === 'web' ? 'sites' : 'arquivos'}: ${t.documents} (${t.chunks} trechos)`).join(' · ') || '–');

    set('learn-text', d.learning ? 'Você autorizou o armazenamento de amostras das solicitações para melhorar o agente.'
      : 'O aprendizado está desligado: o agente não guarda amostras nem indexa automaticamente sites e arquivos que usa.');
    const lt = $('#learn-toggle'); if (document.activeElement !== lt) lt.checked = d.learning;

    const body = $('#recent'); body.innerHTML = '';
    $('#recent-empty').classList.toggle('hidden', d.recent.length > 0);
    d.recent.forEach(r => body.insertAdjacentHTML('beforeend', `<tr><td>${esc(r.at)}</td><td>${esc(r.prompt)}</td>
      <td><span class="pill ${r.cached ? 'cache' : r.status}">${r.cached ? '⚡ cache' : esc(r.status)}</span></td>
      <td>${num(r.tokens)}</td><td>${r.steps}</td><td>${r.seconds ? r.seconds + ' s' : '–'}</td></tr>`));
  }

  function drawChart(series) {
    const vals = series.map(s => s[metric]), max = Math.max(1, ...vals);
    $('#chart').innerHTML = series.map(s => {
      const v = s[metric], hgt = Math.round(100 * v / max), day = s.date.slice(8) + '/' + s.date.slice(5, 7);
      return `<div class="col" title="${day}: ${num(v)}"><span class="v">${v ? num(v) : ''}</span><div class="b" style="height:${v ? Math.max(hgt, 3) : 0}%"></div><span class="d">${day.slice(0, 2)}</span></div>`;
    }).join('');
    const total = vals.reduce((a, b) => a + b, 0);
    set('chart-hint', `${num(total)} ${metric === 'runs' ? 'solicitações' : 'tokens'} no período`);
  }

  document.querySelectorAll('[data-metric]').forEach(b => b.onclick = () => {
    metric = b.dataset.metric; document.querySelectorAll('[data-metric]').forEach(x => x.setAttribute('aria-pressed', x === b)); if (last) drawChart(last.series);
  });
  $('#learn-toggle').onchange = async e => { try { await api('/api/profile/', { method: 'POST', body: { allow_learning: e.target.checked } }); load(); }
    catch (err) { OA.toast(err.message); e.target.checked = !e.target.checked; } };

  async function load() { try { render(await api('/api/dashboard/')); } catch (_) { /* tenta de novo no próximo ciclo */ } }
  load(); setInterval(() => { if (!document.hidden) load(); }, 6000);
})();
