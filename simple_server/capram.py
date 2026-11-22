# coding=utf-8
"""CAP analysis and the GP RAM (install) APDU builders - the pysim-free port.

This module owns the *pure* half of the RAM support: the structural ``.cap``
parse, the INSTALL/LOAD/DELETE APDU builders, the install-parameter
composition, the capmem analysis and the step-record assembly.  The card
exchange (one secured packet per GP APDU, the format probe, the NV reads) lives
on :class:`simple_server.server.ServerState`, exactly like the plain SCP80
send's :meth:`~simple_server.server.ServerState._send_secured_packet`.

Ported legacy helpers, name for name (``pysim_simple_server.server``)::

    _cap_parse               -> cap_parse
    _cap_info_body           -> cap_info_body
    _lv                      -> lv
    _ber_len                 -> ber_len
    _single_ef_value         -> single_ef_value
    _quota_tlv               -> quota_tlv
    _quota_provided          -> quota_provided
    _compose_install_params  -> compose_install_params
    _cap_install_apdu        -> cap_install_apdu
    _cap_make_selectable_apdu-> cap_make_selectable_apdu
    _ram_delete_apdu         -> ram_delete_apdu
    _cap_apdu_sequence       -> cap_apdu_sequence
    _ram_remote_sw_ok        -> remote_sw_ok
    _por_remote_sw           -> por_remote_sw
    _ram_step_result         -> step_result
    _ram_free_nv             -> free_nv
    _scp81_decode_memory     -> decode_memory
    _ram_nv_fields           -> nv_fields
    _ram_install_plan        -> install_plan
    _ram_reinstall_plan      -> reinstall_plan

Not ported here (the locked API surface): the CAP compatibility test
(``_cap_compat_blocks``/``_probe_import_versions``), the batch plans
(``_ram_delete_plan`` and the batch worker) and the TAR probe.

Spec anchors (GP Card Specification v2.3.1, checked in the pinned PDF):
§11.5.2.1/Table 11-41 (the INSTALL P1 roles: b2 load = 0x02, b3 install =
0x04, b4 make selectable = 0x08, their combinations), §11.5.2.3.1/Table 11-42
(the INSTALL [for load] data field: LV AID / conditional LV Security Domain
AID / LV hash / LV load parameters / LV token), §11.5.2.3.2/Table 11-43
(INSTALL [for install]: LV load file AID / LV module AID / LV application AID
/ LV privileges - length ``01`` or ``03``, value 1 or 3 octets - / LV install
parameters / LV token), §11.5.2.3.3/Table 11-44 (INSTALL [for make
selectable]), §11.5.2.3.7/Tables 11-48/11-49 (the load/install parameter tags:
``C9`` mandatory; ``EF`` System Specific Parameters with ``C7``/``C8`` as
2-or-4-byte quotas *nested inside it*, ``CA`` inside the EF and ``EA`` a
sibling), §9.7 (the quota coding: a 2-byte integer up to 32767, a 4-byte one
above), §11.6.2/Tables 11-56/11-57 (LOAD: P1.b8 = last block, P2 = the
sequential block number) and Table 11-58 (the load file = a ``C4`` Load File
Data Block), §11.2/Tables 11-20-11-23 (DELETE: P1 00, P2.b8 = related objects,
the ``4F`` AID TLV), §11.4/Tables 11-32-11-35 (GET STATUS and the ``4F 00``
"all occurrences" search criterion).  **The CAP component load order is not a
GP rule:** it is the JC VM file structure (JC VM Specification v2.2.2 §6.2,
Table 6-3 "Reference Component Install Order"), verified here against the TCA
reference trace
(``samples/tca_ram_traces/test_mycard_keyset2_applet_full_load.log``) - the
same order our builders reproduce byte for byte, and the trace's
INSTALL/LOAD/INSTALL APDUs are pinned by the tests.
TS 102 226 V18.5.0 §8.2.1.3.2.1 (the ``CA`` is "included in the System Specific
Parameters (Tag 'EF')"; §8.2.1.3.2.2 makes ``EA`` a separate object - a card
must answer 6A80 when both are present) and §8.2.1.7.2 (GET DATA ``FF21``:
``81`` applet count / ``82`` free NV / ``83`` free volatile).  JC VM spec
§4.5.2/6.6/6.7 (the import list and the platform requirement), Table 6-4 (the
header package flags).

The APDUs are case 3 (no trailing ``Le``) although the GP command tables list
``Le '00'``: on the card's SCP80 command-scripting layer a trailing ``Le``
counts as a phantom second command whose SW masks the real result (the
legacy's live finding, kept - a deliberate deviation).
"""

import io
import re
import struct
import zipfile

from pysim_simple_server import capmem

from simple_server import ota
from simple_server import tlv

__all__ = [
    'CAP_LOAD_ORDER', 'DELETE_NOT_FOUND', 'REMOTE_SW_OK',
    'RAM_PROBE_APDU', 'RAM_PROBE_APDU_LE', 'lv', 'ber_len', 'single_ef_value',
    'quota_tlv', 'quota_provided', 'compose_install_params', 'cap_parse',
    'cap_info_body', 'cap_install_apdu', 'cap_make_selectable_apdu',
    'ram_delete_apdu', 'cap_apdu_sequence', 'remote_sw_ok', 'por_remote_sw',
    'step_result', 'decode_memory', 'free_nv', 'nv_fields', 'install_plan',
    'reinstall_plan',
]

#: The CAP components in load-file order (JC VM Specification v2.2.2 §6.2,
#: Table 6-3 "Reference Component Install Order" - **not** a GP rule; the same
#: order the TCA reference trace shows, and the load file is the concatenation
#: of these components).  A missing component is simply skipped.  A reorder
#: corrupts every load file: do not "fix" it.
CAP_LOAD_ORDER = ('Header', 'Directory', 'Import', 'Applet', 'Class', 'Method',
                  'StaticField', 'Export', 'ConstantPool', 'RefLocation',
                  'Descriptor')

#: Remote-command status words that count as success in the RAM dialog:
#: ``9000`` normal, and ``CAFE`` - observed card behaviour (the pinned GP
#: specification does not define it; the live card uses it to mark the end of
#: a GET STATUS page).  ``61xx``/``62xx``/``63xx`` are the general
#: "more data"/warning forms.
REMOTE_SW_OK = ('9000', 'CAFE')

#: A GP DELETE whose object is already gone: the delete's goal is met, so a
#: delete step tolerates these and the chain proceeds (``6A88`` referenced data
#: not found, ``6A82`` file/application not found).
DELETE_NOT_FOUND = ('6A88', '6A82')

#: The RAM command-format probes (TS 102 226 §5.2.1): a read-only GET STATUS
#: [ISD] sent bare (compact, with the chained GET RESPONSE the compact form
#: needs) and without ``Le`` (the expanded forms do not use GET RESPONSE).
RAM_PROBE_APDU = '80F28000024F0000C0000000'
RAM_PROBE_APDU_LE = '80F28000024F0000'


# ---- the length helpers ----------------------------------------------------

def lv(hex_str):
    """Length-prefix a hex string with a one-octet length ("LV" coding)."""
    return '%02x%s' % (len(hex_str) // 2, hex_str)


def ber_len(n):
    """A BER-TLV length as a hex string (short form, else ``81 nn``...).

    Lower case like the legacy helper - the APDU strings these builders return
    are compared byte for byte against the legacy builders by the drift test,
    so the case is part of the contract.  The long form is at most three
    length octets (``82 nnnn`` / ``83 nnnnnn``); a larger length would be an
    invalid field, so :func:`simple_server.tlv.encode_ber_len` refuses it (an
    APDU cannot carry a >0xFFFFFF-octet TLV anyway).
    """
    return tlv.encode_ber_len(n).hex()


# ---- the install-parameter composition (v3.20.0/the one-EF rule) -----------

def single_ef_value(hex_str):
    """The value of a lone System Specific Parameters EF TLV, or None.

    ``None`` when the input is not EF-form at all; raises :class:`ValueError`
    for a malformed EF, or for a bare CA - the CA (SIM file access and toolkit
    parameters) belongs *inside* the EF (TS 102 226 §8.2.1.3.2.1), so appending
    it would add a second, unrelated object.
    """
    if not hex_str:
        return None
    try:
        data = bytes.fromhex(hex_str)
    except ValueError:
        raise ValueError('stk_params is not valid hex') from None
    if not data:
        return None
    if data[0] == 0xCA:
        raise ValueError('the CA (SIM file access and toolkit parameters) '
                         'belongs inside the System Specific Parameters EF '
                         '(TS 102 226 8.2.1.3.2.1): send EF <len> CA ...')
    if data[0] != 0xEF:
        return None
    if len(data) < 2:
        raise ValueError('stk_params: malformed EF TLV (the length byte is '
                         'missing)')
    first = data[1]
    if first == 0x80:
        raise ValueError('stk_params: malformed EF TLV (indefinite length)')
    if first >= 0x80 and len(data) < 2 + (first & 0x7F):
        raise ValueError('stk_params: malformed EF TLV (truncated length)')
    ln, off = tlv.parse_ber_len(data, 1)
    if off + ln != len(data):
        raise ValueError('stk_params: malformed EF TLV (the length does not '
                         'match the value)')
    return data[off:off + ln].hex().upper()


def quota_tlv(tag, value):
    """A C7/C8 memory-quota TLV (GP Card Spec v2.3.1 §11.5.2.3.7, Table 11-49,
    and the coding of §9.7).

    Table 11-49 nests the quotas inside the System Specific Parameters
    ``EF``; this helper only codes the TLV itself.  The value is a 2-octet
    integer up to 32767 and a 4-octet integer above.  An integral float is
    accepted (a JSON client may send ``200.0``).
    """
    try:
        num = float(str(value).strip())
        if not num.is_integer():
            raise ValueError
        amount = int(num)
    except (TypeError, ValueError):
        raise ValueError('%s quota must be an integer in bytes' % tag) from None
    if not 0 <= amount <= 0xFFFFFFFF:
        raise ValueError('%s quota out of range (0..4294967295): %d'
                         % (tag, amount))
    return tag + ('%02X%04X' % (2, amount) if amount <= 0x7FFF
                  else '%02X%08X' % (4, amount))


def quota_provided(value):
    """Whether a memory-quota field was given.

    ``None`` and blank strings mean "not requested", while ``0`` is a valid
    quota (the vendor reference form carried ``C7 02 0000 C8 02 0000``), so an
    integral zero must be composed, not dropped.
    """
    return value is not None and str(value).strip() != ''


def _tolerant_tlvs(data):
    """The legacy top-level tag/value walk (a malformed length yields ``b''``).

    The legacy ``_ber_tlv_list`` appended a zero-length TLV for a tag whose
    length byte is an indefinite ``80`` (or a truncated long form), so the
    one-EF guard saw an ``EF`` there.  Keep that walk for the guard's verdict
    (the refusal message stays the legacy's) and let
    :func:`_tlvs_complete` decide whether the field is well formed at all.
    """
    out = []
    off = 0
    while off + 1 < len(data):
        tag = data[off]
        tlen, voff = tlv.parse_ber_len(data, off + 1)
        if voff + tlen > len(data):
            break
        out.append((tag, data[voff:voff + tlen]))
        if voff + tlen <= off:      # no forward progress: stop
            break
        off = voff + tlen
    return out


def _tlvs_complete(data):
    """Whether a strict one-tag-octet TLV walk consumes every octet."""
    off = 0
    while off < len(data):
        try:
            _tag, _length, _value, rest = tlv.parse_one(
                data, off, multi_byte_tags=False)
        except ValueError:
            return False
        nxt = len(data) - len(rest)
        if nxt <= off:              # no forward progress: stop
            return False
        off = nxt
    return True


def _privileges_lv(privileges):
    """The GP privileges field as an LV (GP Table 11-43/11-44: 1 or 3 octets).

    A 2-octet value is an invalid length the card rejects, so a bad request is
    refused at plan time (a 400) instead of becoming a card surprise.  The
    empty value is the default ``'00'`` (no privilege).
    """
    value = str(privileges or '00').replace(' ', '')
    if len(value) not in (2, 6):
        raise ValueError('privileges must be 1 or 3 bytes (GP Card Spec '
                         'v2.3.1 Table 11-43/11-44)')
    try:
        bytes.fromhex(value)
    except ValueError:
        raise ValueError('privileges is not valid hex') from None
    return lv(value)


def compose_install_params(install_params, stk_params, nv_quota=None,
                           volatile_quota=None):
    """The INSTALL [for install] parameter field, with the spec rules enforced.

    - the memory quotas (C7 volatile / C8 non-volatile) and an EF-form STK part
      (the CA) share **one** System Specific Parameters EF: the CA is
      "included in the System Specific Parameters (Tag 'EF')" (TS 102 226
      §8.2.1.3.2.1) and GPD_SPE_013 v1.1 Table 6-5 composes the quotas and the
      CA together (pySim's ``gen_install_parameters``); GP Table 11-49 nests
      C7/C8/CA in the same ``EF`` and supplies their coding (§9.7);
    - an EF-form STK part next to a caller-composed EF is refused: the card
      takes the first EF (the quotas) and the CA is ignored - live 2026-10-05:
      por_ok but no toolkit registration, no menu;
    - a malformed caller ``install_params`` (a TLV walk that does not consume
      the whole field) is refused - the tolerant walk would swallow the next
      object (a truncated length once hid a second ``EF``);
    - an EA-form (or opaque) STK part is a sibling of EF (the reference TCA
      loader sends ``C9 00 EF 00 EA ...``);
    - the quotas are composed from the primitives only when no
      ``install_params`` were given (there is no EF to place them into
      otherwise).

    Raises :class:`ValueError` (the endpoint's 400) on a refusal.
    """
    raw = (install_params or '').replace(' ', '')
    stk = (stk_params or '').replace(' ', '')
    has_quota = quota_provided(nv_quota) or quota_provided(volatile_quota)
    stk_ef = single_ef_value(stk)
    if raw:
        if has_quota:
            raise ValueError('memory quotas are composed only when '
                             'install_params is empty; merge them into the '
                             'install EF instead')
        try:
            raw_bytes = bytes.fromhex(raw)
        except ValueError:
            raise ValueError('install_params is not valid hex') from None
        if stk_ef is not None and any(tag == 0xEF
                                      for tag, _value in _tolerant_tlvs(
                                          raw_bytes)):
            raise ValueError('the EF-form STK part would add a second System '
                             'Specific Parameters (EF) object (TS 102 226 '
                             '8.2.1.3.2.1): merge the CA into the install '
                             "parameters' EF, or pass the quotas via "
                             'volatile_quota/nv_quota with an empty '
                             'install_params')
        if not _tlvs_complete(raw_bytes):
            raise ValueError('install_params is malformed: the TLV walk does '
                             'not consume the whole field')
        return (raw + stk).upper()
    # Compose from the primitives: C9 is mandatory (GP Table 11-49).
    inner = ''
    if quota_provided(volatile_quota):
        inner += quota_tlv('C7', volatile_quota)
    if quota_provided(nv_quota):
        inner += quota_tlv('C8', nv_quota)
    if stk_ef is not None:
        inner += stk_ef
        stk = ''
    params = 'C900'
    if inner:
        params += 'EF' + ber_len(len(inner) // 2) + inner
    return (params + stk).upper()


# ---- the GP APDU builders --------------------------------------------------

def cap_install_apdu(loadfile_aid, module_aid, instance_aid='', privileges='00',
                     install_params='', stk_params='', make_selectable=True,
                     nv_quota=None, volatile_quota=None):
    """INSTALL [for install] APDU (GP Card Spec v2.3.1 §11.5.2.3.2, Table 11-43).

    The final step of :func:`cap_apdu_sequence` and the ``/api/ram-install-app``
    install operation.  Case 3: no trailing ``Le`` (a trailing octet would be a
    phantom command on the card's SCP80 layer).
    """
    instance = instance_aid or module_aid
    params = compose_install_params(install_params, stk_params,
                                    nv_quota=nv_quota,
                                    volatile_quota=volatile_quota)
    p1 = 0x0C if make_selectable else 0x04
    data = (lv(loadfile_aid) + lv(module_aid) + lv(instance)
            + _privileges_lv(privileges) + lv(params) + '00')
    return '80E6%02X00%02X%s' % (p1, len(data) // 2, data)


def cap_make_selectable_apdu(instance_aid, privileges='00'):
    """INSTALL [for make selectable] APDU (§11.5.2.3.3, Table 11-44).

    ``'00' '00' lv(AID) lv(privileges) lv(params) lv(token)`` - the parameters
    and token are empty here.  Case 3 (no ``Le``).
    """
    data = ('00' + '00' + lv(instance_aid) + _privileges_lv(privileges) + '00'
            + '00')
    return '80E60800%02X%s' % (len(data) // 2, data)


def ram_delete_apdu(aid, cascade=False):
    """GP DELETE (§11.2, Tables 11-20/11-23).

    P1=00 (last/only command), P2.b8 selects "object" (``00``) or "object and
    related objects" (``80``), and the data field carries the mandatory ``4F``
    AID TLV.  Case 3 (no trailing ``Le``: on the card's SCP80 layer a trailing
    octet counts as a phantom second command whose SW masks the real result).
    The PWA's ``ramDeleteApdu`` mirrors these bytes.
    """
    aid = re.sub(r'[^0-9a-fA-F]', '', str(aid or '')).upper()
    if not aid:
        return ''
    data = '4F' + ber_len(len(aid) // 2) + aid
    return ('80E400' + ('80' if cascade else '00')
            + ber_len(len(data) // 2) + data).upper()


def cap_apdu_sequence(loadfile_aid, module_aid, loadfile_data, sd_aid='',
                      privileges='00', install_params='', stk_params='',
                      make_selectable=True, block_size=240, instance_aid=None,
                      nv_quota=None, volatile_quota=None):
    """The RAM APDU sequence of a parsed ``.cap``.

    INSTALL [for load] (GP §11.5.2.3.1, Table 11-42), the LOAD blocks
    (§11.6.2, Tables 11-56/11-57: ``block_size``-octet payloads, the block
    counter in P2, the last block P1=0x80) and INSTALL [for install]
    (§11.5.2.3.2, Table 11-43).

    The INSTALL [for load] Security Domain AID is conditional (Table 11-42) and
    only sent when one was supplied (empty -> ``00``, the card defaults to the
    ISD); every command is case 3 (no trailing ``Le``).
    """
    ifl_data = (lv(loadfile_aid) + (lv(sd_aid) if sd_aid else '00')
                + '00' + '00' + '00')
    apdus = ['80E60200%02X%s' % (len(ifl_data) // 2, ifl_data)]
    loadfile_tlv = 'C4' + ber_len(len(loadfile_data) // 2) + loadfile_data
    # Consecutive block_size-octet blocks (char offsets, two per octet).  The
    # earlier form indexed with the block number, which produced overlapping
    # one-byte-shifted copies and failed mid-load with SW 6400 (live
    # 2026-09-16).
    blocks = [loadfile_tlv[off:off + block_size * 2]
              for off in range(0, len(loadfile_tlv), block_size * 2)]
    for index, block in enumerate(blocks):
        p1 = 0x80 if index == len(blocks) - 1 else 0x00
        apdus.append('80E8%02X%02X%02X%s'
                     % (p1, index % 256, len(block) // 2, block))
    apdus.append(cap_install_apdu(
        loadfile_aid, module_aid, instance_aid=instance_aid,
        privileges=privileges, install_params=install_params,
        stk_params=stk_params, make_selectable=make_selectable,
        nv_quota=nv_quota, volatile_quota=volatile_quota))
    return apdus


# ---- the .cap archive -------------------------------------------------------

def _cap_hex(body):
    """The request's CAP hex (``cap_hex``; ``cap`` as the API alias)."""
    body = body or {}
    return re.sub(r'\s', '',
                  str(body.get('cap_hex') or body.get('cap') or ''))


def cap_parse(cap_hex):
    """Parse a ``.cap`` archive (hex) -> ``(loadfile_aid, module_aid, load)``.

    The ``.cap`` is a ZIP archive of component files.  The Header gives the
    package AID (the load file AID), the Applet component the applet/module
    AID, and the load file is every component in :data:`CAP_LOAD_ORDER`.

    Every malformed input is a :class:`ValueError` (the endpoint's 400): a
    non-ZIP, a missing/truncated Header or Applet, a wrong magic.
    """
    loadfile_aid, module_aid, loadfile_data, _components = _cap_archive(cap_hex)
    return loadfile_aid, module_aid, loadfile_data


def _cap_archive(cap_hex):
    """The parse behind :func:`cap_parse`, plus the component dict."""
    try:
        cap_bytes = bytes.fromhex(str(cap_hex or '').strip())
    except ValueError:
        raise ValueError('the .cap is not valid hex') from None
    if not cap_bytes:
        raise ValueError('the .cap is empty')
    try:
        archive = zipfile.ZipFile(io.BytesIO(cap_bytes))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError('the .cap is not a ZIP archive: %s' % exc) from None
    try:
        names = archive.namelist()
        components = {}
        for name in names:
            lowered = name.lower()
            if lowered.endswith('.cap') and not lowered.endswith('.capx'):
                key = name.split('/')[-1][:-len('.cap')]
                components[key] = archive.read(name)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError('the .cap archive is unreadable: %s' % exc) from None
    finally:
        archive.close()

    if 'Header' not in components:
        raise ValueError('.cap file missing Header component')
    if 'Applet' not in components:
        raise ValueError('.cap file missing Applet component')

    # Header: tag(1) size(2) magic(4) minor(1) major(1) flags(1)
    # package(minor(1) major(1) aid(LV))
    header = components['Header']
    try:
        magic = struct.unpack('>I', header[3:7])[0]
        if magic != 0xDECAFFED:
            raise ValueError('Invalid .cap Header magic: 0x%08X (expected '
                             '0xDECAFFED)' % magic)
        aid_len = header[12]
        loadfile_aid = header[13:13 + aid_len].hex().upper()
        if len(header) < 13 + aid_len:
            raise ValueError('.cap file has a truncated Header (the package '
                             'AID is cut short)')
    except struct.error:
        raise ValueError('the .cap Header is truncated') from None
    except IndexError:
        raise ValueError('the .cap Header is truncated') from None
    if not loadfile_aid:
        raise ValueError('.cap file Header carries an empty package AID')

    # Applet: tag(1) size(2) count(1) [aid_len(1) aid(N) install_offset(2)]*
    applet = components['Applet']
    try:
        num_applets = applet[3]
        if num_applets < 1:
            raise ValueError('.cap file has no applets')
        aid_len2 = applet[4]
        module_aid = applet[5:5 + aid_len2].hex().upper()
    except IndexError:
        raise ValueError('the .cap Applet component is truncated') from None
    if len(module_aid) // 2 != aid_len2:
        raise ValueError('the .cap Applet component is truncated')
    if not module_aid:
        raise ValueError('.cap file Applet carries an empty module AID')

    loadfile_parts = [components[name] for name in CAP_LOAD_ORDER
                      if name in components]
    loadfile_data = b''.join(loadfile_parts).hex().upper()
    return loadfile_aid, module_aid, loadfile_data, components


def cap_info_body(body):
    """``POST /api/cap-info``: validate a ``.cap`` and estimate its memory.

    Read-only and card-free.  Runs the same structural parse as the install
    paths (so a corrupt or wrong file fails here first) plus the capmem
    analyzer.  The estimate is informational - the install paths stay
    self-sufficient (v3.6.48).

    ``memory['components']`` is re-ordered into :data:`CAP_LOAD_ORDER` - the
    JC VM Specification v2.2.2 §6.2/Table 6-3 load order (not a GP rule) - and
    lists only those components (a ``Debug.cap``, which the JC VM spec says is
    not for download, is dropped; the legacy reported it after the load file).
    The PWA maps a failed LOAD block to the component it covers by
    accumulating those sizes from the load file's start, so the order is part
    of the contract (the capmem report lists the components in ZIP order,
    which is usually - but not always - the load order).
    """
    cap_hex = _cap_hex(body)
    if not cap_hex:
        raise ValueError('No cap_hex provided')
    loadfile_aid, module_aid, loadfile_data, components = _cap_archive(cap_hex)
    try:
        report, memory = capmem.analyze_bytes(bytes.fromhex(cap_hex))
    except Exception as exc:                    # a malformed component
        raise ValueError('cap analysis failed: %s' % exc) from None
    # The load file (all components) is our proxy for the GP "non-volatile
    # code" requirement, so the JSON can report the C6+C8-style total.
    info = capmem.memory_json(report, memory,
                              load_file_bytes=len(loadfile_data) // 2)
    info['components'] = [{'name': name, 'size': len(components[name])}
                          for name in CAP_LOAD_ORDER if name in components]
    return {'ok': True, 'load_file_aid': loadfile_aid, 'module_aid': module_aid,
            'load_file_bytes': len(loadfile_data) // 2, 'memory': info}


# ---- the step records (the PoR verdict) ------------------------------------

def remote_sw_ok(sw):
    """True for a remote status word the RAM dialog treats as success."""
    value = str(sw or '').upper()
    return bool(value) and (value in REMOTE_SW_OK
                            or value[:2] in ('61', '62', '63'))


def por_remote_sw(por):
    """The last remote-command status word of a decoded PoR, or ``''``.

    A compact response carries ``last_status_word``, the scripting form one
    status word per command.
    """
    decoded = (por or {}).get('decoded') or {}
    sw = str(decoded.get('last_status_word') or '').upper()
    if sw:
        return sw
    for response in reversed((por or {}).get('responses') or []):
        if response.get('status_word'):
            return str(response['status_word']).upper()
    return ''


def step_result(step_name, last_sw, por, por_hex, num_bytes, segments):
    """Assemble a RAM step record and its failure reason: ``(step, error)``.

    A step fails on a non-``por_ok`` PoR, on a remote SW outside
    :data:`REMOTE_SW_OK`, or when a PoR arrived but could not be decoded.  A
    ``9000`` transport SW with no PoR at all is ``no_por``, not a failure.
    """
    step = {'name': step_name, 'sw': last_sw, 'bytes': num_bytes,
            'segments': segments}
    error = None
    if por:
        status = str(por.get('response_status') or '')
        remote_sw = por_remote_sw(por)
        step['por_status'] = status or 'unknown'
        step['por_type'] = por.get('response_type')
        step['por_cntr'] = por.get('cntr')
        step['por_data'] = (por.get('decoded') or {}).get(
            'last_response_data', '')
        step['por_raw'] = por.get('raw')
        if remote_sw:
            step['por_sw'] = remote_sw
        if por.get('bad_format'):
            # the expanded script was rejected as malformed before executing:
            # there is no remote status word
            step['por_bad_format'] = por['bad_format']
            step['por_bad_format_name'] = por.get('bad_format_name')
            error = 'bad format %s (%s)' % (por['bad_format'],
                                            por.get('bad_format_name') or '')
        elif status != 'por_ok':
            error = 'PoR %s' % (status or 'unknown')
        elif remote_sw and not remote_sw_ok(remote_sw):
            error = 'remote SW %s' % remote_sw
    elif last_sw == '9000' and not por_hex:
        step['por_status'] = 'no_por'
    else:
        step['por_status'] = 'unknown'
        if por_hex:
            step['por_raw'] = por_hex
        error = 'PoR undecodable'
    if error:
        step['por_error'] = error
    return step, error


# ---- the NV footprint (GET DATA FF21) -------------------------------------

def decode_memory(rapdu):
    """A GET DATA FF21 value: ``81`` applet count, ``82`` free NV, ``83`` free
    volatile (TS 102 226 §8.2.1.7.2).  ``None`` when the answer is not FF21."""
    rapdu = bytes(rapdu or b'')
    if len(rapdu) < 5 or rapdu[0] != 0xFF or rapdu[1] != 0x21:
        return None
    content = rapdu[3:3 + rapdu[2]]
    out = {}
    off = 0
    while off + 1 < len(content):
        tag, tlen = content[off], content[off + 1]
        value = content[off + 2:off + 2 + tlen]
        off += 2 + tlen
        if tag in (0x81, 0x01):
            out['applets'] = int.from_bytes(value, 'big')
        elif tag in (0x82, 0x02):
            out['free_nv'] = int.from_bytes(value, 'big')
        elif tag in (0x83, 0x03):
            out['free_volatile'] = int.from_bytes(value, 'big')
    return out or None


def free_nv(por_data_hex):
    """The free non-volatile memory (bytes) of a GET DATA FF21 R-APDU, or None."""
    try:
        rapdu = bytes.fromhex(por_data_hex or '')
    except ValueError:
        return None
    memory = decode_memory(rapdu)
    return memory.get('free_nv') if memory else None


def nv_fields(nv_before, nv_after):
    """The NV footprint fields of an install response (``{}`` without FF21)."""
    if nv_before is None and nv_after is None:
        return {}
    delta = (nv_before - nv_after
             if (nv_before is not None and nv_after is not None) else None)
    return {'nv_before': nv_before, 'nv_after': nv_after, 'nv_delta': delta}


# ---- the card-independent plans -------------------------------------------

def install_plan(body):
    """The card-independent part of a CAP install.

    Parse the ``.cap``, build the INSTALL [for load] -> LOAD xN -> INSTALL [for
    install] chain and read the transport knobs.  ``delete_package`` prepends a
    cascade DELETE on the package AID (a re-install over a leftover package;
    the delete step tolerates "not found" - see
    :meth:`~simple_server.server.ServerState._ram_plan_run`).

    Nothing here touches the card, so the same plan can be reused for every
    card of a batch.  Every refusal is a :class:`ValueError` (the endpoint's
    400).
    """
    body = body or {}
    cap_hex = _cap_hex(body)
    if not cap_hex:
        raise ValueError('No cap_hex provided')
    loadfile_aid, module_aid, loadfile_data = cap_parse(cap_hex)

    # LOAD block size: the GP limit is 240 payload octets; an explicit
    # override is capped to it.
    requested = body.get('load_block_size')
    if requested in (None, ''):
        requested = None
    else:
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            requested = -1
        if not 1 <= requested <= 240:
            raise ValueError('load_block_size must be 1..240')
    block_size = requested or 240

    # The install parameters are composed per the spec: an EF-form STK part
    # shares one System Specific Parameters EF with the quotas (TS 102 226
    # §8.2.1.3.2.1) - an invalid combination is refused before anything is
    # sent.
    sequence = cap_apdu_sequence(
        loadfile_aid, module_aid, loadfile_data,
        sd_aid=str(body.get('sd_aid') or '').replace(' ', ''),
        privileges=str(body.get('privileges') or '').replace(' ', '') or '00',
        install_params=str(body.get('install_params') or '').replace(' ', ''),
        stk_params=str(body.get('stk_params') or '').replace(' ', ''),
        make_selectable=body.get('make_selectable', True),
        block_size=block_size,
        nv_quota=body.get('nv_quota'),
        volatile_quota=body.get('volatile_quota'))
    steps = []
    for index, apdu in enumerate(sequence):
        if index == 0:
            name = 'INSTALL [for load]'
        elif index == len(sequence) - 1:
            name = 'INSTALL [for install]'
        else:
            name = 'LOAD (%d/%d)' % (index, len(sequence) - 2)
        steps.append({'name': name, 'apdu': apdu})
    if body.get('delete_package'):
        # A leftover package blocks INSTALL [for load]: the cascade delete
        # clears the ELF, its modules and its applications first.  It is a
        # delete step, so "not found" (a fresh card) lets the chain proceed.
        steps.insert(0, {'name': 'DELETE (package + applications)',
                         'apdu': ram_delete_apdu(loadfile_aid, cascade=True),
                         'delete': True})
    return {'kind': 'install-cap', 'seq': steps,
            'spi2': str(body.get('spi2') or '01'),
            'include_cpi': body.get('includeCpi', True),
            'ram_format': ota.normalise_ram_format(body.get('ram_format')),
            'nv': True, 'load_size': block_size,
            'response': {'load_file_aid': loadfile_aid,
                         'module_aid': module_aid,
                         'load_block_size': block_size,
                         'load_block_size_requested': requested,
                         'load_block_size_auto': not requested}}


def reinstall_plan(body, kind='reinstall-app'):
    """DELETE the applet instance, then INSTALL [for install] (the loaded app).

    No ``.cap`` and no LOAD: the same parameters the single INSTALL [for
    install] uses.  The delete tolerates "not found" - a fresh card simply
    installs.  ``kind`` is ``'install-app'`` for the single operation and
    ``'reinstall-app'`` for the batch (only the progress/log label differs).
    """
    body = body or {}
    loadfile = str(body.get('loadfile_aid') or '').replace(' ', '').upper()
    module = str(body.get('module_aid') or '').replace(' ', '').upper()
    if not loadfile or not module:
        raise ValueError('loadfile_aid and module_aid required')
    instance = (str(body.get('instance_aid') or '').replace(' ', '').upper()
                or module)
    install_apdu = cap_install_apdu(
        loadfile, module, instance_aid=instance,
        privileges=str(body.get('privileges') or '').replace(' ', '') or '00',
        install_params=str(body.get('install_params') or '').replace(' ', ''),
        stk_params=str(body.get('stk_params') or '').replace(' ', ''),
        make_selectable=body.get('make_selectable', True),
        nv_quota=body.get('nv_quota'),
        volatile_quota=body.get('volatile_quota'))
    return {'kind': kind,
            'seq': [{'name': 'DELETE (application)',
                     'apdu': ram_delete_apdu(instance, cascade=False),
                     'delete': True},
                    {'name': 'INSTALL [for install]', 'apdu': install_apdu}],
            'spi2': str(body.get('spi2') or '01'),
            'include_cpi': body.get('includeCpi', True),
            'ram_format': ota.normalise_ram_format(body.get('ram_format')),
            'response': {'load_file_aid': loadfile, 'module_aid': module,
                         'instance_aid': instance}}
