'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const icon = (name, extra = '') => `<svg class="icon${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const page = document.body.dataset.page;
const pageTitles = {overview: 'Overview', library: 'Library', services: 'Services', backups: 'Backups'};
const legacyAnchors = {'#series': '/library#series', '#films': '/library#films', '#library': '/library', '#backups': '/backups', '#services': '/services'};
if (page === 'overview' && legacyAnchors[window.location.hash]) window.location.replace(legacyAnchors[window.location.hash]);
$('#page-title').textContent = pageTitles[page];
document.title = page === 'overview' ? 'MartinFlix' : `${pageTitles[page]} · MartinFlix`;
document.querySelector(`[data-nav="${page}"]`)?.setAttribute('aria-current', 'page');
for (const section of document.querySelectorAll('.page')) section.hidden = section.dataset.for !== page;

let catalog = {apps: {}, branches: {}, startAfter: {}, pipeline: []};
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
  if (!health.alerts.length) return '<div class="app-health"><span class="badge ok">No app alerts</span></div>';
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

function serviceCard(service, groupServices, countLabel) {
  const app = appInfo(service.name);
  const href = serviceHref(service);
  const title = href
    ? `<a class="service-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(app.name)}${icon('external', ' icon-small')}<span class="sr-only"> (opens in a new tab)</span></a>`
    : esc(app.name);
  const {label, tone} = serviceState(service);
  const dependencies = (catalog.startAfter[service.name] ?? []).filter(name => groupServices.some(item => item.name === name));
  const dependencyNote = dependencies.length ? `<p class="service-note">Own network · ${countLabel === 'reachable' ? 'Uses' : 'Starts after'}
    ${dependencies.map(name => esc(appInfo(name).name)).join(' and ')}${countLabel === 'reachable' ? '' : ' are healthy'}.</p>` : '';
  const unavailable = dependencies.filter(name => {
    const dependency = groupServices.find(item => item.name === name);
    return dependency && needsAttention(dependency);
  });
  const localOnly = service.web?.localOnly && !href;
  return `<li class="service-card tone-${tone}">
    <div class="service-identity">
      ${appIcon(service.name)}
      <div><h4>${title}</h4><p>${esc(app.role)}</p></div>
    </div>
    <div class="service-status"><span class="badge ${tone}">${esc(label)}</span><span class="service-detail">${esc(service.status || 'No status detail')}</span></div>
    ${appHealth(service)}
    ${localOnly ? '<p class="service-note">Web interface available on the host only.</p>' : ''}
    ${dependencyNote}
    ${unavailable.length ? `<p class="service-warning">${icon('alert', ' icon-small')}Check ${unavailable.map(name => esc(appInfo(name).name)).join(' and ')}</p>` : ''}
  </li>`;
}

function serviceGroup(group, countLabel, showVpnBranch) {
  const services = group.services;
  const running = services.filter(serviceIsUp).length;
  const nested = new Set();
  const branches = services.flatMap(parent => {
    const branch = parent.name === 'nordvpn' && !showVpnBranch ? null : catalog.branches[parent.name];
    if (!branch) return [];
    const children = services.filter(service => branch.members.includes(service.name));
    if (!children.length) return [];
    nested.add(parent.name);
    children.forEach(child => nested.add(child.name));
    return [`<div class="branch">
      <ul class="branch-parent">${serviceCard(parent, services, countLabel)}</ul>
      <div class="branch-children">
        <h4>${esc(branch.title)}</h4>
        <p>${esc(branch.description)}</p>
        ${needsAttention(parent) ? `<p class="service-warning">${icon('alert', ' icon-small')}Upstream: check ${esc(appInfo(parent.name).name)}</p>` : ''}
        <ul class="service-grid">${children.map(child => serviceCard(child, services, countLabel)).join('')}</ul>
      </div>
    </div>`];
  });
  const standalone = services.filter(service => !nested.has(service.name));
  const compact = branches.length === 0 && services.length <= 2;
  return `<section class="service-group${compact ? ' is-compact' : ''}">
    <div class="group-title"><h3>${esc(group.name)}</h3><span>${running} of ${services.length} ${countLabel}</span></div>
    ${branches.join('')}
    ${standalone.length ? `<ul class="service-grid">${standalone.map(service => serviceCard(service, services, countLabel)).join('')}</ul>` : ''}
  </section>`;
}

function renderPipeline(docker, services) {
  const byName = Object.fromEntries(services.map(service => [service.name, service]));
  const gateway = byName.nordvpn;
  const tunnelled = gateway && (docker.source !== 'app-probes' || docker.downloadsBehindVpn);
  const stages = catalog.pipeline
    .map(stage => ({...stage, members: stage.services.map(name => byName[name]).filter(Boolean)}))
    .filter(stage => stage.members.length);
  if (!stages.length) {
    $('#pipeline').innerHTML = '<p class="empty">No pipeline services are installed yet.</p>';
    return;
  }
  const items = stages.map(stage => {
    const down = stage.members.filter(member => serviceState(member).tone === 'crit' || serviceState(member).tone === 'warn');
    const tone = down.length ? 'crit' : stage.members.some(member => healthIssues(member).length) ? 'warn' : 'ok';
    const summary = down.length ? `${down.length} down` : tone === 'warn' ? 'Has alerts' : 'Flowing';
    return `<li class="stage tone-${tone}${stage.tunnel && tunnelled ? ' is-tunnelled' : ''}">
      <div class="stage-head"><h3>${esc(stage.name)}</h3><span class="badge ${tone}">${summary}</span></div>
      <p>${esc(stage.detail)}</p>
      <ul class="stage-apps">${stage.members.map(member => {
        const {label, tone: memberTone} = serviceState(member);
        return `<li>${appIcon(member.name)}<span>${esc(appInfo(member.name).name)}</span><span class="dot ${memberTone}" title="${esc(label)}"></span><span class="sr-only">${esc(label)}</span></li>`;
      }).join('')}</ul>
    </li>`;
  }).join('');
  let tunnel = '';
  if (tunnelled) {
    const {label, tone} = serviceState(gateway);
    tunnel = `<p class="tunnel tone-${tone}">${icon('tunnel')}<span><strong>Find and Download run through the Gluetun VPN tunnel.</strong> Gateway ${esc(label.toLowerCase())}.</span></p>`;
  }
  $('#pipeline').innerHTML = `<ol class="pipeline">${items}</ol>${tunnel}`;
}

function attentionRows(docker, services) {
  const rows = [];
  if (!docker.available) rows.push({tone: 'crit', name: 'Service status', message: docker.error || 'Status unavailable'});
  for (const service of services) {
    if (appInfo(service.name).optional) continue;
    const {label, tone} = serviceState(service);
    if (tone !== 'ok') rows.push({tone, name: appInfo(service.name).name, message: `${label} · ${service.status}`});
    for (const issue of healthIssues(service)) {
      rows.push({tone: issue.severity === 'error' ? 'crit' : 'warn', name: appInfo(service.name).name, message: issue.message});
    }
  }
  return rows;
}

function renderAttention(docker, services) {
  const rows = attentionRows(docker, services);
  $('#attention-panel').hidden = rows.length === 0;
  $('#attention-list').innerHTML = rows.map(row => `<li class="tone-${row.tone}">
    ${icon('alert')}<strong>${esc(row.name)}</strong><span>${esc(row.message)}</span></li>`).join('');
  return rows.length;
}

function renderOverview(docker, services, backups, probes) {
  const required = services.filter(service => !appInfo(service.name).optional);
  const attention = renderAttention(docker, services);
  $('#service-summary').classList.toggle('is-alert', !docker.available || attention > 0);
  $('#services-metric-label').textContent = probes ? 'Services reachable' : 'Services running';
  $('#running').textContent = docker.available ? `${required.filter(serviceIsUp).length} of ${required.length}` : 'Unavailable';
  $('#services-summary-detail').textContent = !docker.available ? 'Status unavailable'
    : attention ? `${attention} ${attention === 1 ? 'issue needs' : 'issues need'} attention` : 'Everything required is up';
  renderPipeline(docker, services);
  const latest = backups[0];
  const incomplete = latest && (latest.errors?.length || latest.missing?.length);
  $('#backup-summary').classList.toggle('is-warn', Boolean(incomplete));
  $('#latest-backup').textContent = latest ? relativeDay(latest.created) : 'None yet';
  $('#backup-detail').textContent = latest ? (incomplete ? 'Incomplete snapshot' : 'Captured · restore untested') : 'No snapshots found';
}

function renderBackups(backups) {
  $('#backups-content').innerHTML = backups.length
    ? `<table class="table"><thead><tr><th scope="col">Captured</th><th scope="col" class="num">Archives</th><th scope="col">Result</th></tr></thead><tbody>${
      backups.map(backup => {
        const problems = [...(backup.missing || []), ...(backup.errors || [])];
        return `<tr><td>${esc(date(backup.created))}</td><td class="num">${esc(backup.files ?? 0)}</td><td>${problems.length
          ? `<span class="badge warn">Incomplete</span> <span class="muted">Missing ${esc(problems.join(', '))}</span>`
          : '<span class="badge ok">Captured</span> <span class="muted">Restore untested</span>'}</td></tr>`;
      }).join('')}</tbody></table>`
    : '<div class="empty-state"><h3>No snapshots yet</h3><p>Run <code>python3 backup.py create</code> on the host to capture the latest app backups. They appear here once a manifest is written.</p></div>';
}

const servicesPage = page === 'services';

async function get(url) {
  const response = await fetch(url, {cache: 'no-store'});
  if (response.status === 401 && response.headers.get('X-MartinFlix-Login') === 'required') window.location.assign('/login');
  if (!response.ok) throw new Error('Request failed');
  return response.json();
}

function date(value) {
  const stamp = new Date(value);
  return isNaN(stamp) ? 'Unknown' : stamp.toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});
}

function relativeDay(value) {
  const stamp = new Date(value);
  if (isNaN(stamp)) return 'Unknown';
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(stamp).setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days} days ago`;
  return stamp.toLocaleDateString(undefined, {day: 'numeric', month: 'short'});
}

async function status() {
  try {
    const data = await get('/api/status');
    catalog = data.catalog || catalog;
    const docker = data.docker;
    const probes = docker.source === 'app-probes';
    const services = docker.groups.flatMap(group => group.services);
    $('#nav-password').hidden = !data.canChangePassword;
    const problems = attentionRows(docker, services).length;
    $('#nav-flag').hidden = problems === 0;
    $('#nav-flag').innerHTML = `${problems}<span class="sr-only"> ${problems === 1 ? 'issue' : 'issues'}</span>`;
    const seerr = services.find(service => service.name === 'seerr' && serviceIsUp(service) && serviceHref(service));
    $('#seerr-link').hidden = !seerr;
    if (seerr) $('#seerr-link').href = serviceHref(seerr);
    if (servicesPage) {
      $('#services-description').textContent = probes
        ? 'Live health checks for the services this installer set up.'
        : 'Container status from Docker, grouped by stack, with each app’s own health alerts.';
      $('#services-source').textContent = probes
        ? 'Reachability shows which apps respond. Connected Arr apps also report their own health alerts.'
        : 'Docker reports container status once a minute. Connected Arr apps also report their own health alerts.';
      $('#services-content').innerHTML = docker.available
        ? docker.groups.map(group => serviceGroup(group, probes ? 'reachable' : 'running', !probes || docker.downloadsBehindVpn)).join('')
        : `<p class="empty">${esc(docker.error)}</p>`;
    } else if (page === 'overview') {
      renderOverview(docker, services, data.backups.snapshots, probes);
    } else if (page === 'backups') {
      renderBackups(data.backups.snapshots);
    }
    $('#updated').textContent = 'Updated ' + new Date().toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
  } catch {
    $('#updated').textContent = 'Could not refresh';
    $('#running').textContent = 'Unavailable';
    $('#services-summary-detail').textContent = 'Could not load status';
    $('#service-summary').classList.add('is-alert');
    $('#pipeline').innerHTML = '<p class="empty">Could not load status. Check the dashboard server and try again.</p>';
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
    $('#plex-streams').textContent = plex.streams ?? '—';
    $('#plex-detail').textContent = !plex.configured ? 'Not connected' : plex.error ||
      (plex.streams ? 'Active via Tautulli' : 'Nothing streaming right now');
    const streams = plex.sessions || [];
    $('#now-playing').hidden = streams.length === 0;
    $('#streams-list').innerHTML = streams.map(stream =>
      `<li>${icon('play')}<strong>${esc(stream.title)}</strong><span class="badge muted">${esc(stream.state)}</span></li>`).join('');
    Object.assign(queues, data.queues);
    for (const library of Object.values(libraries)) renderLibraryCount(library);
  } catch {
    $('#plex-streams').textContent = '—';
    $('#plex-detail').textContent = 'Could not load Plex activity';
    $('#now-playing').hidden = true;
  }
}

function posterCard(item, index, kind) {
  const airing = item.nextAiring && new Date(item.nextAiring);
  const dateFormat = {day: 'numeric', month: 'short', year: 'numeric'};
  const release = item.releaseDate && new Date(item.releaseDate + 'T00:00:00Z');
  const when = airing ? airing.toLocaleDateString(undefined, {weekday: 'short', day: 'numeric', month: 'short'})
    : release ? release.toLocaleDateString(undefined, {...dateFormat, timeZone: 'UTC'}) : 'Date unknown';
  const detail = airing ? `Next episode ${when}` : release ? `Released ${when}` : 'Release date unknown';
  const title = item.year ? `${item.title} (${item.year})` : item.title;
  return `<li class="poster-card">
    <div class="poster-art">
      <span class="poster-placeholder" aria-hidden="true">${esc(item.title)}</span>
      ${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async" width="250" height="375">` : ''}
    </div>
    <h3 class="poster-title" id="poster-${kind}-${index}">${esc(title)}</h3>
    <p class="poster-meta">${esc(detail)}</p>
  </li>`;
}

function renderLibraryCount({kind, data, count}) {
  if (!count) return;
  if (!data?.configured || data.error) {
    count.textContent = '';
    return;
  }
  const queued = queues[kind];
  count.textContent = `${data.items.length} ${kind === 'sonarr' ? 'upcoming' : 'missing'}${queued == null ? '' : ` · ${queued} in the download queue`}`;
}

function libraryMessage(kind, data) {
  const series = kind === 'sonarr';
  if (!data.configured) return `<div class="empty-state"><h3>${series ? 'Sonarr' : 'Radarr'} isn’t connected</h3><p>Save its API key in the dashboard secrets directory. The dashboard guide explains where.</p></div>`;
  return `<p class="empty">${esc(data.error)}</p>`;
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
  let items = data.items.filter(item => item.title.toLowerCase().includes(search));
  if (library.limit) items = items.slice(0, library.limit);
  content.innerHTML = items.length
    ? `<ul class="poster-grid${library.limit ? ' is-row' : ''}">${items.map((item, index) => posterCard(item, index, kind + (library.limit ? '-next' : ''))).join('')}</ul>`
    : search ? `<p class="empty">No titles match “${esc(filter.value.trim())}”.</p>`
      : `<div class="empty-state"><h3>${kind === 'sonarr' ? 'Nothing airing soon' : 'No missing films'}</h3><p>${kind === 'sonarr'
        ? 'No monitored series have an episode due in the next 12 months. Add or monitor series in Sonarr.'
        : 'Every monitored film has a file. Request more in Seerr or add them in Radarr.'}</p></div>`;
}

function hidePosterOnError(content) {
  content.addEventListener('error', event => {
    if (event.target.matches('.poster-art img')) event.target.hidden = true;
  }, true);
}

async function loadLibrary(library) {
  try {
    library.data = await get('/api/' + library.kind);
    renderLibrary(library);
  } catch {
    library.data = null;
    if (library.count) library.count.textContent = '';
    library.content.innerHTML = '<p class="empty">Could not load titles.</p>';
  }
}

if (page === 'library') {
  for (const section of document.querySelectorAll('[data-library]')) {
    const content = section.querySelector('.library-content');
    const filter = section.querySelector('input');
    const library = {kind: section.dataset.library, content, filter, count: section.querySelector('.library-count')};
    hidePosterOnError(content);
    filter.addEventListener('input', () => renderLibrary(library));
    libraries[library.kind] = library;
  }
} else if (page === 'overview') {
  const content = $('#up-next');
  hidePosterOnError(content);
  libraries.sonarr = {kind: 'sonarr', content, limit: 6};
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const button = $('#refresh');
  button.disabled = true;
  button.classList.add('is-busy');
  const jobs = [status()];
  if (page === 'overview' || page === 'library') {
    jobs.push(activity());
    for (const library of Object.values(libraries)) jobs.push(loadLibrary(library));
  }
  await Promise.allSettled(jobs);
  button.disabled = false;
  button.classList.remove('is-busy');
  refreshing = false;
}

$('#refresh').addEventListener('click', refresh);
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 60000);
