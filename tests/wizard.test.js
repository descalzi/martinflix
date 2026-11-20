'use strict';
const test = require('node:test');
const assert = require('node:assert');
const wizard = require('../docs/setup.js');

const defaults = {
  mode: 'new', lan: false, address: '', media: '/mnt/media', vpn: false, provider: 'nordvpn', exit: 'Prague',
  downloader: '', prowlarr: true, flaresolverr: true, bazarr: false, behindVpn: false, state: '', ports: {},
};
const commands = options => wizard.buildCommands({...defaults, ...options});
const flat = command => command.replace(/ \\\n  /g, ' ');

test('a new installation uses the defaults', () => {
  const [fetch, check, install] = commands({});
  assert.match(fetch.command, /git clone --depth 1/);
  assert.strictEqual(install.command, 'python3 deploy/bootstrap/install.py \\\n  --media-path=/mnt/media \\\n  --enable-prowlarr \\\n  --enable-flaresolverr \\\n  --download-network=direct');
  assert.ok(check.command.endsWith('--check'));
});

test('custom answers become flags, quoting only when needed', () => {
  const args = wizard.installArguments({...defaults, lan: true, address: '192.168.4.166', media: '/mnt/my media',
    vpn: true, exit: 'United Kingdom', downloader: 'transmission', behindVpn: true, ports: {sonarr: '18989', 'transmission-peer': '51413'}});
  assert.deepStrictEqual(args, ['--dashboard-bind=192.168.4.166', "--media-path='/mnt/my media'", '--enable-vpn',
    '--vpn-provider=nordvpn', "--vpn-country='United Kingdom'", '--download-client=transmission', '--enable-prowlarr',
    '--enable-flaresolverr', '--download-network=vpn', '--sonarr-port=18989', '--transmission-peer-port=51413']);
});

test('an update keeps saved settings and only names a custom state directory', () => {
  const [fetch, check, run] = commands({mode: 'update', lan: true, address: 'ignored', state: '/srv/martinflix'});
  assert.match(fetch.command, /git pull --ff-only/);
  assert.strictEqual(flat(run.command), 'python3 deploy/bootstrap/install.py --update --state-dir=/srv/martinflix');
  assert.strictEqual(flat(check.command), 'python3 deploy/bootstrap/install.py --update --state-dir=/srv/martinflix --check');
});

test('an update passes only the changes that were made', () => {
  const [, , plain] = commands({mode: 'update', changes: {access: 'keep', route: 'keep'}});
  assert.strictEqual(flat(plain.command), 'python3 deploy/bootstrap/install.py --update');
  const [, , changed] = commands({mode: 'update', changes: {access: 'lan', address: '192.168.4.166', vpn: true, exit: 'United Kingdom',
    route: 'vpn', bazarr: true, media: '/mnt/media'}});
  assert.strictEqual(flat(changed.command), "python3 deploy/bootstrap/install.py --update --dashboard-bind=192.168.4.166 " +
    "--media-path=/mnt/media --enable-vpn --vpn-country='United Kingdom' --enable-bazarr --download-network=vpn");
  assert.throws(() => commands({mode: 'update', changes: {access: 'lan', address: 'nope'}}), /LAN IPv4/);
});

test('a reset can be checked before anything is archived', () => {
  const [, check, run] = commands({mode: 'reset'});
  assert.match(flat(check.command), /--reset --check$/);
  assert.match(run.command, /--reset$/);
});

test('invalid answers explain the fix', () => {
  assert.throws(() => commands({lan: true, address: '127.0.0.1'}), /LAN IPv4/);
  assert.throws(() => commands({media: ''}), /Enter the media directory/);
  assert.throws(() => commands({media: 'relative/path'}), /absolute/);
  assert.throws(() => commands({ports: {sonarr: '8765'}}), /different port/);
  assert.throws(() => commands({mode: 'update', state: '~/state'}), /absolute/);
});

test('Bazarr is added with the media directory and follows the download route', () => {
  const args = wizard.installArguments({...defaults, bazarr: true, prowlarr: false, flaresolverr: false,
    ports: {bazarr: '16767'}});
  assert.deepStrictEqual(args, ['--media-path=/mnt/media', '--enable-bazarr', '--download-network=direct', '--bazarr-port=16767']);
  assert.throws(() => wizard.validateStep('downloads', {...defaults, media: '', bazarr: true}), /Bazarr needs a media directory/);
});

test('shell quoting survives single quotes', () => {
  assert.strictEqual(wizard.shellQuote("/mnt/martin's media"), "'/mnt/martin'\"'\"'s media'");
});

test('Discover asks setup for a TMDB key and names a region only when it is not the UK', () => {
  assert.deepStrictEqual(wizard.installArguments({...defaults, prowlarr: false, flaresolverr: false, discover: true, region: 'GB'}),
    ['--media-path=/mnt/media', '--set-tmdb-key']);
  assert.ok(wizard.installArguments({...defaults, discover: true, region: 'IE'}).includes('--discover-region=IE'));
  assert.ok(!wizard.installArguments({...defaults, discover: false, region: 'IE'}).some(arg => arg.startsWith('--discover')));
  const update = wizard.installArguments({mode: 'update', changes: {route: 'keep', discover: true, region: 'US'}});
  assert.deepStrictEqual(update, ['--update', '--set-tmdb-key', '--discover-region=US']);
});
