# coding=utf-8
"""EF.PBR (TS 31.102 §4.4.2.1): the phonebook structure and its files.

EF_PBR ('4F30', linear fixed) "describes the structure of the phonebook. All
Efs representing the phonebook are specified here ... together with their file
identifiers (FID) and their short file identifiers (SFI), if applicable."
(Pinned TS 31.102 V18.10.0 §4.4.2.1, p. 139 - ``ts_131102v181000p.pdf``.)
The record structure this module decodes, quoted from the same clause:

    Three different types of file linking exist.  Type 1 files: Files that
    contain as many records as the reference/master file (EFADN, EFADN1) and
    are linked on record number bases (Rec1 -> Rec1). [...] Type 2 files:
    Files that contain less entries than the master file and are linked via
    pointers in the index administration file (EFIAP).  Type 3 files: Files
    that are linked by a record identifier within a record.

    Table 4.1: 'A8' Indicating files where the amount of records equal to
    master EF, type 1; 'A9' Indicating files that are linked using the index
    administration file, type 2 [...]; 'AA' Indicating files that are linked
    using a record identifier, type 3.

    The primitive tag identifies clearly the type of data, its value field
    indicates the file identifier and, if applicable, the SFI value of the
    specified EF.  That is, the length value of a primitive tag indicates if
    an SFI value is available for the EF or not: Length = '02' Value: 'FID (2
    bytes)'; Length = '03' Value: 'FID (2 bytes)', 'SFI (1 byte)'.

    Table 4.2: 'C0' EFADN data object, 'C1' EFIAP, 'C2' EFEXT1, 'C3' EFSNE,
    'C4' EFANR, 'C5' EFPBC, 'C6' EFGRP, 'C7' EFAAS, 'C8' EFGAS, 'C9' EFUID,
    'CA' EFEMAIL, 'CB' EFCCP1, 'CC' EFPURI data object.

    Each record specifies the structure of up to 254 entries in the phone
    book. [...] If more than 254 entries are to be stored, a second record is
    needed in the reference file.

This module is the pure decoder for that record structure plus the helpers the
file tree needs: which files the PBR of one DF references, and how they are
appended to that DF's children (the phonebook files are not in the standard
file list, so a tree listing would otherwise not show them).

The 'AA' "path to the file" form is neither resolved nor injected - the clause
above makes the value length distinguish bare FID / same directory / full
path, so a value that is not 2 or 3 bytes is skipped (the shared decode
vectors carry that as a documented case).  A truncated TLV, a trailing 'FF'
pad byte and an unknown primitive tag are tolerated, never raised: the record
decoder mirrors the PWA's ``efDecPbr`` and the two are pinned against the same
``frontend/tests/pbr_vectors.json``.

The legacy server's ``_pbr_parse``/``_pbr_norm_path``/``_pbr_files_for_node``/
``_pbr_inject_children`` are the reference; the only shape change is that the
cache is passed in (this server keeps it on :class:`~simple_server.server.
ServerState` instead of a module global).  Pure: no transport, no card.
"""

import re

__all__ = [
    'PBR_KINDS', 'PBR_CONTAINERS', 'parse_pbr', 'norm_path', 'files_for_node',
    'inject_children',
]

#: The primitive EF_PBR tags and the phonebook kinds they name (TS 31.102
#: §4.4.2.1; the PWA's ``efDecPbr`` holds the same table).
PBR_KINDS = {
    'C0': 'EF.ADN', 'C1': 'EF.IAP', 'C2': 'EF.EXT1', 'C3': 'EF.SNE',
    'C4': 'EF.ANR', 'C5': 'EF.PBC', 'C6': 'EF.GRP', 'C7': 'EF.AAS',
    'C8': 'EF.GAS', 'C9': 'EF.UID', 'CA': 'EF.EMAIL', 'CB': 'EF.CCP1',
    'CC': 'EF.PURI',
}

#: The constructed link containers and their link type (1/2/3).
PBR_CONTAINERS = {'A8': 1, 'A9': 2, 'AA': 3}

_HEX_RE = re.compile(r'[^0-9A-Fa-f]')


def pbr_tlvs(data):
    """BER-TLV walk of a hex string: ``[(tag, value_hex)]``.

    Short and long form lengths (``81 nn``/``82 nnnn``/...); the walk stops
    cleanly at a truncated TLV - it never raises.  The EF.PBR parser's own
    helper: it is not part of the module's public surface (the decoder is
    ``parse_pbr``, whose behaviour is pinned against the legacy
    ``_pbr_tlvs``).
    """
    out = []
    index = 0
    size = len(data)
    while index + 4 <= size:
        tag = data[index:index + 2]
        length = int(data[index + 2:index + 4], 16)
        index += 4
        if length & 0x80:
            octets = length & 0x7F
            if not octets or index + 2 * octets > size:
                break
            length = int(data[index:index + 2 * octets], 16)
            index += 2 * octets
        if index + 2 * length > size:
            break
        out.append((tag, data[index:index + 2 * length]))
        index += 2 * length
    return out


def parse_pbr(records):
    """Parse EF.PBR records into the referenced phonebook files.

    Returns ``[{'fid','kind','link','sfi'}]`` in record order, deduplicated by
    FID (the first occurrence wins - a larger phonebook repeats the links in
    further records).  ``kind`` is the TS 31.102 name, ``link`` the container
    type (1/2/3) and ``sfi`` the short file identifier (``None`` when the
    primitive carries just the FID, length ``'02'``).  Tolerant: FF padding, a
    truncated TLV and an unknown primitive tag are skipped.
    """
    files = []
    seen = set()
    for record in records or ():
        data = _HEX_RE.sub('', str(record or '')).upper()
        while len(data) >= 2 and data.endswith('FF'):
            data = data[:-2]
        for tag, value in pbr_tlvs(data):
            link = PBR_CONTAINERS.get(tag)
            if link is None:
                continue
            for primitive_tag, primitive in pbr_tlvs(value):
                kind = PBR_KINDS.get(primitive_tag)
                if not kind:
                    continue
                if len(primitive) == 4:
                    fid, sfi = primitive, None
                elif len(primitive) == 6:
                    fid, sfi = primitive[:4], primitive[4:]
                else:
                    continue
                if fid in seen:
                    continue
                seen.add(fid)
                files.append({'fid': fid, 'kind': kind, 'link': link,
                              'sfi': sfi})
    return files


def norm_path(segments, adf_aids=None):
    """Normalise a tree path for the PBR path comparison.

    Upper-case, single segments, ``3F00`` -> ``MF``.  An ADF is rooted at
    itself while a tree request carries the whole chain (``MF/ADF.USIM/5F3A``),
    so a leading ``MF`` before an ADF segment is dropped - otherwise the ADF's
    DF.PHONEBOOK could never match its cache entry.  ``adf_aids`` maps an AID
    to the ADF name (the Profiler's tree walk addresses an ADF by AID, the
    file manager by name; the cache paths are name-based); it is **kept for
    legacy parity** and covered by the drift/unit tests only - the server's
    tree injection normalises the model's name-based path, so no production
    caller passes it.
    """
    out = []
    for segment in segments or ():
        text = str(segment if segment is not None else '').strip().upper()
        if not text:
            continue
        if adf_aids and text in adf_aids:
            text = adf_aids[text]
        out.append('MF' if text in ('MF', '3F00') else text)
    if len(out) > 1 and out[0] == 'MF' and out[1].startswith('ADF.'):
        out.pop(0)
    return out


def files_for_node(pbrs, path):
    """The parsed PBR files when ``path`` is the DF holding a cached EF.PBR.

    A full-path match wins; a bare DF request may fall back to the single
    cached entry with that DF FID.  A full path that failed the exact match is
    **never** answered by another DF's copy (the three PBR copies all sit in a
    DF with FID ``5F3A``), and a bare FID stays silent when several copies
    match.  ``path`` is a normalised segment list (:func:`norm_path`).
    """
    if not pbrs or not path:
        return None
    wanted = '/'.join(path)
    df_fid = path[-1]
    exact = []
    by_fid = []
    for entry in pbrs:
        if not isinstance(entry, dict):
            continue
        entry_path = norm_path((entry.get('path') or '').split('/'))
        if len(entry_path) < 2:
            continue
        if '/'.join(entry_path[:-1]) == wanted:
            exact.append(entry)
        elif entry_path[-2] == df_fid:
            by_fid.append(entry)
    if exact:
        return parse_pbr(exact[0].get('records'))
    if len(path) == 1 and len(by_fid) == 1:
        return parse_pbr(by_fid[0].get('records'))
    return None


def inject_children(pbrs, path, children):
    """Append the PBR-referenced files to a tree ``children`` list.

    ``children`` entries are ``{'name','fid','isDir',...}`` dicts; a FID the
    list already carries (the standard model lists EF.PBR itself) is not added
    twice.  Returns the number of appended children.
    """
    pbr_files = files_for_node(pbrs, path)
    if not pbr_files:
        return 0
    known = {str(child.get('fid') or '').upper()
             for child in children if child.get('fid')}
    added = 0
    for entry in pbr_files:
        if entry['fid'] in known:
            continue
        known.add(entry['fid'])
        children.append({'name': entry['kind'], 'fid': entry['fid'],
                         'isDir': False, 'pbr': True, 'sfi': entry['sfi']})
        added += 1
    return added
