#!/usr/bin/env python3
"""Build the release package: ``dist/simple-<version>.zip``.

The package is a snapshot of the tagged source with the development-only files
removed - no git history, no tests, no frontend sources or build configuration.
It is what the READMEs' "download the release package" path installs; pysim is
NOT included (``setup.sh`` / ``setup.bat`` fetch it from its upstream
repository).

Usage:
    python tools/build_release.py [--out DIR] [--expect-version X.Y.Z] [--list]
"""

import argparse
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The PWA's runtime files: everything ``sw.js`` precaches, plus the service
# worker itself.  tests/test_release_package.py cross-checks this list against
# sw.js, so a newly precached file cannot silently go missing from a package.
FRONTEND_FILES = (
    'index.html', 'style.css', 'sw.js', 'help.html', 'help-ru.html',
    'uicc_files.json', 'manifest.json', 'favicon.svg',
    'icon-192.png', 'icon-512.png',
    'sim.png', 'sim.svg', 'sim_anim.svg', 'nosim.svg',
    'aes-bundle.js', 'des-bundle.js',
)

# Everything a user needs (and the source of what the package ships - the GPL
# "corresponding source" is also the public repo at the same tag).
PACKAGE_ENTRIES = (
    'pysim_simple_server',
    'docs',
    'tools',
    'setup.sh', 'setup.bat', 'start.sh', 'start.bat',
    'update.sh', 'update.bat',
    'pyproject.toml', 'requirements-pysim.txt',
    'README.md', 'README_RUS.md', 'CHANGELOG.md',
    'LICENSE', 'THIRD-PARTY-NOTICES',
)


def version():
    """The server's VERSION - the single source of truth for the package name."""
    text = (REPO / 'pysim_simple_server' / 'server.py').read_text(encoding='utf-8')
    m = re.search(r"^VERSION = '([^']+)'$", text, re.M)
    if not m:
        sys.exit('error: VERSION not found in pysim_simple_server/server.py')
    return m.group(1)


def _iter_files(src):
    """The files to package from a PACKAGE_ENTRIES path (no bytecode, no caches)."""
    if src.is_file():
        yield src
        return
    for p in sorted(src.rglob('*')):
        if p.is_dir() or '__pycache__' in p.parts or p.suffix == '.pyc':
            continue
        yield p


def build(out_dir):
    """Write the package and return its path."""
    ver = version()
    name = 'simple-%s' % ver
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (name + '.zip')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for entry in PACKAGE_ENTRIES:
            src = REPO / entry
            if not src.exists():
                sys.exit('error: missing %s' % entry)
            for p in _iter_files(src):
                z.write(p, '%s/%s' % (name, p.relative_to(REPO).as_posix()))
        for f in FRONTEND_FILES:
            src = REPO / 'frontend' / f
            if not src.is_file():
                sys.exit('error: missing frontend/%s' % f)
            z.write(src, '%s/frontend/%s' % (name, f))
    return out


def main():
    ap = argparse.ArgumentParser(description='Build the SIMple release package.')
    ap.add_argument('--out', default=str(REPO / 'dist'), help='output directory (default: <repo>/dist)')
    ap.add_argument('--expect-version', metavar='VER',
                    help='fail unless the source VERSION matches (CI: the tag without "v")')
    ap.add_argument('--list', action='store_true', help='print the archive contents')
    args = ap.parse_args()

    if args.expect_version and version() != args.expect_version:
        sys.exit('error: version mismatch: source says %s, expected %s'
                 % (version(), args.expect_version))
    out = build(args.out)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    if args.list:
        for n in names:
            print(n)
    print('%s: %d files, %d bytes' % (out, len(names), out.stat().st_size))


if __name__ == '__main__':
    main()
