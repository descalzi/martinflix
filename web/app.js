'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const icon = (name, extra = '') => `<svg class="icon${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const page = document.body.dataset.page;
const pageTitles = {overview: 'Overview', live: 'Live', library: 'Library', discover: 'Discover', add: 'Add to library', services: 'Services', backups: 'Backups', settings: 'Settings'};
const legacyAnchors = {'#series': '/library#series', '#films': '/library#films', '#library': '/library', '#backups': '/backups', '#services': '/services'};
if (page === 'overview' && legacyAnchors[window.location.hash]) window.location.replace(legacyAnchors[window.location.hash]);
$('#page-title').textContent = pageTitles[page];
document.title = page === 'overview' ? 'MartinFlix' : `${pageTitles[page]} · MartinFlix`;
// Add lives under Library, so Library stays highlighted there.
if (page === 'add') document.querySelector('[data-nav="library"]').setAttribute('aria-current', 'true');
else document.querySelector(`[data-nav="${page}"]`)?.setAttribute('aria-current', 'page');
for (const section of document.querySelectorAll('.page')) section.hidden = section.dataset.for !== page;

let catalog = {apps: {}, branches: {}, uses: {}, groupRest: {}};
const appInfo = name => catalog.apps[name] ?? {name, role: 'Container'};

function serviceIsUp(service) {
  return ['running', 'reachable'].includes(service.state);
}

function needsAttention(service) {
  return !serviceIsUp(service) || service.status.toLowerCase().includes('unhealthy');
}

function serviceState(service) {
  const unhealthy = service.status.toLowerCase().includes('unhealthy');
  const state = unhealthy ? 'unhealthy' : service.state;
  const label = state === 'exited' ? 'Stopped' : state === 'missing' ? 'Missing' : state.charAt(0).toUpperCase() + state.slice(1);
  const optional = appInfo(service.name).optional;
  const tone = serviceIsUp({state}) ? 'ok' : ['unhealthy', 'exited', 'unreachable', 'dead'].includes(state) ? 'crit'
    : state === 'missing' && optional ? 'muted' : 'warn';
  return {state, label, tone};
}

function healthIssues(service) {
  const health = service.health;
  if (!health) return [];
  if (health.state === 'unavailable') return [{severity: 'warning', message: 'App health check did not respond'}];
  return (health.alerts || []).filter(alert => alert.severity !== 'info');
}

function serviceHref(service) {
  const web = service.web;
  if (!web) return '';
  const host = window.location.hostname;
  if (web.localOnly && !['localhost', '127.0.0.1', '[::1]'].includes(host)) return '';
  return `${web.scheme}://${host}:${web.port}${web.path}`;
}

function appHealth(service) {
  const health = service.health;
  if (!health) return '';
  if (health.state !== 'available') {
    const unavailable = health.state === 'unavailable';
    return `<div class="app-health"><span class="badge ${unavailable ? 'warn' : 'muted'}">${unavailable ? 'App health unavailable' : 'App health not connected'}</span></div>`;
  }
  // The status chip already says the app is fine, so only problems get a line of their own.
  if (!health.alerts.length) return '';
  return `<ul class="app-health">${health.alerts.map(({severity, message}) => {
    const tone = severity === 'error' ? 'crit' : severity === 'info' ? 'muted' : 'warn';
    const label = severity === 'error' ? 'Error' : severity === 'info' ? 'Info' : 'Warning';
    return `<li><span class="badge ${tone}">${label}</span><span>${esc(message)}</span></li>`;
  }).join('')}</ul>`;
}

function appIcon(name) {
  const app = appInfo(name);
  return `<span class="app-icon" aria-hidden="true">${app.icon
    ? `<img src="/assets/${esc(app.icon)}.webp" width="28" height="28" alt="" loading="lazy">`
    : esc(app.name.slice(0, 2).toUpperCase())}</span>`;
}

/** "3 days, 4 hours", "5 hours, 12 minutes", "12 minutes" or "less than a minute". */
function durationSince(stamp) {
  const seconds = Math.max(0, (Date.now() - new Date(stamp)) / 1000);
  const plural = (count, unit) => `${count} ${unit}${count === 1 ? '' : 's'}`;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds % 86400 / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  if (days) return hours ? `${plural(days, 'day')}, ${plural(hours, 'hour')}` : plural(days, 'day');
  if (hours) return minutes ? `${plural(hours, 'hour')}, ${plural(minutes, 'minute')}` : plural(hours, 'hour');
  return minutes ? plural(minutes, 'minute') : 'less than a minute';
}

/** "2d 4h", "3h 49m", "12m" or "<1m": short enough for a status chip. */
function shortDuration(stamp) {
  const minutes = Math.floor(Math.max(0, (Date.now() - new Date(stamp)) / 1000) / 60);
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor(minutes % 1440 / 60);
  if (days) return hours ? `${days}d ${hours}h` : `${days}d`;
  if (hours) return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`;
  return minutes ? `${minutes}m` : '<1m';
}

/** One chip per card: the uptime while an app is up, otherwise what is wrong with it. */
function statusChip(service) {
  const {label, tone} = serviceState(service);
  const starting = /\(health: starting\)/.test(service.status || '');
  let text = label;
  let tip = service.status || label;
  if (tone === 'ok' && starting) {
    return `<span class="badge warn service-chip" title="${esc(tip)}">Starting</span>`;
  }
  if (tone === 'ok') {
    if (service.startedAt && !isNaN(new Date(service.startedAt))) {
      text = `Up ${shortDuration(service.startedAt)}`;
      tip = `Up ${durationSince(service.startedAt)}`;
    } else if (service.uptime) {
      text = `Up ${service.uptime.charAt(0).toLowerCase()}${service.uptime.slice(1)}`;
    } else {
      text = 'Online';
      tip = 'Responds to health checks. Its uptime is not reported.';
    }
  }
  return `<span class="badge ${tone} service-chip" title="${esc(tip)}">${esc(text)}</span>`;
}

function relatedNames(names) {
  const list = names.map(name => appInfo(name).name);
  return list.length > 1 ? `${list.slice(0, -1).join(', ')} and ${list.at(-1)}` : list[0];
}

function serviceCard(service, groupServices) {
  const app = appInfo(service.name);
  const href = serviceHref(service);
  const title = href
    ? `<a class="service-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(app.name)}${icon('external', ' icon-small')}<span class="sr-only"> (opens in a new tab)</span></a>`
    : esc(app.name);
  const {tone} = serviceState(service);
  const uses = (catalog.uses?.[service.name] ?? []).filter(name => groupServices.some(item => item.name === name));
  const unavailable = uses.filter(name => {
    const dependency = groupServices.find(item => item.name === name);
    return dependency && needsAttention(dependency);
  });
  const localOnly = service.web?.localOnly && !href;
  return `<li class="service-card tone-${tone}">
    <div class="service-identity">
      ${appIcon(service.name)}
      <div><h4>${title}</h4><p>${esc(app.role)}</p></div>
      ${statusChip(service)}
    </div>
    ${tone === 'ok' || !service.status ? '' : `<p class="service-detail">${esc(service.status)}</p>`}
    ${appHealth(service)}
    ${localOnly ? '<p class="service-note">Its web page only opens on the host itself. Rerun setup with your LAN address to reach it from here.</p>' : ''}
    ${!service.web && app.note ? `<p class="service-note">${esc(app.note)}</p>` : ''}
    ${uses.length ? `<p class="service-note">Uses ${esc(relatedNames(uses))}</p>` : ''}
    ${unavailable.length ? `<p class="service-warning">${icon('alert', ' icon-small')}Check ${esc(relatedNames(unavailable))}</p>` : ''}
    ${controlButtons(service)}
  </li>`;
}

/** Restart and Update, when the controller can act on this app. */
function controlButtons(service, extra = '') {
  if (!service.control) return '';
  const name = esc(service.name);
  return `<div class="service-actions">
    ${extra}
    <button class="button button-quiet button-small" type="button" data-control="restart" data-service="${name}">${icon('refresh')}${serviceIsUp(service) ? 'Restart' : 'Start'}</button>
    ${service.control.canUpdate === false ? '' : `<button class="button button-quiet button-small" type="button" data-control="update" data-service="${name}">${icon('up')}Update</button>`}
  </div>`;
}

/* The VPN test: where the downloads really leave from, and whether that is through the VPN. */
let vpnCheck = null;
let vpnChecking = false;

function placeName(place) {
  if (!place) return '';
  let country = place.country || '';
  try { country = new Intl.DisplayNames(undefined, {type: 'region'}).of(country) || country; } catch { /* keep the code */ }
  return [place.city, country].filter(Boolean).join(', ');
}

function vpnResult() {
  if (vpnChecking) return `<div class="vpn-result" role="status">${icon('refresh')}<span>Checking where downloads go out…</span></div>`;
  if (!vpnCheck) return '';
  const check = vpnCheck;
  // The controller names Compose services; the catalog knows two of them by their older names.
  const catalogName = name => ({gluetun: 'nordvpn', transmission: 'transmission-daemon'})[name] || name;
  const app = appInfo(catalogName(check.probe)).name;
  const when = new Date(check.checkedAt).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
  const exit = check.exit;
  const home = check.home ? `your own connection${check.home.ip ? ` (${check.home.ip}${check.home.org ? `, ${check.home.org.replace(/^AS\d+ /, '')}` : ''})` : ''}` : 'your own connection';
  const direct = check.direct?.length ? ` ${check.direct.map(name => appInfo(catalogName(name)).name).join(' and ')} ${check.direct.length === 1 ? 'goes' : 'go'} out directly, as chosen at setup.` : '';
  const lines = {
    protected: ['ok', 'tick', `Protected: ${app} goes out through the VPN from ${placeName(exit)} (${exit?.ip}${exit?.org ? `, ${exit.org.replace(/^AS\d+ /, '')}` : ''}), not ${home}.${direct}`],
    exposed: ['crit', 'alert', `Not protected: ${app} goes out on ${home}, not through the VPN. Stop downloads and restart Gluetun.`],
    down: ['warn', 'alert', `The VPN is down: ${check.problem || 'Gluetun isn’t connected.'} Downloads behind it are blocked until it reconnects, so nothing goes out unprotected.`],
  }[check.verdict] || ['warn', 'alert', check.error || 'The VPN check didn’t finish.'];
  return `<div class="vpn-result tone-${lines[0]}" role="status">${icon(lines[1])}<span>${esc(lines[2])} <span class="muted">Checked ${esc(when)}.</span></span></div>`;
}

async function testVpn() {
  vpnChecking = true;
  document.querySelectorAll('.vpn-slot').forEach(slot => { slot.innerHTML = vpnResult(); });
  document.querySelectorAll('[data-vpn-test]').forEach(button => { button.disabled = true; });
  try {
    vpnCheck = await post('/api/vpn-test', {});
  } catch (error) {
    vpnCheck = {verdict: null, error: error.message, checkedAt: new Date().toISOString()};
  }
  vpnChecking = false;
  document.querySelectorAll('.vpn-slot').forEach(slot => { slot.innerHTML = vpnResult(); });
  document.querySelectorAll('[data-vpn-test]').forEach(button => { button.disabled = false; });
}

/** The service that others share, such as Gluetun for the VPN apps: a card spanning the section it heads. */
function gatewayBar(service) {
  const app = appInfo(service.name);
  const vpn = service.name === 'nordvpn' && service.control;
  const test = vpn ? `<button class="button button-quiet button-small" type="button" data-vpn-test${vpnChecking ? ' disabled' : ''}>${icon('shield')}Test VPN</button>` : '';
  return `<div class="gateway service-card tone-${serviceState(service).tone}">
    ${appIcon(service.name)}
    <div class="gateway-name"><h4>${esc(app.name)}</h4><p>${esc(app.role)}</p></div>
    ${statusChip(service)}
    ${controlButtons(service, test)}
    ${vpn ? `<div class="vpn-slot">${vpnResult()}</div>` : ''}
  </div>`;
}

function serviceSection(title, description, cards, gateway = '') {
  return `<div class="service-section">
    <div class="section-head"><h4>${esc(title)}</h4>${description ? `<p>${esc(description)}</p>` : ''}</div>
    ${gateway}
    <ul class="service-grid">${cards}</ul>
  </div>`;
}

function serviceGroup(group, countLabel, showVpnBranch) {
  const services = group.services;
  const running = services.filter(serviceIsUp).length;
  const nested = new Set();
  const sections = services.flatMap(parent => {
    const branch = parent.name === 'nordvpn' && !showVpnBranch ? null : catalog.branches[parent.name];
    if (!branch) return [];
    const children = services.filter(service => branch.members.includes(service.name));
    if (!children.length) return [];
    nested.add(parent.name);
    children.forEach(child => nested.add(child.name));
    return [serviceSection(branch.title, branch.description,
      children.map(child => serviceCard(child, services)).join(''), gatewayBar(parent))];
  });
  const standalone = services.filter(service => !nested.has(service.name));
  if (standalone.length) {
    const cards = standalone.map(service => serviceCard(service, services)).join('');
    const rest = catalog.groupRest?.[group.name];
    sections.push(sections.length && rest ? serviceSection(rest.title, rest.description, cards)
      : `<ul class="service-grid">${cards}</ul>`);
  }
  const compact = nested.size === 0 && services.length <= 2;
  return `<section class="service-group${compact ? ' is-compact' : ''}">
    <div class="group-title"><h3>${esc(group.name)}</h3><span>${running} of ${services.length} ${countLabel}</span></div>
    ${sections.join('')}
  </section>`;
}

function attentionRows(docker, services) {
  const rows = [];
  if (!docker.available) rows.push({tone: 'crit', name: 'Service status', message: docker.error || 'Status unavailable'});
  for (const service of services) {
    if (appInfo(service.name).optional) continue;
    const {label, tone} = serviceState(service);
    if (tone !== 'ok') rows.push({tone, name: appInfo(service.name).name, message: label.toLowerCase()});
    for (const issue of healthIssues(service)) {
      rows.push({tone: issue.severity === 'error' ? 'crit' : 'warn', name: appInfo(service.name).name,
        message: issue.severity === 'error' ? 'has an error' : 'has a warning'});
    }
  }
  return rows;
}

function renderOverview(docker, services, backups, probes) {
  const required = services.filter(service => !appInfo(service.name).optional);
  const rows = attentionRows(docker, services);
  const summary = $('#service-summary');
  summary.classList.toggle('is-alert', rows.some(row => row.tone === 'crit'));
  summary.classList.toggle('is-warn', rows.length > 0 && !rows.some(row => row.tone === 'crit'));
  $('#services-metric-label').textContent = probes ? 'Services reachable' : 'Services running';
  $('#running').textContent = docker.available ? `${required.filter(serviceIsUp).length} of ${required.length}` : 'Unavailable';
  // Name the problems so the tile is enough to act on; the link opens the full picture.
  const named = rows.slice(0, 2).map(row => `${row.name} ${row.message}`);
  if (rows.length > 2) named.push(`${rows.length - 2} more`);
  $('#services-summary-detail').textContent = rows.length ? named.join(' · ') : 'Everything required is up';
  summary.setAttribute('aria-label', rows.length ? `Services: ${rows.length} need attention. Open services.` : 'Services: all up. Open services.');

  const latest = backups[0];
  const incomplete = latest && (latest.errors?.length || latest.missing?.length);
  $('#backup-summary').classList.toggle('is-warn', Boolean(incomplete));
  $('#latest-backup').textContent = latest ? relativeDay(latest.created) : 'None yet';
  $('#backup-detail').textContent = latest ? (incomplete ? `Incomplete · missing ${[...(latest.missing || []), ...(latest.errors || [])].join(', ')}` : 'Captured · restore untested') : 'No snapshots found';
}

const BACKUP_APPS = {sonarr: 'Sonarr', radarr: 'Radarr', prowlarr: 'Prowlarr', bazarr: 'Bazarr', transmission: 'Transmission',
  audiobookshelf: 'Audiobookshelf', listenarr: 'Listenarr',
  plex: 'Plex', martinflix: 'MartinFlix'};

function renderBackups(backups, schedule, problem) {
  const running = schedule?.running;
  $('#backup-now').hidden = !schedule;
  $('#backup-now').disabled = Boolean(running);
  const next = schedule?.next ? new Date(schedule.next) : null;
  $('#backup-schedule').textContent = !schedule ? (problem || 'Scheduled backups need the controller.')
    : running ? `Backing up now, started ${new Date(running).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'})}…`
    : `Next backup ${next ? next.toLocaleString(undefined, {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) : 'tonight'} · Saved in ${schedule.folder}`;
  if (schedule?.lastError && !running) {
    $('#backup-schedule').textContent += ` · Last problem: ${schedule.lastError}`;
  }
  $('#backups-content').innerHTML = backups.length
    ? `<table class="table"><thead><tr><th scope="col">Taken</th><th scope="col">Contents</th><th scope="col" class="num">Size</th><th scope="col">Result</th>${schedule ? '<th scope="col"><span class="sr-only">Restore</span></th>' : ''}</tr></thead><tbody>${
      backups.map(backup => {
        const problems = backup.errors?.length ? backup.errors : (backup.missing || []);
        const apps = (backup.apps || []).map(name => BACKUP_APPS[name] || name).join(', ');
        const kind = backup.kind === 'manual' ? ' <span class="muted">· on request</span>' : '';
        return `<tr><td>${esc(date(backup.created))}${kind}</td><td>${esc(apps || `${backup.files ?? 0} archives`)}</td>
          <td class="num">${esc(backup.size == null ? '—' : bytesLabel(backup.size))}</td><td>${problems.length
          ? `<span class="badge warn">Incomplete</span> <span class="muted">${esc(problems.join('; '))}</span>`
          : '<span class="badge ok">Complete</span>'}</td>${schedule ? `<td class="row-action">${(backup.apps || []).length
          ? `<button class="button button-quiet button-small" type="button" data-control="restore" data-backup="${esc(backup.id)}" data-created="${esc(backup.created || '')}"${running ? ' disabled' : ''}>Restore</button>` : ''}</td>` : ''}</tr>`;
      }).join('')}</tbody></table>`
    : `<div class="empty-state"><h3>No backups yet</h3><p>${esc(schedule ? 'The first one runs tonight at 01:00, or use Back up now.' : problem || 'Backups start once the controller answers.')}</p></div>`;
  // While a backup runs, check back until it's done.
  clearTimeout(renderBackups.timer);
  if (running) renderBackups.timer = setTimeout(status, 5000);
}

const servicesPage = page === 'services';

const controlServices = new Map();

function controlText(action, service) {
  const name = appInfo(service.name).name;
  const followers = service.control.followers ?? [];
  const after = followers.length ? relatedNames(followers) : '';
  if (action === 'restart') {
    const verb = serviceIsUp(service) ? 'Restart' : 'Start';
    const own = {
      dashboard: 'This page stops for a few seconds, then reloads.',
      controller: 'Restarts and updates are unavailable for a few seconds while it comes back.',
    }[service.name];
    return {
      title: `${verb} ${name}?`, verb,
      detail: own ? own : after ? `${after} restart${followers.length === 1 ? 's' : ''} after it, because ${followers.length === 1 ? 'it uses' : 'they use'} its connection. Downloads pause for a minute or so.`
        : verb === 'Start' ? `Starts the ${name} container again.` : `${name} is unavailable for a few seconds while it restarts.`,
      busy: `${verb === 'Start' ? 'Starting' : 'Restarting'}…`,
    };
  }
  return {
    title: `Update ${name}?`, verb: 'Update',
    detail: `Downloads the newest ${name} image and, if there is one, recreates ${name} with it${after ? `, then ${after}` : ''}. Your settings stay. This can take a few minutes.`,
    busy: 'Updating…',
  };
}

/** After a dashboard restart: wait until it answers again, then reload so the page talks to the new one. */
async function waitForDashboard() {
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  await pause(3000);
  for (let tries = 0; tries < 60; tries += 1) {
    try {
      // Any answer, even "sign in again", means it is back.
      await fetch('/api/status', {cache: 'no-store'});
      window.location.reload();
      return;
    } catch {
      await pause(2000);
    }
  }
  $('#control-notice').textContent = 'The dashboard has not come back yet. Reload this page in a moment.';
}

async function post(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (response.status === 401 && response.headers.get('X-MartinFlix-Login') === 'required') window.location.assign('/login');
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.error || 'The request failed. Try again.');
  return data;
}

/** The PIN dialog, shared by every action that needs it. `send(pin)` does the action; `after(data)` runs once it worked. */
let pinRequest = null;
function askPin(text, send, after) {
  pinRequest = {text, send, after};
  $('#control-title').textContent = text.title;
  $('#control-detail').textContent = text.detail;
  $('#control-pin').value = '';
  $('#control-error').hidden = true;
  setPinBusy(false);
  $('#control-dialog').showModal();
  $('#control-pin').focus();
}

let pinBusy = false;
function setPinBusy(state) {
  pinBusy = state;
  const confirm = $('#control-confirm');
  $('#control-pin').disabled = state;
  $('#control-cancel').disabled = state;
  confirm.setAttribute('aria-disabled', String(state));
  confirm.classList.toggle('is-busy', state);
  confirm.innerHTML = `${icon(pinRequest.text.icon || 'up')}${esc(state ? pinRequest.text.busy : pinRequest.text.verb)}`;
}

function setupControls() {
  const dialog = $('#control-dialog');
  const pin = $('#control-pin');
  const error = $('#control-error');
  document.addEventListener('click', event => {
    if (event.target.closest('[data-vpn-test]:not(:disabled)')) {
      testVpn();
      return;
    }
    const button = event.target.closest('[data-control]');
    if (!button || button.disabled) return;
    const action = button.dataset.control;
    if (action === 'backup') {
      askPin({title: 'Back up now?', verb: 'Back up', busy: 'Starting…', icon: 'plus',
        detail: 'Takes a backup of every app now, alongside the nightly ones. The apps keep running; it takes a few minutes.'},
      code => post('/api/backup', {pin: code}),
      data => { $('#backup-notice').textContent = data.message; $('#backup-notice').hidden = false; status(); });
      return;
    }
    if (action === 'release') {
      startUpdate();
      return;
    }
    if (action === 'setup-again') {
      setupAgain();
      return;
    }
    if (action === 'restore') {
      startRestore(button.dataset.backup, button.dataset.created);
      return;
    }
    const service = controlServices.get(button.dataset.service);
    if (!service) return;
    const text = {...controlText(action, service), icon: action === 'restart' ? 'refresh' : 'up'};
    askPin(text, code => post(`/api/control/${action}`, {service: service.name, pin: code}), data => {
      $('#control-notice').textContent = data.message;
      $('#control-notice').hidden = false;
      if (service.name === 'dashboard') {
        waitForDashboard();
        return;
      }
      status();
      // Apps take a little while to report healthy again after a restart.
      setTimeout(status, 15000);
    });
  });
  $('#control-cancel').addEventListener('click', () => dialog.close());
  // A running action cannot be stopped, so Escape waits for it like the Cancel button does.
  dialog.addEventListener('cancel', event => { if (pinBusy) event.preventDefault(); });
  $('#control-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (pinBusy || !pinRequest) return;
    if (!/^[0-9]{4,8}$/.test(pin.value)) {
      error.textContent = 'Enter your 4 to 8 digit PIN.';
      error.hidden = false;
      pin.focus();
      return;
    }
    error.hidden = true;
    setPinBusy(true);
    let data;
    try {
      data = await pinRequest.send(pin.value);
    } catch (failure) {
      setPinBusy(false);
      error.textContent = failure.message;
      error.hidden = false;
      pin.value = '';
      pin.focus();
      return;
    }
    pinBusy = false;
    dialog.close();
    // Outside the try: a mistake in what follows must show as one, not as a wrong PIN in a closed dialog.
    pinRequest.after?.(data);
  });
}

async function get(url) {
  const response = await fetch(url, {cache: 'no-store'});
  if (response.status === 401 && response.headers.get('X-MartinFlix-Login') === 'required') window.location.assign('/login');
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.error || 'Request failed');
  return data;
}

function date(value) {
  const stamp = new Date(value);
  return isNaN(stamp) ? 'Unknown' : stamp.toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});
}

function dayOffset(stamp) {
  return Math.round((new Date(stamp).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86400000);
}

function relativeDay(value) {
  const stamp = new Date(value);
  if (isNaN(stamp)) return 'Unknown';
  const days = -dayOffset(stamp);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days} days ago`;
  return stamp.toLocaleDateString(undefined, {day: 'numeric', month: 'short'});
}

/** "Today", "Tomorrow", "Thu 26 Nov", or with the year when it isn't this year. */
function upcomingDay(stamp, options = {}) {
  const days = dayOffset(stamp);
  if (days === 0) return 'Today';
  if (days === 1) return 'Tomorrow';
  const format = {weekday: 'short', day: 'numeric', month: 'short', ...options};
  if (days >= 7) delete format.weekday;
  if (new Date(stamp).getFullYear() !== new Date().getFullYear()) format.year = 'numeric';
  return new Date(stamp).toLocaleDateString(undefined, format);
}

async function status() {
  try {
    const data = await get('/api/status');
    catalog = data.catalog || catalog;
    const docker = data.docker;
    const probes = docker.source === 'app-probes';
    const services = docker.groups.flatMap(group => group.services);
    $('#nav-password').hidden = !data.canChangePassword;
    $('#nav-pin').hidden = !data.canChangePin;
    $('#nav-discover').hidden = !data.discover && page !== 'discover';
    const newer = data.release?.available;
    $('#nav-update').hidden = !newer;
    if (page === 'overview') {
      $('#update-banner').hidden = !newer;
      if (newer) $('#update-banner-title').textContent = `MartinFlix ${newer.version} is ready`;
    }
    const problems = attentionRows(docker, services).length;
    $('#nav-flag').hidden = problems === 0;
    $('#nav-flag').innerHTML = `${problems}<span class="sr-only"> ${problems === 1 ? 'issue' : 'issues'}</span>`;
    if (servicesPage) {
      $('#services-description').textContent = probes
        ? 'Live health checks for the services this installer set up.'
        : 'Container status from Docker, grouped by stack, with each app’s own health alerts.';
      controlServices.clear();
      for (const service of services) if (service.control) controlServices.set(service.name, service);
      $('#services-content').innerHTML = docker.available
        ? docker.groups.map(group => serviceGroup(group, probes ? 'reachable' : 'running', !probes || docker.downloadsBehindVpn)).join('')
        : `<p class="empty">${esc(docker.error)}</p>`;
    } else if (page === 'overview') {
      renderOverview(docker, services, data.backups.snapshots, probes);
    } else if (page === 'backups') {
      renderBackups(data.backups.snapshots, data.backups.schedule, data.backups.scheduleProblem);
    }
    $('#updated').textContent = 'Updated ' + new Date().toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
  } catch {
    $('#updated').textContent = 'Could not refresh';
    $('#running').textContent = 'Unavailable';
    $('#services-summary-detail').textContent = 'Could not load status';
    $('#service-summary').classList.add('is-alert');
    $('#services-content').innerHTML = '<p class="empty">Could not load status. Check the dashboard server and try again.</p>';
    $('#backups-content').innerHTML = '<p class="empty">Could not load snapshots.</p>';
  }
}

const libraries = {};
const queues = {};

async function activity() {
  try {
    const data = await get('/api/activity');
    const plex = data.plex;
    // No Plex on this install: leave the space to the tiles that apply.
    $('#plex-summary').hidden = !plex.configured;
    $('#plex-streams').textContent = plex.error ? '—' : plex.streams ?? '—';
    const titles = (plex.sessions || []).map(stream => stream.title);
    $('#plex-detail').textContent = plex.error || (titles.length ? titles.join(' · ') : 'Nothing playing right now');
    Object.assign(queues, data.queues);
    for (const library of Object.values(libraries)) renderLibraryCount(library);
  } catch {
    $('#plex-streams').textContent = '—';
    $('#plex-detail').textContent = 'Could not load Plex activity';
  }
}

/** "4.2 MB/s": decimal units, as Transmission itself shows them. */
function speedLabel(bytes) {
  if (!bytes) return '0 B/s';
  const units = ['B/s', 'kB/s', 'MB/s', 'GB/s'];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit += 1; }
  return `${new Intl.NumberFormat(undefined, {maximumFractionDigits: value < 10 && unit ? 1 : 0}).format(value)} ${units[unit]}`;
}

function bytesLabel(bytes) {
  return bytes == null ? '' : sizeLabel(bytes).replace(/ free$/, '');
}

function etaLabel(seconds) {
  if (seconds == null) return '';
  if (seconds < 60) return 'under a minute left';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h ${minutes % 60} min left` : `${Math.round(hours / 24)} days left`;
}

/** A round top for the speed axis: 1, 2 or 5 times a power of ten, at least 100 kB/s. */
function niceMax(value) {
  const floor = Math.max(value, 100000);
  const power = 10 ** Math.floor(Math.log10(floor));
  return [1, 2, 5, 10].map(step => step * power).find(step => step >= floor);
}

/** SVG paths for one series over the last hour. Gaps in sampling break the line instead of bridging them. */
function speedPath(samples, index, max, width, height, area = false) {
  const end = Date.now() / 1000;
  const start = end - 3600;
  const runs = [[]];
  for (const sample of samples) {
    if (sample[0] < start) continue;
    if (sample[index] == null) { if (runs.at(-1).length) runs.push([]); continue; }
    const x = ((sample[0] - start) / 3600) * width;
    const y = height - Math.min(sample[index] / max, 1) * (height - 2) - 1;
    runs.at(-1).push([x.toFixed(1), y.toFixed(1)]);
  }
  return runs.filter(run => run.length > 1).map(run => {
    const line = 'M' + run.map(point => point.join(' ')).join('L');
    return area ? `${line}L${run.at(-1)[0]} ${height}L${run[0][0]} ${height}Z` : line;
  }).join('');
}

let chartSamples = [];
function drawSpeedChart(samples) {
  chartSamples = samples;
  const recent = samples.filter(sample => sample[0] >= Date.now() / 1000 - 3600);
  const peak = Math.max(0, ...recent.flatMap(sample => [sample[1] || 0, sample[2] || 0]));
  const max = niceMax(peak);
  $('#chart-max').textContent = speedLabel(max);
  $('#chart-mid').textContent = speedLabel(max / 2);
  $('#chart-down-area').setAttribute('d', speedPath(samples, 1, max, 600, 200, true));
  $('#chart-down').setAttribute('d', speedPath(samples, 1, max, 600, 200));
  $('#chart-up').setAttribute('d', speedPath(samples, 2, max, 600, 200));
  const downPeak = Math.max(0, ...recent.map(sample => sample[1] || 0));
  $('#chart-plot').setAttribute('aria-label',
    `Download and upload speed over the last hour. Download peaked at ${speedLabel(downPeak)}.`);
}

/** Crosshair and tooltip: the sample nearest the pointer, or moved with the arrow keys. */
function setupSpeedChart() {
  const plot = $('#chart-plot');
  const cursor = $('#chart-cursor');
  const tip = $('#chart-tip');
  let index = null;
  const show = position => {
    const start = Date.now() / 1000 - 3600;
    const visible = chartSamples.filter(sample => sample[0] >= start);
    if (!visible.length) return;
    index = Math.max(0, Math.min(visible.length - 1, position));
    const sample = visible[index];
    const x = ((sample[0] - start) / 3600) * plot.clientWidth;
    cursor.style.left = `${x}px`;
    cursor.hidden = false;
    const time = new Date(sample[0] * 1000).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    tip.innerHTML = `<strong>${esc(time)}</strong>
      <span><i class="legend-key key-down"></i>Download ${esc(sample[1] == null ? 'no data' : speedLabel(sample[1]))}</span>
      <span><i class="legend-key key-up"></i>Upload ${esc(sample[2] == null ? 'no data' : speedLabel(sample[2]))}</span>`;
    tip.hidden = false;
    tip.style.left = `${Math.min(Math.max(x, 80), plot.clientWidth - 80)}px`;
  };
  const nearest = event => {
    const start = Date.now() / 1000 - 3600;
    const visible = chartSamples.filter(sample => sample[0] >= start);
    const time = start + (event.offsetX / plot.clientWidth) * 3600;
    let best = 0;
    visible.forEach((sample, position) => { if (Math.abs(sample[0] - time) < Math.abs(visible[best][0] - time)) best = position; });
    return best;
  };
  plot.addEventListener('pointermove', event => { if (event.target === plot || plot.contains(event.target)) show(nearest(event)); });
  plot.addEventListener('pointerleave', () => { cursor.hidden = true; tip.hidden = true; index = null; });
  plot.addEventListener('keydown', event => {
    const step = {ArrowLeft: -1, ArrowRight: 1, Home: -1e6, End: 1e6}[event.key];
    if (step == null) return;
    event.preventDefault();
    show((index ?? 1e6) + step);
  });
  plot.addEventListener('blur', () => { cursor.hidden = true; tip.hidden = true; index = null; });
}

function liveRow(item, extra) {
  return `<li class="live-row">
    <div class="live-thumb">${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async">` : ''}</div>
    <div class="live-main">
      <p class="live-title">${esc(item.title)}</p>
      ${item.detail ? `<p class="live-detail">${esc(item.detail)}</p>` : ''}
      ${extra}
    </div>
  </li>`;
}

function progressBar(percent, label) {
  const value = Math.max(0, Math.min(100, percent ?? 0));
  return `<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(value)}" aria-label="${esc(label)}"><span data-width="${value}"></span></div>`;
}

/** The page's security policy blocks inline style attributes, so bar widths are set from here. */
function sizeProgress(root) {
  for (const bar of root.querySelectorAll('.progress span[data-width]')) bar.style.width = `${bar.dataset.width}%`;
}

async function live() {
  try {
    const data = await get('/api/live');
    const transfer = data.transfer;
    const playing = data.playing;
    $('#live-transfer-panel').hidden = !transfer.available;
    $('#live-downloads-panel').hidden = !transfer.available;
    $('#live-playing-panel').hidden = !playing.configured;
    $('#live-empty').hidden = transfer.available || playing.configured;
    if (playing.configured) {
      const sessions = playing.sessions || [];
      $('#live-playing-count').textContent = playing.error ? 'From Plex'
        : sessions.length ? `${sessions.length} ${sessions.length === 1 ? 'stream' : 'streams'}` : 'From Plex';
      $('#live-playing').innerHTML = playing.error ? `<p class="error error-inline">${esc(playing.error)}</p>`
        : sessions.length ? `<ul class="live-list">${sessions.map(session => liveRow(session, `
            ${progressBar(session.percent, `${session.title} progress`)}
            <p class="live-meta">${esc([session.user, session.device, session.state === 'paused' ? 'Paused' : session.state === 'buffering' ? 'Buffering' : '', session.stream].filter(Boolean).join(' · '))}</p>`)).join('')}</ul>`
        : '<p class="empty">Nothing playing right now.</p>';
      sizeProgress($('#live-playing'));
    }
    if (transfer.available) {
      if (transfer.error) {
        $('#live-stats').innerHTML = `<p class="error error-inline">${esc(transfer.error)}</p>`;
      } else {
        const stats = [['Download', speedLabel(transfer.down)], ['Upload', speedLabel(transfer.up)],
          ['Downloading', String(transfer.downloading + transfer.queued)], ['Seeding', String(transfer.seeding)],
          ['Ratio', transfer.ratio == null ? '—' : transfer.ratio.toFixed(2)]];
        $('#live-stats').innerHTML = stats.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
        drawSpeedChart(transfer.samples || []);
        const downloads = transfer.downloads || [];
        $('#live-downloads-count').textContent = downloads.length
          ? `${downloads.length} ${downloads.length === 1 ? 'download' : 'downloads'}${transfer.queued ? `, ${transfer.queued} waiting` : ''}` : '';
        $('#live-downloads').innerHTML = downloads.length ? `<ul class="live-list">${downloads.map(item => liveRow(item, `
            ${progressBar(item.percent, `${item.title} downloaded`)}
            <p class="live-meta">${esc([`${Math.floor(item.percent)}%`, item.state === 'downloading' ? speedLabel(item.down) : item.state === 'queued' ? 'Waiting' : 'Checking',
              item.state === 'downloading' ? etaLabel(item.eta) : '', bytesLabel(item.size)].filter(Boolean).join(' · '))}${item.problem ? ' · <span class="text-crit">Transmission reports a problem</span>' : ''}</p>`)).join('')}</ul>`
          : '<p class="empty">Nothing downloading right now.</p>';
        sizeProgress($('#live-downloads'));
      }
    }
  } catch (error) {
    $('#live-empty').hidden = false;
    $('#live-empty').innerHTML = `<h3>Could not load live activity</h3><p>${esc(error.message)}</p>`;
  }
}

/** "Today 21:14", "Yesterday 20:03", "Mon 19:40" within the week, then "12 Nov", with the year once it isn't this one. */
function watchedWhen(epoch) {
  const date = new Date(epoch * 1000);
  const time = date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const today = new Date();
  const days = Math.round((new Date(today.getFullYear(), today.getMonth(), today.getDate()) - day) / 86400000);
  if (days <= 0) return `Today ${time}`;
  if (days === 1) return `Yesterday ${time}`;
  if (days < 7) return `${date.toLocaleDateString([], {weekday: 'short'})} ${time}`;
  return date.toLocaleDateString([], {day: 'numeric', month: 'short', ...(date.getFullYear() === today.getFullYear() ? {} : {year: 'numeric'})});
}

/** Plex's watch history as a table: one line per play. On a phone the episode moves under the title and who under when. */
function historyTable(history) {
  const rows = history.map(item => {
    const when = item.watchedAt ? watchedWhen(item.watchedAt) : '';
    const watched = item.finished !== false ? 'Finished' : `${Math.round(item.progress || 0)}%`;
    const full = item.watchedAt ? new Date(item.watchedAt * 1000).toLocaleString([], {dateStyle: 'full', timeStyle: 'short'}) : '';
    return `<tr>
      <td class="history-art"><div class="live-thumb">${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async">` : ''}</div></td>
      <th scope="row" class="history-title"><span>${esc(item.title)}</span>${item.detail ? `<span class="history-sub">${esc(item.detail)}</span>` : ''}</th>
      <td class="history-episode"><span>${esc(item.detail || '')}</span></td>
      <td class="history-who">${esc(item.user || '')}</td>
      <td class="history-watched${item.finished !== false ? '' : ' is-partial'}">${esc(watched)}</td>
      <td class="history-when"><time${full ? ` title="${esc(full)}"` : ''}>${esc(when)}</time><span class="history-sub">${esc([item.user, watched].filter(Boolean).join(' · '))}</span></td>
    </tr>`;
  }).join('');
  return `<div class="history-wrap"><table class="history">
    <thead><tr><th scope="col"><span class="sr-only">Poster</span></th><th scope="col">Title</th><th scope="col" class="history-episode">Episode</th>
      <th scope="col" class="history-who">Who</th><th scope="col" class="history-watched">Watched</th><th scope="col" class="history-when">When</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

const historyView = {rows: [], request: 0};

/** Recently watched, narrowed by who, series or films, and title; Show more adds the next page. */
async function watchHistory({more = false} = {}) {
  const request = ++historyView.request;
  const query = new URLSearchParams({who: $('#history-who').value, kind: document.querySelector('[name=history-kind]:checked')?.value || '',
    q: $('#history-search').value.trim(), offset: more ? historyView.rows.length : 0});
  $('#history-more').disabled = true;
  try {
    const data = await get('/api/watch-history?' + query);
    if (request !== historyView.request) return;
    $('#live-history-panel').hidden = !data.configured;
    if (!data.configured) return;
    if (data.error) {
      $('#live-history').innerHTML = `<p class="error error-inline">${esc(data.error)}</p>`;
      return;
    }
    historyView.rows = more ? [...historyView.rows, ...data.history] : data.history;
    const who = $('#history-who');
    const chosen = who.value;
    who.innerHTML = '<option value="">Everyone</option>' + data.users.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
    who.value = data.users.includes(chosen) ? chosen : '';
    const filtered = query.get('who') || query.get('kind') || query.get('q');
    $('#live-history').innerHTML = (data.notice ? `<p class="option-help">${esc(data.notice)}</p>` : '')
      + (historyView.rows.length ? historyTable(historyView.rows)
        : `<p class="empty">${filtered ? 'No plays match.' : 'Nothing watched yet.'}</p>`);
    $('#history-count').textContent = data.total ? `${historyView.rows.length} of ${data.total} ${data.total === 1 ? 'play' : 'plays'}` : '';
    $('#history-more').hidden = !data.more;
  } catch {
    $('#live-history-panel').hidden = false;
    $('#live-history').innerHTML = '<p class="error error-inline">Could not load the watch history.</p>';
  } finally {
    $('#history-more').disabled = false;
  }
}

function setupHistory() {
  let typing;
  $('#history-who').addEventListener('change', () => watchHistory());
  for (const choice of document.querySelectorAll('[name=history-kind]')) choice.addEventListener('change', () => watchHistory());
  $('#history-search').addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(() => watchHistory(), 250); });
  $('#history-more').addEventListener('click', () => watchHistory({more: true}));
}

/** The Overview's compact view: speeds and what's downloading, with the last hour faintly behind them. */
async function transferSummary() {
  try {
    const data = await get('/api/live?summary=1');
    const transfer = data.transfer;
    $('#transfer-summary').hidden = !transfer.available;
    if (!transfer.available) return;
    if (transfer.error) {
      $('#transfer-speed').textContent = '—';
      $('#transfer-detail').textContent = transfer.error;
      return;
    }
    $('#transfer-speed').textContent = `↓ ${speedLabel(transfer.down)}`;
    const active = transfer.downloading + transfer.queued;
    $('#transfer-detail').textContent = `${active ? `${active} downloading` : 'Nothing downloading'} · ↑ ${speedLabel(transfer.up)}`;
    const samples = transfer.samples || [];
    const recent = samples.filter(sample => sample[0] >= Date.now() / 1000 - 3600);
    const max = niceMax(Math.max(0, ...recent.map(sample => sample[1] || 0)));
    $('#transfer-summary .spark-area').setAttribute('d', speedPath(samples, 1, max, 300, 100, true));
    $('#transfer-summary .spark-line').setAttribute('d', speedPath(samples, 1, max, 300, 100));
  } catch {
    $('#transfer-detail').textContent = 'Could not load transfer speeds';
  }
}

function episodeCode(episode) {
  if (!episode || episode.season == null || episode.number == null) return '';
  return `S${String(episode.season).padStart(2, '0')}E${String(episode.number).padStart(2, '0')}`;
}

function shortDate(value) {
  const day = new Date(value);
  return day.toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: day.getFullYear() === new Date().getFullYear() ? undefined : 'numeric'});
}

/** A problem worth a look: episodes that aired but weren't downloaded, or a film past the day Radarr could get it. */
function posterProblem(item, kind) {
  if (kind === 'sonarr' && item.missing) {
    const {count, since, first} = item.missing;
    const code = first ? episodeCode(first) : '';
    return {flag: 'Missing', text: `${count} episode${count === 1 ? '' : 's'} missing${code ? `, from ${code}` : ''} · aired ${shortDate(since)}`};
  }
  if (kind === 'radarr' && item.overdue) {
    const film = item.availability || {};
    return {flag: 'Overdue', text: film.date ? `Not downloaded since ${film.type} ${shortDate(film.date + 'T12:00:00')}` : 'Available, not downloaded'};
  }
  return null;
}

const LIBRARY_APPS = {sonarr: 'Sonarr', radarr: 'Radarr', listenarr: 'Listenarr'};
const LIBRARY_NOUNS = {sonarr: 'series', radarr: 'films', listenarr: 'audiobooks'};

/** An audiobook's author, and its series and state, on its card. */
function bookDetails(item, all) {
  const state = item.downloading ? 'Downloading' : item.downloaded ? `Downloaded${item.size ? ` · ${bytesLabel(item.size)}` : ''}`
    : item.monitored ? 'Wanted' : 'Not monitored';
  return {meta: item.authors?.length ? item.authors.join(', ') : 'Unknown author',
    extra: [item.series, all || item.downloading ? state : ''].filter(Boolean).join(' · ')};
}

/** In the whole-library view: what each title holds on disk, which is what deleting it frees. */
function libraryDetails(item, kind) {
  if (kind === 'listenarr') return bookDetails(item, true);
  const quiet = item.monitored ? '' : 'Not monitored';
  if (kind === 'sonarr') {
    const {files = 0, total = 0} = item.episodes || {};
    const held = files ? `${files} of ${total} episode${total === 1 ? '' : 's'} · ${bytesLabel(item.size)}` : 'No episodes downloaded';
    return {meta: held, extra: [item.status && item.status[0].toUpperCase() + item.status.slice(1), quiet].filter(Boolean).join(' · ')};
  }
  if (item.downloaded) return {meta: `Downloaded · ${bytesLabel(item.size)}`, extra: quiet};
  const {meta, extra} = posterDetails(item, kind);
  return {meta, extra: [quiet, extra].filter(Boolean).join(' · ')};
}

function posterDetails(item, kind) {
  if (kind === 'listenarr') return bookDetails(item, false);
  if (kind === 'sonarr') {
    const airing = item.nextAiring && new Date(item.nextAiring);
    const code = episodeCode(item.nextEpisode);
    if (!airing) return {meta: 'No episode due in the next 12 months', extra: ''};
    const when = upcomingDay(airing);
    return {meta: code ? `${code} · ${when}` : `Next episode ${when}`, extra: item.nextEpisode?.title || ''};
  }
  // When Radarr can download it, by the film's minimum availability, as in Radarr's Missing list.
  const film = item.availability || {};
  const day = film.date ? new Date(film.date + 'T12:00:00') : null;
  if (film.available) {
    const since = day ? ` · ${film.type} ${shortDate(day)}` : '';
    return {meta: `Available${since}`, extra: item.downloading ? 'Downloading' : 'Not downloaded yet'};
  }
  if (day) return {meta: `${film.type} · ${upcomingDay(day)}`, extra: ''};
  return {meta: 'Release date unknown', extra: ''};
}

const LINK_NAMES = {imdb: 'IMDb', tmdb: 'TMDB', tvdb: 'TheTVDB', audible: 'Audible'};

/** Audiobookshelf's page for a book, at this dashboard's host on Audiobookshelf's own port. */
function listenLink(item, title) {
  const {port, path} = item.listen || {};
  if (!Number.isInteger(port) || !/^\/audiobookshelf\/item\/[A-Za-z0-9_%-]+$/.test(path || '')) return '';
  const href = `http://${window.location.hostname}:${port}${path}`;
  return `<a class="poster-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer" aria-label="Listen to ${esc(title)} in Audiobookshelf">Listen${icon('external')}</a>`;
}

/** IMDb and TMDB links for a poster, opening in a new tab. */
function titleLinks(links, title) {
  return Object.entries(links || {}).filter(([site, url]) => LINK_NAMES[site] && /^https:\/\//.test(url))
    .map(([site, url]) => `<a class="poster-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer" aria-label="${esc(title)} on ${LINK_NAMES[site]}">${LINK_NAMES[site]}${icon('external')}</a>`)
    .join('');
}

function posterCard(item, kind, {all = false, deletable = false} = {}) {
  const {meta, extra} = all ? libraryDetails(item, kind) : posterDetails(item, kind);
  const problem = posterProblem(item, kind);
  const title = item.year && kind !== 'listenarr' ? `${item.title} (${item.year})` : item.title;
  const remove = deletable && item.id
    ? `<button class="poster-delete" type="button" data-delete="${esc(kind)}" data-id="${esc(item.id)}" aria-label="Delete ${esc(title)}" title="Delete">${icon('trash')}</button>`
    : '';
  const links = (kind === 'listenarr' ? listenLink(item, item.title) : '') + titleLinks(item.links, title);
  return `<li class="poster-card${problem ? ' has-problem' : ''}">
    <div class="poster-art"${links ? ` tabindex="0" aria-label="${esc(title)}: show links"` : ''}>
      <span class="poster-placeholder" aria-hidden="true">${esc(item.title)}</span>
      ${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async" width="250" height="375">` : ''}
      ${problem ? `<span class="poster-flag">${icon('alert')}${esc(problem.flag)}</span>` : ''}
      ${remove}
      ${links ? `<div class="poster-links">${links}</div>` : ''}
    </div>
    <h3 class="poster-title">${esc(title)}</h3>
    ${problem ? `<p class="poster-problem">${esc(problem.text)}</p>` : ''}
    ${problem && kind === 'radarr' ? '' : `<p class="poster-meta">${esc(meta)}</p>
    ${extra ? `<p class="poster-extra">${esc(extra)}</p>` : ''}`}
  </li>`;
}

/** The Overview shows what's coming up; the Library shows everything monitored, with problems first. */
function libraryItems(library) {
  const items = library.data?.items || [];
  if (!library.upcomingOnly || library.show === 'all') return items;
  return library.kind === 'sonarr'
    ? items.filter(item => item.nextAiring).sort((a, b) => a.nextAiring.localeCompare(b.nextAiring))
    : items.filter(item => !item.availability?.available && item.availability?.date)
      .sort((a, b) => a.availability.date.localeCompare(b.availability.date));
}

function problemCount(library) {
  return (library.data?.items || []).filter(item => posterProblem(item, library.kind)).length;
}

function renderLibraryCount(library) {
  const {kind, data, count} = library;
  if (!count) return;
  if (!data?.configured || data.error) {
    count.textContent = library.limit ? count.dataset.default : '';
    return;
  }
  const total = libraryItems(library).length;
  if (kind === 'listenarr') {
    const downloading = libraryItems(library).filter(item => item.downloading).length;
    const size = libraryItems(library).reduce((sum, item) => sum + (item.size || 0), 0);
    count.textContent = library.show === 'all'
      ? `${total} ${total === 1 ? 'book' : 'books'} · ${bytesLabel(size)} on disk`
      : `${total} wanted${downloading ? ` · ${downloading} downloading` : ''}`;
    count.classList.remove('has-problem');
    return;
  }
  const queued = queues[kind];
  const queue = queued ? ` · ${queued} in the download queue` : '';
  if (library.limit) {
    // The Overview only shows what's coming, so it mentions problems and leaves the details to the Library.
    const problems = problemCount(library);
    const flagged = !problems ? '' : kind === 'sonarr' ? ` · ${problems} with missing episodes` : ` · ${problems} overdue`;
    count.textContent = (kind === 'sonarr' ? `${total} ${total === 1 ? 'series airs' : 'series air'} in the next 12 months`
      : `${total} ${total === 1 ? 'film has' : 'films have'} a release ahead`) + flagged + queue;
    count.classList.toggle('has-problem', problems > 0);
  } else if (library.show === 'all') {
    const size = libraryItems(library).reduce((sum, item) => sum + (item.size || 0), 0);
    count.textContent = `${total} ${kind === 'sonarr' ? 'series' : total === 1 ? 'film' : 'films'} · ${bytesLabel(size)} on disk`;
    count.classList.remove('has-problem');
  } else {
    const problems = problemCount(library);
    const flagged = problems ? ` · ${problems} need${problems === 1 ? 's' : ''} a look` : '';
    count.textContent = `${total} ${kind === 'sonarr' ? 'series' : 'missing'}${flagged}${queue}`;
    count.classList.toggle('has-problem', problems > 0);
  }
}

function libraryMessage(kind, data) {
  const name = LIBRARY_APPS[kind];
  if (!data.configured) return `<div class="empty-state"><h3>${name} isn’t connected</h3><p>Save its API key in the dashboard secrets directory. The dashboard guide explains where.</p></div>`;
  return `<p class="empty">${esc(data.error)}</p>`;
}

function emptyLibrary(library) {
  if (library.kind === 'listenarr') {
    return library.show === 'all'
      ? '<div class="empty-state"><h3>No audiobooks yet</h3><p>Listenarr has no books.</p><a class="text-link" href="/add?kind=listenarr">Add a book</a></div>'
      : '<div class="empty-state"><h3>Nothing wanted</h3><p>Every book Listenarr is looking for has been downloaded.</p><a class="text-link" href="/add?kind=listenarr">Add a book</a></div>';
  }
  if (library.show === 'all') {
    return library.kind === 'sonarr'
      ? '<div class="empty-state"><h3>No series yet</h3><p>Sonarr has no series.</p><a class="text-link" href="/add">Add a series</a></div>'
      : '<div class="empty-state"><h3>No films yet</h3><p>Radarr has no films.</p><a class="text-link" href="/add?kind=radarr">Add a film</a></div>';
  }
  if (library.kind === 'sonarr') {
    return '<div class="empty-state"><h3>Nothing airing soon</h3><p>No monitored series have an episode due in the next 12 months.</p><a class="text-link" href="/add">Add a series</a></div>';
  }
  return library.upcomingOnly
    ? '<div class="empty-state"><h3>No releases ahead</h3><p>None of your monitored films still to download has a release date coming up.</p><a class="text-link" href="/add?kind=radarr">Add a film</a></div>'
    : '<div class="empty-state"><h3>No missing films</h3><p>Every monitored film has been downloaded.</p><a class="text-link" href="/add?kind=radarr">Add a film</a></div>';
}

function renderLibrary(library) {
  const {kind, data, content, filter} = library;
  renderLibraryCount(library);
  if (!data) return;
  if (!data.configured || data.error) {
    content.innerHTML = libraryMessage(kind, data);
    return;
  }
  const search = filter ? filter.value.trim().toLowerCase() : '';
  let items = libraryItems(library).filter(item => [item.title, ...(item.authors || []), item.series || '']
    .some(text => text.toLowerCase().includes(search)));
  if (library.limit) items = items.slice(0, library.limit);
  content.innerHTML = items.length
    ? `<ul class="poster-grid${library.limit ? ' is-rail' : ''}${kind === 'listenarr' ? ' is-square' : ''}">${items.map(item => posterCard(item, kind,
      {all: library.show === 'all', deletable: !library.limit})).join('')}</ul>`
    : search ? `<p class="empty">No titles match “${esc(filter.value.trim())}”.</p>` : emptyLibrary(library);
}

function hidePosterOnError(content) {
  content.addEventListener('error', event => {
    if (event.target.matches('.poster-art img')) event.target.hidden = true;
  }, true);
}

async function loadLibrary(library) {
  const show = library.show;
  try {
    const data = await get('/api/' + library.kind + (show === 'all' ? '?show=all' : ''));
    // A newer choice of view wins over an older answer still on its way.
    if (library.show !== show) return;
    library.data = data;
    renderLibrary(library);
  } catch {
    library.data = null;
    if (library.count && !library.limit) library.count.textContent = '';
    library.content.innerHTML = '<p class="empty">Could not load titles.</p>';
  }
}

function overviewLibrary(kind, content, count, extra = {}) {
  hidePosterOnError(content);
  count.dataset.default = count.textContent;
  return {kind, content, count, limit: 12, ...extra};
}

if (page === 'library') {
  // Audiobooks show only where they're installed.
  for (const section of document.querySelectorAll('[data-library]:not([hidden])')) {
    const content = section.querySelector('.library-content');
    const filter = section.querySelector('[data-library-filter]');
    const library = {kind: section.dataset.library, content, filter, count: section.querySelector('.library-count'), show: 'attention'};
    hidePosterOnError(content);
    filter.addEventListener('input', () => renderLibrary(library));
    for (const choice of section.querySelectorAll('.library-show input')) {
      choice.addEventListener('change', () => {
        library.show = choice.value;
        for (const intro of section.querySelectorAll('[data-show-intro]')) intro.hidden = intro.dataset.showIntro !== library.show;
        library.data = null;
        library.count.textContent = '';
        content.innerHTML = `<p class="empty">Loading ${LIBRARY_NOUNS[library.kind]}…</p>`;
        loadLibrary(library);
      });
    }
    libraries[library.kind] = library;
  }
  setupDelete();
} else if (page === 'overview') {
  libraries.sonarr = overviewLibrary('sonarr', $('#next-series'), $('#next-series-count'));
  libraries.sonarr.upcomingOnly = true;
  libraries.radarr = overviewLibrary('radarr', $('#next-films'), $('#next-films-count'), {upcomingOnly: true});
}

/* Library: delete a series or film from Sonarr or Radarr, and its files only when asked. */
function setupDelete() {
  const dialog = $('#delete-dialog');
  const files = $('#delete-files');
  const warning = $('#delete-warning');
  const error = $('#delete-error');
  const confirm = $('#delete-confirm');
  let target = null;
  let busy = false;

  const describe = () => {
    const {kind, item} = target;
    const app = LIBRARY_APPS[kind];
    const player = kind === 'listenarr' ? 'Audiobookshelf' : 'Plex';
    $('#delete-detail').textContent = files.checked
      ? `Removes it from ${app} and deletes its folder from your media.`
      : `Removes it from ${app}, which stops monitoring and downloading it. Its files stay in your media folder, and ${player} keeps showing them.`;
    warning.hidden = !files.checked;
    // What's on disk is only known in the All view; otherwise say it plainly.
    const saved = kind === 'sonarr' ? item.episodes?.files : item.downloaded === undefined ? undefined : Number(item.downloaded);
    const held = saved === 0 ? null : kind === 'sonarr'
      ? (saved ? `${saved} downloaded episode${saved === 1 ? '' : 's'}` : 'every downloaded episode')
      : kind === 'listenarr' ? 'the downloaded audiobook' : 'the downloaded film';
    warning.querySelector('span').textContent = held
      ? `This deletes ${held}${item.size ? ` (${bytesLabel(item.size)})` : ''} for good. It can’t be undone.`
      : 'Nothing has been downloaded for it, so only its folder goes. It can’t be undone.';
    confirm.innerHTML = `${icon(busy ? 'refresh' : 'trash')}${busy ? 'Deleting…' : files.checked ? 'Delete with files' : 'Delete'}`;
  };
  const setBusy = state => {
    busy = state;
    files.disabled = state;
    $('#delete-cancel').disabled = state;
    confirm.setAttribute('aria-disabled', String(state));
    confirm.classList.toggle('is-busy', state);
    describe();
  };

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-delete]');
    if (!button) return;
    const library = libraries[button.dataset.delete];
    const item = library?.data?.items?.find(entry => String(entry.id) === button.dataset.id);
    if (!item) return;
    target = {library, kind: library.kind, item};
    $('#delete-title').textContent = `Delete ${item.year && library.kind !== 'listenarr' ? `${item.title} (${item.year})` : item.title}?`;
    files.checked = false;
    error.hidden = true;
    setBusy(false);
    dialog.showModal();
    $('#delete-cancel').focus();
  });
  files.addEventListener('change', describe);
  $('#delete-cancel').addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  $('#delete-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !target) return;
    const {library, kind, item} = target;
    error.hidden = true;
    setBusy(true);
    try {
      const data = await post(`/api/delete/${kind}`, {id: item.id, files: files.checked});
      busy = false;
      dialog.close();
      library.data.items = library.data.items.filter(entry => entry.id !== item.id);
      renderLibrary(library);
      const notice = $('#library-notice');
      notice.textContent = `${data.title} was deleted from ${LIBRARY_APPS[kind]}${data.files ? ', with its files' : '. Its files are still in your media folder'}.`;
      notice.hidden = false;
    } catch (failure) {
      setBusy(false);
      error.textContent = failure.message;
      error.hidden = false;
    }
  });
}

/* Add page: search Sonarr or Radarr's own lookup, then add a title with the defaults or the admin's choices. */
const addSettings = {};
const addResults = new Map();

function addKind() {
  return document.querySelector('#add-form input[name="kind"]:checked').value;
}

async function loadAddSettings(kind) {
  addSettings[kind] ??= get('/api/add-settings/' + kind).catch(error => {
    delete addSettings[kind];
    throw error;
  });
  return addSettings[kind];
}

function sizeLabel(bytes) {
  if (bytes == null) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit += 1; }
  return `${new Intl.NumberFormat(undefined, {maximumFractionDigits: value < 10 ? 1 : 0}).format(value)} ${units[unit]} free`;
}

async function showAddTarget(kind) {
  const target = $('#add-target');
  try {
    const settings = await loadAddSettings(kind);
    if (addKind() !== kind) return;
    target.classList.toggle('is-problem', !settings.ready);
    if (!settings.ready) {
      target.textContent = settings.problem;
      return;
    }
    if (kind === 'listenarr') {
      target.textContent = `New books go to ${settings.folder}, with Listenarr’s default quality profile, and Audiobookshelf shows them once downloaded.`;
      return;
    }
    const profile = settings.profiles.find(item => item.id === settings.profile);
    target.textContent = `New titles use the ${profile.name} profile and ${settings.folder} unless you change them when adding.`;
  } catch {
    target.textContent = 'Could not read the add settings.';
  }
}

/** "11 h 32 min" for an audiobook's length in minutes. */
function runtimeLabel(minutes) {
  if (!minutes) return '';
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ''}` : `${minutes} min`;
}

function resultCard(item, kind) {
  if (kind === 'listenarr') return bookResultCard(item);
  const facts = kind === 'sonarr'
    ? [item.year, item.network, item.seasons ? `${item.seasons} ${item.seasons === 1 ? 'season' : 'seasons'}` : '', item.status === 'ended' ? 'Ended' : item.status === 'upcoming' ? 'Upcoming' : '']
    : [item.year, item.studio, item.runtime ? `${item.runtime} min` : ''];
  const action = item.added
    ? '<span class="badge ok">In your library</span>'
    : `<button class="button button-primary button-small" type="button" data-add="${esc(item.id)}" aria-expanded="false">${icon('plus')}Add</button>`;
  return `<li class="result" data-id="${esc(item.id)}">
    <div class="poster-art result-art">
      <span class="poster-placeholder" aria-hidden="true">${esc(item.title)}</span>
      ${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" width="120" height="180">` : ''}
    </div>
    <div class="result-body">
      <h3 id="result-title-${esc(item.id)}">${esc(item.title)}</h3>
      <p class="result-facts">${esc(facts.filter(Boolean).join(' · '))}</p>
      ${item.overview ? `<p class="result-overview">${esc(item.overview)}</p>` : ''}
    </div>
    <div class="result-action">${action}</div>
  </li>`;
}

function bookResultCard(item) {
  const action = item.added
    ? '<span class="badge ok">In your library</span>'
    : addButton(item.id);
  const facts = [item.authors?.join(', '), item.narrators?.length ? `Narrated by ${item.narrators.join(', ')}` : '',
    item.series, runtimeLabel(item.runtime), item.year].filter(Boolean);
  return `<li class="result" data-id="${esc(item.id)}">
    <div class="poster-art result-art is-square">
      <span class="poster-placeholder" aria-hidden="true">${esc(item.title)}</span>
      ${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" width="120" height="120">` : ''}
    </div>
    <div class="result-body">
      <h3 id="result-title-${esc(item.id)}">${esc(item.title)}</h3>
      ${item.subtitle ? `<p class="result-overview">${esc(item.subtitle)}</p>` : ''}
      <p class="result-facts">${esc(facts.join(' · '))}</p>
    </div>
    <div class="result-action">${action}</div>
  </li>`;
}

function seasonLabel(number) {
  return number === 0 ? 'Specials' : String(number);
}

/** The options panel shown in place of the Add button. Every control starts on the default. */
function addOptionsForm(item, kind, settings) {
  if (kind === 'listenarr') {
    return `<form class="add-options" aria-labelledby="result-title-${esc(item.id)}" novalidate>
      <label class="check"><input type="checkbox" name="search" checked><span>Start searching now</span></label>
      <p class="error error-inline" role="alert" hidden></p>
      <div class="option-actions">
        <button class="button button-primary" type="submit">${icon('plus')}Add book</button>
        <button class="button button-quiet" type="button" data-cancel>Cancel</button>
      </div>
    </form>`;
  }
  const series = kind === 'sonarr';
  const id = `add-${kind}-${item.id}`;
  const profiles = settings.profiles.map(profile =>
    `<option value="${esc(profile.id)}"${profile.id === settings.profile ? ' selected' : ''}>${esc(profile.name)}</option>`).join('');
  const folders = settings.folders.length > 1 ? `<div class="field"><label for="${id}-folder">Folder</label>
    <select id="${id}-folder" name="folder">${settings.folders.map(folder =>
      `<option value="${esc(folder.path)}"${folder.path === settings.folder ? ' selected' : ''}${folder.accessible ? '' : ' disabled'}>${esc(folder.path)}${folder.freeSpace != null ? ` · ${esc(sizeLabel(folder.freeSpace))}` : ''}</option>`).join('')}</select></div>` : '';
  const regular = (item.seasonNumbers || []).filter(number => number > 0);
  const latest = regular.at(-1);
  const choice = series ? `<div class="field"><label for="${id}-monitor">Seasons</label>
      <select id="${id}-monitor" name="monitor">
        <option value="all" selected>All seasons</option>
        <option value="future">Future episodes only</option>
        ${latest ? `<option value="latest">Latest season (${latest})</option>` : ''}
        ${regular.length > 1 ? '<option value="first">First season</option>' : ''}
        ${(item.seasonNumbers || []).length ? '<option value="custom">Choose seasons…</option>' : ''}
      </select></div>`
    : `<div class="field"><label for="${id}-availability">Available from</label>
      <select id="${id}-availability" name="availability" aria-describedby="${id}-availability-help">
        <option value="announced">Announcement</option>
        <option value="inCinemas">Cinema release</option>
        <option value="released" selected>Digital or physical release</option>
      </select>
      <small class="option-help" id="${id}-availability-help">Radarr only grabs the film once it reaches this point.</small></div>`;
  const seasons = series && (item.seasonNumbers || []).length ? `<fieldset class="season-picker" hidden>
      <legend>Seasons to monitor</legend>
      <div class="chips">${item.seasonNumbers.map(number => `<label class="chip"><input type="checkbox" name="season" value="${number}"${number > 0 ? ' checked' : ''}><span>${seasonLabel(number)}</span></label>`).join('')}</div>
    </fieldset>` : '';
  return `<form class="add-options" aria-labelledby="result-title-${esc(item.id)}" novalidate>
    <div class="option-row">
      <div class="field"><label for="${id}-profile">Quality profile</label><select id="${id}-profile" name="profile">${profiles}</select></div>
      ${choice}
      ${folders}
    </div>
    ${seasons}
    <label class="check"><input type="checkbox" name="search" checked><span>Start searching now</span></label>
    <p class="error error-inline" role="alert" hidden></p>
    <div class="option-actions">
      <button class="button button-primary" type="submit">${icon('plus')}Add ${series ? 'series' : 'film'}</button>
      <button class="button button-quiet" type="button" data-cancel>Cancel</button>
    </div>
  </form>`;
}

function addButton(id) {
  return `<button class="button button-primary button-small" type="button" data-add="${esc(id)}" aria-expanded="false">${icon('plus')}Add</button>`;
}

function closeOptions(card) {
  card.classList.remove('is-open');
  card.querySelector('.result-action').innerHTML = addButton(card.dataset.id);
  card.querySelector('[data-add]').focus();
}

async function openOptions(card, kind) {
  const action = card.querySelector('.result-action');
  const item = addResults.get(`${kind}:${card.dataset.id}`);
  action.querySelector('[data-add]').disabled = true;
  try {
    const settings = await loadAddSettings(kind);
    if (!settings.ready) throw new Error(settings.problem);
    for (const open of document.querySelectorAll('.result.is-open')) if (open !== card) closeOptions(open);
    card.classList.add('is-open');
    action.innerHTML = addOptionsForm(item, kind, settings);
    action.querySelector('select, input').focus();
    card.scrollIntoView({block: 'nearest', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  } catch (error) {
    action.innerHTML = `<p class="error error-inline">${esc(error.message || 'Could not load the add options.')}</p>${addButton(card.dataset.id)}`;
  }
}

async function submitOptions(form, card, kind) {
  const error = form.querySelector('.error');
  const data = new FormData(form);
  // Audiobooks are found by their ASIN; series and films by a number.
  const request = kind === 'listenarr' ? {id: card.dataset.id, search: data.get('search') === 'on'}
    : {id: Number(card.dataset.id), profile: Number(data.get('profile')), search: data.get('search') === 'on'};
  if (data.get('folder')) request.folder = data.get('folder');
  if (kind === 'sonarr') {
    request.monitor = data.get('monitor');
    if (request.monitor === 'custom') {
      request.seasons = data.getAll('season').map(Number);
      if (!request.seasons.length) {
        error.textContent = 'Choose at least one season, or pick another option under Seasons.';
        error.hidden = false;
        return;
      }
    }
  } else if (kind === 'radarr') {
    request.availability = data.get('availability');
  }
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  submit.textContent = 'Adding…';
  error.hidden = true;
  try {
    const response = await fetch('/api/add/' + kind, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || 'Could not add this title.');
    card.classList.remove('is-open');
    card.querySelector('.result-action').innerHTML = `<span class="badge ok" role="status">${result.searching ? 'Added · searching' : 'Added'}</span>`;
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
    submit.disabled = false;
    submit.innerHTML = `${icon('plus')}Add ${{sonarr: 'series', radarr: 'film', listenarr: 'book'}[kind]}`;
  }
}

if (page === 'add') {
  const form = $('#add-form');
  const results = $('#add-results');
  const term = $('#add-term');
  let lastSearch = null;
  hidePosterOnError(results);
  const syncKind = () => {
    const kind = addKind();
    term.placeholder = {sonarr: 'Search for a series', radarr: 'Search for a film', listenarr: 'Search for a book, author or narrator'}[kind];
    showAddTarget(kind);
    if (lastSearch && lastSearch.kind !== kind && term.value.trim()) form.requestSubmit();
  };
  form.addEventListener('change', event => { if (event.target.name === 'kind') syncKind(); });
  // Discover links here with an exact TMDB ID to find, and the title to show while it searches.
  const handoff = new URLSearchParams(window.location.search);
  let find = /^tmdb:[1-9][0-9]{0,9}$/.test(handoff.get('find') || '') ? handoff.get('find') : null;
  if (find && ['sonarr', 'radarr'].includes(handoff.get('kind'))) {
    form.querySelector(`input[name="kind"][value="${handoff.get('kind')}"]`).checked = true;
    term.value = handoff.get('title') || '';
  } else {
    find = null;
  }
  term.addEventListener('input', () => { find = null; });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const kind = addKind();
    const query = find || term.value.trim();
    const shown = term.value.trim() || query;
    const exact = Boolean(find);
    find = null;
    if (!query) return term.focus();
    const button = $('#add-submit');
    button.disabled = true;
    button.textContent = 'Searching…';
    lastSearch = {kind, query};
    results.innerHTML = '<p class="empty">Searching…</p>';
    try {
      const data = await get(`/api/search/${kind}?` + new URLSearchParams({term: query}));
      if (lastSearch.kind !== kind || lastSearch.query !== query) return;
      for (const item of data.results) addResults.set(`${kind}:${item.id}`, item);
      results.innerHTML = data.results.length
        ? `<p class="result-count">${data.results.length} ${data.results.length === 1 ? 'match' : 'matches'} for “${esc(shown)}”</p><ul class="results" data-kind="${kind}">${data.results.map(item => resultCard(item, kind)).join('')}</ul>`
        : `<div class="empty-state"><h3>Nothing found for “${esc(shown)}”</h3><p>${kind === 'listenarr' ? 'Check the spelling, or search for the author.' : 'Check the spelling, or try the original title.'}</p></div>`;
      // A title picked on Discover opens straight onto its options.
      const only = results.querySelector('.result [data-add]');
      if (exact && data.results.length === 1 && only) openOptions(only.closest('.result'), kind);
    } catch (error) {
      results.innerHTML = `<p class="error">${esc(error.message)}</p>`;
    } finally {
      button.disabled = false;
      button.textContent = 'Search';
    }
  });
  results.addEventListener('click', event => {
    const card = event.target.closest('.result');
    if (!card) return;
    const kind = card.closest('.results').dataset.kind;
    if (event.target.closest('[data-add]')) openOptions(card, kind);
    if (event.target.closest('[data-cancel]')) closeOptions(card);
  });
  results.addEventListener('change', event => {
    if (event.target.name !== 'monitor') return;
    const picker = event.target.form.querySelector('.season-picker');
    if (picker) picker.hidden = event.target.value !== 'custom';
  });
  results.addEventListener('submit', event => {
    if (!event.target.matches('.add-options')) return;
    event.preventDefault();
    const card = event.target.closest('.result');
    submitOptions(event.target, card, card.closest('.results').dataset.kind);
  });
  results.addEventListener('keydown', event => {
    if (event.key === 'Escape' && event.target.closest('.add-options')) closeOptions(event.target.closest('.result'));
  });
  showAddTarget(addKind());
  const asked = new URLSearchParams(window.location.search).get('kind');
  const choice = ['radarr', 'listenarr'].includes(asked) && form.querySelector(`input[value="${asked}"]`);
  if (choice && !choice.closest('[hidden]')) {
    choice.checked = true;
    syncKind();
  }
  term.focus();
}

/* Settings: MartinFlix's own version and updates, and the choices made at setup. */
const CHANNEL_NAMES = {stable: 'Stable', edge: 'Edge', dev: 'Development'};
const SETTING_LABELS = {discover: 'Discover', discoverRegion: 'Discover country', tmdbKey: 'TMDB key', plexName: 'Plex server name',
  plexUrl: 'Plex address', plexToken: 'Plex token', backupDir: 'Backup folder', vpnLocation: 'VPN exit',
  vpnProtocol: 'VPN connection', vpnUser: 'VPN credentials', channel: 'Release channel', telegramToken: 'Telegram',
  audiobooks: 'Audiobooks'};
let systemData = null;
let settingsData = null;
let loadedVersion = null;
let following = false;
let followTimer = null;
let lastRunState = null;

function when(value, options = {dateStyle: 'medium', timeStyle: 'short'}) {
  const stamp = new Date(value);
  return value && !isNaN(stamp) && stamp.getFullYear() > 2000 ? stamp.toLocaleString(undefined, options) : '';
}

function changeList(changes, available) {
  if (!changes) {
    return `<p class="muted">The list of changes is on <a href="https://github.com/descalzi/martinflix/commits/main" target="_blank" rel="noopener noreferrer">GitHub</a>.</p>`;
  }
  if (changes.older) return '<p class="muted">This is an earlier version than the one running now, because the release channel changed.</p>';
  const more = changes.total > changes.commits.length ? `<li class="muted">and ${changes.total - changes.commits.length} more</li>` : '';
  return `<ul class="change-list" tabindex="0" aria-label="Changes in this version">${changes.commits.map(commit => `<li><span>${esc(commit.title)}</span><span class="muted">${esc(when(commit.date, {day: 'numeric', month: 'short'}))}</span></li>`).join('')}${more}</ul>
    ${changes.url ? `<a class="text-link" href="${esc(changes.url)}" target="_blank" rel="noopener noreferrer">Compare on GitHub${icon('external')}</a>` : ''}`;
}

// A check started from this page: the button says it's checking, then how it went, for a few seconds.
const releaseCheck = {pending: false, startedAt: 0, before: null, result: null, until: 0};

function renderCheckButton(data) {
  const button = $('#release-check');
  if (releaseCheck.pending) {
    // Done once the controller has finished a check it hadn't before; a minute at most.
    if (data.checking) releaseCheck.seen = true;
    const finished = !data.checking && (releaseCheck.seen || data.checkedAt !== releaseCheck.before || data.problem
      || Date.now() - releaseCheck.startedAt > 60000);
    if (finished && Date.now() - releaseCheck.startedAt > 1000) {
      releaseCheck.pending = false;
      releaseCheck.result = data.available ? 'available' : data.problem ? 'problem' : 'current';
      releaseCheck.until = Date.now() + 6000;
      setTimeout(() => systemData && renderCheckButton(systemData), 6100);
    }
  }
  const checking = releaseCheck.pending || data.checking;
  const result = !checking && Date.now() < releaseCheck.until ? releaseCheck.result : null;
  const [glyph, text] = checking ? ['refresh', 'Checking…'] : result === 'current' ? ['tick', 'Up to date']
    : result === 'problem' ? ['alert', 'Couldn’t check'] : ['refresh', 'Check for updates'];
  button.innerHTML = `${icon(glyph)}${text}`;
  button.disabled = checking;
  button.classList.toggle('is-busy', checking);
  button.classList.toggle('is-done', result === 'current');
}

function renderRelease(data) {
  const current = data.current;
  const channel = CHANNEL_NAMES[current.channel] || current.channel;
  const running = data.updater?.state === 'running';
  $('#release-summary').textContent = current.channel === 'dev'
    ? `Version ${current.version}, built on this machine. It isn’t offered updates.`
    : `Version ${current.version} · ${channel} channel${current.revision ? ` · ${current.revision.slice(0, 7)}` : ''}`;
  $('#release-check').hidden = current.channel === 'dev' || running;
  renderCheckButton(data);
  const available = data.available;
  let body = '';
  // A newer version already here can be installed even when the registry can't be reached right now.
  if (data.problem && !data.available) body += `<p class="error error-inline">${esc(data.problem)}</p>`;
  if (available) {
    const published = when(available.created, {day: 'numeric', month: 'short', year: 'numeric'});
    body += `<div class="release-card">
      <div><h3>Version ${esc(available.version)} is ready</h3><p class="muted">${published ? `Published ${esc(published)}` : 'Downloaded and ready to install'}${data.changes?.total ? ` · ${data.changes.total} change${data.changes.total === 1 ? '' : 's'}` : ''}</p></div>
      <button class="button button-primary" type="button" data-control="release"${running ? ' disabled' : ''}>${icon('up')}Update now</button>
    </div>${changeList(data.changes, available)}`;
  } else if (current.channel !== 'dev') {
    const checked = when(data.checkedAt, {weekday: 'short', hour: '2-digit', minute: '2-digit'});
    body += `<p class="release-current">${icon('check')}<span>${data.checking || releaseCheck.pending ? 'Checking for a newer version…'
      : checked ? `Up to date. Last checked ${esc(checked)}; MartinFlix checks again every six hours.` : 'Checks for updates a couple of minutes after starting, then every six hours.'}</span></p>`;
  }
  $('#release-content').innerHTML = body;
}

function renderRun(updater) {
  const panel = $('#setup-run');
  // Backups shows restores; Settings shows updates and settings changes.
  // Finishing a stopped restore shows on Backups too.
  const restoreSide = updater && (updater.kind === 'restore' || (updater.kind === 'finish' && updater.detail === 'restore'));
  const here = updater && (page === 'backups') === restoreSide;
  const recent = here && (updater.state === 'running' || Date.now() - new Date(updater.finishedAt) < 86400000);
  panel.hidden = !recent;
  if (!recent) return;
  const taken = updater.detail && updater.kind === 'restore' ? when(updater.detail.replace(/^(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})$/, '$1T$2:$3:$4')) || updater.detail : '';
  const titles = {
    update: {running: `Updating to ${updater.version}…`, succeeded: `Updated to ${updater.version}`, failed: 'The update did not finish'},
    settings: {running: 'Applying your settings…', succeeded: 'Settings applied', failed: 'The settings could not be applied'},
    restore: {running: `Restoring the backup from ${taken}…`, succeeded: `Restored the backup from ${taken}`, failed: 'The restore did not finish'},
    finish: {running: 'Running setup again…', succeeded: 'Setup finished', failed: 'Setup still did not finish'},
  }[updater.kind] || {running: 'Running setup…', succeeded: 'Setup finished', failed: 'Setup did not finish'};
  $('#setup-run-title').textContent = titles[updater.state];
  const started = when(updater.startedAt, {hour: '2-digit', minute: '2-digit'});
  const finished = when(updater.finishedAt, {hour: '2-digit', minute: '2-digit'});
  $('#setup-run-detail').textContent = updater.state === 'running'
    ? updater.kind === 'restore' ? `Started ${started}. The apps stop while their data is replaced; follow it here. The dashboard restarts at the end, and this page reconnects by itself.`
      : `Started ${started}. The dashboard restarts near the end and this page reloads by itself.`
    : updater.state === 'failed' ? `Stopped at ${finished}, at the step marked below; nothing after it changed.`
    : `Finished at ${finished}.`;
  $('#setup-run-badge').innerHTML = updater.state === 'running' ? '<span class="badge">Running</span>'
    : updater.state === 'succeeded' ? '<span class="badge ok">Done</span>' : '<span class="badge crit">Failed</span>';
  $('#setup-run-actions').hidden = updater.state !== 'failed';
  // A second try at finishing a restore is still about that restore.
  $('#setup-again').dataset.kind = updater.kind === 'finish' ? updater.detail || 'finish' : updater.kind;
  renderSteps(updater);
  const log = $('#setup-log');
  const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = updater.log || 'Starting…';
  if (atEnd || updater.state === 'running') log.scrollTop = log.scrollHeight;
}

/** Setup's steps as a checklist, from the progress it reports; the raw output stays one click away. */
function renderSteps(updater) {
  const steps = updater.progress?.steps;
  const list = $('#setup-steps');
  const raw = $('#setup-raw');
  if (!Array.isArray(steps) || !steps.length) {
    // An older setup, or one that hasn't reported yet: the output itself is the progress.
    list.hidden = true;
    $('#setup-message').hidden = true;
    raw.open = true;
    raw.dataset.auto = 'open';
    return;
  }
  if (raw.dataset.auto === 'open') { raw.open = false; delete raw.dataset.auto; }
  const failed = updater.state === 'failed';
  list.innerHTML = steps.map((step, index) => {
    // A step still "running" when setup has finished is the one it stopped at.
    const state = step.state === 'running' && updater.state !== 'running' ? (failed ? 'failed' : 'done') : step.state;
    const mark = {done: icon('tick'), failed: icon('alert'), running: '<span class="spinner" aria-hidden="true"></span>'}[state] || '';
    const apps = Object.entries(step.apps || {});
    const detail = state === 'running' && step.note ? `<p class="setup-note">${esc(step.note)}</p>` : '';
    const appList = apps.length && (state === 'running' || state === 'failed')
      ? `<ul class="setup-apps">${apps.map(([name, text]) => `<li><span>${esc(name)}</span> ${esc(text)}</li>`).join('')}</ul>` : '';
    return `<li class="is-${esc(state)}"><span class="setup-mark">${mark}</span><div><p>${esc(step.title)}</p>${detail}${appList}</div>
      <span class="sr-only">${{done: 'done', failed: 'failed', running: 'in progress'}[state] || ''}</span></li>`;
  }).join('');
  list.hidden = false;
  const message = $('#setup-message');
  message.textContent = failed ? updater.progress.message || 'Setup stopped. Its output below says why.' : '';
  message.hidden = !failed;
}

async function system() {
  clearTimeout(followTimer);
  let data;
  try {
    data = await get('/api/system');
  } catch {
    if (following) {
      // The dashboard itself restarts during an update. Keep asking until the new one answers.
      $('#setup-run-detail').textContent = 'MartinFlix is restarting. This page reloads when it’s back…';
      followTimer = setTimeout(system, 3000);
    } else {
      $('#release-summary').textContent = 'Could not reach the controller.';
    }
    return;
  }
  if (!data.configured) {
    $('#release-summary').textContent = 'Updates and settings need the controller. Run the update once from a terminal.';
    return;
  }
  // A newer dashboard is running than the one that served this page: load the new page.
  if (loadedVersion && data.current.version !== loadedVersion) {
    window.location.reload();
    return;
  }
  loadedVersion = data.current.version;
  systemData = data;
  renderRelease(data);
  renderRun(data.updater);
  const state = data.updater?.state;
  const running = state === 'running';
  $('#settings-form').inert = running;
  if (lastRunState === 'running' && !running) {
    following = false;
    if (data.updater.kind !== 'update' && page === 'settings') loadSettings();
    if (page === 'backups') status();
  }
  lastRunState = state;
  if (running || following || data.checking || releaseCheck.pending) {
    followTimer = setTimeout(system, running || following ? 3000 : releaseCheck.pending ? 1500 : 5000);
  }
}

function startRestore(name, created) {
  const taken = when(created) || name;
  askPin({title: `Restore the backup from ${taken}?`, verb: 'Restore', busy: 'Starting…', icon: 'refresh',
    detail: `Every app’s settings and data go back to how they were on ${taken}: series and films added since are forgotten, and the PIN and dashboard password become the ones from then. What it replaces is kept aside on the VM, not deleted. Media files stay as they are. The apps stop for a few minutes; follow it here.`},
  code => post('/api/restore', {pin: code, backup: name}),
  () => { following = true; lastRunState = 'running'; system(); });
}

function setupAgain() {
  const kind = $('#setup-again').dataset.kind;
  askPin({title: 'Run setup again?', verb: 'Run setup', busy: 'Starting…', icon: 'refresh',
    detail: kind === 'restore' || kind === 'finish'
      ? 'Setup starts every app on the data that’s here now and finishes connecting them. The backup isn’t restored a second time. The dashboard restarts near the end, and this page reloads by itself.'
      : 'Setup runs again with your current settings and starts anything that isn’t running. The dashboard restarts near the end, and this page reloads by itself.'},
  code => post('/api/system/finish', {pin: code, kind}),
  () => { following = true; lastRunState = 'running'; system(); });
}

function startUpdate() {
  const available = systemData?.available;
  if (!available) return;
  askPin({title: `Update to MartinFlix ${available.version}?`, verb: 'Update', busy: 'Starting…', icon: 'up',
    detail: 'Setup runs again from the new version and keeps your settings. Apps restart only if the new version changes them. The dashboard restarts near the end, and this page reloads by itself.'},
  code => post('/api/system/update', {pin: code}),
  () => { following = true; lastRunState = 'running'; system(); });
}

function setOptions(select, values, current) {
  const options = [...values];
  if (current && !options.includes(current)) options.unshift(current);
  select.innerHTML = options.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
  select.value = current || options[0] || '';
}

function fillSettings(data) {
  const form = $('#settings-form');
  form.tmdbKey.value = form.plexToken.value = form.vpnUser.value = form.vpnPassword.value = form.telegramToken.value = '';
  form.telegramOn.checked = Boolean(data.telegram);
  form.telegramChat.value = data.telegramChat || '';
  $('#setting-telegram-token-help').innerHTML = data.telegram ? 'A token is saved. Paste another to replace it.'
    : 'In Telegram, message <strong>@BotFather</strong>, send <code>/newbot</code> and follow its questions. It replies with the token, such as <code>123456789:AAH…</code>.';
  $('#telegram-chats').hidden = true;
  $('#telegram-notice').hidden = true;
  form.tmdbClear.checked = false;
  form.discover.checked = data.discover !== 'off';
  if (![...form.discoverRegion.options].some(option => option.value === data.discoverRegion)) {
    form.discoverRegion.insertAdjacentHTML('afterbegin', `<option value="${esc(data.discoverRegion)}">${esc(data.discoverRegion)}</option>`);
  }
  form.discoverRegion.value = data.discoverRegion;
  $('#setting-tmdb-help').textContent = data.ownTmdbKey ? 'Discover uses your key. Paste another to replace it.'
    : 'Discover uses MartinFlix’s key. Paste a key from TMDB’s API settings to use yours.';
  $('#setting-tmdb-clear-row').hidden = !data.ownTmdbKey;
  $('#setting-plex-name-row').hidden = !data.plex;
  $('#setting-plex-url-row').hidden = data.plex;
  $('#setting-plex-token-row').hidden = data.plex;
  $('#settings-plex-detail').textContent = data.plex ? 'This machine runs Plex for your media folder.'
    : 'A Plex server elsewhere, for Live’s now playing and watch history.';
  form.plexName.value = data.plexName;
  form.plexUrl.value = data.plexUrl;
  $('#setting-plex-token-help').textContent = data.plexToken ? 'A token is saved. Paste another to replace it.'
    : 'In Plex Web, open any title’s ⋯ → Get Info → View XML, and copy the value after X-Plex-Token= in the address.';
  form.backupDir.value = data.backupDir;
  $('#settings-vpn').hidden = !data.vpn;
  setOptions(form.vpnLocation, data.choices.vpnLocations, data.vpnLocation);
  form.vpnProtocol.value = data.vpnProtocol;
  form.channel.value = data.channel;
  // Setup installs apps but never removes them, so once on, the switch stays on.
  form.audiobooks.checked = form.audiobooks.disabled = Boolean(data.audiobooks);
  $('#setting-audiobooks-help').textContent = data.audiobooks
    ? 'Installed. Your audiobooks are in the Audiobooks folder of your media folder; both apps are on the Services page.'
    : 'Installs both, with an Audiobooks folder in your media folder. Afterwards, save your dashboard password again (Change password) so they sign in with it too.';
  form.hidden = false;
  settingsData = data;
  showSettingsChanges();
  showTelegram();
}

function settingsChanges() {
  const form = $('#settings-form');
  const data = settingsData;
  const settings = {};
  const secrets = {};
  const discover = form.discover.checked ? 'on' : 'off';
  if (discover !== data.discover) settings.discover = discover;
  if (form.discoverRegion.value !== data.discoverRegion) settings.discoverRegion = form.discoverRegion.value;
  const key = form.tmdbKey.value.trim();
  if (key) secrets.tmdbKey = key;
  else if (form.tmdbClear.checked) secrets.tmdbKey = '';
  if (data.plex) {
    if (form.plexName.value.trim() !== data.plexName) settings.plexName = form.plexName.value.trim();
  } else {
    if (form.plexUrl.value.trim().replace(/\/$/, '') !== data.plexUrl) settings.plexUrl = form.plexUrl.value.trim();
    if (form.plexToken.value.trim()) secrets.plexToken = form.plexToken.value.trim();
  }
  if (form.backupDir.value.trim() !== data.backupDir) settings.backupDir = form.backupDir.value.trim();
  if (data.vpn) {
    if (form.vpnLocation.value !== data.vpnLocation) settings.vpnLocation = form.vpnLocation.value;
    if (form.vpnProtocol.value !== data.vpnProtocol) settings.vpnProtocol = form.vpnProtocol.value;
    if (form.vpnUser.value.trim() || form.vpnPassword.value) {
      secrets.vpnUser = form.vpnUser.value.trim();
      secrets.vpnPassword = form.vpnPassword.value;
    }
  }
  if (form.channel.value !== data.channel) settings.channel = form.channel.value;
  if (!data.audiobooks && form.audiobooks.checked) settings.audiobooks = 'on';
  const telegramToken = form.telegramToken.value.trim();
  const telegramChat = form.telegramChat.value.trim();
  if (!form.telegramOn.checked) {
    if (data.telegram) secrets.telegramToken = '';
  } else if (telegramToken || !data.telegram) {
    secrets.telegramToken = telegramToken;
    secrets.telegramChat = telegramChat;
  } else if (telegramChat !== (data.telegramChat || '')) {
    secrets.telegramChat = telegramChat;
  }
  return {settings, secrets};
}

function settingsProblem({settings, secrets}) {
  if ('plexName' in settings && !/^[\p{L}\p{N}_][\p{L}\p{N}_ .()'-]{0,49}$/u.test(settings.plexName)) return 'Use up to 50 letters, numbers, spaces, dots, dashes or brackets for the Plex server name.';
  if ('plexUrl' in settings && !/^(https?:\/\/[A-Za-z0-9.\[\]:-]+(:[0-9]{1,5})?\/?)?$/.test(settings.plexUrl)) return 'Use the Plex server’s address, such as http://192.168.1.10:32400.';
  if ('backupDir' in settings && (!settings.backupDir.startsWith('/') || settings.backupDir.includes(','))) return 'Use a full path for the backup folder, starting with /, without commas.';
  if (secrets.tmdbKey && !/^[A-Za-z0-9._-]{20,1024}$/.test(secrets.tmdbKey)) return 'That doesn’t look like a TMDB API key. Copy it again from TMDB’s API settings.';
  if (secrets.plexToken && !/^[A-Za-z0-9_-]{10,200}$/.test(secrets.plexToken)) return 'That doesn’t look like a Plex token.';
  if ('telegramChat' in secrets && 'telegramToken' in secrets && !secrets.telegramToken) return 'Paste the bot token from @BotFather.';
  if (secrets.telegramToken && !/^[0-9]{5,12}:[A-Za-z0-9_-]{30,64}$/.test(secrets.telegramToken)) return 'That doesn’t look like a Telegram bot token. Copy it again from @BotFather.';
  if ('telegramChat' in secrets && !/^(-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$/.test(secrets.telegramChat)) return 'Enter the chat ID, or choose Find chats.';
  if ('vpnUser' in secrets && (!secrets.vpnUser || !secrets.vpnPassword || /\s/.test(secrets.vpnUser + secrets.vpnPassword))) return 'Enter both the new NordVPN service username and password.';
  return '';
}

function showSettingsChanges() {
  const changes = settingsChanges();
  const names = [...new Set([...Object.keys(changes.settings), ...Object.keys(changes.secrets)
    .map(name => ({vpnPassword: 'vpnUser', telegramChat: 'telegramToken'})[name] || name)])]
    .map(name => SETTING_LABELS[name] || name);
  const line = $('#settings-changes');
  line.classList.remove('is-problem');
  line.textContent = names.length ? `Changed: ${names.join(', ')}.` : 'No changes.';
  $('#settings-save').disabled = !names.length;
  $('.settings-bar').classList.toggle('has-changes', names.length > 0);
  $('#settings-reset').disabled = !names.length;
  return names;
}

async function loadSettings() {
  try {
    const data = await get('/api/settings');
    if (!data.configured) return;
    fillSettings(data);
  } catch (error) {
    $('#settings-notice').textContent = `Could not load the settings: ${error.message}`;
    $('#settings-notice').hidden = false;
  }
}

/** Telegram's fields show when it is switched on; the test button once a saved setup is unchanged. */
function showTelegram() {
  const form = $('#settings-form');
  const on = form.telegramOn.checked;
  $('#setting-telegram-token-row').hidden = $('#setting-telegram-chat-row').hidden = !on;
  const changed = Object.keys(settingsChanges().secrets).some(name => name.startsWith('telegram'));
  $('#telegram-test').hidden = !(settingsData?.telegram && on && !changed);
}

function telegramNotice(text, problem = false) {
  const notice = $('#telegram-notice');
  notice.textContent = text;
  notice.classList.toggle('is-problem', problem);
  notice.hidden = !text;
}

function setupTelegram() {
  const form = $('#settings-form');
  form.addEventListener('input', showTelegram);
  form.addEventListener('change', showTelegram);
  $('#telegram-find').addEventListener('click', async () => {
    const button = $('#telegram-find');
    button.disabled = true;
    telegramNotice('');
    try {
      const data = await post('/api/telegram/chats', {token: form.telegramToken.value.trim()});
      const list = $('#telegram-chats');
      list.innerHTML = data.chats.length
        ? data.chats.map(chat => `<button class="chat-choice" type="button" data-chat="${esc(chat.id)}">${esc(chat.name)} <small>${esc(chat.id)}</small></button>`).join('')
        : '<p class="option-help">No chats yet. Send your bot a message in Telegram, then choose Find chats again.</p>';
      list.hidden = false;
    } catch (error) {
      telegramNotice(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $('#telegram-chats').addEventListener('click', event => {
    const chip = event.target.closest('[data-chat]');
    if (!chip) return;
    form.telegramChat.value = chip.dataset.chat;
    for (const other of $('#telegram-chats').querySelectorAll('[data-chat]')) other.classList.toggle('is-chosen', other === chip);
    form.telegramChat.dispatchEvent(new Event('input', {bubbles: true}));
  });
  $('#telegram-test').addEventListener('click', async () => {
    const button = $('#telegram-test');
    button.disabled = true;
    try {
      await post('/api/telegram/test', {});
      telegramNotice('Sent. Check the chat in Telegram.');
    } catch (error) {
      telegramNotice(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

/** Settings changed but not saved: their names, or none while a save is already running. */
function unsavedSettings() {
  if (!settingsData || following || lastRunState === 'running') return [];
  const changes = settingsChanges();
  return [...new Set([...Object.keys(changes.settings), ...Object.keys(changes.secrets)
    .map(name => ({vpnPassword: 'vpnUser', telegramChat: 'telegramToken'})[name] || name)])]
    .map(name => SETTING_LABELS[name] || name);
}

/** Leaving Settings with changes not saved asks first: in the page for its own links, in the browser otherwise. */
function guardUnsavedSettings() {
  const dialog = $('#leave-dialog');
  let destination = null;
  let leaving = false;
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (link.target && link.target !== '_self') return;
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin || (url.pathname === window.location.pathname && url.hash)) return;
    const names = unsavedSettings();
    if (!names.length) return;
    event.preventDefault();
    destination = url.href;
    const listed = names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names.at(-1)}` : names[0];
    $('#leave-detail').textContent = `Your changes to ${listed} aren’t saved yet.`;
    dialog.showModal();
    $('#leave-save').focus();
  });
  $('#leave-stay').addEventListener('click', () => {
    dialog.close();
    $('#settings-save').focus();
  });
  $('#leave-discard').addEventListener('click', () => {
    leaving = true;
    dialog.close();
    window.location.assign(destination);
  });
  // Saving asks for the PIN and runs setup here, so the page stays to show it.
  $('#leave-save').addEventListener('click', () => {
    dialog.close();
    $('#settings-form').requestSubmit();
  });
  window.addEventListener('beforeunload', event => {
    if (leaving || !unsavedSettings().length) return;
    event.preventDefault();
    event.returnValue = '';
  });
}

function setupSettings() {
  guardUnsavedSettings();
  const form = $('#settings-form');
  setupTelegram();
  form.addEventListener('input', showSettingsChanges);
  form.addEventListener('change', showSettingsChanges);
  form.addEventListener('reset', () => setTimeout(() => settingsData && fillSettings(settingsData)));
  $('#release-check').addEventListener('click', async () => {
    Object.assign(releaseCheck, {pending: true, seen: false, startedAt: Date.now(), before: systemData?.checkedAt ?? null, result: null, until: 0});
    if (systemData) renderRelease(systemData);
    try {
      await post('/api/system/check', {});
    } catch (error) {
      releaseCheck.pending = false;
      $('#settings-notice').textContent = error.message;
      $('#settings-notice').hidden = false;
      if (systemData) renderRelease(systemData);
      return;
    }
    setTimeout(system, 1500);
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    const changes = settingsChanges();
    const names = showSettingsChanges();
    if (!names.length) return;
    const problem = settingsProblem(changes);
    if (problem) {
      $('#settings-changes').textContent = problem;
      $('#settings-changes').classList.add('is-problem');
      return;
    }
    const vpn = changes.settings.vpnLocation || changes.settings.vpnProtocol || changes.secrets.vpnUser;
    askPin({title: 'Save these settings?', verb: 'Save', busy: 'Starting…', icon: 'check',
      detail: `Setup runs again with the new ${names.join(', ').toLowerCase()}. ${vpn ? 'The VPN reconnects and downloads pause for a minute or so. ' : ''}It takes a minute or two; follow it here.`},
    code => post('/api/settings', {pin: code, ...changes}),
    () => {
      form.tmdbKey.value = form.plexToken.value = form.vpnUser.value = form.vpnPassword.value = form.telegramToken.value = '';
      following = true;
      lastRunState = 'running';
      system();
    });
  });
}

let refreshing = false;
/** "Fri 12 Dec" for upcoming dates, the year for older ones. */
function discoverDate(item) {
  if (!item.date) return '';
  const when = new Date(item.date + 'T00:00:00');
  return when > new Date() ? upcomingDay(item.date + 'T00:00:00') : String(item.year);
}

function discoverOwned() {
  return `<span class="discover-owned">${icon('tick')}In your library</span>`;
}

function discoverCard(item) {
  const options = `/add?${new URLSearchParams({kind: item.kind, find: `tmdb:${item.id}`, title: item.title})}`;
  // Hovering, focusing or tapping the poster shows the synopsis and Add over it. Add uses the usual defaults;
  // Options opens the Add page to choose the quality profile, seasons or folder first.
  return `<li class="discover-card${item.inLibrary ? ' is-owned' : ''}" data-kind="${esc(item.kind)}" data-tmdb="${esc(item.id)}">
    <div class="poster-art" tabindex="0" aria-label="${esc(item.title)}${item.inLibrary ? ', in your library' : ''}: show synopsis">
      <span class="poster-placeholder" aria-hidden="true">${esc(item.title)}</span>
      ${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" width="342" height="513">` : ''}
      <span class="owned-mark" title="In your library">${icon('tick')}<span class="sr-only">In your library</span></span>
      <div class="discover-hover">
        <p>${esc(item.overview || 'No synopsis yet.')}</p>
        <div class="discover-hover-action">${item.inLibrary ? discoverOwned()
          : `<button class="button button-primary button-small" type="button" data-discover-add aria-label="Add ${esc(item.title)}">${icon('plus')}Add</button>
             <a class="discover-options" href="${esc(options)}" aria-label="Add ${esc(item.title)} with options">Options</a>`}
          <a class="discover-options" href="https://www.themoviedb.org/${item.kind === 'sonarr' ? 'tv' : 'movie'}/${encodeURIComponent(item.id)}" target="_blank" rel="noopener noreferrer" aria-label="${esc(item.title)} on TMDB">TMDB${icon('external')}</a></div>
        <p class="discover-problem" role="alert" hidden></p>
      </div>
    </div>
    <h3 class="poster-title">${esc(item.title)}</h3>
    <p class="poster-meta">${esc(discoverDate(item))}</p>
  </li>`;
}

function setupDiscover() {
  $('#discover-content').addEventListener('click', async event => {
    const button = event.target.closest('[data-discover-add]');
    if (!button || button.disabled) return;
    const card = button.closest('.discover-card');
    const problem = card.querySelector('.discover-problem');
    button.disabled = true;
    button.classList.add('is-busy');
    button.innerHTML = `${icon('refresh')}Adding…`;
    problem.hidden = true;
    try {
      await post(`/api/add/${card.dataset.kind}`, {tmdb: Number(card.dataset.tmdb)});
      card.classList.add('is-owned');
      card.querySelector('.discover-hover-action').innerHTML = discoverOwned();
      card.querySelector('.poster-art').setAttribute('aria-label', `${card.querySelector('.poster-title').textContent}, in your library: show synopsis`);
    } catch (error) {
      button.disabled = false;
      button.classList.remove('is-busy');
      button.innerHTML = `${icon('plus')}Add`;
      problem.textContent = error.message;
      problem.hidden = false;
    }
  });
}

let discoverLoaded = 0;
async function discover() {
  // TMDB lists change slowly, so the minute-by-minute refresh doesn't reload them.
  if (Date.now() - discoverLoaded < 15 * 60000) return;
  const content = $('#discover-content');
  try {
    const data = await get('/api/discover');
    discoverLoaded = Date.now();
    content.innerHTML = data.rows.map(row => `<section class="discover-row" aria-labelledby="discover-${esc(row.key)}">
      <div class="discover-head"><h2 id="discover-${esc(row.key)}">${esc(row.title)}</h2><p>${esc(row.detail)}</p></div>
      ${row.error ? `<p class="error error-inline">${esc(row.error)}</p>`
        : row.items.length ? `<ul class="discover-rail">${row.items.map(discoverCard).join('')}</ul>`
        : '<p class="empty">Nothing here right now.</p>'}
    </section>`).join('');
    hidePosterOnError(content);
  } catch (error) {
    content.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

async function refresh() {
  if (refreshing || page === 'add') return;
  refreshing = true;
  const button = $('#refresh');
  button.disabled = true;
  button.classList.add('is-busy');
  const jobs = [status()];
  if (page === 'discover') jobs.push(discover());
  if (page === 'live') jobs.push(live(), watchHistory());
  if (page === 'overview') jobs.push(transferSummary());
  if ((page === 'settings' || page === 'backups') && !following) jobs.push(system());
  if (page === 'overview' || page === 'library') {
    jobs.push(activity());
    for (const library of Object.values(libraries)) jobs.push(loadLibrary(library));
  }
  await Promise.allSettled(jobs);
  button.disabled = false;
  button.classList.remove('is-busy');
  refreshing = false;
}

if (page === 'services' || page === 'backups' || page === 'settings') setupControls();
if (page === 'live') {
  setupSpeedChart();
  setupHistory();
}
if (page === 'discover') setupDiscover();
if (page === 'settings') {
  setupSettings();
  loadSettings();
}
// Live figures move faster than the rest: every five seconds on Live, ten on the Overview's summary.
if (page === 'live') setInterval(() => { if (!document.hidden) live(); }, 5000);
if (page === 'overview') setInterval(() => { if (!document.hidden) transferSummary(); }, 10000);
$('#refresh').addEventListener('click', refresh);
$('#refresh').hidden = page === 'add';
if (page === 'add') {
  status();
  $('#updated').hidden = true;
  if (new URLSearchParams(window.location.search).has('find')) $('#add-form').requestSubmit();
} else {
  refresh();
}
setInterval(() => { if (!document.hidden) refresh(); }, 60000);
