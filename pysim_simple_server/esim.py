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

from osmocom.tlv import flatten_dict_lists
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
    """Select ISD-R on logical channel 0; returns the lchan's scc."""
    rs = getattr(app, 'rs', None)
    apps = getattr(getattr(rs, 'mf', None), 'applications', None) or {}
    isd_r = apps.get(AID_ISD_R.lower())
    if isd_r is None:
        raise EsimError('not_an_euicc')
    lchan = rs.lchan[0]
    lchan.select_file(isd_r)
    return lchan.scc


def _restore(app):
    """Return to MF so later server operations start from a known selection."""
    try:
        app.rs.soft_reset()
    except Exception:
        pass


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


def chip_info(app):
    """EID (ES10b GetEuiccData), EUICCInfo1/2 and the configured addresses."""
    out = {'eid': None, 'info1': None, 'info2': None, 'addresses': None,
           'errors': {}}
    scc = _select_isdr(app)
    try:
        parts = (
            ('eid', lambda: CardApplicationISDR.get_eid(scc)),
            ('info1', lambda: _flatten(CardApplicationISDR.store_data_tlv(
                scc, EuiccInfo1(), EuiccInfo1))),
            ('info2', lambda: _flatten(CardApplicationISDR.store_data_tlv(
                scc, EuiccInfo2(), EuiccInfo2))),
            ('addresses', lambda: _flatten(CardApplicationISDR.store_data_tlv(
                scc, EuiccConfiguredAddresses(), EuiccConfiguredAddresses))),
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
        out.append({
            'iccid': p.get('iccid'),
            'isdp_aid': p.get('isdp_aid'),
            'state': p.get('profile_state'),
            'nickname': p.get('profile_nickname'),
            'provider': p.get('service_provider_name'),
            'name': p.get('profile_name'),
            'class': p.get('profile_class'),
            'icon_type': p.get('icon_type'),
            'owner': owner.get('profile_owner_plmn') if isinstance(owner, dict) else None,
        })
    return {'profiles': out, 'error': None}


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
        op = n.get('profile_mgmt_operation')
        operations = sorted(k for k, v in op.items() if v) if isinstance(op, dict) else []
        out.append({
            'seq_number': n.get('seq_number'),
            'operations': operations,
            'address': n.get('notification_address'),
            'iccid': n.get('iccid'),
        })
    return {'notifications': out, 'error': None}


def set_profile_state(app, action, iccid=None, isdp_aid=None, refresh=True):
    """ES10c Enable/DisableProfile for one profile (by ICCID or ISD-P AID)."""
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
    if action == 'enable':
        cmd = EnableProfileReq(children=[ProfileIdentifier(children=ident), flag])
        resp_cls, key = EnableProfileResp, 'enable_result'
    else:
        cmd = DisableProfileReq(children=[ProfileIdentifier(children=ident), flag])
        resp_cls, key = DisableProfileResp, 'disable_result'
    flat = _flatten(_transceive(app, cmd, resp_cls))
    result = flat.get(key)
    ok = result == 'ok'
    return {'ok': ok, 'result': result if isinstance(result, str) else 'undefinedError',
            'message': _error_text(result)}
