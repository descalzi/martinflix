'use strict';
const $ = s => document.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const apps = {
  tailscale: ['Tailscale', 'Private network', 'tailscale'],
  portainer: ['Portainer', 'Container management', 'portainer'],
  plex: ['Plex', 'Media streaming', 'plex'],
  nordvpn: ['Gluetun', 'NordVPN gateway', 'gluetun'],
  'transmission-daemon': ['Transmission', 'Downloads', 'transmission'],
  flaresolverr: ['FlareSolverr', 'Indexer support', 'flaresolverr'],
  prowlarr: ['Prowlarr', 'Indexer management', 'prowlarr'],
  bazarr: ['Bazarr', 'Subtitles', 'bazarr'],
  sonarr: ['Sonarr', 'TV library', 'sonarr'],
  radarr: ['Radarr', 'Film library', 'radarr'],
  'bookorbit-db': ['PostgreSQL', 'BookOrbit database', 'postgresql'],
  'bookorbit-app': ['BookOrbit', 'Book library', 'bookorbit'],
  homepage: ['Homepage', 'App launcher', 'homepage'],
  seerr: ['Seerr', 'Media requests', 'seerr'],
  tautulli: ['Tautulli', 'Plex activity', 'tautulli'],
  bookshelf: ['Bookshelf', 'Optional book automation', 'bookshelf'],
};

const stackBranches = {
  nordvpn: {
    title: 'VPN network',
    description: 'These services share Gluetun’s network.',
    members: ['transmission-daemon', 'flaresolverr', 'prowlarr', 'bazarr'],
  },
  'bookorbit-db': {
    title: 'BookOrbit database',
    description: 'BookOrbit starts after PostgreSQL is healthy.',
    members: ['bookorbit-app'],
  },
};

const startupDependencies = {
  sonarr: ['prowlarr', 'transmission-daemon'],
  radarr: ['prowlarr', 'transmission-daemon'],
};

function serviceIsUp(service) {
  return ['running', 'reachable'].includes(service.state);
}

function needsAttention(service) {
  return !serviceIsUp(service) || service.status.toLowerCase().includes('unhealthy');
}

function appHealth(service) {
  const health = service.health;
  if (!health) return '';
  if (health.state !== 'available') {
    const unavailable = health.state === 'unavailable';
    return `<div class="app-health"><span class="service-badge ${unavailable ? 'warn' : 'muted'}">
      ${unavailable ? 'App health unavailable' : 'App health not connected'}</span></div>`;
  }
  if (!health.alerts.length) {
    return '<div class="app-health"><span class="service-badge good">No app health alerts</span></div>';
  }
  return `<ul class="app-health">${health.alerts.map(({severity, message}) => `<li>
    <span class="service-badge ${severity === 'error' ? 'alert' : severity === 'info' ? 'muted' : 'warn'}">
      ${severity === 'error' ? 'Error' : severity === 'info' ? 'Info' : 'Warning'}</span>
    <span>${esc(message)}</span>
  </li>`).join('')}</ul>`;
}

function serviceCard(service, groupServices, countLabel) {
  const [name, role, icon] = apps[service.name] ?? [service.name, 'Container'];
  const unhealthy = service.status.toLowerCase().includes('unhealthy');
  const state = unhealthy ? 'unhealthy' : service.state;
  const label = state === 'exited' ? 'Stopped' : state === 'missing' ? 'Missing' :
    state.charAt(0).toUpperCase() + state.slice(1);
  const tone = serviceIsUp({state}) ? 'good' : ['unhealthy', 'exited', 'unreachable'].includes(state) ? 'alert' :
    state === 'missing' && service.name === 'bookshelf' ? 'muted' : 'warn';
  const down = tone === 'alert' || (state === 'missing' && tone !== 'muted') ||
    ['dead', 'paused', 'restarting', 'removing', 'created'].includes(state);
  const dependencies = (startupDependencies[service.name] ?? [])
    .filter(name => groupServices.some(item => item.name === name));
  const dependencyNote = dependencies.length ? `<p class="service-dependency">
    Own network · ${countLabel === 'reachable' ? 'Uses' : 'Starts after'}
    ${dependencies.map(name => esc(apps[name][0])).join(' and ')}${countLabel === 'reachable' ? '.' : ' are healthy.'}
  </p>` : '';
  const unavailable = dependencies.filter(name => {
    const dependency = groupServices.find(item => item.name === name);
    return dependency && needsAttention(dependency);
  });
  return `<li class="service-card${down ? ' is-down' : ''}">
    <div class="service-identity">
      <span class="service-icon" aria-hidden="true">${icon
        ? `<img src="/assets/${icon}.webp" width="32" height="32" alt="" loading="lazy">`
        : esc(name.slice(0, 2).toUpperCase())}</span>
      <div><h4>${esc(name)}</h4><p>${esc(role)}</p></div>
    </div>
    <div class="service-status">
      <span class="service-badge ${tone}">${esc(label)}</span>
      <span class="service-detail">${esc(service.status || 'No status detail')}</span>
    </div>
    ${appHealth(service)}
    ${dependencyNote}
    ${unavailable.length ? `<p class="dependency-warning">Check ${unavailable.map(name => esc(apps[name][0])).join(' and ')}</p>` : ''}
  </li>`;
}

function serviceGroup(group, countLabel, showVpnBranch) {
  const services = group.services;
  const running = services.filter(serviceIsUp).length;
  const nested = new Set();
  const branches = services.flatMap(parent => {
    const branch = parent.name === 'nordvpn' && !showVpnBranch ? null : stackBranches[parent.name];
    if (!branch) return [];
    const children = services.filter(service => branch.members.includes(service.name));
    if (!children.length) return [];
    nested.add(parent.name);
    children.forEach(child => nested.add(child.name));
    return [`<div class="stack-branch">
      <ul class="stack-parent">${serviceCard(parent, services, countLabel)}</ul>
      <div class="stack-children">
        <h4>${esc(branch.title)}</h4>
        <p>${esc(branch.description)}</p>
        ${needsAttention(parent) ? `<p class="dependency-warning">Upstream: check ${esc(apps[parent.name][0])}</p>` : ''}
        <ul class="service-items">${children.map(child => serviceCard(child, services, countLabel)).join('')}</ul>
      </div>
    </div>`];
  });
  const standalone = services.filter(service => !nested.has(service.name));
  const compact = branches.length === 0 && services.length <= 2;
  return `<div class="service-group${compact ? ' service-group-compact' : ''}">
    <div class="group-title"><h3>${esc(group.name)}</h3><small>${running} of ${services.length} ${countLabel}</small></div>
    ${branches.join('')}
    ${standalone.length ? `<ul class="service-items">${standalone.map(service => serviceCard(service, services, countLabel)).join('')}</ul>` : ''}
  </div>`;
}

const servicesPage = document.body.dataset.page === 'services';
if (!servicesPage && window.location.hash === '#services') window.location.replace('/services');
if (!servicesPage && window.location.hash === '#library') window.location.replace('/#series');
$(servicesPage ? '#nav-services' : '#nav-overview').setAttribute('aria-current', 'page');
$('#page-name').textContent = servicesPage ? 'SERVICES' : 'OVERVIEW';
$('#seerr-link').href = window.location.protocol + '//' + window.location.hostname + ':5055/';

async function get(url) {
  const response = await fetch(url, {cache: 'no-store'});
  if (response.status === 401 && response.headers.get('X-MartinFlix-Login') === 'required') {
    window.location.assign('/login');
  }
  if (!response.ok) throw new Error('Request failed');
  return response.json();
}

function date(value) {
  const stamp = new Date(value);
  return isNaN(stamp) ? 'Unknown' : stamp.toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});
}

function renderOverview(docker, services, backups, probes) {
  const required = services.filter(service => service.name !== 'bookshelf');
  const attention = required.filter(service => needsAttention(service) ||
    service.health?.state === 'unavailable' || service.health?.alerts?.length);
  $('#service-summary').classList.toggle('needs-attention', !docker.available || attention.length > 0);
  $('#services-metric-label').textContent = probes ? 'SERVICES REACHABLE' : 'SERVICES RUNNING';
  $('#running').textContent = docker.available ? `${required.filter(serviceIsUp).length} / ${required.length}` : 'Unavailable';
  $('#services-summary-detail').textContent = !docker.available ? 'Open service status'
    : attention.length ? `${attention.length} need attention · View services` : 'All required services up · View services';
  $('#seerr-row').hidden = !services.some(service => service.name === 'seerr' && serviceIsUp(service));

  const latest = backups[0];
  $('#latest-backup').textContent = latest ? date(latest.created) : 'None yet';
  $('#backup-detail').textContent = latest ? (latest.errors?.length || latest.missing?.length ? 'Incomplete snapshot' : 'Snapshot recorded') : 'No snapshots found';
  $('#backups-content').innerHTML = backups.length
    ? backups.map(backup => `<div class="backup-row"><span>${esc(date(backup.created))} <small> · ${esc(backup.files ?? 0)} archives</small></span><span class="tag">${backup.errors?.length || backup.missing?.length ? 'Incomplete · ' + esc([...(backup.missing || []), ...(backup.errors || [])].join(', ')) : 'Captured · restore untested'}</span></div>`).join('')
    : '<p class="empty">No MartinFlix snapshots found.</p>';
}

async function status() {
  try {
    const data = await get('/api/status');
    const docker = data.docker;
    const probes = docker.source === 'app-probes';
    $('#nav-password').hidden = !data.canChangePassword;
    if (servicesPage) {
      $('#services-description').textContent = probes ? 'Live health checks for configured services.' : 'Container status and configured service dependencies.';
      $('#services-source').textContent = probes
        ? 'Reachability shows which apps respond. Connected Arr apps also report their own health alerts.'
        : 'Docker reports container status. Connected Arr apps also report their own health alerts.';
      $('#services-content').innerHTML = docker.available
        ? docker.groups.map(group => serviceGroup(group, probes ? 'reachable' : 'running', !probes || docker.downloadsBehindVpn)).join('')
        : `<p class="empty">${esc(docker.error)}</p>`;
    } else {
      renderOverview(docker, docker.groups.flatMap(group => group.services), data.backups.snapshots, probes);
    }
    $('#updated').textContent = 'UPDATED ' + new Date().toLocaleTimeString();
  } catch {
    $('#running').textContent = 'Unavailable';
    $('#services-summary-detail').textContent = 'Could not load status · View services';
    $('#service-summary').classList.add('needs-attention');
    $('#services-content').innerHTML = '<p class="empty">Could not load status. Check the local server and try again.</p>';
  }
}

const libraries = {};
const queues = {};

async function activity() {
  try {
    const data = await get('/api/activity');
    const plex = data.plex;
    $('#plex-streams').textContent = plex.streams ?? '—';
    $('#plex-detail').textContent = !plex.configured ? 'Plex activity not connected' : plex.error ||
      (plex.streams ? 'Active via Tautulli' : 'Nothing streaming right now');
    const streams = plex.sessions || [];
    $('#now-playing').hidden = streams.length === 0;
    $('#streams-list').innerHTML = streams.map(stream =>
      `<li><strong>${esc(stream.title)}</strong><span>${esc(stream.state)}</span></li>`
    ).join('');
    Object.assign(queues, data.queues);
    for (const library of Object.values(libraries)) renderLibraryCount(library);
  } catch {
    $('#plex-streams').textContent = '—';
    $('#plex-detail').textContent = 'Could not load Plex activity';
    $('#now-playing').hidden = true;
    for (const library of Object.values(libraries)) {
      delete queues[library.kind];
      renderLibraryCount(library);
    }
  }
}

function posterCard(item, index, kind) {
  const airing = item.nextAiring && new Date(item.nextAiring);
  const dateFormat = {day: 'numeric', month: 'short', year: 'numeric'};
  const release = item.releaseDate && new Date(item.releaseDate + 'T00:00:00Z');
  const detail = airing
    ? `Next airing · ${airing.toLocaleDateString(undefined, dateFormat)}`
    : release ? `Release · ${release.toLocaleDateString(undefined, {...dateFormat, timeZone: 'UTC'})}`
      : 'Release date unknown';
  const title = item.year ? `${item.title} (${item.year})` : item.title;
  const hint = airing ? 'Series' : 'Missing film';
  return `<li class="poster-card">
    <button type="button" class="poster-art" aria-label="${esc(title)}" aria-describedby="poster-details-${kind}-${index}" aria-expanded="false">
      <span class="poster-placeholder" aria-hidden="true">${esc(title)}</span>
      ${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async" width="250" height="375">` : ''}
      <span class="poster-overlay" aria-hidden="true">
        <span class="poster-hint">${esc(hint)}</span>
        <strong>${esc(title)}</strong>
        <span>${esc(detail)}</span>
      </span>
      <span class="sr-only" id="poster-details-${kind}-${index}">${esc(hint)}. ${esc(detail)}.</span>
    </button>
  </li>`;
}

function renderLibraryCount({kind, data, count}) {
  if (!data?.configured || data.error) {
    count.textContent = '';
    return;
  }
  const queued = queues[kind];
  count.textContent = `${data.items.length} ${kind === 'sonarr' ? 'upcoming' : 'missing'}${queued == null ? '' : ` · ${queued} queued`}`;
}

function renderLibrary(library) {
  const {kind, data, content, filter} = library;
  renderLibraryCount(library);
  if (!data) return;
  const series = kind === 'sonarr';
  if (!data.configured || data.error) {
    const message = data.error || `Connect ${series ? 'Sonarr' : 'Radarr'} using the dashboard setup guide.`;
    content.innerHTML = `<p class="empty">${esc(message)}</p>`;
    return;
  }
  const search = filter.value.trim().toLowerCase();
  const items = data.items.filter(item => item.title.toLowerCase().includes(search));
  content.innerHTML = items.length
    ? `<ul class="poster-grid">${items.map((item, index) => posterCard(item, index, kind)).join('')}</ul>`
    : `<p class="empty">${search ? 'No titles match your search.' :
      series ? 'No monitored series have a next airing within 12 months.' : 'No monitored films are missing.'}</p>`;
}

function libraryPanel(section) {
  const kind = section.dataset.library;
  const content = section.querySelector('.library-content');
  const filter = section.querySelector('input');
  const library = {kind, content, filter, count: section.querySelector('.library-count')};
  content.addEventListener('error', event => {
    if (event.target.matches('.poster-art img')) event.target.hidden = true;
  }, true);
  content.addEventListener('click', event => {
    const poster = event.target.closest('.poster-art');
    if (!poster) return;
    const opened = poster.getAttribute('aria-expanded') === 'true';
    content.querySelector('.poster-art[aria-expanded="true"]')?.setAttribute('aria-expanded', 'false');
    poster.setAttribute('aria-expanded', String(!opened));
  });
  content.addEventListener('keydown', event => {
    if (event.key === 'Escape') event.target.closest('.poster-art')?.setAttribute('aria-expanded', 'false');
  });
  filter.addEventListener('input', () => renderLibrary(library));
  return library;
}

async function loadLibrary(library) {
  try {
    library.data = await get('/api/' + library.kind);
    renderLibrary(library);
  } catch {
    library.data = null;
    library.count.textContent = '';
    library.content.innerHTML = '<p class="empty">Could not load titles.</p>';
  }
}

if (!servicesPage) {
  for (const section of document.querySelectorAll('[data-library]')) {
    const library = libraryPanel(section);
    libraries[library.kind] = library;
  }
}

function refresh() {
  status();
  if (!servicesPage) {
    activity();
    for (const library of Object.values(libraries)) loadLibrary(library);
  }
}

$('#refresh').addEventListener('click', refresh);
refresh();
