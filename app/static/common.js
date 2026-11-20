// Gemeinsame Helfer für app.html (Studierende) und admin.html
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString('de-CH') : '–';

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} },
};

// Platzhalter hervorheben: [TYP_1] sowie Ersatztexte aus der Entitätsliste
function hl(text, entities) {
  let out = esc(text);
  const extra = [...new Set((entities || []).map(e => e.placeholder).filter(p => p && p !== 'BLOCK' && !/^\[[A-Z_]+_\d+\]$/.test(p)))];
  for (const p of extra) out = out.split(esc(p)).join(`<mark>${esc(p)}</mark>`);
  return out.replace(/\[([A-Z_]+_\d+)\]/g, '<mark>[$1]</mark>');
}

function msgs(body) {
  return (body?.messages || []).map(m => {
    let c = m.content;
    if (Array.isArray(c)) c = c.map(p => p.type === 'text' ? p.text : `[${p.type}]`).join('\n');
    return { role: m.role, content: c ?? '' };
  });
}

function entityTable(entities) {
  if (!entities?.length) return '<p class="empty">Nichts erkannt.</p>';
  const act = {mask: 'maskiert', redact: 'entfernt', block: 'blockiert'};
  return `<table><tr><th>Typ</th><th>Original</th><th>Ersetzt durch</th><th>Aktion</th><th>Quelle</th><th>Score</th></tr>
    ${entities.map(e => `<tr><td>${esc(e.type)}</td><td>${esc(e.original)}</td><td><mark>${esc(e.placeholder)}</mark></td>
      <td>${esc(act[e.action] || e.action || 'maskiert')}</td><td>${e.source === 'rule' ? 'eigene Regel' : 'eingebaut'}</td><td>${e.score ?? ''}</td></tr>`).join('')}
  </table>`;
}

function renderLogList(items, selected, onclickName, showUser) {
  return items.map(it => `
    <div class="logrow ${it.id === selected ? 'active' : ''}" onclick="${onclickName}('${esc(it.id)}')">
      <span class="time">${fmtTime(it.ts)}</span>
      ${showUser ? `<span class="badge user">${esc(it.user_name || '–')}</span>` : ''}
      <span class="model">${esc(it.model || '?')}</span>
      <span class="badge ent">${it.entity_count} PII</span>
      <span class="badge ${esc(it.status)}">${esc(it.status)}${it.duration_ms ? ' · ' + (it.duration_ms/1000).toFixed(1) + 's' : ''}</span>
    </div>`).join('') || '<p class="empty" style="padding:16px">Noch keine Requests.</p>';
}

function renderLogDetail(d) {
  const o = msgs(d.original_body), a = msgs(d.anon_body);
  const rows = o.map((m, i) => [m, a[i] || {role: m.role, content: ''}]);
  const blocked = d.status === 'blocked';
  return `
    <div class="meta">${fmtTime(d.ts)}${d.user_name ? ' · ' + esc(d.user_name) : ''} · ${esc(d.model || '?')} · ${d.stream ? 'stream' : 'non-stream'} ·
      Status: ${esc(d.status)}${d.duration_ms ? ' · ' + d.duration_ms + ' ms' : ''}</div>
    ${blocked ? '<div class="msgbox warn">Von der Guardrail blockiert: Diese Anfrage hat das LLM nie erreicht.</div>' : ''}
    ${d.error ? `<div class="msg" style="border-color:var(--err)"><pre>${esc(d.error)}</pre></div>` : ''}
    <h2>Erkannt (${(d.entities || []).length})</h2>
    ${entityTable(d.entities)}
    <h2>Anfrage: Eingang (links) vs. an das LLM (rechts)</h2>
    <div class="cols">
      <div class="col orig"><h3>▶ Eingang (Original)</h3>
        ${rows.map(([m]) => `<div class="msg"><div class="role">${esc(m.role)}</div><pre>${esc(m.content)}</pre></div>`).join('')}
      </div>
      <div class="col anon"><h3>▶ Ausgang an das LLM</h3>
        ${blocked ? '<p class="empty">– nicht gesendet –</p>' : rows.map(([, m]) => `<div class="msg"><div class="role">${esc(m.role)}</div><pre>${hl(m.content, d.entities)}</pre></div>`).join('')}
      </div>
    </div>
    ${blocked ? '' : `<h2>Antwort</h2>
    <div class="cols">
      <div class="col anon"><h3>▶ Vom LLM (mit Platzhaltern)</h3>
        <div class="msg"><pre>${hl(d.response_anon, d.entities) || '<span class="empty">–</span>'}</pre></div></div>
      <div class="col orig"><h3>▶ An den Client (zurückübersetzt)</h3>
        <div class="msg"><pre>${esc(d.response_final) || '<span class="empty">–</span>'}</pre></div></div>
    </div>`}`;
}

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; } catch (e) { return false; }
}
