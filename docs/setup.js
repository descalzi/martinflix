'use strict';

const portOptions = [
  ['dashboard', 'Dashboard', 8765],
  ['sonarr', 'Sonarr', 8989],
  ['radarr', 'Radarr', 7878],
  ['prowlarr', 'Prowlarr', 9696],
  ['transmission', 'Transmission', 9091],
];

function shellQuote(value) {
  return "'" + value.replace(/'/g, "'\"'\"'") + "'";
}

function installArguments({lan, address, media, downloads, state, ports}) {
  const args = [];
  if (lan) {
    const parts = address.split('.');
    if (parts.length !== 4 || parts.some(part => !/^(0|[1-9][0-9]{0,2})$/.test(part) || Number(part) > 255) ||
        Number(parts[0]) === 0 || Number(parts[0]) === 127 || Number(parts[0]) >= 224) {
      throw new Error('Enter the Linux host’s LAN IPv4 address.');
    }
    args.push('--dashboard-bind=' + shellQuote(address));
  }
  if (downloads && !media) throw new Error('Enter a media directory to enable downloads.');
  for (const [flag, value, label] of [['media-path', media, 'Media directory'], ['state-dir', state, 'State directory']]) {
    if (!value) continue;
    if (!value.startsWith('/') || /[\u0000-\u001f\u007f]/.test(value)) {
      throw new Error(label + ' must be an absolute Linux path starting with /.');
    }
    args.push('--' + flag + '=' + shellQuote(value));
  }
  if (downloads) args.push('--enable-downloads');
  const usedPorts = new Set();
  for (const [name, label, fallback] of portOptions.slice(0, downloads ? 5 : 3)) {
    const raw = ports[name];
    const port = raw === '' ? fallback : Number(raw);
    if ((raw !== '' && !/^[0-9]+$/.test(raw)) || port < 1024 || port > 65535) {
      throw new Error(label + ' needs a port from 1024 to 65535.');
    }
    if (usedPorts.has(port)) throw new Error('Each enabled service needs a different port.');
    usedPorts.add(port);
    if (port !== fallback) args.push('--' + name + '-port=' + port);
  }
  return args;
}

const byId = id => document.getElementById(id);
const root = byId('options');
const copyButtons = document.querySelectorAll('[data-copy]');
for (const [name, label, fallback] of portOptions) {
  const row = document.createElement('div');
  const caption = document.createElement('label');
  const input = document.createElement('input');
  input.id = name + '-port';
  input.type = 'number';
  input.min = 1024;
  input.max = 65535;
  input.placeholder = String(fallback);
  caption.htmlFor = input.id;
  caption.textContent = label + ' port';
  row.append(caption, input);
  byId('ports').append(row);
}

function update() {
  const lan = root.elements.access.value === 'lan';
  const downloads = byId('downloads').checked;
  byId('lan-options').hidden = !lan;
  for (const name of ['prowlarr', 'transmission']) byId(name + '-port').parentElement.hidden = !downloads;
  const error = byId('error');
  byId('copy-status').textContent = '';
  const check = byId('check-command');
  const install = byId('install-command');
  try {
    const args = installArguments({
      lan, downloads, address: byId('address').value.trim(), media: byId('media').value.trim(),
      state: byId('state').value.trim(), ports: Object.fromEntries(portOptions.map(([name]) => [name, byId(name + '-port').value])),
    });
    const command = ['python3 deploy/bootstrap/install.py', ...args].join(' ');
    check.value = command + ' --check';
    install.value = command;
    error.hidden = true;
    for (const button of copyButtons) button.disabled = false;
  } catch (issue) {
    check.value = '';
    install.value = '';
    error.textContent = issue.message;
    error.hidden = false;
    for (const button of copyButtons) button.disabled = true;
  }
}

root.addEventListener('submit', event => event.preventDefault());
root.addEventListener('input', update);
for (const button of copyButtons) {
  button.addEventListener('click', async () => {
    const output = byId(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(output.value);
      byId('copy-status').textContent = 'Copied to clipboard.';
    } catch {
      output.focus();
      output.select();
      byId('copy-status').textContent = 'Command selected. Copy it with your keyboard or device menu.';
    }
  });
}
update();
