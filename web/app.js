'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const icon = (name, extra = '') => `<svg class="icon${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const page = document.body.dataset.page;
const pageTitles = {overview: 'Overview', library: 'Library', add: 'Add to library', services: 'Services', backups: 'Backups'};
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
  </li>`;
}

/** The service that others share, such as Gluetun for the VPN apps, shown as the bar their section hangs from. */
function gatewayBar(service) {
  const app = appInfo(service.name);
  return `<div class="gateway tone-${serviceState(service).tone}">
    ${appIcon(service.name)}
    <p><strong>${esc(app.name)}</strong><span>${esc(app.role)}</span></p>
    ${statusChip(service)}
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
    const problems = attentionRows(docker, services).length;
    $('#nav-flag').hidden = problems === 0;
    $('#nav-flag').innerHTML = `${problems}<span class="sr-only"> ${problems === 1 ? 'issue' : 'issues'}</span>`;
    if (servicesPage) {
      $('#services-description').textContent = probes
        ? 'Live health checks for the services this installer set up.'
        : 'Container status from Docker, grouped by stack, with each app’s own health alerts.';
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

function episodeCode(episode) {
  if (!episode || episode.season == null || episode.number == null) return '';
  return `S${String(episode.season).padStart(2, '0')}E${String(episode.number).padStart(2, '0')}`;
}

function posterDetails(item, kind) {
  if (kind === 'sonarr') {
    const airing = item.nextAiring && new Date(item.nextAiring);
    const when = airing ? upcomingDay(airing) : 'Date unknown';
    const code = episodeCode(item.nextEpisode);
    return {meta: code ? `${code} · ${when}` : `Next episode ${when}`, extra: item.nextEpisode?.title || ''};
  }
  if (item.nextRelease) {
    const when = upcomingDay(item.nextRelease.date + 'T12:00:00');
    return {meta: `${item.nextRelease.type} · ${when}`, extra: ''};
  }
  if (item.releaseDate) {
    const released = new Date(item.releaseDate + 'T00:00:00Z').toLocaleDateString(undefined, {day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC'});
    return {meta: `Released ${released}`, extra: 'Not downloaded yet'};
  }
  return {meta: 'Release date unknown', extra: ''};
}

function posterCard(item, kind) {
  const {meta, extra} = posterDetails(item, kind);
  const title = item.year ? `${item.title} (${item.year})` : item.title;
  return `<li class="poster-card">
    <div class="poster-art">
      <span class="poster-placeholder" aria-hidden="true">${esc(item.title)}</span>
      ${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async" width="250" height="375">` : ''}
    </div>
    <h3 class="poster-title">${esc(title)}</h3>
    <p class="poster-meta">${esc(meta)}</p>
    ${extra ? `<p class="poster-extra">${esc(extra)}</p>` : ''}
  </li>`;
}

function libraryItems(library) {
  const items = library.data?.items || [];
  return library.upcomingOnly ? items.filter(item => item.nextRelease)
    .sort((a, b) => a.nextRelease.date.localeCompare(b.nextRelease.date)) : items;
}

function renderLibraryCount(library) {
  const {kind, data, count} = library;
  if (!count) return;
  if (!data?.configured || data.error) {
    count.textContent = library.limit ? count.dataset.default : '';
    return;
  }
  const total = libraryItems(library).length;
  const queued = queues[kind];
  const queue = queued ? ` · ${queued} in the download queue` : '';
  if (library.limit) {
    count.textContent = kind === 'sonarr' ? `${total} ${total === 1 ? 'series airs' : 'series air'} in the next 12 months${queue}`
      : `${total} ${total === 1 ? 'film has' : 'films have'} a release ahead${queue}`;
  } else {
    count.textContent = `${total} ${kind === 'sonarr' ? 'upcoming' : 'missing'}${queue}`;
  }
}

function libraryMessage(kind, data) {
  const name = kind === 'sonarr' ? 'Sonarr' : 'Radarr';
  if (!data.configured) return `<div class="empty-state"><h3>${name} isn’t connected</h3><p>Save its API key in the dashboard secrets directory. The dashboard guide explains where.</p></div>`;
  return `<p class="empty">${esc(data.error)}</p>`;
}

function emptyLibrary(library) {
  if (library.kind === 'sonarr') {
    return '<div class="empty-state"><h3>Nothing airing soon</h3><p>No monitored series have an episode due in the next 12 months.</p><a class="text-link" href="/add">Add a series</a></div>';
  }
  return library.upcomingOnly
    ? '<div class="empty-state"><h3>No releases ahead</h3><p>None of your monitored films have a cinema, digital or physical release coming up.</p><a class="text-link" href="/add?kind=radarr">Add a film</a></div>'
    : '<div class="empty-state"><h3>No missing films</h3><p>Every monitored film has a file.</p><a class="text-link" href="/add?kind=radarr">Add a film</a></div>';
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
  let items = libraryItems(library).filter(item => item.title.toLowerCase().includes(search));
  if (library.limit) items = items.slice(0, library.limit);
  content.innerHTML = items.length
    ? `<ul class="poster-grid${library.limit ? ' is-rail' : ''}">${items.map(item => posterCard(item, kind)).join('')}</ul>`
    : search ? `<p class="empty">No titles match “${esc(filter.value.trim())}”.</p>` : emptyLibrary(library);
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
  for (const section of document.querySelectorAll('[data-library]')) {
    const content = section.querySelector('.library-content');
    const filter = section.querySelector('input');
    const library = {kind: section.dataset.library, content, filter, count: section.querySelector('.library-count')};
    hidePosterOnError(content);
    filter.addEventListener('input', () => renderLibrary(library));
    libraries[library.kind] = library;
  }
} else if (page === 'overview') {
  libraries.sonarr = overviewLibrary('sonarr', $('#next-series'), $('#next-series-count'));
  libraries.radarr = overviewLibrary('radarr', $('#next-films'), $('#next-films-count'), {upcomingOnly: true});
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
    const profile = settings.profiles.find(item => item.id === settings.profile);
    target.textContent = `New titles use the ${profile.name} profile and ${settings.folder} unless you change them when adding.`;
  } catch {
    target.textContent = 'Could not read the add settings.';
  }
}

function resultCard(item, kind) {
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

function seasonLabel(number) {
  return number === 0 ? 'Specials' : String(number);
}

/** The options panel shown in place of the Add button. Every control starts on the default. */
function addOptionsForm(item, kind, settings) {
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
    action.querySelector('select').focus();
    card.scrollIntoView({block: 'nearest', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  } catch (error) {
    action.innerHTML = `<p class="error error-inline">${esc(error.message || 'Could not load the add options.')}</p>${addButton(card.dataset.id)}`;
  }
}

async function submitOptions(form, card, kind) {
  const error = form.querySelector('.error');
  const data = new FormData(form);
  const request = {id: Number(card.dataset.id), profile: Number(data.get('profile')), search: data.get('search') === 'on'};
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
  } else {
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
    submit.innerHTML = `${icon('plus')}Add ${kind === 'sonarr' ? 'series' : 'film'}`;
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
    term.placeholder = kind === 'sonarr' ? 'Search for a series' : 'Search for a film';
    showAddTarget(kind);
    if (lastSearch && lastSearch.kind !== kind && term.value.trim()) form.requestSubmit();
  };
  form.addEventListener('change', event => { if (event.target.name === 'kind') syncKind(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const kind = addKind();
    const query = term.value.trim();
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
        ? `<p class="result-count">${data.results.length} ${data.results.length === 1 ? 'match' : 'matches'} for “${esc(query)}”</p><ul class="results" data-kind="${kind}">${data.results.map(item => resultCard(item, kind)).join('')}</ul>`
        : `<div class="empty-state"><h3>Nothing found for “${esc(query)}”</h3><p>Check the spelling, or try the original title.</p></div>`;
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
  if (new URLSearchParams(window.location.search).get('kind') === 'radarr') {
    form.querySelector('input[value="radarr"]').checked = true;
    syncKind();
  }
  term.focus();
}

let refreshing = false;
async function refresh() {
  if (refreshing || page === 'add') return;
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
$('#refresh').hidden = page === 'add';
if (page === 'add') {
  status();
  $('#updated').hidden = true;
} else {
  refresh();
}
setInterval(() => { if (!document.hidden) refresh(); }, 60000);
