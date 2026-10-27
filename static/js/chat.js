/* Chat do Orbit Agent: envia o prompt (AJAX), acompanha a execução por polling e renderiza cada passo em tempo real. */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const { api, esc, md, human, toast } = OA;
  const STAR = '<svg viewBox="0 0 24 24"><path d="M12 2l2.4 6.6L21 11l-6.6 2.4L12 20l-2.4-6.6L3 11l6.6-2.4z"/></svg>';
  const GREETING = 'Olá! Sou seu agente de workspace. Posso criar, ler, editar e organizar arquivos no ambiente seguro, além de consultar páginas públicas. O que vamos fazer?';

  const el = { app: $('#app'), msgs: $('#messages'), prompt: $('#prompt'), send: $('#send'), status: $('#status'),
    title: $('#conv-title'), convs: $('#conv-list') };
  const state = { conv: null, busy: false, runId: null, stop: false, filesTimer: null };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const h = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html !== undefined) n.innerHTML = html; return n; };
  const scrollDown = () => { el.msgs.scrollTop = el.msgs.scrollHeight; };
  const nearBottom = () => el.msgs.scrollHeight - el.msgs.scrollTop - el.msgs.clientHeight < 160;

  /* ------------------------------------------------------------ mensagens */
  function addUser(text) {
    const m = h('div', 'msg user', `<div class="stack"><div class="bubble">${esc(text).replace(/\n/g, '<br>')}</div></div>`);
    el.msgs.appendChild(m); scrollDown();
  }
  function addAssistantStatic(msg) {
    const m = newAssistant(); m.bubble.classList.remove('hidden'); m.content.innerHTML = md(msg.content);
    m.thinking.remove(); if (msg.id) finishMeta(m, { message_id: msg.id, feedback: msg.feedback, run_id: msg.run_id, cached: msg.cached });
    return m;
  }
  function newAssistant() {
    const root = h('div', 'msg assistant', `<div class="avatar">${STAR}</div><div class="stack"><div class="steps"></div>
      <div class="bubble hidden"><div class="content"></div><div class="live"></div></div><div class="thinking"><i></i><i></i><i></i></div><div class="meta"></div></div>`);
    el.msgs.appendChild(root);
    return { root, steps: $('.steps', root), bubble: $('.bubble', root), content: $('.content', root), live: $('.live', root),
      thinking: $('.thinking', root), meta: $('.meta', root), calls: {} };
  }
  function greeting() { el.msgs.innerHTML = ''; const m = newAssistant(); m.thinking.remove(); m.bubble.classList.remove('hidden'); m.content.innerHTML = '<p>' + esc(GREETING) + '</p>'; }

  /* ------------------------------------------------------------ eventos do agente */
  const argsText = a => Object.entries(a || {}).map(([k, v]) => `${k}=${String(typeof v === 'string' ? v : JSON.stringify(v)).replace(/\s+/g, ' ').slice(0, 42)}`).join(', ');

  function handleEvent(m, ev) {
    const p = ev.payload, follow = nearBottom();
    switch (ev.kind) {
      case 'context': {
        const parts = [];
        if (p.chunks) parts.push(`📚 ${p.chunks} trecho(s) recuperado(s)`);
        if (p.memories) parts.push(`🧠 ${p.memories} memória(s)`);
        m.steps.appendChild(h('div', 'chip', esc(parts.join(' · ')))); break;
      }
      case 'text':
        m.bubble.classList.remove('hidden'); m.live.innerHTML = '';
        m.content.appendChild(h('div', 'seg', md(p.text))); break;
      case 'tool_call': {
        const row = h('div', 'step running', `<span class="dot"></span><div class="step-body"><div class="step-head"><b>${esc(p.name)}</b>
          <span class="args">${esc(argsText(p.args))}</span><span class="ms"></span></div><div class="step-out"></div></div>`);
        m.steps.appendChild(row); m.calls[p.id] = row; break;
      }
      case 'tool_result': {
        const row = m.calls[p.id]; if (!row) break;
        row.classList.remove('running', 'wait'); row.classList.add(p.ok ? 'ok' : 'fail');
        $('.ms', row).textContent = p.ms ? `${p.ms} ms` : '';
        const ap = $('.approve', row); if (ap) ap.remove();
        $('.step-out', row).innerHTML = `<details><summary>${p.ok ? 'resultado' : 'erro'}</summary><pre>${esc(p.preview || '')}</pre></details>`;
        if (p.changed) scheduleFiles(); break;
      }
      case 'approval': {
        const row = m.calls[p.call_id]; if (!row) break;
        row.classList.remove('running'); row.classList.add('wait');
        const box = h('div', 'approve', `<span class="risk">${p.risk === 'high' ? 'Ação de risco alto.' : 'Ação no navegador.'} Permitir?</span>
          <button class="btn allow" type="button">Permitir</button><button class="btn deny" type="button">Negar</button>`);
        const decide = async d => { box.remove(); row.classList.remove('wait'); row.classList.add('running');
          try { await api(`/api/approvals/${p.approval_id}/`, { method: 'POST', body: { decision: d } }); } catch (e) { toast(e.message); } };
        $('.allow', box).onclick = () => decide('allow'); $('.deny', box).onclick = () => decide('deny');
        $('.step-out', row).appendChild(box); el.status.textContent = 'Aguardando sua aprovação…'; break;
      }
      case 'error':
        m.bubble.classList.remove('hidden');
        m.content.appendChild(h('div', 'seg err', 'Algo deu errado: ' + esc(p.message))); break;
      case 'done': m.done = p; break;
    }
    if (follow) scrollDown();
  }

  function finishMeta(m, p) {
    m.thinking && m.thinking.remove(); m.live.innerHTML = '';
    const u = p.usage || {}; const bits = [];
    if (u.cached) bits.push(`⚡ resposta em cache · ~${OA.num(u.saved_tokens)} tokens economizados`);
    else if (u.input || u.output) bits.push(`${OA.num((u.input || 0) + (u.output || 0))} tokens${u.cache_read ? ` (${OA.num(u.cache_read)} do cache)` : ''}`);
    if (p.tools) bits.push(`${p.tools} ferramenta${p.tools > 1 ? 's' : ''}`);
    if (p.cached) bits.push('⚡ cache');
    m.meta.innerHTML = bits.map(esc).join(' <span class="sep">·</span> ');
    if (p.run_id && !u.cached && !p.cached && !m.stepsLoaded && !m.steps.children.length) {
      const b = h('button', '', 'ver passos'); b.onclick = () => loadSteps(m, p.run_id, b); m.meta.appendChild(b);
    }
    if (p.message_id) {
      const up = h('button', p.feedback === 1 ? 'on' : '', '👍'), down = h('button', p.feedback === -1 ? 'on' : '', '👎');
      up.title = 'Resposta útil'; down.title = 'Resposta ruim';
      const send = async v => { const cur = up.classList.contains('on') ? 1 : down.classList.contains('on') ? -1 : 0; const val = cur === v ? 0 : v;
        try { await api(`/api/messages/${p.message_id}/feedback/`, { method: 'POST', body: { value: val } });
          up.classList.toggle('on', val === 1); down.classList.toggle('on', val === -1); } catch (e) { toast(e.message); } };
      up.onclick = () => send(1); down.onclick = () => send(-1);
      m.meta.append(up, down);
    }
  }

  async function loadSteps(m, runId, btn) {
    btn.remove(); m.stepsLoaded = true;
    try { const d = await api(`/api/runs/${runId}/events/?after=0`); d.events.filter(e => e.kind !== 'text' && e.kind !== 'done').forEach(e => handleEvent(m, e));
      $$('.step.running', m.steps).forEach(r => r.classList.remove('running')); } catch (e) { toast(e.message); }
  }

  /* ------------------------------------------------------------ execução + polling */
  function setBusy(b) {
    state.busy = b; el.prompt.disabled = false;
    el.send.classList.toggle('stop', b);
    el.send.setAttribute('aria-label', b ? 'Parar' : 'Enviar');
    el.send.innerHTML = b ? '<svg class="i" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>'
      : '<svg class="i" viewBox="0 0 24 24"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg>';
    el.status.textContent = b ? 'Pensando…' : 'Pronto';
  }

  async function poll(m, runId, after = 0) {
    let seq = after, idle = 0, finished = false;
    while (!finished) {
      let d;
      try { d = await api(`/api/runs/${runId}/events/?after=${seq}`); }
      catch (e) { if (e.status === 404) break; await sleep(1500); continue; }
      for (const ev of d.events) { seq = ev.seq; handleEvent(m, ev); if (ev.kind === 'done') finished = true; }
      if (d.partial && !finished) { m.bubble.classList.remove('hidden'); m.live.innerHTML = md(d.partial); if (nearBottom()) scrollDown(); el.status.textContent = 'Escrevendo…'; }
      else if (!finished && d.status === 'running') {
        const running = $('.step.running', m.steps); el.status.textContent = running ? `Executando ${$('b', running).textContent}…` : 'Pensando…'; }
      if (d.done) finished = true;
      idle = d.events.length ? 0 : idle + 1;
      if (!finished) await sleep(idle > 20 ? 1000 : idle > 6 ? 600 : 300);
    }
    const p = m.done || {};
    finishMeta(m, { ...p, run_id: runId, cached: false });
    return p;
  }

  async function send(text) {
    text = (text || '').trim();
    if (!text || state.busy) return;
    if (!state.conv && el.msgs.children.length === 1) el.msgs.innerHTML = '';  // some a saudação
    addUser(text); el.prompt.value = ''; autoGrow(); setBusy(true);
    const m = newAssistant(); scrollDown();
    try {
      const r = await api('/api/runs/', { method: 'POST', body: { prompt: text, conversation_id: state.conv } });
      state.conv = r.conversation_id; state.runId = r.run_id;
      await poll(m, r.run_id);
    } catch (e) {
      m.thinking.remove(); m.bubble.classList.remove('hidden');
      m.content.innerHTML = `<div class="err">${esc(e.message)}</div>`;
    }
    setBusy(false); loadConversations(); scheduleFiles(0); el.prompt.focus();
  }

  async function stop() {
    if (!state.runId) return;
    try { await api(`/api/runs/${state.runId}/cancel/`, { method: 'POST' }); el.status.textContent = 'Cancelando…'; } catch (e) { toast(e.message); }
  }

  /* ------------------------------------------------------------ conversas */
  async function loadConversations() {
    try {
      const d = await api('/api/conversations/'); el.convs.innerHTML = '';
      d.conversations.forEach(c => {
        const row = h('div', 'conv' + (c.id === state.conv ? ' active' : ''), `<button class="t" type="button">${esc(c.title)}</button>
          <button class="del" type="button" aria-label="Excluir conversa" title="Excluir">×</button>`);
        $('.t', row).onclick = () => openConversation(c.id);
        $('.del', row).onclick = async () => { if (!confirm('Excluir esta conversa?')) return;
          await api(`/api/conversations/${c.id}/`, { method: 'DELETE' }); if (state.conv === c.id) newChat(); loadConversations(); };
        el.convs.appendChild(row);
      });
    } catch (_) { /* silencioso */ }
  }

  async function openConversation(id) {
    if (state.busy) { toast('Aguarde a resposta atual terminar.'); return; }
    el.app.classList.remove('show-sidebar');
    const d = await api(`/api/conversations/${id}/`);
    state.conv = d.id; el.title.textContent = d.title; el.msgs.innerHTML = '';
    d.messages.forEach(msg => msg.role === 'user' ? addUser(msg.content) : addAssistantStatic(msg));
    scrollDown(); loadConversations();
    if (d.active_run) { setBusy(true); const m = newAssistant(); state.runId = d.active_run; await poll(m, d.active_run); setBusy(false); scheduleFiles(0); }
  }

  function newChat() {
    if (state.busy) return;
    state.conv = null; el.title.textContent = 'Agente geral'; greeting(); loadConversations(); el.prompt.focus();
    el.app.classList.remove('show-sidebar');
  }

  /* ------------------------------------------------------------ arquivos */
  function scheduleFiles(delay = 400) { clearTimeout(state.filesTimer); state.filesTimer = setTimeout(refreshFiles, delay); }
  async function refreshFiles() { try { renderFiles(await api('/api/files/')); } catch (_) { /* silencioso */ } }

  const ICONS = { pdf: '📕', docx: '📘', xlsx: '📗', csv: '📊', png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', webp: '🖼️', py: '🐍', json: '🧾', md: '📝' };
  function renderFiles(d) {
    const files = d.files, list = $('#file-list');
    $('#file-count').textContent = `${files.length} arquivo${files.length === 1 ? '' : 's'}`;
    $('#drop-text').innerHTML = files.length ? 'Arraste arquivos aqui para enviar' : 'Nenhum arquivo ainda.<br>Peça ao agente para criar um ou arraste arquivos aqui.';
    list.innerHTML = '';
    files.forEach(f => {
      const b = h('button', 'file', `<span>${ICONS[f.ext] || '📄'}</span><span class="n" title="${esc(f.path)}">${esc(f.path)}</span><span class="s">${human(f.size)}</span>`);
      b.type = 'button'; b.onclick = () => openFile(f.path); list.appendChild(b);
    });
    const u = d.usage, pct = Math.min(100, Math.round(100 * u.bytes / u.quota_bytes)), bar = $('#quota-bar');
    bar.className = 'bar' + (pct > 90 ? ' bad' : pct > 70 ? ' warn' : ''); $('i', bar).style.width = pct + '%';
    $('#quota-text').textContent = `${human(u.bytes)} de ${human(u.quota_bytes)} · ${u.files}/${u.max_files} arquivos`;
  }

  const dlg = $('#file-dialog'); let current = null;
  async function openFile(path) {
    try {
      const d = await api(`/api/files/read/?path=${encodeURIComponent(path)}`); current = d;
      $('#dlg-title').textContent = d.path;
      $('#dlg-text').textContent = d.kind === 'binary' ? '(arquivo binário — use Baixar)' : d.text + (d.truncated ? '\n\n… (prévia truncada)' : '');
      $('#dlg-download').href = `/api/files/download/?path=${encodeURIComponent(d.path)}`;
      $('#dlg-undo').classList.toggle('hidden', !d.can_undo);
      $('#dlg-use').textContent = d.editable ? 'Pedir edição ao agente' : 'Perguntar ao agente';
      dlg.showModal();
    } catch (e) { toast(e.message); }
  }
  $('#dlg-close').onclick = () => dlg.close();
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
  $('#dlg-use').onclick = () => { dlg.close(); el.prompt.value = current.editable ? `No arquivo ${current.path}, ` : `Sobre o arquivo ${current.path}: `; el.prompt.focus(); autoGrow(); };
  $('#dlg-undo').onclick = async () => { try { const d = await api('/api/files/undo/', { method: 'POST', body: { path: current.path } }); renderFiles(d); toast('Edição desfeita.'); dlg.close(); } catch (e) { toast(e.message); } };
  $('#dlg-delete').onclick = async () => { if (!confirm(`Excluir ${current.path}? (vai para a lixeira)`)) return;
    try { renderFiles(await api('/api/files/delete/', { method: 'POST', body: { path: current.path } })); dlg.close(); } catch (e) { toast(e.message); } };

  async function upload(files) {
    if (!files.length) return;
    const form = new FormData(); [...files].forEach(f => form.append('files', f));
    try { const d = await api('/api/files/upload/', { method: 'POST', form }); renderFiles(d);
      if (d.errors.length) toast(d.errors.map(x => `${x.name}: ${x.error}`).join(' · ')); else toast(`${d.uploaded.length} arquivo(s) enviado(s).`); }
    catch (e) { toast(e.message); }
  }
  const dz = $('#dropzone');
  $('#upload-btn').onclick = () => $('#upload-input').click();
  $('#upload-input').onchange = e => { upload(e.target.files); e.target.value = ''; };
  ['dragenter', 'dragover'].forEach(t => dz.addEventListener(t, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => dz.addEventListener(t, e => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => upload(e.dataTransfer.files));

  /* ------------------------------------------------------------ ferramentas diretas */
  let tools = [];
  async function loadTools() {
    const d = await api('/api/tools/'); tools = d.tools; const sel = $('#tool-select'); sel.innerHTML = '';
    const groups = {};
    tools.forEach(t => (groups[t.category] ||= []).push(t));
    Object.entries(groups).forEach(([cat, items]) => { const og = document.createElement('optgroup'); og.label = cat;
      items.forEach(t => { const o = document.createElement('option'); o.value = t.name; o.textContent = t.name; og.appendChild(o); }); sel.appendChild(og); });
    sel.onchange = buildForm; buildForm();
  }
  function buildForm() {
    const t = tools.find(x => x.name === $('#tool-select').value); if (!t) return;
    $('#tool-desc').innerHTML = esc(t.description) + (t.risk !== 'low' ? `<span class="risk-tag ${t.risk}">risco ${t.risk === 'high' ? 'alto' : 'médio'}</span>` : '');
    const form = $('#tool-form'); form.innerHTML = '';
    Object.entries(t.schema.properties).forEach(([key, spec]) => {
      const req = t.schema.required.includes(key), id = `tf-${key}`;
      const row = h('div', 'form-row'); const label = h('label', '', `${esc(key)}${req ? ' *' : ''} <span class="muted">— ${esc(spec.description)}</span>`); label.htmlFor = id;
      let input;
      if (spec.type === 'boolean') { input = h('input'); input.type = 'checkbox'; }
      else if (spec.type === 'string' && ['content', 'old_text', 'new_text', 'text'].includes(key)) input = h('textarea');
      else { input = h('input'); input.type = spec.type === 'integer' ? 'number' : 'text'; }
      input.id = id; input.dataset.key = key; input.dataset.type = spec.type; row.append(label, input); form.appendChild(row);
    });
    $('#tool-out').classList.add('hidden');
  }
  $('#tool-run').onclick = async () => {
    const t = tools.find(x => x.name === $('#tool-select').value); if (!t) return;
    const args = {};
    $$('#tool-form [data-key]').forEach(i => { const k = i.dataset.key, ty = i.dataset.type;
      if (ty === 'boolean') args[k] = i.checked; else if (i.value !== '') args[k] = ty === 'integer' ? Number(i.value) : ty === 'array' ? i.value.split(',').map(s => s.trim()) : i.value; });
    if (t.risk === 'high' && !confirm(`"${t.name}" é uma ação de risco alto. Executar mesmo assim?`)) return;
    const out = $('#tool-out'); out.classList.remove('hidden'); out.textContent = 'Executando…';
    try { const r = await api('/api/tools/run/', { method: 'POST', body: { name: t.name, args } });
      let txt = r.output; try { txt = JSON.stringify(JSON.parse(r.output), null, 2); } catch (_) { /* texto puro */ }
      out.textContent = `${r.ok ? '✔' : '✖'} ${r.ms} ms\n\n${txt}`; if (r.changed) scheduleFiles(0); }
    catch (e) { out.textContent = '✖ ' + e.message; }
  };

  /* ------------------------------------------------------------ memória */
  async function loadMemories() {
    const d = await api('/api/memories/'), box = $('#mem-list'); box.innerHTML = '';
    if (!d.memories.length) { box.innerHTML = '<div class="empty">Nada por aqui ainda. Diga ao agente “lembre que…” ou adicione acima.</div>'; return; }
    d.memories.forEach(m => { const r = h('div', 'mem', `<span class="k">${esc(m.kind)}</span><span class="c">${esc(m.content)}</span><button type="button" aria-label="Esquecer" title="Esquecer">×</button>`);
      $('button', r).onclick = async () => { await api(`/api/memories/${m.id}/`, { method: 'DELETE' }); loadMemories(); }; box.appendChild(r); });
  }
  $('#mem-add').onclick = async () => { const i = $('#mem-input'); if (!i.value.trim()) return;
    try { await api('/api/memories/', { method: 'POST', body: { content: i.value } }); i.value = ''; loadMemories(); } catch (e) { toast(e.message); } };
  $('#mem-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('#mem-add').click(); });

  /* ------------------------------------------------------------ abas e UI */
  $$('.tab').forEach(tab => tab.onclick = () => {
    $$('.tab').forEach(t => t.setAttribute('aria-selected', t === tab));
    $$('.pane').forEach(p => p.classList.toggle('hidden', p.id !== 'pane-' + tab.dataset.tab));
    if (tab.dataset.tab === 'memory') loadMemories();
    if (tab.dataset.tab === 'tools' && !tools.length) loadTools();
  });
  function autoGrow() { el.prompt.style.height = 'auto'; el.prompt.style.height = Math.min(el.prompt.scrollHeight, 180) + 'px'; }
  el.prompt.addEventListener('input', autoGrow);
  el.prompt.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(el.prompt.value); } });
  el.send.onclick = () => state.busy ? stop() : send(el.prompt.value);
  $('#new-chat').onclick = newChat;
  $('#qa-files').onclick = () => { el.app.classList.remove('show-sidebar'); send('Liste os arquivos do meu workspace e resuma o que há neles.'); };
  $('#qa-web').onclick = () => { el.app.classList.remove('show-sidebar'); el.prompt.value = 'Consulte https:// e resuma o conteúdo.'; el.prompt.focus();
    el.prompt.setSelectionRange(17, 17); autoGrow(); };
  $('#toggle-side').onclick = () => el.app.classList.toggle('show-sidebar');
  $('#toggle-panel').onclick = () => el.app.classList.toggle('show-panel');
  $('#auto-approve').onchange = async e => { try { await api('/api/profile/', { method: 'POST', body: { auto_approve: e.target.checked } });
    toast(e.target.checked ? 'Aprovação automática ativada (só cliques e digitação).' : 'Aprovação automática desativada.'); } catch (err) { toast(err.message); } };

  if (matchMedia('(max-width:520px)').matches) el.prompt.placeholder = 'Escreva sua mensagem…';
  greeting(); loadConversations(); refreshFiles();
})();
