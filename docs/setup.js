'use strict';

const REPOSITORY = 'https://github.com/descalzi/martinflix.git';
const INSTALLER = 'python3 deploy/bootstrap/install.py';
const portOptions = [
  ['dashboard', 'Dashboard', 8765],
  ['sonarr', 'Sonarr', 8989],
  ['radarr', 'Radarr', 7878],
  ['prowlarr', 'Prowlarr', 9696],
  ['transmission', 'Transmission', 9091],
  ['transmission-peer', 'Transmission peer', 51155],
  ['bazarr', 'Bazarr', 6767],
];
const STEPS = {
  new: ['task', 'access', 'media', 'vpn', 'downloads', 'options', 'review'],
  update: ['task', 'location', 'changes', 'review'],
  reset: ['task', 'access', 'media', 'vpn', 'downloads', 'options', 'review'],
};
const STEP_LABELS = {
  task: 'Task', access: 'Access', media: 'Media', vpn: 'VPN', downloads: 'Downloads',
  options: 'Ports & storage', location: 'Installation', changes: 'Changes', review: 'Review',
};

function shellQuote(value) {
  if (/^[A-Za-z0-9_./:@%+,-]+$/.test(value)) return value;
  return "'" + value.replace(/'/g, "'\"'\"'") + "'";
}

/** One flag per line keeps long commands readable; the backslashes keep them valid to paste. */
function formatCommand(parts) {
  const line = parts.join(' ');
  return line.length <= 64 ? line : parts.join(' \\\n  ');
}

function checkAddress(address) {
  const parts = address.split('.');
  if (parts.length !== 4 || parts.some(part => !/^(0|[1-9][0-9]{0,2})$/.test(part) || Number(part) > 255) ||
      Number(parts[0]) === 0 || Number(parts[0]) === 127 || Number(parts[0]) >= 224) {
    throw new Error('Enter the host’s LAN IPv4 address, such as 192.168.1.50.');
  }
}

function checkPath(value, label) {
  if (value && (!value.startsWith('/') || /[\u0000-\u001f\u007f]/.test(value))) {
    throw new Error(label + ' must be an absolute Linux path starting with /.');
  }
}

function enabledPorts(options) {
  return portOptions.filter(([name]) => name === 'prowlarr' ? options.prowlarr : name.startsWith('transmission')
    ? options.downloader : name === 'bazarr' ? options.bazarr : true);
}

function checkPorts(options) {
  const used = new Set();
  for (const [name, label, fallback] of enabledPorts(options)) {
    const raw = options.ports[name] ?? '';
    const port = raw === '' ? fallback : Number(raw);
    if ((raw !== '' && !/^[0-9]+$/.test(raw)) || port < 1024 || port > 65535) {
      throw new Error(label + ' needs a port from 1024 to 65535.');
    }
    if (used.has(port)) throw new Error('Each enabled service needs a different port.');
    used.add(port);
  }
}

/** Throws an Error describing the first problem on one step, using the current answers. */
function validateStep(step, options) {
  if (step === 'access' && options.lan) checkAddress(options.address);
  if (step === 'media') {
    if (!options.media) throw new Error('Enter the media directory. Sonarr and Radarr need it to file series and films.');
    checkPath(options.media, 'Media directory');
  }
  if (step === 'downloads' && options.downloader && !options.media) {
    throw new Error('Transmission needs a media directory. Add one in the Media step or choose “Set up later”.');
  }
  if (step === 'downloads' && options.bazarr && !options.media) {
    throw new Error('Bazarr needs a media directory. Add one in the Media step or turn Bazarr off.');
  }
  if (step === 'options') {
    checkPath(options.state, 'State directory');
    checkPorts(options);
  }
  if (step === 'location') checkPath(options.state, 'State directory');
  if (step === 'changes') {
    const change = options.changes || {};
    if (change.access === 'lan') checkAddress(change.address);
    checkPath(change.media, 'Media directory');
    if (change.flaresolverr && !change.prowlarr && change.prowlarrInstalled === false) {
      throw new Error('FlareSolverr needs Prowlarr.');
    }
  }
}

/** Flags for the changes chosen on an update. Anything left as it is adds nothing. */
function changeArguments(change) {
  const args = [];
  if (change.access === 'local') args.push('--dashboard-bind=127.0.0.1');
  if (change.access === 'lan') args.push('--dashboard-bind=' + shellQuote(change.address));
  if (change.media) args.push('--media-path=' + shellQuote(change.media));
  if (change.vpn) {
    args.push('--enable-vpn');
    args.push((change.exit === 'Prague' ? '--vpn-city=' : '--vpn-country=') + shellQuote(change.exit));
  }
  if (change.transmission) args.push('--download-client=transmission');
  if (change.prowlarr) args.push('--enable-prowlarr');
  if (change.flaresolverr) args.push('--enable-flaresolverr');
  if (change.bazarr) args.push('--enable-bazarr');
  if (change.route && change.route !== 'keep') args.push('--download-network=' + change.route);
  if (change.plexUrl) args.push('--plex-url=' + shellQuote(change.plexUrl));
  if (change.plexToken || change.plexUrl) args.push('--set-plex-token');
  if (change.page && change.page !== 'keep') args.push('--discover=' + change.page);
  if (change.discover) args.push('--set-tmdb-key');
  if (change.region && change.region !== 'keep') args.push('--discover-region=' + change.region);
  return args;
}

function installArguments(options) {
  const mode = options.mode || 'new';
  const args = [];
  if (mode === 'update') {
    args.push('--update');
    validateStep('location', options);
    validateStep('changes', options);
    if (options.state) args.push('--state-dir=' + shellQuote(options.state));
    return [...args, ...changeArguments(options.changes || {})];
  }
  for (const step of ['access', 'media', 'downloads', 'options']) validateStep(step, options);
  if (options.lan) args.push('--dashboard-bind=' + shellQuote(options.address));
  if (options.media) args.push('--media-path=' + shellQuote(options.media));
  if (options.state) args.push('--state-dir=' + shellQuote(options.state));
  if (options.vpn) {
    args.push('--enable-vpn', '--vpn-provider=' + shellQuote(options.provider));
    args.push((options.exit === 'Prague' ? '--vpn-city=' : '--vpn-country=') + shellQuote(options.exit));
  }
  if (options.downloader) args.push('--download-client=' + shellQuote(options.downloader));
  if (options.prowlarr) args.push('--enable-prowlarr');
  if (options.prowlarr && options.flaresolverr) args.push('--enable-flaresolverr');
  if (options.bazarr) args.push('--enable-bazarr');
  if (options.downloader || options.prowlarr || options.bazarr) args.push('--download-network=' + (options.behindVpn ? 'vpn' : 'direct'));
  for (const [name, , fallback] of enabledPorts(options)) {
    const raw = options.ports[name] ?? '';
    if (raw !== '' && Number(raw) !== fallback) args.push('--' + name + '-port=' + Number(raw));
  }
  if (options.plexUrl) args.push('--plex-url=' + shellQuote(options.plexUrl), '--set-plex-token');
  if (options.discover === false) args.push('--discover=off');
  if (options.discover !== false && options.ownKey) args.push('--set-tmdb-key');
  if (options.discover !== false && options.region && options.region !== 'GB') args.push('--discover-region=' + options.region);
  if (mode === 'reset') args.push('--reset');
  return args;
}

/** The ordered commands for a task. Each has a title, an optional note, and the text to copy. */
function buildCommands(options) {
  const mode = options.mode || 'new';
  const args = installArguments(options);
  const install = formatCommand([INSTALLER, ...args]);
  const checkCommand = formatCommand([INSTALLER, ...args, '--check']);
  const fetch = mode === 'new'
    ? {title: 'Get MartinFlix', note: 'Already have a checkout? Run git pull --ff-only inside it instead.',
      command: `git clone --depth 1 ${REPOSITORY}\ncd martinflix`}
    : {title: 'Get the latest version', note: 'Run inside the checkout you installed from.', command: 'cd martinflix\ngit pull --ff-only'};
  const check = {title: 'Check', command: checkCommand,
    note: mode === 'reset' ? 'Validates the new setup. Nothing is stopped or archived yet.'
      : 'Changes nothing. Fix anything it reports and run it again.'};
  const run = {
    new: {title: 'Install', command: install, note: 'Safe to rerun with the same options to resume.'},
    update: {title: 'Update', command: install, note: 'Containers may restart briefly. App data, passwords and anything you didn’t change are kept.'},
    reset: {title: 'Reset and install', command: install, note: 'Type RESET when the installer asks.'},
  }[mode];
  return [fetch, check, run];
}

if (typeof module !== 'undefined') module.exports = {installArguments, buildCommands, validateStep, shellQuote, STEPS};

if (typeof document !== 'undefined') {
  const byId = id => document.getElementById(id);
  const root = byId('options');
  const sections = Object.fromEntries([...root.querySelectorAll('.step')].map(section => [section.dataset.step, section]));
  let activeIndex = 0;
  let furthest = 0;
  let preferVpnRoute = true;

  for (const [name, label, fallback] of portOptions) {
    const row = document.createElement('div');
    row.className = 'field';
    const caption = document.createElement('label');
    const input = document.createElement('input');
    input.id = name + '-port';
    input.type = 'text';
    input.inputMode = 'numeric';
    input.placeholder = String(fallback);
    caption.htmlFor = input.id;
    caption.textContent = label + ' port';
    row.append(caption, input);
    byId('ports').append(row);
  }

  const mode = () => root.elements.mode.value;
  const steps = () => STEPS[mode()];

  function currentOptions() {
    const update = mode() === 'update';
    return {
      mode: mode(),
      lan: root.elements.access.value === 'lan',
      address: byId('address').value.trim(),
      media: byId('media').value.trim(),
      vpn: byId('vpn').checked,
      provider: byId('vpn-provider').value,
      exit: byId('vpn-exit').value,
      // Transmission saves into the media directory, so like Bazarr it only counts once one is set.
      downloader: byId('media').value.trim() ? byId('download-client').value : '',
      prowlarr: byId('prowlarr').checked,
      flaresolverr: byId('prowlarr').checked && byId('flaresolverr').checked,
      // Bazarr needs the media directory, so it only counts once one is set.
      bazarr: byId('bazarr').checked && Boolean(byId('media').value.trim()),
      behindVpn: byId('vpn').checked && preferVpnRoute,
      discover: byId('discover').checked,
      ownKey: byId('tmdb-own').checked,
      plexUrl: byId('plex-url').value.trim().replace(/\/+$/, ''),
      region: byId('discover-region').value,
      state: (update ? byId('state-update') : byId('state')).value.trim(),
      ports: Object.fromEntries(portOptions.map(([name]) => [name, byId(name + '-port').value.trim()])),
      changes: {
        access: byId('u-access').value,
        address: byId('u-address').value.trim(),
        vpn: byId('u-vpn').checked,
        exit: byId('u-vpn-exit').value,
        route: byId('u-route').value,
        transmission: byId('u-transmission').checked,
        prowlarr: byId('u-prowlarr').checked,
        flaresolverr: byId('u-flaresolverr').checked,
        bazarr: byId('u-bazarr').checked,
        discover: byId('u-discover').checked,
        page: byId('u-discover-page').value,
        plexUrl: byId('u-plex-url').value.trim().replace(/\/+$/, ''),
        plexToken: byId('u-plex-token').checked,
        region: byId('u-discover-region').value,
        media: byId('u-media').value.trim(),
      },
    };
  }

  function renderProgress() {
    const list = byId('progress');
    list.replaceChildren();
    steps().forEach((step, index) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'progress-step';
      button.dataset.index = String(index);
      button.disabled = index > furthest;
      if (index < activeIndex) item.classList.add('is-done');
      if (index === activeIndex) button.setAttribute('aria-current', 'step');
      const number = document.createElement('span');
      number.className = 'progress-number';
      number.setAttribute('aria-hidden', 'true');
      number.textContent = String(index + 1);
      const label = document.createElement('span');
      label.textContent = STEP_LABELS[step];
      button.append(number, label);
      item.append(button);
      list.append(item);
    });
  }

  function renderCommands(options) {
    const list = byId('command-list');
    const error = byId('error');
    try {
      const commands = buildCommands(options);
      error.hidden = true;
      list.replaceChildren(...commands.map((item, index) => {
        const entry = document.createElement('li');
        entry.className = 'command';
        const head = document.createElement('div');
        head.className = 'command-head';
        const title = document.createElement('h3');
        title.id = 'command-title-' + index;
        title.textContent = item.title;
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'button button-tinted button-small';
        copy.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-copy"/></svg><span>Copy</span>';
        copy.setAttribute('aria-describedby', title.id);
        const pre = document.createElement('pre');
        pre.tabIndex = 0;
        pre.setAttribute('aria-labelledby', title.id);
        const code = document.createElement('code');
        code.textContent = item.command;
        pre.append(code);
        copy.addEventListener('click', () => copyCommand(copy, pre, item.command));
        head.append(title, copy);
        entry.append(head, pre);
        if (item.note) {
          const note = document.createElement('p');
          note.textContent = item.note;
          entry.append(note);
        }
        return entry;
      }));
    } catch (issue) {
      list.replaceChildren();
      error.textContent = 'Commands paused: ' + issue.message;
      error.hidden = false;
    }
  }

  async function copyCommand(button, pre, text) {
    const label = button.querySelector('span');
    try {
      await navigator.clipboard.writeText(text);
      label.textContent = 'Copied';
      button.querySelector('use').setAttribute('href', '#i-check');
      byId('copy-status').textContent = 'Copied to clipboard.';
      setTimeout(() => {
        label.textContent = 'Copy';
        button.querySelector('use').setAttribute('href', '#i-copy');
      }, 2000);
    } catch {
      const range = document.createRange();
      range.selectNodeContents(pre);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      pre.focus();
      byId('copy-status').textContent = 'Command selected. Copy it with your keyboard or device menu.';
    }
  }

  function renderReview(options) {
    const summary = [];
    if (options.mode === 'update') {
      const change = options.changes;
      summary.push(['Task', 'Update the existing installation'], ['Installation', options.state || 'Default location']);
      if (change.access === 'local') summary.push(['Open apps from', 'The Linux host only']);
      if (change.access === 'lan') summary.push(['Open apps from', change.address]);
      if (change.vpn) summary.push(['Add', `VPN gateway · ${byId('u-vpn-exit').selectedOptions[0].textContent.replace(' (default)', '')}`]);
      const added = [change.transmission && 'Transmission', change.prowlarr && 'Prowlarr', change.flaresolverr && 'FlareSolverr',
        change.bazarr && 'Bazarr'].filter(Boolean);
      if (added.length) summary.push(['Add apps', added.join(', ')]);
      if (change.route !== 'keep') summary.push(['Download traffic', change.route === 'vpn' ? 'Move through the VPN' : 'Move to the normal connection']);
      if (change.media) summary.push(['Media directory', change.media]);
      if (change.plexUrl) summary.push(['Plex server', change.plexUrl]);
      if (change.plexToken || change.plexUrl) summary.push(['Plex token', 'Enter it when setup asks']);
      if (change.page !== 'keep') summary.push(['Discover page', change.page === 'on' ? 'Show it' : 'Hide it']);
      if (change.discover) summary.push(['TMDB key', 'Use my own']);
      if (change.region !== 'keep') summary.push(['Discover region', byId('u-discover-region').selectedOptions[0].textContent]);
      if (summary.length === 2) summary.push(['Changes', 'None · reapply the latest version with your current settings']);
      else summary.push(['Everything else', 'Kept as it is']);
    } else {
      summary.push(['Task', options.mode === 'reset' ? 'Start from scratch' : 'New installation']);
      summary.push(['Open apps from', options.lan ? options.address : 'The Linux host only']);
      summary.push(['Media directory', options.media || 'Not yet']);
      summary.push(['VPN', options.vpn ? `NordVPN · ${byId('vpn-exit').selectedOptions[0].textContent.replace(' (default)', '')}` : 'Not installed']);
      const apps = [options.downloader && 'Transmission', options.prowlarr && 'Prowlarr', options.flaresolverr && 'FlareSolverr',
        options.bazarr && 'Bazarr'].filter(Boolean);
      summary.push(['Download apps', apps.join(', ') || 'None yet']);
      if (apps.length) summary.push(['Download traffic', options.behindVpn ? 'Through the VPN' : 'Normal internet connection']);
      summary.push(['Discover', options.discover ? `${byId('discover-region').selectedOptions[0].textContent.replace(' (default)', '')} · ${options.ownKey ? 'your TMDB key' : 'MartinFlix’s TMDB key'}` : 'Off']);
      summary.push(['Plex', options.plexUrl ? `${options.plexUrl} · setup asks for the token` : 'Not connected']);
      summary.push(['State directory', options.state || 'Default location']);
      for (const [name, label, fallback] of enabledPorts(options)) {
        const port = options.ports[name];
        if (port && Number(port) !== fallback) summary.push([label + ' port', port]);
      }
    }
    const review = byId('review');
    review.replaceChildren(...summary.map(([label, value]) => {
      const row = document.createElement('div');
      const term = document.createElement('dt');
      const definition = document.createElement('dd');
      term.textContent = label;
      definition.textContent = value;
      row.append(term, definition);
      return row;
    }));
    byId('review-title').textContent = {new: 'Ready to install', update: 'Ready to update', reset: 'Ready to start from scratch'}[options.mode];
    byId('reset-warning').hidden = options.mode !== 'reset';
  }

  function update(event) {
    const route = byId('route-vpn');
    if (event?.target === route) preferVpnRoute = route.checked;
    if (event?.target?.name === 'mode') furthest = 0;
    const options = currentOptions();
    const mediaBase = (options.media || '…').replace(/\/+$/, '');
    for (const path of document.querySelectorAll('[data-media-folder]')) path.textContent = mediaBase + '/' + path.dataset.mediaFolder;
    byId('lan-options').hidden = !options.lan;
    byId('u-lan-options').hidden = options.changes.access !== 'lan';
    byId('u-vpn-options').hidden = !options.changes.vpn;
    // Adding the gateway usually means wanting downloads behind it, so suggest that route once.
    if (event?.target?.id === 'u-vpn' && options.changes.vpn && byId('u-route').value === 'keep') {
      byId('u-route').value = 'vpn';
      options.changes.route = 'vpn';
    }
    byId('vpn-options').hidden = !options.vpn;
    const hasDownloadApps = Boolean(options.downloader || options.prowlarr || options.bazarr);
    byId('bazarr').disabled = !options.media;
    byId('bazarr-help').textContent = options.media
      ? 'Finds subtitles in English, French, Brazilian Portuguese and Spanish, and saves them beside your videos.'
      : 'Needs the media directory from the Media step.';
    byId('flaresolverr').disabled = !options.prowlarr;
    byId('download-client').disabled = !options.media;
    byId('client-help').textContent = options.media
      ? 'Downloads go to the downloads folder in your media directory.'
      : 'Transmission needs the media directory from the Media step, so it is off for now.';
    route.disabled = !options.vpn || !hasDownloadApps;
    route.checked = !route.disabled && preferVpnRoute;
    byId('route-help').textContent = !options.vpn ? 'Turn on the VPN gateway in the previous step to use this.'
      : !hasDownloadApps ? 'Choose a download client or Prowlarr first.'
        : route.checked ? 'On by default. If the tunnel drops, these apps lose internet access instead of leaking.'
          : 'These apps will use the host’s normal internet connection.';
    byId('prowlarr-port').parentElement.hidden = !options.prowlarr;
    byId('transmission-port').parentElement.hidden = !options.downloader;
    byId('transmission-peer-port').parentElement.hidden = !options.downloader;
    byId('bazarr-port').parentElement.hidden = !options.bazarr;
    byId('commands-lede').textContent = options.mode === 'new'
      ? 'Run these on the Linux host as your normal user, not with sudo.'
      : 'Run these in your existing MartinFlix checkout, as the account that installed it.';
    byId('step-error').hidden = true;
    if (steps()[activeIndex] === 'review') renderReview(options);
    renderCommands(options);
    renderProgress();
  }

  function showStep(index, focus = true) {
    const order = steps();
    activeIndex = index;
    furthest = Math.max(furthest, index);
    for (const [name, section] of Object.entries(sections)) section.hidden = name !== order[index];
    byId('step-error').hidden = true;
    byId('back').hidden = index === 0;
    byId('next').hidden = index === order.length - 1;
    byId('skip').hidden = index === 0 || index >= order.length - 2;
    byId('next').firstChild.textContent = index === order.length - 2 ? 'Review' : 'Next';
    if (order[index] === 'review') renderReview(currentOptions());
    renderProgress();
    if (focus) sections[order[index]].querySelector('h2').focus();
  }

  function fail(message) {
    const error = byId('step-error');
    error.textContent = message;
    error.hidden = false;
  }

  function goTo(target) {
    const order = steps();
    const options = currentOptions();
    for (let index = activeIndex; index < target; index++) {
      try {
        validateStep(order[index], options);
      } catch (issue) {
        if (index !== activeIndex) showStep(index);
        fail(issue.message);
        return;
      }
    }
    showStep(target);
  }

  root.addEventListener('submit', event => {
    event.preventDefault();
    if (activeIndex < steps().length - 1) goTo(activeIndex + 1);
  });
  byId('back').addEventListener('click', () => showStep(activeIndex - 1));
  byId('skip').addEventListener('click', () => {
    furthest = steps().length - 1;
    goTo(steps().length - 1);
  });
  byId('progress').addEventListener('click', event => {
    const button = event.target.closest('.progress-step');
    if (!button || button.disabled) return;
    const target = Number(button.dataset.index);
    if (target <= activeIndex) showStep(target);
    else goTo(target);
  });
  root.addEventListener('input', update);
  root.addEventListener('change', update);
  showStep(0, false);
  update();
  byId('next').disabled = false;
}
