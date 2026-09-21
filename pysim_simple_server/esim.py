# coding=utf-8
"""eSIM / LPA local operations (ES10a/b/c) on the equipped eUICC.

Scope: chip details (EID, EUICCInfo1/2, configured addresses), profile
listing with metadata (GetProfilesInfo), profile switching (Enable/Disable)
and a read-only notification viewer (ListNotification).  No profile
downloads, no notification processing/removal and no SM-DP+ interaction.

Every command goes through pySim's ES10 static API
(``CardApplicationISDR.store_data_tlv`` / ``get_eid``) on the card's own
logical channel; the ISD-R is selected first and the previous selection is
restored afterwards.  The caller holds ``_CARD_LOCK``.
"""

import re
import sys

from osmocom.tlv import BER_TLV_IE, bertlv_parse_one_rawtag, flatten_dict_lists
from pySim.euicc import (
    AID_ISD_R, CardApplicationISDR, DisableProfileReq, DisableProfileResp,
    EnableProfileReq, EnableProfileResp, EuiccConfiguredAddresses, EuiccInfo1,
    EuiccInfo2, Iccid, IsdpAid, ListNotificationReq, ListNotificationResp,
    ProfileIdentifier, ProfileInfo, ProfileInfoListReq, ProfileInfoListResp,
    RefreshFlag, TagList,
)
from pySim.utils import b2h


class EsimError(Exception):
    """eSIM operation failed; ``code`` is a stable identifier for the API."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


# ES10c result codes -> short English fallback (the PWA localizes by code).
RESULT_MESSAGES = {
    'ok': 'ok',
    'iccidOrAidNotFound': 'profile not found',
    'profileNotInDisabledState': 'profile is not disabled',
    'profileNotInEnabledState': 'profile is not enabled',
    'disallowedByPolicy': 'disallowed by policy',
    'wrongProfileReenabling': 'wrong profile re-enabling',
    'catBusy': 'card is busy with a CAT session',
    'undefinedError': 'undefined error',
}


def is_euicc(app):
    """True when the equipped card runs an eUICC profile (SGP.02/22/32)."""
    rs = getattr(app, 'rs', None)
    return 'eUICC' in str(getattr(rs, 'profile', '') or '')


def _select_isdr(app):
    """Select ISD-R on logical channel 0; returns the lchan's scc.

    The shell app is passed as cmd_app so pySim keeps its command-set
    bookkeeping in sync with the selection: ``equip()`` only unregisters the
    sets of the file selected at that moment, so a selection made without
    cmd_app leaves the old file's sets registered and the next equip dies
    re-registering them (cmd2: 'Attribute already exists')."""
    rs = getattr(app, 'rs', None)
    apps = getattr(getattr(rs, 'mf', None), 'applications', None) or {}
    isd_r = apps.get(AID_ISD_R.lower())
    if isd_r is None:
        raise EsimError('not_an_euicc')
    lchan = rs.lchan[0]
    lchan.select_file(isd_r, app)
    return lchan.scc


def _restore(app):
    """Return to MF so later server operations start from a known selection.

    Best effort: a card whose active profile is disabled has no filesystem to
    select, so the restore can legitimately fail - the selection metadata is
    then guarded by the status endpoint instead of crashing it.  The shell app
    is passed as cmd_app so the command-set bookkeeping follows the selection."""
    try:
        app.rs.soft_reset(app)
    except Exception as e:
        sys.stderr.write('ESIM: selection restore failed: %s\n' % e)


def _run(app, fn):
    scc = _select_isdr(app)
    try:
        return fn(scc)
    finally:
        _restore(app)


def _normalize(value):
    """bytes -> hex string, recursively; scalars pass through."""
    if isinstance(value, (bytes, bytearray)):
        return b2h(bytes(value)).upper()
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def _flatten(resp):
    """ES10 response object -> flat dict (single root key unwrapped)."""
    if resp is None:
        return {}
    flat = flatten_dict_lists(resp.to_dict())
    if len(flat) == 1:
        flat = next(iter(flat.values()))
    return _normalize(flat)


def _as_list(value):
    """A repeated TLV child decodes to a dict for one entry, a list for many."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _repeated(container, key):
    """Collect `key` children from a container that may itself be a dict or a
    list of dicts (flatten_dict_lists collapses repeated parents either way)."""
    out = []
    for item in _as_list(container):
        if isinstance(item, dict):
            out.extend(_as_list(item.get(key)))
    return out


def _transceive(app, cmd_do, resp_cls):
    return _run(app, lambda scc: CardApplicationISDR.store_data_tlv(scc, cmd_do, resp_cls))


def _error_text(result):
    if isinstance(result, int):
        return RESULT_MESSAGES.get('undefinedError')
    return RESULT_MESSAGES.get(result, result)


# ---- SGP.22 EUICCInfo decoding -------------------------------------------
#
# pySim's EuiccInfo1/2 classes are incomplete (the capability fields are raw
# GreedyBytes, several SGP.22 TLVs are missing from the class), so the chip
# endpoint requests the raw TLVs and decodes them here, per SGP.22 v2.6
# §5.7.8/§5.7.13 and cross-checked against lpac's euicc/es10c_ex.c.  Unknown
# TLVs are preserved in `raw_tlvs`, never dropped.

UICC_CAPABILITY_BITS = [
    'contactlessSupport', 'usimSupport', 'isimSupport', 'csimSupport',
    'akaMilenage', 'akaCave', 'akaTuak128', 'akaTuak256', 'rfu1', 'rfu2',
    'gbaAuthenUsim', 'gbaAuthenISim', 'mbmsAuthenUsim', 'eapClient',
    'javacard', 'multos', 'multipleUsimSupport', 'multipleIsimSupport',
    'multipleCsimSupport', 'berTlvFileSupport', 'dfLinkSupport', 'catTp',
    'getIdentity', 'profile-a-x25519', 'profile-b-p256', 'suciCalculatorApi',
]
RSP_CAPABILITY_BITS = [
    'additionalProfile', 'crlSupport', 'rpmSupport', 'testProfileSupport',
    'deviceInfoExtensibilitySupport', 'serviceSpecificDataSupport',
]
PPR_ID_BITS = ['pprUpdateControl', 'ppr1', 'ppr2', 'ppr3']
PPR_FLAG_BITS = ['consentRequired']
TRE_PROPERTY_BITS = ['isDiscrete', 'isIntegrated', 'usesRemoteMemory']
EUICC_CATEGORIES = {0: 'other', 1: 'basicEuicc', 2: 'mediumEuicc',
                    3: 'contactlessEuicc'}


class _GetRatRequest(BER_TLV_IE, tag=0xbf43):
    """ES10b GetRat request (no input data, SGP.22 §5.7.13)."""


def _tlvs(data):
    """Walk a BER-TLV buffer -> [(tag, value)]; multi-byte tags kept raw."""
    out = []
    rest = bytes(data or b'')
    while rest:
        tag, _length, value, rest = bertlv_parse_one_rawtag(rest)
        out.append((tag, value))
    return out


def _tlv_value(data, tag):
    """Value bytes of the first `tag` TLV in `data` (b'' when absent)."""
    for t, value in _tlvs(data):
        if t == tag:
            return value
    return b''


def _decode_version(data):
    """VersionType: major/minor/revision bytes -> 'M.m.r'."""
    if len(data) != 3:
        return None
    return '%d.%d.%d' % (data[0], data[1], data[2])


def _decode_bit_string(data, names):
    """ASN.1 BIT STRING content -> list of set bit names.

    The first octet is the number of unused bits in the final octet; bits are
    numbered MSB-first within each octet (SGP.22 v2.6 §5.7.8)."""
    if not data:
        return []
    unused = data[0]
    body = data[1:]
    out = []
    for j, byte in enumerate(body):
        b = byte
        if j == len(body) - 1 and unused:
            b &= ~(0xFF >> (8 - unused)) & 0xFF
        for i in range(8):
            idx = j * 8 + i
            if idx >= len(names):
                break
            if b & 0x80:
                out.append(names[idx])
            b = (b << 1) & 0xFF
    return out


def _decode_ski_list(data):
    """SEQUENCE OF SubjectKeyIdentifier -> hex strings."""
    return [value.hex().upper() for _tag, value in _tlvs(data)]


def _decode_ext_card_resource(data):
    """ETSI TS 102 226 Extended Card Resource Information (inner 81/82/83)."""
    out = {}
    raw = {}
    for tag, value in _tlvs(data):
        if tag == 0x81:
            out['installed_application'] = int.from_bytes(value, 'big')
        elif tag == 0x82:
            out['free_non_volatile_memory'] = int.from_bytes(value, 'big')
        elif tag == 0x83:
            out['free_volatile_memory'] = int.from_bytes(value, 'big')
        else:
            raw['%02X' % tag] = value.hex().upper()
    if raw:
        out['raw_tlvs'] = raw
    return out


def _decode_certification_data_object(data):
    """CertificationDataObject (SGP.22 v2.6 §5.7.8): platform label + DLOA URL."""
    out = {}
    raw = {}
    for tag, value in _tlvs(data):
        if tag == 0x80:
            out['platform_label'] = value.decode('utf-8', 'replace')
        elif tag == 0x81:
            out['discovery_base_url'] = value.decode('utf-8', 'replace')
        else:
            raw['%02X' % tag] = value.hex().upper()
    if raw:
        out['raw_tlvs'] = raw
    return out


def _decode_info1(raw_hex):
    """EUICCInfo1 (BF20): SVN and the CI PKI lists."""
    out = {'svn': None, 'euicc_ci_pki_list_for_verification': [],
           'euicc_ci_pki_list_for_signing': []}
    raw = {}
    for tag, value in _tlvs(_tlv_value(bytes.fromhex(raw_hex or ''), 0xBF20)):
        if tag == 0x82:
            out['svn'] = _decode_version(value)
        elif tag == 0xA9:
            out['euicc_ci_pki_list_for_verification'] = _decode_ski_list(value)
        elif tag == 0xAA:
            out['euicc_ci_pki_list_for_signing'] = _decode_ski_list(value)
        else:
            raw['%02X' % tag] = value.hex().upper()
    if raw:
        out['raw_tlvs'] = raw
    return out


def _decode_info2(raw_hex):
    """EUICCInfo2 (BF22) with every SGP.22 v2.6 field decoded."""
    out = {}
    raw = {}
    for tag, value in _tlvs(_tlv_value(bytes.fromhex(raw_hex or ''), 0xBF22)):
        if tag == 0x81:
            out['profile_version'] = _decode_version(value)
        elif tag == 0x82:
            out['svn'] = _decode_version(value)
        elif tag == 0x83:
            out['euicc_firmware_ver'] = _decode_version(value)
        elif tag == 0x84:
            out['ext_card_resource'] = _decode_ext_card_resource(value)
        elif tag == 0x85:
            out['uicc_capability'] = _decode_bit_string(value, UICC_CAPABILITY_BITS)
        elif tag == 0x86:
            out['ts102241_version'] = _decode_version(value)
        elif tag == 0x87:
            out['globalplatform_version'] = _decode_version(value)
        elif tag == 0x88:
            out['rsp_capability'] = _decode_bit_string(value, RSP_CAPABILITY_BITS)
        elif tag == 0xA9:
            out['euicc_ci_pki_list_for_verification'] = _decode_ski_list(value)
        elif tag == 0xAA:
            out['euicc_ci_pki_list_for_signing'] = _decode_ski_list(value)
        elif tag in (0x8B, 0xAB):   # implicit and explicit category encodings
            out['euicc_category'] = EUICC_CATEGORIES.get(
                int.from_bytes(value, 'big') if value else 0, 'other')
        elif tag == 0x99:
            out['forbidden_profile_policy_rules'] = _decode_bit_string(value, PPR_ID_BITS)
        elif tag == 0x04:           # ppVersion has no context tag
            out['pp_version'] = _decode_version(value)
        elif tag == 0x0C:           # sasAcreditationNumber is a bare UTF8String
            out['ss_acreditation_number'] = value.decode('utf-8', 'replace')
        elif tag == 0xAC:
            out['certification_data_object'] = _decode_certification_data_object(value)
        elif tag == 0xAD:
            out['tre_properties'] = _decode_bit_string(value, TRE_PROPERTY_BITS)
        elif tag == 0xAE:
            out['tre_product_reference'] = value.decode('utf-8', 'replace')
        elif tag == 0xAF:
            out['additional_euicc_profile_package_versions'] = [
                _decode_version(v) for _t, v in _tlvs(value)]
        else:
            raw['%02X' % tag] = value.hex().upper()
    if raw:
        out['raw_tlvs'] = raw
    return out


def _decode_addresses(raw_hex):
    """ES10a GetEuiccConfiguredAddresses (BF3C)."""
    out = {'default_dp_address': None, 'root_ds_address': None}
    raw = {}
    for tag, value in _tlvs(_tlv_value(bytes.fromhex(raw_hex or ''), 0xBF3C)):
        if tag == 0x80:
            out['default_dp_address'] = value.decode('utf-8', 'replace')
        elif tag == 0x81:
            out['root_ds_address'] = value.decode('utf-8', 'replace')
        else:
            raw['%02X' % tag] = value.hex().upper()
    if raw:
        out['raw_tlvs'] = raw
    return out


def _decode_rat(raw_hex):
    """ES10b GetRat (BF43): the Rules Authorisation Table (SGP.22 §5.7.13)."""
    out = []
    table = _tlv_value(_tlv_value(bytes.fromhex(raw_hex or ''), 0xBF43), 0xA0)
    for _tag, rule in _tlvs(table):
        entry = {'ppr_ids': [], 'allowed_operators': [], 'ppr_flags': []}
        for tag, value in _tlvs(rule):
            if tag == 0x80:
                entry['ppr_ids'] = _decode_bit_string(value, PPR_ID_BITS)
            elif tag == 0xA1:
                operators = []
                for _t, op in _tlvs(value):
                    ident = {'plmn': None, 'gid1': None, 'gid2': None}
                    for t2, v2 in _tlvs(op):
                        if t2 == 0x80:
                            ident['plmn'] = v2.hex().upper()
                        elif t2 == 0x81:
                            ident['gid1'] = v2.hex().upper()
                        elif t2 == 0x82:
                            ident['gid2'] = v2.hex().upper()
                    operators.append(ident)
                entry['allowed_operators'] = operators
            elif tag == 0x82:
                entry['ppr_flags'] = _decode_bit_string(value, PPR_FLAG_BITS)
        out.append(entry)
    return out


def _raw_request(scc, cmd_cls):
    """Raw response hex of a request TLV (no pySim response decoding)."""
    return CardApplicationISDR.store_data_tlv(scc, cmd_cls(), None)


def chip_info(app):
    """EID (ES10c GetEuiccData), EUICCInfo1/2, configured addresses and RAT."""
    out = {'eid': None, 'info1': None, 'info2': None, 'addresses': None,
           'rat': None, 'errors': {}}
    scc = _select_isdr(app)
    try:
        parts = (
            ('eid', lambda: CardApplicationISDR.get_eid(scc)),
            ('info1', lambda: _decode_info1(_raw_request(scc, EuiccInfo1))),
            ('info2', lambda: _decode_info2(_raw_request(scc, EuiccInfo2))),
            ('addresses', lambda: _decode_addresses(
                _raw_request(scc, EuiccConfiguredAddresses))),
            ('rat', lambda: _decode_rat(_raw_request(scc, _GetRatRequest))),
        )
        for key, fn in parts:
            try:
                out[key] = fn()
            except Exception as e:   # one unsupported part must not fail the rest
                out['errors'][key] = str(e)
    finally:
        _restore(app)
    return out


def _profile_tag_list():
    """TagList requesting every ProfileInfo tag (pySim-shell's --all set)."""
    tags = [nest.tag for nest in ProfileInfo.nested_collection_cls().nested]
    u8 = []
    for tag in tags:
        if tag <= 255:
            u8.append(tag)
        elif tag <= 65535:
            u8.append(tag >> 8)
            u8.append(tag & 0xff)
    return TagList(decoded=u8)


def profiles(app):
    """ES10c GetProfilesInfo: the profile list with its metadata."""
    resp = _transceive(app, ProfileInfoListReq(children=[_profile_tag_list()]),
                       ProfileInfoListResp)
    flat = _flatten(resp)
    err = flat.get('profile_info_list_error')
    if err is not None:
        return {'profiles': [], 'error': _error_text(err)}
    seq = flat.get('profile_info_seq')
    out = []
    for p in _repeated(seq, 'profile_info'):
        owner = p.get('profile_owner')
        icon = p.get('icon')
        out.append({
            'iccid': p.get('iccid'),
            'isdp_aid': p.get('isdp_aid'),
            'state': p.get('profile_state'),
            'nickname': p.get('profile_nickname'),
            'provider': p.get('service_provider_name'),
            'name': p.get('profile_name'),
            'class': p.get('profile_class'),
            'icon_type': p.get('icon_type'),
            'icon': icon,
            'icon_size': len(icon) // 2 if icon else None,
            'owner': owner.get('profile_owner_plmn') if isinstance(owner, dict) else None,
        })
    return {'profiles': out, 'error': None}


# ProfileMgmtOperation flags in bit order (SGP.22 §5.7.9).  The TLV carries a
# padding-bits octet followed by the flags octet, so a TLV parsed from the
# card nests the flags under 'pmo' while an object built from decoded flags
# carries them directly.
PROFILE_MGMT_OPERATIONS = ('install', 'enable', 'disable', 'delete')


def _profile_operations(op):
    """ProfileMgmtOperation flags -> operation names ([] when unknown)."""
    if not isinstance(op, dict):
        return []
    flags = op.get('pmo') if isinstance(op.get('pmo'), dict) else op
    return [name for name in PROFILE_MGMT_OPERATIONS if flags.get(name)]


def notifications(app):
    """ES10b ListNotification: read-only list of pending notifications."""
    resp = _transceive(app, ListNotificationReq(), ListNotificationResp)
    flat = _flatten(resp)
    err = flat.get('list_notifications_result_error')
    if err is not None:
        return {'notifications': [], 'error': _error_text(err)}
    lst = flat.get('notification_metadata_list')
    out = []
    for n in _repeated(lst, 'notification_metadata'):
        out.append({
            'seq_number': n.get('seq_number'),
            'operations': _profile_operations(n.get('profile_mgmt_operation')),
            'address': n.get('notification_address'),
            'iccid': n.get('iccid'),
        })
    return {'notifications': out, 'error': None}


def build_switch_apdu(action, iccid=None, isdp_aid=None, refresh=True):
    """STORE DATA APDU hex for ES10c Enable/DisableProfile (no sending)."""
    if action not in ('enable', 'disable'):
        raise EsimError('bad_action')
    ident = []
    if isdp_aid:
        aid = re.sub(r'[^0-9a-fA-F]', '', str(isdp_aid))
        if not aid:
            raise EsimError('bad_aid')
        ident.append(IsdpAid(decoded=bytes.fromhex(aid)))
    elif iccid:
        digits = re.sub(r'[^0-9a-fA-F]', '', str(iccid))
        if digits[-1:] in ('f', 'F'):
            digits = digits[:-1]
        if not digits.isdigit():
            raise EsimError('bad_iccid')
        ident.append(Iccid(decoded=digits))
    else:
        raise EsimError('missing_profile')
    flag = RefreshFlag(decoded=1 if refresh else 0)
    req_cls = EnableProfileReq if action == 'enable' else DisableProfileReq
    tx_do = req_cls(children=[ProfileIdentifier(children=ident), flag]).to_tlv()
    return '80E29100%02x%s00' % (len(tx_do), tx_do.hex().upper())


def parse_switch_response(action, data_hex):
    """STORE DATA response TLV -> {'ok', 'result', 'message'}."""
    resp_cls = EnableProfileResp if action == 'enable' else DisableProfileResp
    key = 'enable_result' if action == 'enable' else 'disable_result'
    result = None
    if data_hex:
        resp = resp_cls()
        resp.from_tlv(bytes.fromhex(data_hex))
        result = _flatten(resp).get(key)
    return {'ok': result == 'ok',
            'result': result if isinstance(result, str) else 'undefinedError',
            'message': _error_text(result)}


def switch_profile(app, send_apdu, run_chain, action, iccid=None,
                   isdp_aid=None, refresh=True):
    """Run an ES10c Enable/DisableProfile switch on the ISD-R.

    ``send_apdu(apdu_hex) -> (data_hex, sw)`` performs one raw STORE DATA and
    ``run_chain(sw91)`` answers the proactive command(s) the card sends
    alongside the switch (True when a REFRESH was answered).  The ISD-R is
    selected first and the previous selection restored afterwards, like the
    other ES10 functions.

    With the refresh flag set the ISD-R returns OK *before* the REFRESH
    (SGP.22 v2.6 §5.7.16/§5.7.17 step 6) and the switch completes upon the
    TERMINAL RESPONSE or the following RESET (step 8).  A 91XX status is that
    OK: the STORE DATA is never retried (the mid-switch card answers 6985 to
    the retry) and the caller re-initializes the card afterwards."""
    apdu = build_switch_apdu(action, iccid, isdp_aid, refresh)
    _select_isdr(app)
    try:
        data, sw = send_apdu(apdu)
        if sw == '9000':
            out = parse_switch_response(action, data)
            out['refresh_seen'] = False
            return out
        if sw and sw.startswith('91'):
            refresh_seen = False
            try:
                refresh_seen = bool(run_chain(sw))
            except Exception as e:
                sys.stderr.write('ESIM: REFRESH chain failed: %s\n' % e)
            return {'ok': True, 'result': 'ok', 'message': 'ok',
                    'refresh_seen': refresh_seen}
        return {'ok': False, 'result': 'undefinedError', 'sw': sw,
                'message': 'SW %s' % sw}
    finally:
        _restore(app)
