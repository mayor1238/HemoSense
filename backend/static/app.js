/* HemoSense web app: talks only to the HemoSense server (REST + one live WebSocket). */
(() => {
'use strict';
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const GROUPS = ['A+','A−','B+','B−','AB+','AB−','O+','O−'];
const ID_NAMES = {hospital:'Hospital no.', nhia:'NHIA', nin:'NIN', nbsa:'NBSA donor', emergency:'Emergency ID'};
const WELL_NAMES = ['Anti-A','Anti-B','Anti-D'], COLORS = ['var(--a)','var(--b)','var(--d)'];
const fmtDate = d => d ? new Date(d.length === 10 ? d + 'T00:00:00' : d).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'}) : '–';
const fmtTime = d => d ? new Date(d).toLocaleString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}) : '–';
const uuid = () => crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random()*16|0; return (c==='x'?r:(r&3|8)).toString(16); });

const S = {token: null, user: null, settings: {}, readers: [], ws: null, wsWanted: false, retry: null};
try { S.token = localStorage.getItem('hs.token'); } catch (e) {}

/* ------------------------------------------------ server calls */
async function api(method, path, body) {
  const r = await fetch(path, {method, headers: {'Content-Type':'application/json', ...(S.token ? {Authorization:'Bearer '+S.token} : {})},
                               body: body === undefined ? undefined : JSON.stringify(body)});
  let data = null; try { data = await r.json(); } catch (e) {}
  if (r.status === 401 && path !== '/api/login') { signOutLocal('Your session ended. Please sign in again.'); throw new Error('Please sign in.'); }
  if (!r.ok) throw new Error((data && data.error) || 'Server error ' + r.status);
  return data;
}
async function download(path, filename) {
  const r = await fetch(path, {headers: {Authorization: 'Bearer ' + S.token}});
  if (!r.ok) throw new Error('Download failed (' + r.status + ')');
  const url = URL.createObjectURL(await r.blob()), a = document.createElement('a');
  a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* ------------------------------------------------ sign in / out */
$('loginForm').addEventListener('submit', async e => {
  e.preventDefault(); $('lErr').textContent = ''; $('lGo').disabled = true;
  try {
    const d = await api('POST', '/api/login', {username: $('lUser').value, password: $('lPass').value});
    S.token = d.token; try { localStorage.setItem('hs.token', d.token); } catch (x) {}
    $('lPass').value = ''; await enter();
  } catch (x) { $('lErr').textContent = x.message; } finally { $('lGo').disabled = false; }
});
$('logout').onclick = async () => { try { await api('POST', '/api/logout'); } catch (e) {} signOutLocal(''); };
function signOutLocal(msg) {
  S.token = null; try { localStorage.removeItem('hs.token'); } catch (e) {}
  document.body.dataset.auth = 'out'; $('lErr').textContent = msg || ''; closeSocket();
}
async function enter() {
  const d = await api('GET', '/api/me');
  S.user = d.user; S.settings = d.settings;
  document.body.dataset.auth = 'in';
  $('whoName').textContent = S.user.full_name; $('whoRole').textContent = S.user.role === 'admin' ? 'Administrator' : 'Operator';
  document.querySelectorAll('.admin-only').forEach(el => el.hidden = S.user.role !== 'admin');
  $('facility').textContent = S.settings.facility;
  if (!$('cOp').value) $('cOp').value = S.user.username.toUpperCase();
  openSocket(); resetTest(); route();
}

/* ------------------------------------------------ live link */
function openSocket() {
  S.wsWanted = true; clearTimeout(S.retry);
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/app?token=' + encodeURIComponent(S.token));
  S.ws = ws;
  ws.onopen = () => { connState(); if (T.running) resync(); };
  ws.onmessage = e => { try { onLive(JSON.parse(e.data)); } catch (x) { console.error(x); } };
  ws.onclose = ev => {
    if (S.ws !== ws) return; S.ws = null; connState();
    if (ev.code === 4401) return signOutLocal('Your session ended. Please sign in again.');
    if (S.wsWanted) S.retry = setTimeout(openSocket, 2000);
  };
}
function closeSocket() { S.wsWanted = false; clearTimeout(S.retry); const w = S.ws; S.ws = null; try { w && w.close(); } catch (e) {} connState(); }
function connState() {
  const on = S.ws && S.ws.readyState === 1, n = S.readers.length, el = $('conn');
  el.className = 'conn' + (on ? (n ? ' on' : ' warn') : '');
  el.lastElementChild.textContent = !on ? 'Server offline, retrying' : n ? `${n} reader${n > 1 ? 's' : ''} online` : 'No reader online';
}
function onLive(m) {
  if (m.type === 'readers') { S.readers = m.readers || []; connState(); renderReaders(); if (page() === 'dashboard') loadDashboard(); return; }
  const mine = T.running && m.test_id === T.id;
  if (m.type === 'calibrated' && m.reader_id === $('rSel').value) {
    $('cal').disabled = false; $('cal').textContent = 'Calibrate reader';
    $('e3').textContent = m.ok ? '' : 'Calibration failed: no signal from the 5 kΩ resistor on C0. Check the AD5933 wiring.';
  } else if (m.type === 'ack' && mine) {
    T.acked = true; clearTimeout(T.ackTimer); $('liveSub').textContent = `Reader ${m.reader_id} is measuring. Check its screen shows the same code.`;
  } else if (m.type === 'reading' && mine) {
    if (!T.seen.has(m.i)) { T.seen.add(m.i); addReading({seq: m.i, t_ms: m.t_ms, z: m.z}); }
  } else if (m.type === 'done' && mine) {
    if (m.status === 'interrupted') $('e3').textContent = 'The reader restarted during the test. Retest with a new cartridge.';
    if (m.status === 'error') $('e3').textContent = 'The reader reported a sensor error. Check the AD5933 and retest.';
    finishRun(m.analysis);
  } else if (m.type === 'error') {
    if (T.running && m.reader_id === T.reader) { $('e3').textContent = 'Reader: ' + m.msg; if (!T.acked) stopLocal(); }
    else if (m.reader_id === $('rSel').value) $('e3').textContent = 'Reader: ' + m.msg;
  }
}
async function resync() {
  try {
    const d = await api('GET', '/api/tests/' + T.id);
    d.readings.forEach(r => { if (!T.seen.has(r.seq)) { T.seen.add(r.seq); addReading(r); } });
    if (T.running && ['done','stopped','error','interrupted'].includes(d.status)) finishRun(d.analysis);
  } catch (e) {}
}

/* ------------------------------------------------ pages */
const page = () => (location.hash.slice(1) || 'test');
function route() {
  if (document.body.dataset.auth !== 'in') return;
  const p = ['test','records','research','dashboard','settings'].includes(page()) ? page() : 'test';
  document.querySelectorAll('.page').forEach(s => s.hidden = s.dataset.page !== p);
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.page === p));
  if (p === 'records') loadRecords();
  if (p === 'research') loadResearch();
  if (p === 'dashboard') loadDashboard();
  if (p === 'settings') loadSettings();
}
addEventListener('hashchange', route);

/* ------------------------------------------------ new test */
let T = {};
function setStep(n) {
  T.step = n; [1,2,3,4].forEach(i => $('st' + i).hidden = i !== n);
  [...$('steps').children].forEach((li, i) => li.className = i + 1 === n ? 'on' : i + 1 < n ? 'done' : '');
}
function resetTest() {
  clearTimeout(T.ackTimer); clearTimeout(T.stopTimer);
  T = {step: 1, readings: [], seen: new Set(), running: false};
  ['pId','pName','pDob','cId','cLot'].forEach(id => $(id).value = ''); $('pSex').value = '';
  ['c2id','cCare','cRes','cfm'].forEach(id => $(id).checked = false);
  ['e1','e2','e3','e4'].forEach(id => $(id).textContent = '');
  $('pFound').innerHTML = ''; $('liveCode').hidden = true; $('liveSub').textContent = 'No test running';
  $('cSweep').value = String(S.settings.sweep_s || 120); $('cInt').value = String(S.settings.interval_s || 2);
  if (![...$('cSweep').options].some(o => o.selected)) $('cSweep').value = '120';
  $('save').hidden = false; $('retest').hidden = false; $('next').hidden = true; $('save').disabled = false;
  drawLive(); setStep(1);
}
function strip(p) {
  return `<div><span class="muted">Patient</span><b>${esc(p.full_name)}</b></div><div><span class="muted">${esc(ID_NAMES[p.id_type])}</span><b>${esc(p.id_value)}</b></div><div><span class="muted">Born</span><b>${esc(fmtDate(p.dob))}</b></div>` +
         (p.last ? `<div><span class="muted">Last group</span><b>${esc(p.last.group)}${p.last.lab_confirmed ? ' (lab)' : ''}</b></div>` : '');
}
let lookTimer, lookSeq = 0;
function lookup() {
  clearTimeout(lookTimer);
  lookTimer = setTimeout(async () => {
    const type = $('pType').value, val = $('pId').value.trim(), seq = ++lookSeq;
    if (!val || type === 'emergency') { $('pFound').innerHTML = ''; return; }
    try {
      const d = await api('GET', `/api/patients/find?id_type=${encodeURIComponent(type)}&id_value=${encodeURIComponent(val)}`);
      if (seq !== lookSeq) return;
      const p = d.patient;
      $('pFound').innerHTML = p
        ? `<div class="note warn"><b>Existing patient:</b> ${esc(p.full_name)}, born ${esc(fmtDate(p.dob))}${p.last ? `, last group <b>${esc(p.last.group)}</b> ${p.last.lab_confirmed ? '(lab confirmed)' : '(HemoSense)'}` : ''}. Confirm it is the person in front of you.
           <div class="actions" style="margin-top:10px"><button class="btn small" id="useP">Use this patient</button></div></div>`
        : '<div class="note info">No record for this ID yet. A new patient will be registered.</div>';
      if (p) $('useP').onclick = () => { $('pName').value = p.full_name; $('pDob').value = p.dob || ''; $('pSex').value = p.sex || ''; };
    } catch (e) { $('pFound').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
  }, 350);
}
$('pId').addEventListener('input', lookup); $('pType').addEventListener('change', lookup);
$('sample1').onclick = () => {
  $('pType').value = 'hospital'; $('pId').value = 'OOUTH-' + (100000 + Math.floor(Math.random() * 899999));
  $('pName').value = 'Folake Adeyemi'; $('pDob').value = '1996-04-18'; $('pSex').value = 'Female'; lookup();
};
$('emg').onclick = async () => {
  try {
    const d = await api('POST', '/api/patients/emergency-id', {});
    $('pType').value = 'emergency'; $('pId').value = d.id_value; $('pName').value = d.full_name; $('pDob').value = ''; $('pSex').value = '';
    $('c2id').checked = true; $('cCare').checked = true; $('cRes').checked = false;
    $('pFound').innerHTML = '<div class="note warn">Emergency ID created. Merge it into the real record once the patient is identified.</div>';
  } catch (e) { $('e1').textContent = e.message; }
};
$('n1').onclick = async () => {
  const errs = [];
  if (!$('pId').value.trim()) errs.push('Enter or scan the patient ID.');
  if (!$('pName').value.trim()) errs.push('Enter the full name.');
  if ($('pType').value !== 'emergency' && !$('pDob').value) errs.push('Enter the date of birth.');
  if (!$('c2id').checked) errs.push('Confirm the two-identifier check.');
  if (!$('cCare').checked) errs.push('Care consent is needed before testing.');
  $('e1').innerHTML = errs.map(esc).join('<br>'); if (errs.length) return;
  $('n1').disabled = true;
  try {
    const d = await api('POST', '/api/patients', {id_type: $('pType').value, id_value: $('pId').value.trim(), full_name: $('pName').value.trim(),
                                                    dob: $('pDob').value || null, sex: $('pSex').value || null});
    T.patient = d.patient; T.research = $('cRes').checked;
    $('strip2').innerHTML = $('strip3').innerHTML = strip(d.patient); setStep(2);
  } catch (e) { $('e1').textContent = e.message; } finally { $('n1').disabled = false; }
};
$('sample2').onclick = () => { $('cId').value = 'HSC-' + String(Math.floor(Math.random() * 99999)).padStart(5, '0'); $('cLot').value = 'RG-2611-B'; };
$('b2').onclick = () => setStep(1);
$('n2').onclick = async () => {
  const errs = [];
  if (!$('cId').value.trim()) errs.push('Scan or enter the cartridge ID.');
  if (!$('cLot').value.trim()) errs.push('Enter the reagent lot.');
  if (!$('cOp').value.trim()) errs.push('Enter the operator.');
  if (!errs.length) {
    try { if ((await api('GET', '/api/cartridges/' + encodeURIComponent($('cId').value.trim()))).used) errs.push('This cartridge was already used. Cartridges are single use.'); }
    catch (e) { errs.push(e.message); }
  }
  $('e2').innerHTML = errs.map(esc).join('<br>'); if (errs.length) return;
  T.cart = {cartridge_id: $('cId').value.trim(), reagent_lot: $('cLot').value.trim(), operator: $('cOp').value.trim(),
            sweep_s: +$('cSweep').value, interval_s: +$('cInt').value};
  setStep(3); renderReaders();
};
$('b3').onclick = () => { if (!T.running) setStep(2); };

function renderReaders() {
  const sel = $('rSel'), cur = sel.value;
  sel.innerHTML = S.readers.length
    ? S.readers.map(r => `<option value="${esc(r.id)}">${esc(r.id)}${r.simulated ? ' (demo mode)' : ''}${r.busy ? ' · busy' : ''}</option>`).join('')
    : '<option value="">No reader online</option>';
  if (S.readers.some(r => r.id === cur)) sel.value = cur;
  const r = S.readers.find(x => x.id === sel.value);
  $('rInfo').innerHTML = !r ? '<span class="tag">Offline</span> Switch the reader on; it appears here within a few seconds.'
    : `<span class="tag ok">Online</span> firmware ${esc(r.firmware || '?')} ` +
      (r.sensor_ok === false ? '<span class="tag bad">AD5933 not responding</span>' : r.calibrated === false ? '<span class="tag warn">Needs calibration</span>' : '<span class="tag ok">Calibrated</span>') +
      (r.simulated ? ' <span class="tag warn">Demo mode: simulated readings</span>' : '') + (r.busy ? ' <span class="tag blue">Busy</span>' : '');
  if (!$('cal').textContent.startsWith('Calibrating')) $('cal').disabled = !r || r.busy || T.running;
  $('go').disabled = T.running || !r;
}
$('rSel').onchange = renderReaders;
$('cal').onclick = async () => {
  $('e3').textContent = ''; $('cal').disabled = true; $('cal').textContent = 'Calibrating…';
  try { await api('POST', `/api/readers/${encodeURIComponent($('rSel').value)}/calibrate`, {}); }
  catch (e) { $('e3').textContent = e.message; $('cal').disabled = false; $('cal').textContent = 'Calibrate reader'; return; }
  setTimeout(() => { if ($('cal').textContent.startsWith('Calibrating')) { $('cal').textContent = 'Calibrate reader'; renderReaders(); } }, 8000);
};
$('go').onclick = async () => {
  $('e3').textContent = '';
  const r = S.readers.find(x => x.id === $('rSel').value);
  if (!r) return void ($('e3').textContent = 'No reader online.');
  T.id = uuid(); T.reader = r.id; T.readings = []; T.seen = new Set(); T.running = true; T.acked = false; T.simulated = r.simulated;
  $('go').disabled = $('cal').disabled = $('b3').disabled = true; $('stop').disabled = false;
  $('liveSub').textContent = 'Sending the start command…'; drawLive();
  try {
    const d = await api('POST', '/api/tests', {test_id: T.id, patient_id: T.patient.id, care_consent: true, research_consent: T.research,
                                                 reader_id: r.id, ...T.cart});
    T.code = d.code; T.simulated = d.simulated;
    $('liveCode').textContent = d.code; $('liveCode').hidden = false;
  } catch (e) { stopLocal(); $('e3').textContent = e.message; return; }
  if (!T.acked) T.ackTimer = setTimeout(() => { if (T.running && !T.acked) { stopLocal(); $('e3').textContent = 'The reader did not answer. Check it is online and try again.'; } }, 8000);
};
$('stop').onclick = async () => {
  if (!T.running) return;
  $('stop').disabled = true;
  try { await api('POST', `/api/tests/${T.id}/stop`, {}); } catch (e) {}
  T.stopTimer = setTimeout(() => { if (T.running) resync(); }, 4000);
};
function stopLocal() {
  T.running = false; clearTimeout(T.ackTimer); clearTimeout(T.stopTimer);
  $('stop').disabled = true; $('b3').disabled = false; renderReaders();
}
function addReading(r) {
  T.readings.push(r); T.readings.sort((a, b) => a.t_ms - b.t_ms);
  const t = r.t_ms / 1000, dur = T.cart ? T.cart.sweep_s : 120;
  $('prog').style.width = Math.min(100, t / dur * 100) + '%';
  $('liveSub').textContent = `${Math.round(t)} s of ${dur} s · ${T.readings.length} readings`;
  drawLive();
}
function finishRun(analysis) {
  stopLocal(); $('prog').style.width = '100%';
  T.analysis = analysis; showResult(); setStep(4);
}

/* live wells + chart */
function series() {
  const s = S.settings, settle = +s.settle || 0, rel = s.method !== 'range';
  const rows = T.readings || [];
  if (!rows.length) return {rel, pts: [[], [], []]};
  const base = [0, 1, 2].map(k => {
    const after = rows.slice(settle, settle + 3).map(r => r.z[k]).filter(v => v != null);
    const vals = after.length ? after : [rows[0].z[k]];
    return vals.sort((a, b) => a - b)[Math.floor(vals.length / 2)];
  });
  return {rel, pts: [0, 1, 2].map(k => rows.map(r => [r.t_ms / 1000, rel ? (r.z[k] - base[k]) / base[k] * 100 : r.z[k] / 1000]))};
}
function drawLive() {
  const {rel, pts} = series(), s = S.settings, thr = +s.threshold || 8, sign = s.direction === 'fall' ? -1 : 1;
  $('wells').innerHTML = WELL_NAMES.map((name, k) => {
    const last = pts[k].length ? pts[k][pts[k].length - 1][1] : null;
    let cls = '';
    if (last != null && pts[k].length > (+s.settle || 0) + 3) {
      if (rel) cls = sign * last >= thr ? 'pos' : 'neg';
      else cls = last >= s.agg_low && last <= s.agg_high ? 'pos' : last >= s.non_low && last <= s.non_high ? 'neg' : '';
    }
    const val = last == null ? '–' : rel ? (last >= 0 ? '+' : '') + last.toFixed(1) + '%' : last.toFixed(1) + ' kΩ';
    return `<div class="well ${cls}"><div class="disc"></div><b>${name}</b><div class="val">${val}</div><div class="lab">${cls === 'pos' ? 'Clumping' : cls === 'neg' ? 'No clumping' : 'Waiting'}</div></div>`;
  }).join('');
  const dur = T.cart ? T.cart.sweep_s : (+s.sweep_s || 120);
  const lines = rel ? [{y: sign * thr, dash: true}, {y: 0}] : [{y: s.agg_low, dash: true}, {y: s.agg_high, dash: true}, {y: s.non_low, dash: true}, {y: s.non_high, dash: true}];
  $('liveChart').innerHTML = lineChart(pts, {xMax: dur, unit: rel ? '%' : 'kΩ', refs: lines, minSpan: rel ? 20 : 10});
}
function lineChart(series, o) {
  const W = 640, H = 260, L = 46, R = 12, Tp = 12, B = 30;
  const ys = series.flat().map(p => p[1]).concat(o.refs.map(r => r.y));
  let lo = Math.min(...ys, 0), hi = Math.max(...ys, 0);
  if (hi - lo < o.minSpan) { const mid = (hi + lo) / 2; lo = mid - o.minSpan / 2; hi = mid + o.minSpan / 2; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const X = t => L + (t / o.xMax) * (W - L - R), Y = v => Tp + (hi - v) / (hi - lo) * (H - Tp - B);
  const step = niceStep((hi - lo) / 5);
  let g = '';
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step)
    g += `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="#eef2f7"/><text x="${L - 8}" y="${Y(v) + 4}" text-anchor="end" font-size="11" fill="#94a3b8">${+v.toFixed(1)}${o.unit === '%' ? '%' : ''}</text>`;
  for (let t = 0; t <= o.xMax; t += o.xMax <= 60 ? 10 : 30)
    g += `<text x="${X(t)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="#94a3b8">${t}s</text>`;
  o.refs.forEach(r => g += `<line x1="${L}" x2="${W - R}" y1="${Y(r.y)}" y2="${Y(r.y)}" stroke="${r.dash ? '#94a3b8' : '#cbd5e1'}" ${r.dash ? 'stroke-dasharray="5 4"' : ''}/>`);
  series.forEach((pts, k) => { if (pts.length) g += `<polyline fill="none" stroke="${COLORS[k]}" stroke-width="2.5" stroke-linejoin="round" points="${pts.map(p => X(p[0]) + ',' + Y(p[1])).join(' ')}"/>`; });
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Impedance curves">${g}</svg>`;
}
function niceStep(x) { const p = Math.pow(10, Math.floor(Math.log10(x || 1))); const n = x / p; return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * p; }

/* result */
function showResult() {
  const a = T.analysis || {valid: false, flags: ['No analysis returned.'], wells: []}, p = T.patient, last = p.last;
  const mismatch = a.valid && last && last.group && last.group !== a.group;
  $('result').innerHTML = `
    <h2>Result ${T.code ? '· ' + esc(T.code) : ''} ${T.simulated ? '<span class="tag warn">Simulated readings</span>' : ''}</h2>
    <div class="res-top">
      <div class="group ${a.valid ? '' : 'bad'}">${a.valid ? esc(a.group) : 'Invalid: retest'}</div>
      ${a.valid ? `<div class="conf"><span class="muted">Confidence</span><div class="bar"><div style="width:${Math.round(a.confidence * 100)}%"></div></div><b>${Math.round(a.confidence * 100)}%</b> · ${a.method === 'range' ? 'impedance ranges' : 'change over time'}</div>` : ''}
    </div>
    ${mismatch ? `<div class="note bad"><b>Does not match the last result (${esc(last.group)}).</b> Do not transfuse on this result. Re-check the patient's identity and retest.</div>` : ''}
    ${T.simulated ? '<div class="note warn">Demo mode: these readings were simulated by the reader, not measured from blood.</div>' : ''}
    ${(a.flags || []).length ? `<div class="note bad">${a.flags.map(esc).join('<br>')}</div>` : ''}
    <div class="table-wrap"><table class="table" style="margin-top:12px"><tr><th>Well</th><th>Final</th><th>Reaction</th></tr>
      ${(a.wells || []).map(w => `<tr><td>${esc(w.well)}</td><td>${w.value == null ? '–' : esc(w.value) + ' ' + esc(w.unit)}</td><td>${w.reaction === 'positive' ? '<span class="tag bad">Agglutination</span>' : w.reaction === 'negative' ? '<span class="tag ok">None</span>' : '<span class="tag warn">Unclear</span>'}</td></tr>`).join('')}
    </table></div>
    <div class="wb"><div><span>Name</span><b>${esc(p.full_name)}</b></div><div><span>Born</span><b>${esc(fmtDate(p.dob))}</b></div><div><span>${esc(ID_NAMES[p.id_type])}</span><b>${esc(p.id_value)}</b></div></div>`;
  $('cfm').checked = false; $('e4').textContent = '';
}
$('save').onclick = async () => {
  if (!$('cfm').checked) return void ($('e4').textContent = 'Tick the wristband check before saving.');
  $('save').disabled = true;
  try {
    const d = await api('POST', `/api/tests/${T.id}/save`, {identity_checked: true});
    $('e4').innerHTML = `<span style="color:var(--green)">Saved to ${esc(T.patient.full_name)}'s record${T.research ? ', with an anonymous research copy' : ''}.</span>`;
    $('save').hidden = true; $('retest').hidden = true; $('next').hidden = false;
    T.patient.last = d.test.valid ? {group: d.test.group, lab_confirmed: false} : T.patient.last;
  } catch (e) { $('e4').textContent = e.message; $('save').disabled = false; }
};
$('retest').onclick = () => { $('cId').value = ''; T.readings = []; T.seen = new Set(); drawLive(); $('liveCode').hidden = true; setStep(2); };
$('next').onclick = resetTest;

/* ------------------------------------------------ records */
$('fG').innerHTML += GROUPS.map(g => `<option>${g}</option>`).join('');
let recTimer;
['fQ','fG','fL'].forEach(id => $(id).addEventListener('input', () => { clearTimeout(recTimer); recTimer = setTimeout(loadRecords, 250); }));
async function loadRecords() {
  try {
    const q = new URLSearchParams({q: $('fQ').value, group: $('fG').value, lab: $('fL').value});
    const d = await api('GET', '/api/tests?' + q);
    $('recTable').innerHTML = d.tests.length ? `<thead><tr><th>Saved</th><th>Patient</th><th>ID</th><th>HemoSense</th><th>Lab group</th><th>Confidence</th><th>Cartridge</th><th>Reader</th><th></th></tr></thead><tbody>` +
      d.tests.map(t => `<tr class="click" data-p="${esc(t.patient.id)}"><td>${esc(fmtTime(t.saved_at))}</td><td><b>${esc(t.patient.full_name)}</b></td>
        <td>${esc(ID_NAMES[t.patient.id_type])} ${esc(t.patient.id_value)}</td>
        <td>${t.valid ? `<span class="gp">${esc(t.group)}</span>` : '<span class="tag bad">Invalid</span>'}</td>
        <td><select data-lab="${esc(t.id)}"><option value="">Awaiting</option>${GROUPS.map(g => `<option ${t.lab_group === g ? 'selected' : ''}>${g}</option>`).join('')}</select>
          ${t.lab_group && t.valid ? (t.lab_group === t.group ? ' <span class="tag ok">Agrees</span>' : ' <span class="tag bad">Differs</span>') : ''}</td>
        <td>${t.valid ? Math.round(t.confidence * 100) + '%' : '–'}</td><td>${esc(t.cartridge_id)}</td><td>${esc(t.reader_id)}</td>
        <td>${t.simulated ? '<span class="tag warn">Simulated</span>' : ''}</td></tr>`).join('') + '</tbody>'
      : '<tr><td class="empty">No saved tests match.</td></tr>';
    document.querySelectorAll('[data-lab]').forEach(sel => {
      sel.onclick = e => e.stopPropagation();
      sel.onchange = async () => {
        try { await api('PATCH', `/api/tests/${sel.dataset.lab}/lab`, {group: sel.value || null}); loadRecords(); }
        catch (e) { $('recMsg').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
      };
    });
    document.querySelectorAll('#recTable tr[data-p]').forEach(tr => tr.onclick = () => openPatient(tr.dataset.p));
  } catch (e) { $('recMsg').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
}
async function openPatient(pid) {
  try {
    const d = await api('GET', '/api/patients/' + pid), p = d.patient;
    $('drawer').innerHTML = `<button class="btn ghost small x" id="dx">Close ✕</button><h1>${esc(p.full_name)}</h1>
      <p class="muted">${esc(ID_NAMES[p.id_type])} ${esc(p.id_value)} · born ${esc(fmtDate(p.dob))}${p.sex ? ' · ' + esc(p.sex) : ''}</p>
      ${p.last ? `<div class="note ${p.last.lab_confirmed ? 'ok' : 'info'}">Current blood group <b>${esc(p.last.group)}</b> ${p.last.lab_confirmed ? '(lab confirmed)' : '(HemoSense, awaiting lab)'}</div>` : ''}
      <h2 style="margin-top:24px">Test history</h2><div class="hist">${d.tests.map(t => `<div><b>${t.valid ? esc(t.group) : 'Invalid'}</b> ${t.lab_group ? `· lab ${esc(t.lab_group)}` : ''} ${t.simulated ? '<span class="tag warn">Simulated</span>' : ''}<br>
        <span class="muted">${esc(fmtTime(t.saved_at))} · ${esc(t.code)} · cartridge ${esc(t.cartridge_id)} · ${esc(t.operator)}</span></div>`).join('') || '<p class="muted">No tests.</p>'}</div>`;
    $('drawer').hidden = $('drawerBg').hidden = false;
    $('dx').onclick = closeDrawer;
  } catch (e) { $('recMsg').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
}
function closeDrawer() { $('drawer').hidden = $('drawerBg').hidden = true; }
$('drawerBg').onclick = closeDrawer;
addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

/* ------------------------------------------------ research */
async function loadResearch() {
  try {
    const d = await api('GET', '/api/research'), rows = d.research;
    const labelled = rows.filter(r => r.lab_group), agree = labelled.filter(r => r.lab_group === r.device_group).length;
    $('resKpis').innerHTML = kpi(rows.length, 'Anonymous tests') + kpi(rows.reduce((n, r) => n + r.readings, 0), 'Readings stored') +
      kpi(labelled.length, 'With lab label') + kpi(labelled.length ? Math.round(agree / labelled.length * 100) + '%' : '–', 'Agreement with lab');
    $('resTable').innerHTML = rows.length ? '<thead><tr><th>Date</th><th>Age band</th><th>Sex</th><th>HemoSense</th><th>Lab label</th><th>Readings</th><th>Reagent lot</th><th>Reader</th><th></th></tr></thead><tbody>' +
      rows.map(r => `<tr><td>${esc(fmtTime(r.created_at))}</td><td>${esc(r.age_band)}</td><td>${esc(r.sex || '–')}</td><td class="gp">${esc(r.device_group)}</td>
        <td>${esc(r.lab_group || 'Awaiting')}</td><td>${r.readings}</td><td>${esc(r.reagent_lot)}</td><td>${esc(r.reader_id)}</td><td>${r.simulated ? '<span class="tag warn">Simulated</span>' : ''}</td></tr>`).join('') + '</tbody>'
      : '<tr><td class="empty">No research data yet. Save a test with research consent.</td></tr>';
  } catch (e) { $('resTable').innerHTML = `<tr><td class="empty">${esc(e.message)}</td></tr>`; }
}
$('csv').onclick = () => download('/api/research.csv', 'hemosense-research.csv').catch(e => alert(e.message));
const kpi = (v, l) => `<div class="kpi"><b>${esc(v)}</b><span>${esc(l)}</span></div>`;

/* ------------------------------------------------ dashboard */
async function loadDashboard() {
  try {
    const d = await api('GET', '/api/overview');
    $('kpis').innerHTML = kpi(d.tests_today, 'Tests today') + kpi(d.tests_total, 'Tests saved') + kpi(d.patients, 'Patients') +
      kpi(d.valid_rate == null ? '–' : d.valid_rate + '%', 'Valid first time') + kpi(d.lab_agreement == null ? '–' : d.lab_agreement + '%', `Agreement with lab (${d.lab_checked})`) +
      kpi(d.readers.length, 'Readers online');
    api('GET', '/api/health').then(h => { $('dbInfo').textContent = 'Database: ' + h.database; }).catch(() => {});
    const days = d.per_day, max = Math.max(1, ...days.map(x => x.tests)), W = 640, H = 220, bw = (W - 40) / days.length;
    $('dayChart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Tests per day">` + days.map((x, i) => {
      const h = x.tests / max * (H - 50), xx = 30 + i * bw;
      return `<rect x="${xx + 4}" y="${H - 28 - h}" width="${bw - 8}" height="${Math.max(h, 1)}" rx="4" fill="${x.tests ? 'var(--blue)' : '#e2e8f0'}"/>` +
        (x.tests ? `<text x="${xx + bw / 2}" y="${H - 34 - h}" text-anchor="middle" font-size="11" fill="#334155">${x.tests}</text>` : '') +
        (i % 2 === 0 ? `<text x="${xx + bw / 2}" y="${H - 10}" text-anchor="middle" font-size="10" fill="#94a3b8">${x.day.slice(8)}/${x.day.slice(5, 7)}</text>` : '');
    }).join('') + '</svg>';
    const gmax = Math.max(1, ...Object.values(d.by_group));
    $('groupBars').innerHTML = '<div class="bars">' + GROUPS.map(g => `<div class="row"><span>${g}</span><div class="track"><div class="fill" style="width:${d.by_group[g] / gmax * 100}%"></div></div><span class="n">${d.by_group[g]}</span></div>`).join('') + '</div>';
    $('readerList').innerHTML = d.readers.length ? '<div class="readers">' + d.readers.map(r => `<div class="reader-row"><b>${esc(r.id)}</b><span>firmware ${esc(r.firmware || '?')}</span>
      <span>${r.calibrated === false ? '<span class="tag warn">Needs calibration</span>' : '<span class="tag ok">Calibrated</span>'} ${r.busy ? '<span class="tag blue">Testing</span>' : '<span class="tag">Idle</span>'} ${r.simulated ? '<span class="tag warn">Demo mode</span>' : ''}</span>
      <span class="muted">online since ${esc(fmtTime(r.since))}</span></div>`).join('') + '</div>' : '<p class="muted">No reader online.</p>';
  } catch (e) { $('kpis').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
}

/* ------------------------------------------------ settings */
const SMAP = {sFac:'facility', sMethod:'method', sDir:'direction', sThr:'threshold', sSet:'settle', sSweep:'sweep_s', sInt:'interval_s', sAggLo:'agg_low', sAggHi:'agg_high', sNonLo:'non_low', sNonHi:'non_high'};
async function loadSettings() {
  try { S.settings = await api('GET', '/api/settings'); } catch (e) {}
  const admin = S.user.role === 'admin';
  Object.entries(SMAP).forEach(([id, k]) => { $(id).value = S.settings[k]; $(id).disabled = !admin; });
  $('sSave').disabled = $('sCopper').disabled = !admin; rangeBox();
  $('sMsg').innerHTML = admin ? '' : '<div class="note warn">Only an admin can change these.</div>';
  if (admin) loadUsers();
}
function rangeBox() { $('rangeBox').hidden = $('sMethod').value !== 'range'; }
$('sMethod').onchange = rangeBox;
$('sCopper').onclick = () => { $('sMethod').value = 'relative'; $('sDir').value = 'rise'; $('sThr').value = 8; $('sSet').value = 3; rangeBox(); };
$('sSave').onclick = async () => {
  const body = {}; Object.entries(SMAP).forEach(([id, k]) => body[k] = ['facility','method','direction'].includes(k) ? $(id).value : +$(id).value);
  try {
    S.settings = await api('PUT', '/api/settings', body); $('facility').textContent = S.settings.facility; drawLive();
    $('sMsg').innerHTML = '<div class="note ok">Saved. Every computer now uses these settings for new tests.</div>';
  } catch (e) { $('sMsg').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
};
async function loadUsers() {
  try {
    const d = await api('GET', '/api/users');
    $('uTable').innerHTML = '<thead><tr><th>Username</th><th>Name</th><th>Role</th></tr></thead><tbody>' +
      d.users.map(u => `<tr><td>${esc(u.username)}</td><td>${esc(u.full_name)}</td><td>${esc(u.role)}</td></tr>`).join('') + '</tbody>';
  } catch (e) { $('uMsg').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
}
$('uAdd').onclick = async () => {
  try {
    await api('POST', '/api/users', {username: $('uName').value.trim(), full_name: $('uFull').value.trim(), password: $('uPass').value, role: $('uRole').value});
    ['uName','uFull','uPass'].forEach(id => $(id).value = ''); $('uMsg').innerHTML = '<div class="note ok">User added.</div>'; loadUsers();
  } catch (e) { $('uMsg').innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
};
$('exportAll').onclick = () => download('/api/export.json', 'hemosense-export.json').catch(e => alert(e.message));

/* ------------------------------------------------ start */
resetTest(); connState();
if (S.token) enter().catch(e => { if (e.message !== 'Please sign in.') signOutLocal('Could not reach the server: ' + e.message); });
})();
