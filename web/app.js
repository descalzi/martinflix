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
    ${dependencyNote}
    ${unavailable.length ? `<p class="dependency-warning">Check ${unavailable.map(name => esc(apps[name][0])).join(' and ')}</p>` : ''}
  </li>`;
}

function serviceGroup(group, countLabel) {
  const services = group.services;
  const running = services.filter(serviceIsUp).length;
  const nested = new Set();
  const branches = services.flatMap(parent => {
    const branch = stackBranches[parent.name];
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

let kind = 'sonarr', library = {};
$('#seerr-link').href = window.location.protocol + '//' + window.location.hostname + ':5055/';
async function get(url) {
  const response = await fetch(url, {cache: 'no-store'});
  if (response.status === 401 && response.headers.get('X-MartinFlix-Login') === 'required') {
    window.location.assign('/login');
  }
  if (!response.ok) throw new Error('Request failed');
  return response.json();
}
function date(value){const d = new Date(value); return isNaN(d) ? 'Unknown' : d.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'})}
async function status(){
 try {const data = await get('/api/status'), docker = data.docker, services = docker.groups.flatMap(g=>g.services), running = services.filter(serviceIsUp);
 const probes = docker.source === 'app-probes';
 $('#services-metric-label').textContent = probes ? 'SERVICES REACHABLE' : 'SERVICES RUNNING';
 $('#services-description').textContent = probes ? 'Live health checks for configured services.' : 'Container status and configured service dependencies.';
 $('#services-source').textContent = probes ? 'These checks show whether each app responds to the dashboard.' :
   'Docker reports container status. Dependency lines show the configured stack and do not test VPN connectivity.';
 $('#running').textContent = docker.available ? `${running.length} / ${services.length}` : 'Unavailable';
 $('#system-status').textContent = !docker.available ? 'Unavailable' : services.some(s=>needsAttention(s) && s.name!=='bookshelf') ? 'Needs a look' : 'Looking good';
 $('#system-detail').textContent = docker.available ? (probes ? 'App health checks' : 'Container state only') : docker.error;
 $('#services-content').innerHTML = docker.available ? docker.groups.map(group => serviceGroup(group, probes ? 'reachable' : 'running')).join('') : `<p class="empty">${esc(docker.error)}</p>`;
 const backups=data.backups.snapshots; $('#latest-backup').textContent=backups.length ? date(backups[0].created) : 'None yet'; $('#backup-detail').textContent=backups.length ? (backups[0].errors?.length || backups[0].missing?.length ? 'Incomplete snapshot' : 'Snapshot recorded') : 'No snapshots found';
 $('#backups-content').innerHTML=backups.length ? backups.map(b=>`<div class="backup-row"><span>${esc(date(b.created))} <small> · ${esc(b.files ?? 0)} archives</small></span><span class="tag">${b.errors?.length || b.missing?.length ? 'Incomplete · '+esc([...(b.missing||[]),...(b.errors||[])].join(', ')) : 'Captured · restore untested'}</span></div>`).join('') : '<p class="empty">No MartinFlix snapshots found. See the backup guide to create and verify one.</p>';
 $('#updated').textContent='UPDATED '+new Date().toLocaleTimeString();
 } catch(e){$('#system-status').textContent='Disconnected';$('#system-detail').textContent='Could not reach the local server.';$('#services-content').innerHTML='<p class="empty">Could not load status. Check the local server and try again.</p>'}
}
const libraryContent = $('#library-content');
const libraryFilter = $('#filter');
libraryContent.addEventListener('error', event => {
  if (event.target.matches('.poster-art img')) event.target.hidden = true;
}, true);
libraryContent.addEventListener('click', event => {
  const poster = event.target.closest('.poster-art');
  if (!poster) return;
  const opened = poster.getAttribute('aria-expanded') === 'true';
  libraryContent.querySelector('.poster-art[aria-expanded="true"]')?.setAttribute('aria-expanded', 'false');
  poster.setAttribute('aria-expanded', String(!opened));
});
libraryContent.addEventListener('keydown', event => {
  if (event.key === 'Escape') event.target.closest('.poster-art')?.setAttribute('aria-expanded', 'false');
});

async function loadLibrary() {
  const current = kind;
  libraryContent.innerHTML = '<p class="empty">Loading titles…</p>';
  try {
    library[current] = await get('/api/' + current);
    if (kind === current) renderLibrary();
  } catch {
    if (kind === current) libraryContent.innerHTML = '<p class="empty">Could not load titles.</p>';
  }
}

function posterCard(item, index) {
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
    <button type="button" class="poster-art" aria-label="${esc(title)}" aria-describedby="poster-details-${index}" aria-expanded="false">
      <span class="poster-placeholder" aria-hidden="true">${esc(title)}</span>
      ${item.poster ? `<img src="${esc(item.poster)}" alt="" loading="lazy" decoding="async" width="250" height="375">` : ''}
      <span class="poster-overlay" aria-hidden="true">
        <span class="poster-hint">${esc(hint)}</span>
        <strong>${esc(title)}</strong>
        <span>${esc(detail)}</span>
      </span>
      <span class="sr-only" id="poster-details-${index}">${esc(hint)}. ${esc(detail)}.</span>
    </button>
  </li>`;
}

function renderLibrary() {
  const data = library[kind];
  if (!data) return;
  if (!data.configured || data.error) {
    const message = data.error || `Connect ${kind === 'sonarr' ? 'Sonarr' : 'Radarr'} using the dashboard setup guide.`;
    libraryContent.innerHTML = `<p class="empty">${esc(message)}</p>`;
    return;
  }
  const search = libraryFilter.value.trim().toLowerCase();
  const items = data.items.filter(item => item.title.toLowerCase().includes(search));
  libraryContent.innerHTML = items.length
    ? `<ul class="poster-grid">${items.map(posterCard).join('')}</ul>`
    : `<p class="empty">${search ? 'No titles match your search.' :
      kind === 'sonarr' ? 'No monitored series have a next airing within 12 months.' : 'No monitored films are missing.'}</p>`;
}

const tabs = $('.tabs');
tabs.addEventListener('click', event => {
  const button = event.target.closest('button[data-kind]');
  if (!button) return;
  kind = button.dataset.kind;
  tabs.querySelectorAll('button').forEach(tab => {
    tab.classList.toggle('active', tab === button);
    tab.setAttribute('aria-pressed', String(tab === button));
  });
  $('#library-scope').textContent = kind === 'sonarr'
    ? 'Monitored series · Upcoming or continuing · Next airing within 12 months'
    : 'Missing · Monitored films without a file';
  loadLibrary();
});
libraryFilter.addEventListener('input', renderLibrary);
$('#refresh').addEventListener('click', () => { status(); loadLibrary(); });
status();
loadLibrary();
