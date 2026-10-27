/* Dashboard: busca /api/dashboard/ (AJAX) e atualiza a cada 6 s enquanto a aba está visível.
   Também verifica a base vetorial (Pinecone/local) e permite testar uma busca sem passar pelo modelo. */
(() => {
  const $ = s => document.querySelector(s);
  const { api, esc, human, num, toast } = OA;
  let metric = 'runs', last = null;
  const set = (id, v) => { const n = document.getElementById(id); if (n) n.textContent = v; };
  const STORE = { pinecone: 'Pinecone', local: 'numpy local' };
  const ORIGIN = { auto: 'contexto automático', tool: 'rag_search', test: 'teste' };
  const storeLabel = (s, fallback) => fallback ? 'local (Pinecone falhou)' : (STORE[s] || s);

  function render(d) {
    last = d;
    set('provider', d.provider === 'mock' ? 'Modo demonstração' : `Modelo: ${d.model}`);
    set('k-req', `${num(d.today.requests)} / ${num(d.today.limit)}`);
    set('k-tok', num(d.today.tokens)); set('k-tok-sub', `${num(d.totals.tokens)} no total`);
    set('k-act', num(d.today.actions)); set('k-act-sub', `${num(d.totals.actions)} no total`);
    const pct = d.today.limit ? Math.min(100, Math.round(100 * d.today.requests / d.today.limit)) : 0;
    set('limit-pct', `${pct}% do limite diário`);
    const lb = $('#limit-bar'); lb.className = 'bar' + (pct >= 90 ? ' bad' : pct >= 70 ? ' warn' : ''); lb.firstElementChild.style.width = pct + '%';

    set('k-cache', `${d.totals.cache_hit_rate}%`); set('k-saved', `${num(d.totals.tokens_saved)} tokens economizados`);
    set('k-lat', d.totals.avg_seconds ? `${d.totals.avg_seconds} s` : '–'); set('k-err', `${d.totals.error_rate}%`);
    set('k-fb', `${d.feedback.up} 👍 · ${d.feedback.down} 👎`);

    drawChart(d.series);

    const tools = $('#tools'); tools.innerHTML = '';
    const max = Math.max(1, ...d.tools.map(t => t.count));
    if (!d.tools.length) tools.innerHTML = '<div class="empty">Nenhuma ferramenta usada ainda.</div>';
    d.tools.forEach(t => tools.insertAdjacentHTML('beforeend', `<div class="r"><b title="${esc(t.tool)}">${esc(t.tool)}</b>
      <div class="bar ${t.failures ? 'warn' : ''}"><i style="width:${Math.round(100 * t.count / max)}%"></i></div>
      <small>${t.count}${t.failures ? ` · <span class="err">${t.failures} falha(s)</span>` : ''} · ${t.avg_ms} ms</small></div>`));

    renderRag(d.rag, d.knowledge);

    const w = d.workspace, wp = Math.min(100, Math.round(100 * w.bytes / w.quota_bytes));
    const wb = $('#ws-bar'); wb.className = 'bar' + (wp > 90 ? ' bad' : wp > 70 ? ' warn' : ''); wb.firstElementChild.style.width = wp + '%';
    set('ws-files', `${w.files} de ${w.max_files}`); set('ws-bytes', `${human(w.bytes)} de ${human(w.quota_bytes)}`);
    set('ws-ext', w.by_ext.map(e => `${e.ext} (${e.count})`).join(', ') || '–');

    const k = d.knowledge;
    set('kb-docs', num(k.documents)); set('kb-chunks', num(k.chunks)); set('kb-mem', num(k.memories));
    const EMB = { hash: 'hashing local', st: 'sentence-transformers', pinecone: 'Pinecone Inference' };
    const VS = k.vector_store === 'pinecone' ? `Pinecone (${k.index})` : 'numpy local';
    set('kb-dim', `${k.dimension} · ${EMB[k.backend] || k.backend} · ${VS}`); set('kb-bytes', human(k.vector_bytes));
    const TYPES = { web: 'sites', file: 'arquivos', text: 'textos' };
    set('kb-types', k.by_type.map(t => `${TYPES[t.type] || t.type}: ${t.documents} (${t.chunks} trechos)`).join(' · ') || '–');

    set('learn-text', d.learning ? 'Você autorizou o armazenamento de amostras das solicitações para melhorar o agente.'
      : 'O aprendizado está desligado: o agente não guarda amostras nem indexa automaticamente sites e arquivos que usa.');
    const lt = $('#learn-toggle'); if (document.activeElement !== lt) lt.checked = d.learning;

    const body = $('#recent'); body.innerHTML = '';
    $('#recent-empty').classList.toggle('hidden', d.recent.length > 0);
    d.recent.forEach(r => body.insertAdjacentHTML('beforeend', `<tr><td>${esc(r.at)}</td><td>${esc(r.prompt)}</td>
      <td><span class="pill ${r.cached ? 'cache' : r.status}">${r.cached ? '⚡ cache' : esc(r.status)}</span></td>
      <td>${ragCell(r.rag)}</td><td>${num(r.tokens)}</td><td>${r.steps}</td><td>${r.seconds ? r.seconds + ' s' : '–'}</td></tr>`));
  }

  function ragCell(r) {
    if (!r) return '<span class="muted">–</span>';
    if (!r.hits) return `<span class="muted">0 trechos · ${esc(storeLabel(r.store, r.fallback))}</span>`;
    return `<span class="pill ${r.fallback ? 'warn' : 'rag'}">📚 ${r.hits} · ${esc(storeLabel(r.store, r.fallback))}</span>${r.tool ? ' <small class="muted">+ rag_search</small>' : ''}`;
  }

  function renderRag(r, k) {
    set('rag-engine', k.vector_store === 'pinecone' ? `Motor atual: Pinecone · ${k.index}` : 'Motor atual: numpy local');
    set('rag-rate', `${r.context_rate}%`);
    set('rag-rate-sub', `${r.runs_with_context} de ${r.runs} solicitações receberam trechos`);
    set('rag-searches', num(r.searches));
    set('rag-searches-sub', `${r.with_hits} com resultado · ${r.tool_calls} via rag_search`);
    set('rag-stores', Object.entries(r.by_store).map(([s, n]) => `${STORE[s] || s}: ${n}`).join(' · ') || '–');
    set('rag-fallbacks', r.fallbacks ? `${r.fallbacks} fallback(s) para o local` : 'nenhum fallback');
    set('rag-ms', r.searches ? `${r.avg_ms} ms` : '–');
    set('rag-score', r.avg_top_score ? `nota média do melhor trecho: ${r.avg_top_score}` : '');
    const tb = $('#rag-recent'); tb.innerHTML = '';
    $('#rag-empty').classList.toggle('hidden', r.recent.length > 0);
    r.recent.forEach(x => tb.insertAdjacentHTML('beforeend', `<tr><td>${esc(x.at)}</td><td>${esc(ORIGIN[x.origin] || x.origin)}</td>
      <td><span class="pill ${x.fallback ? 'warn' : x.store === 'pinecone' ? 'rag' : ''}">${esc(storeLabel(x.store, x.fallback))}</span></td>
      <td>${esc(x.query)}</td><td>${x.hits}</td><td>${x.hits ? x.top_score : '–'}</td>
      <td class="src">${x.sources.map(s => esc(s.source)).join('<br>') || '–'}</td><td>${x.ms} ms</td></tr>`));
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

  /* ---------------------------------------------------- verificação da base vetorial */
  async function checkHealth() {
    const box = $('#rag-health'), list = $('#rag-health-list');
    set('rag-health-text', 'Verificando…'); box.className = 'health';
    try {
      const h = await api('/api/dashboard/rag-health/');
      const pine = h.store === 'pinecone';
      box.className = 'health ' + (!h.ok ? 'bad' : h.in_sync ? 'ok' : 'warn');
      set('rag-health-text', !h.ok ? `Pinecone inacessível: ${h.error}`
        : pine ? (h.in_sync ? `Pinecone conectado e em sincronia (${h.ms} ms)` : 'Pinecone conectado, mas a contagem difere do banco local')
          : 'Busca local (numpy): sem Pinecone configurado');
      const rows = [['Motor', pine ? `Pinecone · ${h.index}` : 'numpy local'], ['Embeddings', `${h.embedder} · ${h.dimension} dim.`],
        ['Trechos no banco local', num(h.local_chunks)]];
      if (pine && h.ok) rows.push(['Vetores no seu namespace', `${num(h.remote_vectors)} (${h.namespace})`], ['Vetores no índice inteiro', num(h.index_total)]);
      list.innerHTML = rows.map(([a, b]) => `<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join('');
      if (pine && h.ok && !h.in_sync) list.insertAdjacentHTML('beforeend', '<dt class="span2">A contagem do Pinecone é eventualmente consistente: após salvar algo, pode levar alguns segundos. Se persistir, rode <code>reindex_knowledge</code>.</dt>');
    } catch (e) { box.className = 'health bad'; set('rag-health-text', e.message); }
  }

  async function testSearch() {
    const q = $('#rag-q').value.trim(); if (!q) return;
    const out = $('#rag-results'); out.innerHTML = '<div class="empty">Buscando…</div>';
    try {
      const r = await api('/api/dashboard/rag-test/', { method: 'POST', body: { query: q } });
      set('rag-test-meta', `${storeLabel(r.store, r.fallback)} · ${r.embedder} · ${r.ms} ms · nota mínima para entrar no contexto: ${r.min_score}`
        + (r.error ? ` · erro: ${r.error}` : ''));
      out.innerHTML = r.results.length ? r.results.map(x => `<div class="hit ${x.used ? 'used' : ''}"><div class="row-between"><b>${esc(x.title || x.source)}</b>
        <span class="pill ${x.used ? 'rag' : ''}">${x.score}${x.used ? ' · usado' : ' · abaixo do corte'}</span></div><p>${esc(x.snippet)}</p></div>`).join('')
        : '<div class="empty">Nenhum trecho encontrado. A base está vazia ou não há nada parecido.</div>';
      load();
    } catch (e) { out.innerHTML = ''; toast(e.message); }
  }

  document.querySelectorAll('[data-metric]').forEach(b => b.onclick = () => {
    metric = b.dataset.metric; document.querySelectorAll('[data-metric]').forEach(x => x.setAttribute('aria-pressed', x === b)); if (last) drawChart(last.series);
  });
  $('#learn-toggle').onchange = async e => { try { await api('/api/profile/', { method: 'POST', body: { allow_learning: e.target.checked } }); load(); }
    catch (err) { toast(err.message); e.target.checked = !e.target.checked; } };
  $('#rag-health-btn').onclick = checkHealth;
  $('#rag-test').onclick = testSearch;
  $('#rag-q').addEventListener('keydown', e => { if (e.key === 'Enter') testSearch(); });

  async function load() { try { render(await api('/api/dashboard/')); } catch (_) { /* tenta de novo no próximo ciclo */ } }
  load(); checkHealth(); setInterval(() => { if (!document.hidden) load(); }, 6000);
})();
