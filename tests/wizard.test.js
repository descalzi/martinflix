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
const LAUNCH = 'docker run --rm -it -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME:$HOME" -e HOME';

test('a new installation runs from the image with the defaults, with nothing to clone', () => {
  const [check, install] = commands({});
  assert.strictEqual(flat(install.command), `${LAUNCH} ghcr.io/descalzi/martinflix:${wizard.DEFAULT_CHANNEL} install ` +
    '--media-path=/mnt/media --enable-prowlarr --enable-flaresolverr --download-network=direct');
  assert.ok(install.command.startsWith('docker run --rm -it \\\n  -v /var/run/docker.sock'));
  assert.ok(check.command.endsWith('--check'));
  assert.doesNotMatch(install.command + check.command, /git|python3/);
});

test('the release channel picks the image', () => {
  assert.match(commands({channel: 'stable'})[1].command, /martinflix:stable install/);
  assert.match(commands({mode: 'update', changes: {route: 'keep', channel: 'stable'}})[1].command, /martinflix:stable update/);
  assert.match(flat(commands({mode: 'update', changes: {route: 'keep', channel: 'stable'}})[1].command), /update --channel=stable$/);
});

test('custom answers become flags, quoting only when needed', () => {
  const args = wizard.installArguments({...defaults, lan: true, address: '192.168.4.166', media: '/mnt/my media',
    vpn: true, exit: 'United Kingdom', downloader: 'transmission', behindVpn: true, ports: {sonarr: '18989', 'transmission-peer': '51413'}});
  assert.deepStrictEqual(args, ['--dashboard-bind=192.168.4.166', "--media-path='/mnt/my media'", '--enable-vpn',
    '--vpn-provider=nordvpn', "--vpn-country='United Kingdom'", '--download-client=transmission', '--enable-prowlarr',
    '--enable-flaresolverr', '--download-network=vpn', '--sonarr-port=18989', '--transmission-peer-port=51413']);
});

test('an update keeps saved settings and only names a custom state directory', () => {
  const [check, run] = commands({mode: 'update', lan: true, address: 'ignored', state: '/srv/martinflix'});
  const image = `ghcr.io/descalzi/martinflix:${wizard.DEFAULT_CHANNEL}`;
  assert.strictEqual(flat(run.command), `${LAUNCH} ${image} update --state-dir=/srv/martinflix`);
  assert.strictEqual(flat(check.command), `${LAUNCH} ${image} update --state-dir=/srv/martinflix --check`);
});

test('an update passes only the changes that were made', () => {
  const [, plain] = commands({mode: 'update', changes: {access: 'keep', route: 'keep'}});
  assert.match(flat(plain.command), / update$/);
  const [, changed] = commands({mode: 'update', changes: {access: 'lan', address: '192.168.4.166', vpn: true, exit: 'United Kingdom',
    route: 'vpn', bazarr: true, media: '/mnt/media'}});
  assert.ok(flat(changed.command).endsWith(" update --dashboard-bind=192.168.4.166 " +
    "--media-path=/mnt/media --enable-vpn --vpn-country='United Kingdom' --enable-bazarr --download-network=vpn"));
  assert.throws(() => commands({mode: 'update', changes: {access: 'lan', address: 'nope'}}), /LAN IPv4/);
});

test('a reset can be checked before anything is archived', () => {
  const [check, run] = commands({mode: 'reset'});
  assert.match(flat(check.command), / reset --media-path.* --check$/);
  assert.match(flat(run.command), /martinflix:\w+ reset --media-path/);
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

test('Discover is on by default; switching it off or using your own key become flags', () => {
  assert.deepStrictEqual(wizard.installArguments({...defaults, prowlarr: false, flaresolverr: false, discover: true, region: 'GB'}),
    ['--media-path=/mnt/media']);
  assert.ok(wizard.installArguments({...defaults, discover: true, ownKey: true}).includes('--set-tmdb-key'));
  assert.ok(wizard.installArguments({...defaults, discover: true, region: 'IE'}).includes('--discover-region=IE'));
  const off = wizard.installArguments({...defaults, discover: false, ownKey: true, region: 'IE'});
  assert.deepStrictEqual(off.filter(arg => arg.includes('discover') || arg.includes('tmdb')), ['--discover=off']);
  const update = wizard.installArguments({mode: 'update', changes: {route: 'keep', page: 'on', discover: true, region: 'US'}});
  assert.deepStrictEqual(update, ['--discover=on', '--set-tmdb-key', '--discover-region=US']);
});

test('a Plex address connects Plex and asks setup for its token', () => {
  const args = wizard.installArguments({...defaults, prowlarr: false, flaresolverr: false, discover: true, plexUrl: 'http://192.168.5.204:32400'});
  assert.deepStrictEqual(args, ['--media-path=/mnt/media', '--plex-url=http://192.168.5.204:32400', '--set-plex-token']);
  assert.deepStrictEqual(wizard.installArguments({mode: 'update', changes: {route: 'keep', plexToken: true}}), ['--set-plex-token']);
});

test('Plex is installed with the media directory, and then no outside Plex address is used', () => {
  const args = wizard.installArguments({...defaults, prowlarr: false, flaresolverr: false, plex: true, plexUrl: 'http://elsewhere:32400'});
  assert.deepStrictEqual(args, ['--media-path=/mnt/media', '--enable-plex']);
  assert.deepStrictEqual(wizard.installArguments({mode: 'update', changes: {route: 'keep', plex: true}}), ['--enable-plex']);
});

test('a Plex name is passed only when it differs from martinflix', () => {
  const base = {...defaults, prowlarr: false, flaresolverr: false, plex: true};
  assert.deepStrictEqual(wizard.installArguments({...base, plexName: 'martinflix'}), ['--media-path=/mnt/media', '--enable-plex']);
  assert.ok(wizard.installArguments({...base, plexName: 'martinflix test'}).includes("--plex-name='martinflix test'"));
  assert.deepStrictEqual(wizard.installArguments({mode: 'update', changes: {route: 'keep', plexName: 'martinflix-test'}}),
    ['--plex-name=martinflix-test']);
});

test('a backup folder is passed on and must be an absolute path', () => {
  const args = wizard.installArguments({...defaults, prowlarr: false, flaresolverr: false, backupDir: '/mnt/martinflix/Backups'});
  assert.deepStrictEqual(args, ['--media-path=/mnt/media', '--backup-dir=/mnt/martinflix/Backups']);
  assert.deepStrictEqual(wizard.installArguments({mode: 'update', changes: {route: 'keep', backupDir: '/mnt/nas/Backups'}}),
    ['--backup-dir=/mnt/nas/Backups']);
  assert.throws(() => wizard.validateStep('options', {...defaults, backupDir: 'Backups'}), /Backup folder must be an absolute/);
});
