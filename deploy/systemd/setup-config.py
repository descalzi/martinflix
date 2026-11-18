#!/usr/bin/env python3
"""Create a private systemd environment file on the MartinFlix host."""
import getpass
import os
import secrets
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit('Run this setup script with sudo on the MartinFlix host.')
path = Path('/etc/martinflix/dashboard.env')
if path.exists():
    raise SystemExit('Configuration already exists; edit it directly rather than overwriting it.')
password = getpass.getpass('Choose an admin password (at least 16 characters): ')
if len(password) < 16 or '\n' in password or '\r' in password or '\x00' in password:
    raise SystemExit('Password must be at least 16 characters and on one line.')
if password != getpass.getpass('Confirm password: '):
    raise SystemExit('Passwords did not match; no file created.')
# systemd EnvironmentFile double-quoted strings support these escapes.
def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as out:
    out.write('MARTINFLIX_BIND=0.0.0.0\nMARTINFLIX_PORT=8765\nMARTINFLIX_ADMIN_USER=admin\nMARTINFLIX_ADMIN_PASSWORD=' + quote(password) + '\n')
print('Created /etc/martinflix/dashboard.env with root-only permissions.')
