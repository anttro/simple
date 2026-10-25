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
The optional / service-gated files pySim's model does not carry (the SoLSA,
MExE, BCAST, ACDC, TV and A2X DFs, EF.LRPLMNSI, EF.IAL, EF.IPD, EF.TVCONFIG,
EF.3GPPPSDATAOFF) are appended from ``SUPPLEMENT`` with their spec locations -
the decoded-editing filesystem check resolves a file by its (name, root), so a
same-FID file under another DF must stay a distinct entry.
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


# Files the pySim model does not carry (optional / service-gated), with their
# spec locations: TS 31.102 4.2.86 (EF.LRPLMNSI), 4.2.100 (EF.IAL), 4.2.102
# (EF.IPD), 4.2.108 (EF.TVCONFIG), 4.2.109 (EF.3GPPPSDATAOFF), 4.4.1
# (DF.SoLSA), 4.4.4 (DF.MExE), 4.4.7 (DF.BCAST - its files are OMA BCAST, so
# the DF is listed alone), 4.4.9 (DF.ACDC), 4.4.10 (DF.TV - EF.TVUSD is a
# variable 4Fxx, omitted) and 4.6.6 (DF.A2X); the FID map is
# docs/uicc/UICC_FILES.md 4.13.  Entries are (root, parent FID chain, fid,
# name); a DF is a 'DF.' name and precedes its children so the symbolic chain
# can be resolved.
SUPPLEMENT = (
    ('ADF.USIM', (), '6FDC', 'EF.LRPLMNSI'),
    ('ADF.USIM', (), '6FF0', 'EF.IAL'),
    ('ADF.USIM', (), '6FF2', 'EF.IPD'),
    ('ADF.USIM', (), '6FF9', 'EF.3GPPPSDATAOFF'),
    ('ADF.USIM', (), '6FFB', 'EF.TVCONFIG'),
    ('ADF.USIM', (), '5F3C', 'DF.MExE'),
    ('ADF.USIM', ('5F3C',), '4F40', 'EF.MExE-ST'),
    ('ADF.USIM', ('5F3C',), '4F41', 'EF.ORPK'),
    ('ADF.USIM', ('5F3C',), '4F42', 'EF.ARPK'),
    ('ADF.USIM', ('5F3C',), '4F43', 'EF.TPRPK'),
    ('ADF.USIM', (), '5F70', 'DF.SoLSA'),
    ('ADF.USIM', ('5F70',), '4F30', 'EF.SAI'),
    ('ADF.USIM', ('5F70',), '4F31', 'EF.SLL'),
    ('ADF.USIM', (), '5F80', 'DF.BCAST'),
    ('ADF.USIM', (), '5FA0', 'DF.ACDC'),
    ('ADF.USIM', ('5FA0',), '4F01', 'EF.ACDC_LIST'),
    ('ADF.USIM', (), '5FB0', 'DF.TV'),
    ('MF', ('7F10',), '5F3F', 'DF.A2X'),
    ('MF', ('7F10', '5F3F'), '4F01', 'EF.AST'),
    ('MF', ('7F10', '5F3F'), '4F02', 'EF.A2X_CONFIG'),
)

# The non-IMSI USIM application repeats the ADF.USIM files under its own root
# (the walk emits it as a separate ADF with the same file set).
USIM_ROOT = 'ADF.USIM'
USIM_MIRROR = 'ADF.USIM-NON-IMSI'


def _add_supplement(out, seen, sym_by_path):
    """Append SUPPLEMENT, mirroring the ADF.USIM files under the non-IMSI root."""
    for root, parents, fid, name in SUPPLEMENT:
        roots = [root]
        if root == USIM_ROOT and USIM_MIRROR in seen:
            roots.append(USIM_MIRROR)
        for target in roots:
            key = '/'.join((target,) + tuple(parents) + (fid,)).upper()
            if key in seen:
                continue
            parent_key = '/'.join((target,) + tuple(parents)).upper()
            sym = '%s/%s' % (sym_by_path.get(parent_key, parent_key), name)
            seen.add(key)
            out.append(_entry(key, sym, name, fid,
                              'df' if name.startswith('DF.') else 'ef', target))
            sym_by_path[key] = sym


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
    # The optional / service-gated files pySim's model does not carry.
    _add_supplement(out, seen, {e['path']: e['sym'] for e in out})
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
