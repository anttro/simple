"""Standard UICC/SIM file list for the Remote APDU SELECT helper.

The list is built **without a card** from pySim's card profiles and application
classes - the same model pySim assembles at equip time:

* ``CardProfileUICC`` (TS 102 221) and ``CardProfileSIM`` (TS 51 011) give the
  MF tree (EF.DIR/ICCID/PL/ARR/UMPC, DF.TELECOM, DF.GSM, ...),
* every concrete ``CardApplication`` subclass contributes its ADF subtree
  (ADF.USIM, ADF.ISIM, ADF.ISD-R, ADF.ISD, ADF.ECASD, ...) with the AID.

It ships as ``frontend/uicc_files.json`` so the SIM/USIM RFM SELECT picker has
a file list offline (no card, no server); when a card is equipped the picker
merges the live tree over it and hides probed-absent files.
``tests/test_uicc_files.py`` regenerates the list and fails when pySim adds or
renames files, so the asset cannot silently drift.

Usage (from the repo root)::

    python -m pysim_simple_server.uicc_files --write frontend/uicc_files.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os

# Importing these modules registers their CardApplication subclasses, which are
# discovered through osmocom's all_subclasses() - the same trick fastinit uses.
import pySim.euicc
import pySim.ts_31_102
import pySim.ts_31_103

from osmocom.utils import all_subclasses
from pySim.filesystem import CardApplication, CardDF
from pySim.ts_102_221 import CardProfileUICC
from pySim.ts_51_011 import CardProfileSIM

# Where the frontend asset lives, relative to the repository root.
DEFAULT_ASSET = os.path.join('frontend', 'uicc_files.json')


def _entry(fid_path, sym_path, name, fid, kind, root, aid=None):
    out = {
        'path': fid_path.upper(),
        'name': name,
        'sym': sym_path,
        'fid': (fid or '').upper(),
        'kind': kind,
        'root': root.upper(),
    }
    if aid:
        out['aid'] = aid.upper()
    return out


def _walk(node, fid_path, sym_path, root, out, seen):
    """Depth-first walk of a DF/ADF subtree (children is a fid -> CardFile dict)."""
    for fid, child in sorted(node.children.items()):
        key = ('%s/%s' % (fid_path, fid)).upper()
        if key in seen:
            continue
        seen.add(key)
        is_df = isinstance(child, CardDF)
        out.append(_entry(key, '%s/%s' % (sym_path, child.name), child.name, fid,
                          'df' if is_df else 'ef', root))
        if is_df:
            _walk(child, key, '%s/%s' % (sym_path, child.name), root, out, seen)


def _add_mf_files(files, out, seen):
    for f in files:
        key = ('MF/%s' % f.fid).upper()
        if key in seen:
            continue
        seen.add(key)
        is_df = isinstance(f, CardDF)
        out.append(_entry(key, 'MF/%s' % f.name, f.name, f.fid,
                          'df' if is_df else 'ef', 'MF'))
        if is_df:
            _walk(f, key, 'MF/%s' % f.name, 'MF', out, seen)


def _applications():
    """One instance per concrete CardApplication subclass (as fastinit does)."""
    apps = []
    for app_cls in all_subclasses(CardApplication):
        if hasattr(app_cls, '_' + app_cls.__name__ + '__intermediate'):
            continue
        try:
            apps.append(app_cls())
        except Exception:
            continue
    return apps


def build():
    """Return the standard file list (canonical path, symbolic name, fid, kind)."""
    out = []
    seen = set()
    uicc = CardProfileUICC()
    # CardProfileUICC itself carries no applications - like fastinit does at
    # equip time, attach every concrete CardApplication subclass.
    for app in _applications():
        uicc.add_application(app)
    _add_mf_files(uicc.files_in_mf, out, seen)
    _add_mf_files(CardProfileSIM().files_in_mf, out, seen)
    for app in uicc.applications:
        adf = app.adf
        if adf is None:
            continue
        key = adf.name.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(_entry(key, adf.name, adf.name, '', 'adf', adf.name, app.aid))
        _walk(adf, key, adf.name, adf.name, out, seen)
    # Stable order: MF first, then the ADFs alphabetically; DFs before their EFs
    # is implicit in the walk, paths sort consistently inside a root.
    out.sort(key=lambda e: (0 if e['root'] == 'MF' else 1, e['root'], e['path']))
    return out


def build_document():
    return {
        'generated': datetime.date.today().isoformat(),
        'files': build(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--write', metavar='PATH', default=None,
                        help='write the JSON asset (default: %s)' % DEFAULT_ASSET)
    parser.add_argument('--check', action='store_true',
                        help='compare the asset with the freshly built list')
    args = parser.parse_args(argv)
    doc = build_document()
    if args.check:
        with open(args.write or DEFAULT_ASSET, 'r', encoding='utf-8') as fh:
            cur = json.load(fh)
        if cur.get('files') != doc['files']:
            print('uicc_files.json is out of date - regenerate it')
            return 1
        print('uicc_files.json is up to date (%d entries)' % len(doc['files']))
        return 0
    if args.write:
        with open(args.write, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh, indent=1)
            fh.write('\n')
        print('wrote %s (%d entries)' % (args.write, len(doc['files'])))
        return 0
    print(json.dumps(doc, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
