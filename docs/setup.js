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
const steps = [...root.querySelectorAll('.step')];
const progress = [...document.querySelectorAll('.steps li')];
let activeStep = 0;
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

function currentOptions() {
  return {
    lan: root.elements.access.value === 'lan',
    downloads: byId('downloads').checked,
    address: byId('address').value.trim(),
    media: byId('media').value.trim(),
    state: byId('state').value.trim(),
    ports: Object.fromEntries(portOptions.map(([name]) => [name, byId(name + '-port').value])),
  };
}

function update() {
  const options = currentOptions();
  byId('lan-options').hidden = !options.lan;
  for (const name of ['prowlarr', 'transmission']) byId(name + '-port').parentElement.hidden = !options.downloads;
  const error = byId('error');
  byId('copy-status').textContent = '';
  byId('step-error').hidden = true;
  try {
    const command = ['python3 deploy/bootstrap/install.py', ...installArguments(options)].join(' ');
    byId('check-command').value = command + ' --check';
    byId('install-command').value = command;
    error.hidden = true;
    for (const button of copyButtons) button.disabled = false;
  } catch (issue) {
    error.textContent = 'Preview paused. ' + issue.message;
    error.hidden = false;
    for (const button of copyButtons) button.disabled = true;
  }
}

function showStep(index) {
  activeStep = index;
  steps.forEach((step, number) => {
    step.hidden = number !== index;
    progress[number].classList.toggle('complete', number < index);
    if (number === index) progress[number].setAttribute('aria-current', 'step');
    else progress[number].removeAttribute('aria-current');
  });
  byId('step-error').hidden = true;
  byId('back').hidden = index === 0;
  byId('next').hidden = index === steps.length - 1;
  byId('next').textContent = index === steps.length - 2 ? 'Review setup' : 'Next';
  if (index === steps.length - 1) {
    const options = currentOptions();
    const summary = [
      ['Dashboard', options.lan ? options.address : 'On the Linux host'],
      ['Media directory', options.media || 'Configure later'],
      ['Download services', options.downloads ? 'Transmission and Prowlarr via NordVPN' : 'Configure later'],
    ];
    if (options.state) summary.push(['Installation state', options.state]);
    for (const [name, label, fallback] of portOptions.slice(0, options.downloads ? 5 : 3)) {
      const port = options.ports[name];
      if (port && Number(port) !== fallback) summary.push([label + ' port', port]);
    }
    const review = byId('review');
    review.replaceChildren();
    for (const [label, value] of summary) {
      const term = document.createElement('dt');
      const definition = document.createElement('dd');
      term.textContent = label;
      definition.textContent = value;
      review.append(term, definition);
    }
  }
  steps[index].querySelector('h2').focus();
}

function nextStep() {
  if (activeStep === steps.length - 1) return;
  const options = currentOptions();
  const defaults = Object.fromEntries(portOptions.map(([name]) => [name, '']));
  try {
    installArguments({
      ...options,
      lan: activeStep === 0 && options.lan,
      downloads: activeStep > 0 && options.downloads,
      media: activeStep > 0 ? options.media : '',
      state: activeStep > 1 ? options.state : '',
      ports: activeStep > 1 ? options.ports : defaults,
    });
    showStep(activeStep + 1);
  } catch (issue) {
    const error = byId('step-error');
    error.textContent = issue.message;
    error.hidden = false;
  }
}

root.addEventListener('submit', event => {
  event.preventDefault();
  nextStep();
});
byId('next').addEventListener('click', event => {
  event.preventDefault();
  nextStep();
});
byId('back').addEventListener('click', () => showStep(activeStep - 1));
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
byId('next').disabled = false;
