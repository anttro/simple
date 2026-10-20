#!/usr/bin/env python3
"""Print the CHANGELOG.md section of a version (the release notes body).

Usage: python tools/changelog_section.py X.Y.Z
Exits non-zero when the version has no section - the workflow then falls back
to generated notes.
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / 'CHANGELOG.md'


def section(version):
    text = CHANGELOG.read_text(encoding='utf-8')
    pat = re.compile(r'^## \[%s\][^\n]*\n(.*?)(?=^## |\Z)'
                     % re.escape(version), re.M | re.S)
    m = pat.search(text)
    if not m:
        return None
    body = m.group(1).strip()
    return body + '\n' if body else None


def main(argv):
    if len(argv) != 2:
        sys.exit('usage: changelog_section.py X.Y.Z')
    body = section(argv[1])
    if body is None:
        sys.exit('error: no CHANGELOG.md section for %s' % argv[1])
    sys.stdout.write(body)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
