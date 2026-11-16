/* ChaosLab Online MVP: single-page app (vanilla JS, no build step).
   All data comes from the backend API; nothing is hard-coded except labels. */
'use strict';

/* ---------- safe templating: every interpolated value is HTML-escaped unless wrapped in raw() ---------- */
const ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
class Raw { constructor(s) { this.s = s; } }
const raw = s => new Raw(s);
const fmt = v => v instanceof Raw ? v.s : Array.isArray(v) ? v.map(fmt).join('') : String(v ?? '').replace(/[&<>"']/g, c => ESC[c]);
const h = (strs, ...vals) => raw(strs.reduce((o, s, i) => o + s + (i < vals.length ? fmt(vals[i]) : ''), ''));

/* ---------- state + api ---------- */
const S = {user: null, caps: null, cleanup: null};
const $app = document.getElementById('app');

async function api(method, path, body, isForm) {
  const opts = {method, headers: {}, credentials: 'same-origin'};
  if (body && !isForm) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  if (isForm) opts.body = body;
  let res;
  try { res = await fetch(path, opts); } catch (e) { throw new Error('Cannot reach the ChaosLab server.'); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (res.status === 401 && S.user && !path.startsWith('/api/auth/')) { S.user = null; location.hash = '#/login'; }
  if (!res.ok) { const err = new Error((data && data.error) || `Request failed (${res.status})`); err.status = res.status; throw err; }
  return data;
}

function toast(msg, kind) {
  const t = document.createElement('div');
  t.className = 'toast ' + (kind || '');
  t.textContent = msg;
  document.getElementById('toasts').appendChild(t);
  setTimeout(() => t.remove(), 5000);
}

/* ---------- formatting helpers ---------- */
const fmtDate = iso => iso ? new Date(iso).toLocaleString() : '';
const fmtTime = iso => iso ? new Date(iso).toLocaleTimeString() : '';
const fmtBytes = n => n == null ? '' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const num = (v, unit) => v == null ? raw('<span class="faint">n/a</span>') : `${v}${unit || ''}`;
const refOf = id => 'EXP-' + String(id).padStart(4, '0');
const FAULT_LABELS = {'pod-kill': 'Pod Kill', 'network-delay': 'Network Delay', 'network-failure': 'Network Failure'};

function statusBadge(status) {
  return h`<span class="badge b-${status}"><span class="dot"></span>${status}</span>`;
}
function modeBadge(simulated) {
  return simulated ? h`<span class="badge b-sim" title="Predefined demonstration data">SIMULATED</span>`
                   : h`<span class="badge b-live">LIVE</span>`;
}
function ruleBadge(label) {
  const cls = label === 'N/A' ? 'NA' : label;
  return h`<span class="badge b-${cls}">${label}</span>`;
}
function verdictBadge(v, simulated) {
  if (!v) return raw('<span class="faint">n/a</span>');
  return h`<span class="badge b-${v}">${v}</span>${simulated ? raw(' ') : ''}${simulated ? modeBadge(true) : ''}`;
}

/* ---------- router ---------- */
const NAV = [
  ['#/', 'Dashboard', '▦'], ['#/apps', 'Applications', '⬡'], ['#/experiments/new', 'New experiment', '⚡'],
  ['#/reports', 'Reports', '▤'], ['#/oracle', 'Verification Oracle', '✓'],
];

function shell(active, content) {
  return h`<div class="shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-icon">⚡</div><b>Chaos<span>Lab</span></b></div>
      <nav class="nav">${NAV.map(([href, label, icon]) =>
        h`<a href="${href}" class="${active === href ? 'active' : ''}"><span>${icon}</span>${label}</a>`)}</nav>
      <div class="spacer"></div>
      <div class="userbox"><b>${S.user.name}</b><span class="muted small">${S.user.email}</span><br>
        <button class="btn sm" style="margin-top:10px" data-action="logout">Sign out</button></div>
    </aside>
    <main class="main">${content}</main>
  </div>`;
}

function render(view) { $app.innerHTML = view.s; }

async function route() {
  if (S.cleanup) { S.cleanup(); S.cleanup = null; }
  const hash = location.hash || '#/';
  const [path, query] = hash.split('?');
  const qs = new URLSearchParams(query || '');

  if (S.user === null) {
    try { S.user = (await api('GET', '/api/auth/me')).user || false; } catch (e) { S.user = false; }
  }
  if (!S.user) {
    if (path === '#/register') return render(authView('register'));
    if (path !== '#/login') { location.hash = '#/login'; return; }
    return render(authView('login'));
  }
  if (path === '#/login' || path === '#/register') { location.hash = '#/'; return; }

  try {
    let m;
    if (path === '#/' || path === '') return await pageDashboard();
    if (path === '#/apps') return await pageApps();
    if ((m = path.match(/^#\/apps\/(\d+)$/))) return await pageAppDetail(+m[1]);
    if (path === '#/experiments/new') return await pageNewExperiment(qs.get('app'));
    if ((m = path.match(/^#\/experiments\/(\d+)$/))) return await pageMonitor(+m[1]);
    if (path === '#/reports') return await pageReports();
    if (path === '#/oracle') return await pageOracle(qs.get('exp'));
    render(shell('', h`<div class="empty">Page not found. <a href="#/">Back to dashboard</a></div>`));
  } catch (e) {
    render(shell('', h`<div class="banner err">${e.message}</div>`));
  }
}
window.addEventListener('hashchange', route);

/* ---------- auth ---------- */
function authView(mode) {
  const reg = mode === 'register';
  return h`<div class="auth-wrap"><div class="auth-card">
    <div class="brand"><div class="brand-icon">⚡</div><b>Chaos<span>Lab</span></b></div>
    <h1>${reg ? 'Create your account' : 'Welcome back'}</h1>
    <p class="muted" style="margin:4px 0 20px">${reg ? 'Register applications and verify their resilience.' : 'Sign in to run resilience experiments.'}</p>
    <div id="auth-error"></div>
    <form data-form="${mode}" novalidate>
      ${reg ? h`<div class="field"><label>Name</label><input type="text" name="name" autocomplete="name" required maxlength="80"></div>` : ''}
      <div class="field"><label>Email</label><input type="email" name="email" autocomplete="email" required></div>
      <div class="field"><label>Password</label><input type="password" name="password" autocomplete="${reg ? 'new-password' : 'current-password'}" required>
        ${reg ? h`<div class="hint">At least 8 characters.</div>` : ''}</div>
      <button class="btn primary" style="width:100%;justify-content:center" type="submit">${reg ? 'Create account' : 'Sign in'}</button>
    </form>
    <p class="muted small" style="margin-top:16px;text-align:center">${reg
      ? h`Already have an account? <a href="#/login">Sign in</a>` : h`New to ChaosLab? <a href="#/register">Create an account</a>`}</p>
  </div></div>`;
}

async function submitAuth(form, mode) {
  const f = Object.fromEntries(new FormData(form));
  const box = document.getElementById('auth-error');
  box.innerHTML = '';
  try {
    const data = await api('POST', '/api/auth/' + mode, f);
    S.user = data.user; S.caps = null;
    location.hash = '#/';
  } catch (e) {
    box.innerHTML = h`<div class="banner err" style="margin-bottom:14px">${e.message}</div>`.s;
  }
}

/* ---------- dashboard ---------- */
async function pageDashboard() {
  const d = await api('GET', '/api/dashboard');
  const t = d.totals;
  const stat = (l, v, s) => h`<div class="stat"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s || ''}</div></div>`;
  render(shell('#/', h`
    <div class="page-head"><div><h1>Dashboard</h1><p>Welcome back, ${S.user.name}. Your resilience activity at a glance.</p></div>
      <a class="btn primary" href="#/experiments/new">⚡ New experiment</a></div>
    <div class="grid g4" style="margin-bottom:16px">
      ${stat('Applications', t.applications, 'including the 3 built-in demos')}
      ${stat('Experiments', t.experiments, `${t.live} live · ${t.simulated} simulated`)}
      ${stat('Successful', t.successful, 'Oracle verdict PASS')}
      ${stat('Failed', t.failed, 'Oracle FAIL or execution error')}
    </div>
    <div class="grid g2" style="grid-template-columns:3fr 2fr">
      <div class="card"><div class="card-title"><h2>Recent experiment activity</h2><a href="#/reports" class="small">All reports</a></div>
        ${d.recent.length ? experimentsTable(d.recent) : h`<div class="empty">No experiments yet.<br><a class="btn primary sm" style="margin-top:10px" href="#/experiments/new">Run your first experiment</a></div>`}
      </div>
      <div class="card"><div class="card-title"><h2>Applications</h2><a href="#/apps" class="small">Manage</a></div>
        ${d.applications.map(a => h`<div class="svc-row" style="padding:8px 0;border-bottom:1px solid #1d2a42">
          <div><a href="#/apps/${a.id}"><b>${a.name}</b></a><div class="muted small">${a.is_builtin ? 'Built-in demo' : a.upload_type.toUpperCase() + ' upload'} · ${a.experiments_count} experiment(s)</div></div>
          ${statusBadge(a.status)}</div>`)}
      </div>
    </div>`));
  if (t.active) {   // refresh while something is running
    const timer = setTimeout(() => { if (location.hash === '#/' || location.hash === '') route(); }, 3000);
    S.cleanup = () => clearTimeout(timer);
  }
}

function experimentsTable(list, opts) {
  opts = opts || {};
  return h`<div class="table-wrap"><table><thead><tr><th>ID</th>${opts.noApp ? '' : h`<th>Application</th>`}<th>Target</th><th>Fault</th><th>Mode</th><th>Status</th><th>Oracle</th><th>Started</th></tr></thead><tbody>
    ${list.map(e => h`<tr class="click" data-href="#/experiments/${e.id}">
      <td class="mono"><a href="#/experiments/${e.id}">${refOf(e.id)}</a></td>
      ${opts.noApp ? '' : h`<td>${e.application_name}</td>`}
      <td class="mono">${e.target_service}</td><td>${e.fault_label || FAULT_LABELS[e.fault_type] || e.fault_type}</td>
      <td>${modeBadge(e.simulated)}</td><td>${statusBadge(e.status)}</td>
      <td>${e.oracle_verdict ? h`<span class="badge b-${e.oracle_verdict}">${e.oracle_verdict}</span>` : raw('<span class="faint">-</span>')}</td>
      <td class="muted small">${fmtDate(e.created_at)}</td></tr>`)}
  </tbody></table></div>`;
}

/* ---------- applications ---------- */
async function pageApps() {
  const {applications} = await api('GET', '/api/applications');
  render(shell('#/apps', h`
    <div class="page-head"><div><h1>Applications</h1><p>Register your own applications or use the built-in demos.</p></div></div>
    <div class="grid" style="grid-template-columns:3fr 2fr;align-items:start">
      <div class="card"><div class="card-title"><h2>Your applications</h2></div>
        <div class="table-wrap"><table><thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Uploaded</th><th></th></tr></thead><tbody>
        ${applications.map(a => h`<tr>
          <td><a href="#/apps/${a.id}"><b>${a.name}</b></a>${a.is_builtin ? h` <span class="badge b-ready">BUILT-IN</span>` : ''}
            <div class="muted small">${a.description ? a.description.slice(0, 90) : ''}</div></td>
          <td class="mono">${a.upload_type}${a.file_size != null ? h`<div class="faint">${fmtBytes(a.file_size)}</div>` : ''}</td>
          <td>${statusBadge(a.status)}</td><td class="muted small">${fmtDate(a.created_at)}</td>
          <td>${a.is_builtin ? '' : h`<button class="btn danger sm" data-action="delete-app" data-id="${a.id}" data-name="${a.name}">Delete</button>`}</td></tr>`)}
        </tbody></table></div></div>
      <div class="card"><div class="card-title"><h2>Upload an application</h2></div>
        <div id="upload-error"></div>
        <form data-form="upload" novalidate>
          <div class="field"><label>Application name</label><input type="text" name="name" maxlength="80" required></div>
          <div class="field"><label>Description</label><textarea name="description" rows="3" maxlength="500"></textarea></div>
          <div class="field"><label>Application file</label><input type="file" name="file" required>
            <div class="hint">.zip .tar .tar.gz .tgz .yaml .yml .json .md .txt · max 10 MB. Files are stored for registration only and are never executed.</div></div>
          <button class="btn primary" type="submit">Upload</button>
        </form>
        <div class="banner info" style="margin:16px 0 0">Experiment execution is available for the built-in demo applications. Uploaded applications are registered and stored only in this MVP.</div>
      </div>
    </div>`));
}

async function uploadApp(form) {
  const box = document.getElementById('upload-error');
  box.innerHTML = '';
  const file = form.file.files[0];
  const fail = m => { box.innerHTML = h`<div class="banner err">${m}</div>`.s; };
  if (!form.name.value.trim()) return fail('Please enter an application name.');
  if (!file) return fail('Please choose a file to upload.');
  if (file.size > 10 * 1048576) return fail('File is larger than the 10 MB limit.');
  const btn = form.querySelector('button'); btn.disabled = true;
  try {
    await api('POST', '/api/applications', new FormData(form), true);
    toast('Application uploaded.', 'ok');
    await pageApps();
  } catch (e) { fail(e.message); btn.disabled = false; }
}

async function pageAppDetail(id) {
  const {application: a} = await api('GET', `/api/applications/${id}`);
  const cat = a.catalog;
  render(shell('#/apps', h`
    <div class="page-head"><div><a href="#/apps" class="small">← Applications</a>
      <h1>${a.name} ${statusBadge(a.status)}</h1><p>${a.description || 'No description.'}</p></div>
      <div class="actions">${a.executable ? h`<a class="btn primary" href="#/experiments/new?app=${a.id}">⚡ Run experiment</a>`
        : h`<button class="btn danger" data-action="delete-app" data-id="${a.id}" data-name="${a.name}" data-then="apps">Delete</button>`}</div></div>
    <div class="grid g4" style="margin-bottom:16px">
      <div class="stat"><div class="l">Services</div><div class="v">${cat ? cat.services_count : 'n/a'}</div><div class="s">${cat ? (cat.discovery.source === 'cluster' ? 'discovered from the cluster' : 'static list (cluster unavailable)') : 'discovery not available in MVP'}</div></div>
      <div class="stat"><div class="l">Experiments</div><div class="v">${a.experiments_count}</div><div class="s">your runs on this app</div></div>
      <div class="stat"><div class="l">Fault types</div><div class="v">${cat ? cat.faults.length : 'n/a'}</div><div class="s">${cat ? 'pod kill, network delay, network failure' : ''}</div></div>
      <div class="stat"><div class="l">Registered</div><div class="v" style="font-size:16px;padding-top:8px">${fmtDate(a.created_at)}</div><div class="s">${a.is_builtin ? 'preconfigured' : a.original_filename + ' · ' + fmtBytes(a.file_size)}</div></div>
    </div>
    ${a.note ? h`<div class="banner info">${a.note}</div>` : ''}
    ${cat ? h`<div class="grid g2" style="align-items:start">
      <div class="card"><div class="card-title"><h2>Services (namespace <code>${cat.namespace}</code>)</h2></div>
        ${discoveryNote(cat.discovery, a.id)}
        <div class="table-wrap"><table><thead><tr><th>Service</th><th>Stack</th><th>Ready</th><th></th></tr></thead><tbody>
        ${cat.services.map(s => h`<tr><td class="mono">${s.name}<div class="muted small" style="font-family:inherit">${s.description}</div></td>
          <td class="small">${s.language} · ${s.protocol}</td>
          <td class="small">${s.ready != null ? h`<span class="badge ${s.ready >= 1 ? 'b-completed' : 'b-failed'}">${s.ready}/${s.replicas}</span>` : raw('<span class="faint">n/a</span>')}</td>
          <td>${s.targetable ? '' : h`<span class="badge b-NA">NOT TARGETABLE</span>`}</td></tr>`)}
        </tbody></table></div></div>
      <div>
        <div class="card"><div class="card-title"><h2>Available experiments</h2></div>
          ${cat.faults.map(f => h`<div style="padding:8px 0;border-bottom:1px solid #1d2a42"><b>${f.label}</b> <span class="mono faint">${f.chaos_kind}</span>
            <div class="muted small">${f.description}</div></div>`)}
          <h3 style="margin:14px 0 6px">Curated deep-verification experiments</h3>
          <div class="muted small" style="margin-bottom:6px">Existing ChaosLab YAML experiments with application probes and telemetry (used by Live runs).</div>
          ${cat.curated_experiments.map(c => h`<div class="small" style="padding:3px 0"><code>${c.service}</code> · ${c.fault}
            ${c.application_checks ? h`<span class="badge b-ready">APP PROBES</span>` : ''} ${c.telemetry_checks ? h`<span class="badge b-ready">TELEMETRY</span>` : ''}</div>`)}
        </div></div></div>` : ''}
    <div class="card"><div class="card-title"><h2>Experiment history</h2></div>
      ${a.experiments.length ? experimentsTable(a.experiments.map(e => ({...e, application_name: a.name, simulated: e.mode === 'simulated'})), {noApp: true})
        : h`<div class="empty">${a.executable ? 'No experiments yet on this application.' : 'Experiments cannot be run on uploaded applications in this MVP.'}</div>`}
    </div>`));
}

/* ---------- service discovery status ---------- */
function discoveryNote(d, refreshId) {
  const refresh = refreshId ? h` <button class="btn sm" data-action="refresh-services" data-id="${refreshId}">Refresh</button>` : '';
  return d.source === 'cluster'
    ? h`<div class="banner live" style="margin:10px 0 0">Services discovered from the Kubernetes cluster at ${fmtTime(d.discovered_at)}.${refresh}</div>`
    : h`<div class="banner info" style="margin:10px 0 0">Static service list (cluster discovery unavailable: ${d.error}). Live execution is disabled for this application.${refresh}</div>`;
}

/* ---------- new experiment ---------- */
async function pageNewExperiment(appParam) {
  const [{applications}, capsRes] = await Promise.all([api('GET', '/api/applications'), api('GET', '/api/capabilities')]);
  const builtins = applications.filter(a => a.is_builtin);
  const requested = appParam ? applications.find(a => a.id === +appParam) : null;
  let current = requested && requested.executable ? requested : builtins[0];
  const cats = {};
  const loadCat = async a => cats[a.id] || (cats[a.id] = (await api('GET', `/api/applications/${a.id}`)).application.catalog);
  let cat = await loadCat(current);
  const modes = capsRes.modes;
  const firstSvc = c => c.targetable_services.includes('paymentservice') ? 'paymentservice' : c.targetable_services[0];
  const form = {service: firstSvc(cat), fault: 'pod-kill', duration: 30, mode: 'simulated'};   // safe default: Live must be chosen deliberately

  const draw = () => {
    const fault = cat.faults.find(f => f.id === form.fault);
    const liveOk = modes.live.available && cat.discovery.source === 'cluster';
    const liveWhy = !modes.live.available ? modes.live.reason : 'This application was not found in the cluster (service list is static).';
    if (form.mode === 'live' && !liveOk) form.mode = 'simulated';
    const choice = (name, value, title, sub, selected, disabled) => h`<label class="choice ${selected ? 'sel' : ''} ${disabled ? 'disabled' : ''}">
      <input type="radio" name="${name}" value="${value}" ${selected ? 'checked' : ''} ${disabled ? 'disabled' : ''}>${title}<small>${sub}</small></label>`;
    render(shell('#/experiments/new', h`
      <div class="page-head"><div><h1>New experiment</h1><p>Choose a target and a fault, then review and run.</p></div></div>
      ${requested && !requested.executable ? h`<div class="banner info">“${requested.name}” is an uploaded application and cannot be executed in this MVP. Experiments run against a built-in demo application below.</div>` : ''}
      <div class="grid" style="grid-template-columns:2fr 1fr;align-items:start">
      <form data-form="experiment" id="exp-form">
        <div class="card"><h3><span class="step-n">1</span>Application</h3>
          <div class="choices" style="margin-top:10px">${builtins.map(a => choice('app', a.id, a.name, `${a.services_count} services · preconfigured`, a.id === current.id, false))}</div></div>
        <div class="card"><h3><span class="step-n">2</span>Target microservice</h3>
          ${discoveryNote(cat.discovery)}
          <div class="choices" style="margin-top:10px">${cat.services.filter(s => s.targetable).map(s =>
            choice('service', s.name, s.name, `${s.language} · ${s.protocol}${s.ready != null ? ` · ${s.ready}/${s.replicas} ready` : ''}`, form.service === s.name, false))}</div></div>
        <div class="card"><h3><span class="step-n">3</span>Fault type</h3>
          <div class="choices" style="margin-top:10px">${cat.faults.map(f => choice('fault', f.id, f.label, f.chaos_kind, form.fault === f.id, false))}</div>
          <div class="hint">${fault.description}</div></div>
        <div class="card"><h3><span class="step-n">4</span>Duration: <span class="mono" id="dur-val">${form.duration}s</span></h3>
          <input type="range" name="duration" min="10" max="120" step="5" value="${form.duration}" style="margin-top:10px">
          <div class="hint">${fault.duration_meaning}</div></div>
        <div class="card"><h3><span class="step-n">5</span>Execution mode</h3>
          <div class="choices" style="margin-top:10px;grid-template-columns:1fr 1fr">
            ${choice('mode', 'live', 'Live execution', liveOk ? 'Real Chaos Mesh fault on the cluster' : liveWhy, form.mode === 'live', !liveOk)}
            ${choice('mode', 'simulated', 'Demo execution (SIMULATED)', 'Predefined data, nothing is injected', form.mode === 'simulated', false)}
          </div>
          ${form.mode === 'live' ? h`<div class="banner live" style="margin:12px 0 0">Live mode really injects the fault into the shared demo cluster using Chaos Mesh. Only one live experiment can run at a time.</div>`
            : h`<div class="banner sim" style="margin:12px 0 0">Demo mode plays predefined results. No fault is injected and every metric and invariant is labelled SIMULATED.</div>`}
        </div>
      </form>
      <div class="card" style="position:sticky;top:20px"><h2>Review configuration</h2>
        <dl class="review"><dt>Application</dt><dd>${current.name}</dd><dt>Target microservice</dt><dd class="mono">${form.service}</dd>
          <dt>Fault</dt><dd>${fault.label}</dd><dt>Duration</dt><dd>${form.duration}s</dd>
          <dt>Execution mode</dt><dd>${form.mode === 'live' ? 'Live' : 'Demo (SIMULATED)'}</dd></dl>
        <div id="run-error" style="margin-top:12px"></div>
        <button class="btn primary" style="width:100%;justify-content:center;margin-top:14px" data-action="run-experiment">▶ Run experiment</button>
      </div></div>`));
    const f = document.getElementById('exp-form');
    f.addEventListener('change', async e => {
      const n = e.target.name;
      if (n === 'app') {
        current = builtins.find(a => a.id === +e.target.value);
        try { cat = await loadCat(current); } catch (err) { return toast(err.message, 'err'); }
        if (!cat.targetable_services.includes(form.service)) form.service = firstSvc(cat);
      }
      if (n === 'service') form.service = e.target.value;
      if (n === 'fault') form.fault = e.target.value;
      if (n === 'mode') form.mode = e.target.value;
      if (n === 'duration') form.duration = +e.target.value;
      draw();
    });
    f.querySelector('[name=duration]').addEventListener('input', e => {
      document.getElementById('dur-val').textContent = e.target.value + 's';
    });
    f.querySelector('[name=duration]').addEventListener('change', e => { form.duration = +e.target.value; });
  };
  draw();
  window.__runExperiment = async () => {
    const err = document.getElementById('run-error'); err.innerHTML = '';
    try {
      const r = await api('POST', '/api/experiments', {application_id: current.id, target_service: form.service,
        fault_type: form.fault, duration_seconds: form.duration, mode: form.mode});
      location.hash = `#/experiments/${r.experiment.id}`;
    } catch (e) { err.innerHTML = h`<div class="banner err">${e.message}</div>`.s; }
  };
}

/* ---------- oracle + charts (shared) ---------- */
const GROUP_TITLES = {infrastructure: 'Infrastructure verification', application: 'Application verification', telemetry: 'Telemetry verification'};

function oracleView(o, simulated) {
  if (!o) return h`<div class="empty">The Verification Oracle did not run for this experiment.</div>`;
  const c = o.counts;
  return h`
    <div class="verdict">Overall verdict ${verdictBadge(o.verdict, simulated)}</div>
    <div class="muted small" style="margin:6px 0 10px">${c.pass} PASS · ${c.fail} FAIL · ${c.skip} SKIP · ${c.na} N/A of ${c.total} invariants.
      SKIP and N/A are <b>not</b> counted as PASS: only ${c.executed} invariant(s) were actually executed.</div>
    ${o.failure_reasons.length ? h`<div class="banner err">${o.failure_reasons.join(' · ')}</div>` : ''}
    ${Object.keys(GROUP_TITLES).map(g => h`<div style="margin-top:14px"><h3>${GROUP_TITLES[g]}</h3>
      ${(o.groups[g] || []).map(r => h`<div class="rule">${ruleBadge(r.status_label)}<span class="name">${r.rule}${r.simulated ? raw(' ' + modeBadge(true).s) : ''}</span>
        <span class="msg">${r.message}</span>${r.observed != null ? h`<span class="obs">observed: ${r.observed} · expected: ${r.expected}</span>` : ''}</div>`)}</div>`)}`;
}

function lineChart(samples, key, title, color, simulated, maxOverride) {
  const pts = samples.filter(s => s[key] != null);
  if (pts.length < 2) return '';
  const W = 520, H = 120, P = 10;
  const tmax = Math.max(...pts.map(p => p.t), 1), vmax = maxOverride || Math.max(...pts.map(p => p[key]), 1);
  const xy = p => [P + (p.t / tmax) * (W - 2 * P), H - P - (p[key] / vmax) * (H - 2 * P)];
  const d = pts.map((p, i) => (i ? 'L' : 'M') + xy(p).map(n => n.toFixed(1)).join(',')).join(' ');
  return h`<div class="chart"><div class="small muted" style="margin-bottom:4px">${title}${simulated ? raw(' ' + modeBadge(true).s) : ''}
    <span class="faint"> · max ${Number(vmax.toFixed(2))}</span></div>
    <svg viewBox="0 0 ${W} ${H}"><path d="${d}" fill="none" stroke="${color}" stroke-width="2.2"/></svg></div>`;
}

/* ---------- monitoring ---------- */
const STAGE_ICONS = {baseline: '◎', fault_injection: '⚡', impact: '⚠', recovery: '↻', verification: '⌕', oracle: '✓', completed: '★'};

async function pageMonitor(id) {
  let seen = 0, events = [], timer = null, stopped = false;
  S.cleanup = () => { stopped = true; clearTimeout(timer); };

  const draw = (e) => {
    const sim = e.simulated;
    const stages = ['baseline', 'fault_injection', 'impact', 'recovery', 'verification', 'oracle', 'completed'];
    const labels = {baseline: 'Baseline', fault_injection: 'Fault injection', impact: 'Service impact', recovery: 'Recovery', verification: 'Probes & telemetry', oracle: 'Oracle', completed: 'Completed'};
    let cur = stages.indexOf(e.current_stage);
    if (e.status === 'failed') { const last = [...events].reverse().find(x => stages.includes(x.stage)); cur = last ? stages.indexOf(last.stage) : 0; }
    const stepState = i => e.status === 'completed' ? 'completed' : e.status === 'failed' ? (i < cur ? 'completed' : i === cur ? 'failed' : 'pending')
      : cur < 0 ? 'pending' : i < cur ? 'completed' : i === cur ? 'running' : 'pending';
    const samples = e.samples || [];
    const last = samples[samples.length - 1];
    const r = e.result, m = r ? r.metrics : null;
    const running = e.status === 'running' || e.status === 'pending';

    let health = '<span class="faint">waiting for first sample…</span>', recovery = 'Not started';
    if (last) {
      const ok = last.replicas_ready >= 1;
      health = h`<span class="badge ${ok ? 'b-completed' : 'b-failed'}">${ok ? 'HEALTHY' : 'DEGRADED'}</span> ${last.replicas_ready}${last.replicas_total != null ? '/' + last.replicas_total : ''} replica(s) Ready ${sim ? raw(modeBadge(true).s) : ''}`;
    }
    const idx = stages.indexOf(e.current_stage);
    if (e.status === 'failed') recovery = 'Not determined (execution failed)';
    else if (m && m.recovery_seconds != null) recovery = `Recovered in ${m.recovery_seconds}s`;
    else if (e.status === 'completed') recovery = 'Not measured';
    else if (idx >= 3) recovery = 'Recovering…';
    else if (idx >= 1) recovery = 'Fault active, awaiting recovery';

    render(shell('#/reports', h`
      <div class="page-head"><div><a href="#/reports" class="small">← Reports &amp; history</a>
        <h1>${refOf(e.id)} · ${e.target_service} ${statusBadge(e.status)} ${modeBadge(sim)}</h1>
        <p>${e.application_name} · ${e.fault_label} · ${e.duration_seconds}s · started ${fmtDate(e.started_at || e.created_at)}</p></div>
        <div class="actions">${e.reports.includes('html') ? h`
          <a class="btn" href="/api/experiments/${e.id}/report?format=html" target="_blank" rel="noopener">View report</a>
          <a class="btn" href="/api/experiments/${e.id}/report?format=html&download=1">Download HTML</a>
          <a class="btn" href="/api/experiments/${e.id}/report?format=json&download=1">Download JSON</a>` : ''}
          ${e.reports.includes('engine-html') ? h`<a class="btn" href="/api/experiments/${e.id}/report?format=engine-html" target="_blank" rel="noopener">Engine report</a>` : ''}
          <a class="btn" href="#/oracle?exp=${e.id}">Oracle view</a></div></div>
      ${sim ? h`<div class="banner sim">⚠ SIMULATED EXECUTION: all events, metrics and invariant outcomes on this page are predefined demonstration data. No fault was injected into Kubernetes.</div>`
            : h`<div class="banner live">LIVE EXECUTION: data below was collected by the ChaosLab engine from Chaos Mesh and Kubernetes.</div>`}
      ${e.status === 'failed' ? h`<div class="banner err"><div><b>Execution failed.</b> ${e.error || ''}<br><span class="small">The Oracle did not produce a verdict; this is not a pass.</span></div></div>` : ''}
      <div class="card ${sim ? 'sim-stripe' : ''}">
        <div class="card-title"><h2>Execution progress</h2><span class="mono muted">${e.status === 'completed' ? 100 : e.percent}%</span></div>
        <div class="stepper">${stages.map((s, i) => h`<div class="step ${stepState(i)} ${sim ? 'sim' : ''}"><span class="i">${STAGE_ICONS[s]}</span>${labels[s]}</div>`)}</div>
        <div class="progress ${sim ? 'sim' : ''} ${e.status === 'failed' ? 'failed' : e.status === 'completed' ? 'done' : ''}"><div style="width:${e.status === 'completed' ? 100 : e.percent}%"></div></div>
      </div>
      <div class="grid g3" style="margin-top:16px">
        <div class="stat"><div class="l">Service health</div><div style="margin-top:6px;font-size:13px">${health}</div>
          <div class="s">target <code>${e.target_service}</code></div></div>
        <div class="stat"><div class="l">Recovery status</div><div class="v" style="font-size:17px;padding-top:8px">${recovery}</div></div>
        <div class="stat"><div class="l">Oracle verification</div><div style="margin-top:6px">${e.oracle_verdict ? verdictBadge(e.oracle_verdict, sim) : (running ? statusBadge('running') : statusBadge(e.status))}</div>
          <div class="s">${r && r.oracle ? `${r.oracle.counts.executed} of ${r.oracle.counts.total} invariants executed` : ''}</div></div>
      </div>
      <div class="grid g2" style="margin-top:16px;align-items:start">
        <div class="card"><div class="card-title"><h2>Metrics</h2>${sim ? modeBadge(true) : ''}</div>
          <dl class="kv">
            <dt>Recovery time</dt><dd>${num(m && m.recovery_seconds, ' s')}</dd>
            <dt>Baseline p95 latency</dt><dd>${num(m && m.baseline_p95_ms, ' ms')}</dd>
            <dt>Peak p95 latency</dt><dd>${num(m && m.peak_p95_ms, ' ms')}</dd>
            <dt>Post-recovery p95</dt><dd>${num(m && m.post_recovery_p95_ms, ' ms')}</dd>
            <dt>Peak error rate</dt><dd>${num(m && m.peak_error_rate)}</dd>
            <dt>Error spans / traces</dt><dd>${num(m && m.error_spans)} / ${num(m && m.traces_count)}</dd></dl>
          ${r && !sim ? r.notes.slice(1).map(n => h`<div class="hint">${n}</div>`) : ''}
          <div class="grid" style="margin-top:14px">
            ${lineChart(samples, 'replicas_ready', 'Ready replicas over time (s)', '#34d399', sim)}
            ${lineChart(samples, 'p95_ms', 'p95 latency (ms) over time (s)', '#60a5fa', sim)}
            ${!sim && !samples.some(s => s.p95_ms != null) ? h`<div class="hint">Latency series are not collected for this Live experiment (requires Jaeger telemetry).</div>` : ''}
          </div></div>
        <div class="card"><div class="card-title"><h2>Execution timeline</h2></div>
          <div class="log" id="log">${events.length ? events.map(x => h`<div class="${x.level}"><span class="t">${fmtTime(x.ts)}</span><span>${x.message}</span></div>`) : h`<div class="faint">Waiting for events…</div>`}</div>
          ${r && r.timeline.length ? h`<table style="margin-top:12px"><thead><tr><th>Stage</th><th>Time</th></tr></thead><tbody>${r.timeline.map(t =>
            h`<tr><td>${t.label}</td><td class="muted small">${fmtTime(t.at)}${t.detail ? h`<div class="faint">${t.detail}</div>` : ''}</td></tr>`)}</tbody></table>` : ''}
        </div>
      </div>
      <div class="card" style="margin-top:16px"><div class="card-title"><h2>Verification Oracle</h2></div>
        ${r ? oracleView(r.oracle, sim) : h`<div class="empty">${running ? 'Oracle results appear when the run finishes…' : 'No Oracle results (execution failed).'}</div>`}</div>`));
    const log = document.getElementById('log'); if (log) log.scrollTop = log.scrollHeight;
  };

  const tick = async () => {
    if (stopped) return;
    try {
      const {experiment: e} = await api('GET', `/api/experiments/${id}?after_event=${seen}`);
      if (stopped) return;
      for (const ev of e.events) { events.push(ev); seen = Math.max(seen, ev.id); }
      draw(e);
      if (e.status === 'pending' || e.status === 'running') timer = setTimeout(tick, 1000);
    } catch (err) {
      if (stopped) return;
      if (err.status === 404) return render(shell('', h`<div class="banner err">Experiment not found.</div>`));
      timer = setTimeout(tick, 3000);
    }
  };
  await tick();
}

/* ---------- reports ---------- */
async function pageReports() {
  const [{reports}, {experiments}] = await Promise.all([api('GET', '/api/reports'), api('GET', '/api/experiments')]);
  const noReport = experiments.filter(e => !reports.some(r => r.experiment_id === e.id));
  render(shell('#/reports', h`
    <div class="page-head"><div><h1>Reports &amp; experiment history</h1><p>Every finished experiment produces a downloadable report. Simulated runs are always labelled.</p></div></div>
    <div class="card">${reports.length ? h`<div class="table-wrap"><table><thead><tr><th>Experiment</th><th>Application</th><th>Target</th><th>Fault</th><th>Mode</th><th>Verdict</th><th>Generated</th><th>Download</th></tr></thead><tbody>
      ${reports.map(r => h`<tr>
        <td class="mono"><a href="#/experiments/${r.experiment_id}">${r.experiment_ref}</a></td><td>${r.application_name}</td>
        <td class="mono">${r.target_service}</td><td>${FAULT_LABELS[r.fault_type] || r.fault_type}</td><td>${modeBadge(r.simulated)}</td>
        <td>${r.overall_result === 'NOT_EVALUATED' ? h`<span class="badge b-NA" title="Execution failed before the Oracle ran">NOT EVALUATED</span>` : h`<span class="badge b-${r.overall_result}">${r.overall_result}</span>`}</td>
        <td class="muted small">${fmtDate(r.created_at)}</td>
        <td><div class="actions">
          <a class="btn sm" href="/api/experiments/${r.experiment_id}/report?format=html" target="_blank" rel="noopener">View</a>
          <a class="btn sm" href="/api/experiments/${r.experiment_id}/report?format=html&download=1">HTML</a>
          <a class="btn sm" href="/api/experiments/${r.experiment_id}/report?format=json&download=1">JSON</a></div></td></tr>`)}
      </tbody></table></div>` : h`<div class="empty">No reports yet. <a href="#/experiments/new">Run an experiment</a> to generate one.</div>`}</div>
    ${noReport.length ? h`<div class="card"><div class="card-title"><h2>In progress</h2></div>${experimentsTable(noReport)}</div>` : ''}`));
}

/* ---------- oracle page ---------- */
async function pageOracle(expParam) {
  const [cat, {experiments}] = await Promise.all([api('GET', '/api/oracle/invariants'), api('GET', '/api/experiments')]);
  const done = experiments.filter(e => e.status === 'completed' || e.status === 'failed');
  let selected = expParam ? +expParam : (done[0] && done[0].id);
  let detail = null;
  if (selected) { try { detail = (await api('GET', `/api/experiments/${selected}`)).experiment; } catch (e) { detail = null; } }
  render(shell('#/oracle', h`
    <div class="page-head"><div><h1>Verification Oracle</h1><p>Declarative invariants evaluated against collected evidence. The same Oracle verifies Live and Simulated runs.</p></div></div>
    <div class="grid g2" style="align-items:start">
      <div class="card"><div class="card-title"><h2>Invariant catalog</h2></div>
        ${cat.groups.map(g => h`<div style="margin-bottom:16px"><h3>${GROUP_TITLES[g.id] || g.title}</h3>
          ${g.invariants.map(i => h`<div class="rule"><span class="badge b-ready">INVARIANT</span><span class="name">${i.name}</span><span class="msg">${i.description}</span></div>`)}
          <div class="small muted" style="margin-top:6px"><b>Live:</b> ${g.availability.live}<br><b>Simulated:</b> ${g.availability.simulated}</div></div>`)}
        <h3>Statuses</h3>${cat.statuses.map(s => h`<div class="rule">${ruleBadge(s.status)}<span class="msg" style="grid-column:2">${s.meaning}</span></div>`)}
      </div>
      <div class="card"><div class="card-title"><h2>Experiment results</h2></div>
        ${done.length ? h`<div class="field"><label>Experiment</label><select id="oracle-select">${done.map(e =>
          h`<option value="${e.id}" ${e.id === selected ? 'selected' : ''}>${refOf(e.id)} · ${e.target_service} · ${e.fault_label} · ${e.simulated ? 'SIMULATED' : 'LIVE'}</option>`)}</select></div>`
          : h`<div class="empty">Run an experiment to see Oracle results here.</div>`}
        ${detail ? h`${detail.simulated ? h`<div class="banner sim">⚠ SIMULATED: invariant outcomes are evaluated on predefined evidence.</div>` : ''}
          ${detail.result ? oracleView(detail.result.oracle, detail.simulated) : h`<div class="banner err">Execution failed; the Oracle did not run. ${detail.error || ''}</div>`}` : ''}
      </div></div>`));
  const sel = document.getElementById('oracle-select');
  if (sel) sel.addEventListener('change', () => { location.hash = `#/oracle?exp=${sel.value}`; });
}

/* ---------- global event delegation ---------- */
document.addEventListener('click', async ev => {
  const row = ev.target.closest('tr[data-href]');
  if (row && !ev.target.closest('a,button')) { location.hash = row.dataset.href; return; }
  const el = ev.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'logout') {
    await api('POST', '/api/auth/logout', {}).catch(() => {});
    S.user = false; location.hash = '#/login'; route();
  } else if (action === 'delete-app') {
    if (!confirm(`Delete “${el.dataset.name}” and its stored file? This cannot be undone.`)) return;
    try {
      await api('DELETE', `/api/applications/${el.dataset.id}`);
      toast('Application deleted.', 'ok');
      if (el.dataset.then === 'apps') location.hash = '#/apps'; else await pageApps();
    } catch (e) { toast(e.message, 'err'); }
  } else if (action === 'refresh-services') {
    el.disabled = true;
    try {
      const {application} = await api('GET', `/api/applications/${el.dataset.id}?refresh=1`);
      toast(`Re-discovered services (${application.catalog.discovery.source}).`, 'ok');
      await pageAppDetail(+el.dataset.id);
    } catch (e) { toast(e.message, 'err'); el.disabled = false; }
  } else if (action === 'run-experiment') {
    el.disabled = true;
    await window.__runExperiment();
    el.disabled = false;
  }
});

document.addEventListener('submit', ev => {
  const form = ev.target.closest('form[data-form]');
  if (!form) return;
  ev.preventDefault();
  const kind = form.dataset.form;
  if (kind === 'login' || kind === 'register') submitAuth(form, kind);
  else if (kind === 'upload') uploadApp(form);
  else if (kind === 'experiment') window.__runExperiment && window.__runExperiment();
});

route();
