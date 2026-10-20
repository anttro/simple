#!/usr/bin/env python3
"""Update a packaged SIMple install in place.

The release package is a snapshot of the application, and the slow part of a
reinstall is pysim and its prerequisites (built into ``.venv`` by setup -
pyscard compiles from source on Linux).  This script downloads the latest
release package, replaces the application files in place and re-installs the
app into the **existing** ``.venv``: pysim is kept, so an update takes seconds.

Usage:
    python tools/update.py [--check] [--version X.Y.Z] [--zip FILE] [--no-pip] [--force]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
REPO_API = 'https://api.github.com/repos/anttro/simple/releases/latest'
ASSET_URL = 'https://github.com/anttro/simple/releases/download/v%s/simple-%s.zip'
UA = {'User-Agent': 'simple-update'}
# Never overwritten even if a package carried them (it never does).
SKIP_TOP = {'.venv', 'dist', '__pycache__', '.git'}


def current_version():
    """The installed app's VERSION - the same source the build reads."""
    text = (APP / 'pysim_simple_server' / 'server.py').read_text(encoding='utf-8')
    m = re.search(r"^VERSION = '([^']+)'$", text, re.M)
    if not m:
        sys.exit('error: cannot read VERSION from pysim_simple_server/server.py')
    return m.group(1)


def latest_version():
    req = urllib.request.Request(REPO_API, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        tag = (json.load(r).get('tag_name') or '').strip()
    if not tag.startswith('v') or len(tag) < 2:
        sys.exit('error: unexpected release tag %r' % tag)
    return tag[1:]


def download(version, dest):
    url = ASSET_URL % (version, version)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)


def safe_extract(zf, dest):
    """Extract, refusing paths outside dest and restoring the permission bits
    (zipfile drops them, which would strip +x from setup.sh/start.sh/update.sh)."""
    dest = Path(dest).resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if target != dest and dest not in target.parents:
            sys.exit('error: unsafe path in the package: %s' % info.filename)
    zf.extractall(dest)
    for info in zf.infolist():
        mode = (info.external_attr >> 16) & 0o777
        if mode and not info.is_dir():
            try:
                os.chmod(dest / info.filename, mode)
            except OSError:
                pass


def verify(root, version):
    """The package must be the version it claims (the tag is the version)."""
    src = root / 'pysim_simple_server' / 'server.py'
    if not src.is_file():
        sys.exit('error: the package lacks pysim_simple_server/server.py')
    m = re.search(r"^VERSION = '([^']+)'$", src.read_text(encoding='utf-8'), re.M)
    if not m or m.group(1) != version:
        sys.exit('error: the package version (%s) does not match %s'
                 % (m.group(1) if m else '?', version))


def replace_tree(new_root):
    """Copy the new package over the app directory; the venv and the run log
    (neither is in the package) stay untouched."""
    count = 0
    for src in sorted(new_root.rglob('*')):
        if not src.is_file():
            continue
        rel = src.relative_to(new_root)
        if rel.parts[0] in SKIP_TOP or '__pycache__' in rel.parts or src.suffix == '.pyc':
            continue
        dst = APP / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        count += 1
    return count


def venv_python():
    for rel in ('bin/python', 'Scripts/python.exe'):
        p = APP / '.venv' / rel
        if p.exists():
            return p
    return None


def reinstall():
    py = venv_python()
    if py is None:
        print('note: no .venv in %s - run setup.sh / setup.bat (or install manually)' % APP)
        return False
    # --no-build-isolation: setuptools is in the venv (setup installed it);
    # this keeps the update working offline.
    r = subprocess.run([str(py), '-m', 'pip', 'install', '--no-build-isolation',
                        '-e', str(APP)], capture_output=True, text=True)
    if r.returncode != 0:
        print('warning: the files were updated, but "pip install -e" failed:')
        print((r.stderr or r.stdout).strip()[-800:])
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description='Update a packaged SIMple install in place.')
    ap.add_argument('--check', action='store_true',
                    help='print the current and latest versions and exit')
    ap.add_argument('--version', metavar='X.Y.Z',
                    help='update to this release (default: the latest)')
    ap.add_argument('--zip', metavar='FILE',
                    help='update from a local package file (offline)')
    ap.add_argument('--no-pip', action='store_true',
                    help='replace the files only (do not re-install into .venv)')
    ap.add_argument('--force', action='store_true', help='re-apply the same version')
    args = ap.parse_args()

    cur = current_version()
    version = args.version
    if not version and not args.zip:
        try:
            version = latest_version()
        except Exception as e:
            sys.exit('error: cannot reach GitHub (%s); pass --version X.Y.Z' % e)
    if args.check:
        print('current: %s' % cur)
        print('latest:  %s' % (version or '?'))
        return 0
    if not version:
        m = re.search(r'simple-([0-9][^\\/]*)\.zip$', str(args.zip))
        if not m:
            sys.exit('error: cannot tell the package version; pass --version')
        version = m.group(1)
    if version == cur and not args.force:
        print('already up to date (%s)' % cur)
        return 0

    with tempfile.TemporaryDirectory(prefix='simple-update-') as tmp:
        tmp = Path(tmp)
        if args.zip:
            zpath = Path(args.zip)
            if not zpath.is_file():
                sys.exit('error: %s not found' % zpath)
        else:
            zpath = tmp / 'package.zip'
            print('downloading %s ...' % (ASSET_URL % (version, version)))
            try:
                download(version, zpath)
            except Exception as e:
                sys.exit('error: download failed (%s)' % e)
        try:
            with zipfile.ZipFile(zpath) as zf:
                safe_extract(zf, tmp)
        except zipfile.BadZipFile as e:
            sys.exit('error: %s is not a valid package: %s' % (zpath, e))
        root = tmp / ('simple-%s' % version)
        if not root.is_dir():
            found = [p for p in tmp.iterdir()
                     if p.is_dir() and (p / 'pysim_simple_server').is_dir()]
            if len(found) != 1:
                sys.exit('error: the package layout is unexpected')
            root = found[0]
        verify(root, version)
        count = replace_tree(root)

    print('updated %s -> %s (%d files)' % (cur, version, count))
    if args.no_pip:
        print('note: --no-pip - .venv was left untouched')
    elif reinstall():
        print('app re-installed into .venv (pysim kept)')
    print('restart the server: ./start.sh (or start.bat)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
