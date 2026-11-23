"""Checking that downloads leave through the VPN, and from where.

The controller asks a download app behind Gluetun (Transmission, when installed) for its public address and
location, so the answer is the one that app really uses. When that fails, Gluetun's recent log usually says why.
"""
import json
import re

LOOKUP = 'https://ipinfo.io/json'
# The download apps that may share Gluetun's network, in the order the check prefers them.
DOWNLOAD_APPS = ('transmission', 'prowlarr')
PROBLEMS = (
    (re.compile(r'AUTH_FAILED'), 'NordVPN rejected the login. Check the service credentials under Settings → VPN, '
                                 'and that the account isn’t at its device limit.'),
    (re.compile(r'TLS (handshake|key negotiation) failed|TLS Error'),
     'The VPN server didn’t complete the secure handshake. Gluetun keeps trying other servers.'),
    (re.compile(r'(?i)i/o timeout|network is unreachable|no route to host|connection refused'),
     'Gluetun can’t reach a VPN server from this network. If UDP is blocked here, try TCP under Settings → VPN.'),
)


def location(text):
    """The parts of the lookup's answer worth showing."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get('ip'), str):
        raise ValueError('unexpected answer')
    return {key: data.get(key) for key in ('ip', 'city', 'region', 'country', 'org')}


def diagnose(stack, container):
    logs = stack.run(['logs', '--since', '15m', container], timeout=30)
    text = (logs.stdout or '') + (logs.stderr or '')
    for pattern, message in PROBLEMS:
        if pattern.search(text):
            return message
    return 'The VPN isn’t connected. Gluetun’s log on the VM says why: docker logs ' + container


def check(stack):
    """Where the downloads go out, and whether that is through the VPN."""
    specs = stack.services()
    if 'gluetun' not in specs:
        return {'installed': False}
    containers = stack.containers()
    gluetun = containers.get('gluetun') or {}
    behind = [app for app in DOWNLOAD_APPS if specs.get(app, {}).get('network_mode') == 'service:gluetun'
              and (containers.get(app) or {}).get('state') == 'running']
    direct = [app for app in DOWNLOAD_APPS if app in specs and app not in behind
              and specs[app].get('network_mode') != 'service:gluetun']
    result = {'installed': True, 'state': gluetun.get('state', 'missing'), 'health': gluetun.get('health'),
              'probe': behind[0] if behind else 'gluetun', 'direct': direct, 'exit': None, 'problem': None}
    if gluetun.get('state') != 'running':
        result['problem'] = 'Gluetun isn’t running, so nothing behind it can reach the internet.'
        return result
    probe = result['probe']
    command = (['curl', '-fsS', '--max-time', '15', LOOKUP] if probe != 'gluetun'
               else ['wget', '-qO-', '-T', '15', LOOKUP])
    answer = stack.compose('exec', '-T', probe, *command, timeout=60)
    try:
        if answer.returncode:
            raise ValueError(answer.stderr)
        result['exit'] = location(answer.stdout)
    except ValueError:
        name = stack.run(['inspect', '--format', '{{.Name}}', gluetun['id']], timeout=30).stdout.strip().lstrip('/')
        result['problem'] = diagnose(stack, name or gluetun['id'])
    return result
