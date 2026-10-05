"""Event-data builders for the test scripts' semantic ``event`` action.

The PWA's Phone tab builds the same objects client-side (``EVENT_FORMS``);
this module is the server-side mirror for the events a test script can send
semantically.  Every other event goes through the raw ``envelope`` action
(the Phone form shows its built hex, which can be pasted into ``data``).

Field sets and defaults follow the PWA's forms and the pinned specs
(TS 31.111 7.5.x, TS 102 223 8.x); the byte vectors are pinned in both the
Node and the Python suites so the two implementations cannot drift apart.

Supported events:

* ``0x03`` Location status - status + optional Location information (8.19,
  normal service only).
* ``0x0B`` Access technology change - the 8.61 single-access-technology byte.
* ``0x12`` Network rejection - registration type, access technology, cause,
  the location area (LAI/RAI/TAI by registration group), the optional
  extended EMM cause and the extended information object.
* ``0x1D`` Data connection status change - status/type/transaction id plus
  the optional cause, host-clock date-time (8.39), location information,
  access technology, location status, APN and PDP/PDN/PDU type.
"""

import re
import time

EVENT_LOCATION_STATUS = 0x03
EVENT_ACCESS_TECH = 0x0B
EVENT_NETWORK_REJECTION = 0x12
EVENT_DATA_CONNECTION = 0x1D

# The single-ENVELOPE budget of the event data (TS 102 223: the inner data is
# capped at 252 bytes, 8 of them the event-list + device-identities header;
# chained delivery is not implemented).  server.py's _EVENT_DATA_MAX and the
# PWA's EVENT_INNER_MAX mirror this value.
EVENT_DATA_MAX = 244

# The event code from a script's `event` value (a hex byte, or one of these
# names - the PWA's display names and short keywords).
EVENT_NAMES = {
    'location status': EVENT_LOCATION_STATUS,
    'location_status': EVENT_LOCATION_STATUS,
    'location-status': EVENT_LOCATION_STATUS,
    'access technology change': EVENT_ACCESS_TECH,
    'access_tech': EVENT_ACCESS_TECH,
    'access-technology-change': EVENT_ACCESS_TECH,
    'network rejection': EVENT_NETWORK_REJECTION,
    'network_rejection': EVENT_NETWORK_REJECTION,
    'network-rejection': EVENT_NETWORK_REJECTION,
    'data connection status change': EVENT_DATA_CONNECTION,
    'data_connection': EVENT_DATA_CONNECTION,
    'data-connection-status-change': EVENT_DATA_CONNECTION,
}


class EventError(ValueError):
    """Invalid event parameters - a user-facing message."""


def resolve_event(value):
    """The event code of a script's `event` value (a hex byte or a name), or
    None when it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 0xFF else None
    s = str(value or '').strip()
    if not s:
        return None
    if re.fullmatch(r'(0x)?[0-9A-Fa-f]{1,2}', s):
        return int(s, 16)
    return EVENT_NAMES.get(s.lower())


def event_name(code):
    """A display name for an event code (the short keyword)."""
    for name, c in (('location_status', EVENT_LOCATION_STATUS),
                    ('access_tech', EVENT_ACCESS_TECH),
                    ('network_rejection', EVENT_NETWORK_REJECTION),
                    ('data_connection', EVENT_DATA_CONNECTION)):
        if c == code:
            return name
    return '0x%02X' % code


# ─── field helpers ──────────────────────────────────────────────────────

_DIGITS_RE = re.compile(r'^\d+$')


def _int(fields, key, default, lo, hi):
    v = fields.get(key)
    if v in (None, ''):
        return default
    text = str(v).strip()
    try:
        n = int(text, 16) if text.lower().startswith('0x') else int(text, 10)
    except ValueError:
        raise EventError('%s must be an integer' % key)
    if not lo <= n <= hi:
        raise EventError('%s must be %d..%d' % (key, lo, hi))
    return n


def _hex(fields, key, default, lo_len, hi_len):
    v = re.sub(r'\s', '', str(fields.get(key) or default or '')).upper()
    if len(v) % 2 or not (lo_len <= len(v) <= hi_len) or \
            not re.fullmatch(r'[0-9A-F]+', v):
        raise EventError('%s must be %d-%d hex digits'
                         % (key, lo_len, hi_len))
    return v


def _digits(fields, key, default, lo, hi):
    v = str(fields.get(key) or default or '').strip()
    if not _DIGITS_RE.match(v) or not lo <= len(v) <= hi:
        raise EventError('%s must be %d-%d digits' % (key, lo, hi))
    return v


def plmn_hex(mcc, mnc):
    """MCC/MNC -> the 3-byte TS 24.008 BCD PLMN (the PWA's encPlmn: MCC
    digits swapped, MNC padded to 3 digits with 'F')."""
    d = mcc.ljust(3, '0')[:3] + mnc.ljust(3, 'F')[:3]
    return (d[1] + d[0] + d[5] + d[2] + d[4] + d[3]).upper()


def _local_now():
    return time.localtime()


def _tz_offset_minutes():
    """Minutes east of UTC for the host clock."""
    try:
        return int(time.localtime().tm_gmtoff // 60)
    except AttributeError:            # Windows
        return -int(time.timezone // 60)


def scts_bytes(st, tz_min):
    """The 7-byte TS 123 040 TP-SCTS (8.39 Date-Time and Time zone): swapped
    semi-octet BCD digits, the time zone in quarters of an hour with the sign
    in bit 3."""
    semi = lambda v: ((v % 10) << 4) | (v // 10)
    off = int(tz_min)
    tz = semi(int(round(abs(off) / 15.0)) & 0xFF) | (0x08 if off < 0 else 0)
    return bytes([semi(st.tm_year % 100), semi(st.tm_mon), semi(st.tm_mday),
                  semi(st.tm_hour), semi(st.tm_min), semi(st.tm_sec), tz])


# ─── the builders ───────────────────────────────────────────────────────

def build_location_status(fields):
    """TS 31.111 7.5.2: status + Location information (8.19) for normal
    service only."""
    status = _int(fields, 'status', 0, 0, 2)
    out = '9B01%02X' % status
    if status == 0 and fields.get('mcc') not in (None, ''):
        mcc = _digits(fields, 'mcc', None, 3, 3)
        mnc = _digits(fields, 'mnc', '01', 2, 3)
        lac = _hex(fields, 'lac', '0000', 4, 4)
        cell = _hex(fields, 'cell', '0001', 4, 4)
        out += '9307' + plmn_hex(mcc, mnc) + lac + cell
    return out


def build_access_tech(fields):
    """TS 102 223 8.61: the single access technology byte."""
    tech = _int(fields, 'tech', 0, 0, 255)
    return 'BF01%02X' % tech


def build_network_rejection(fields):
    """TS 31.111 7.5.5 (the PWA form's build): registration type selects the
    location object - LAI for the location-updating group, RAI for GPRS, TAI
    for EPS/5GS."""
    rt = _int(fields, 'reg_type', 0x00, 0x00, 0x11)
    access_tech = _int(fields, 'access_tech', 0x00, 0, 255)
    cause = _int(fields, 'cause', 0x02, 0, 255)
    group = 'lu' if rt <= 0x02 else ('gprs' if rt <= 0x08 else 'eps')
    mcc = _digits(fields, 'mcc', '250', 3, 3)
    mnc = _digits(fields, 'mnc', '01', 2, 3)
    plmn = plmn_hex(mcc, mnc)
    out = 'F401%02X' % rt
    out += 'BF01%02X' % access_tech
    out += 'F501%02X' % cause
    if group == 'lu':
        out += '9305' + plmn + _hex(fields, 'lac', '0000', 4, 4)
    elif group == 'gprs':
        out += 'F306' + plmn + _hex(fields, 'lac', '0000', 4, 4) \
            + _hex(fields, 'rac', '01', 2, 2)
    else:
        tac = _hex(fields, 'tac', '0001', 4, 6)
        out += '7D0%X' % (3 + len(tac) // 2) + plmn + tac
        if fields.get('ext_cause') not in (None, ''):
            out += '5701%02X' % _int(fields, 'ext_cause', 0, 0, 255)
    ext_type = fields.get('ext_info_type')
    if ext_type not in (None, ''):
        ext = _hex(fields, 'ext_info', '', 2, 20)
        out += 'F2%02X%02X%s' % (1 + len(ext) // 2, _int(fields, 'ext_info_type', 0, 0, 255), ext)
    return out


def build_data_connection(fields, now=None):
    """TS 31.111 7.5.25.2 object order; the conditional objects are omitted
    when left empty.  ``now`` (a struct_time) makes the date-time testable."""
    status = _int(fields, 'status', 0, 0, 2)
    dtype = _int(fields, 'type', 0, 0, 2)
    out = '9D01%02X' % status
    out += 'AA01%02X' % dtype
    if fields.get('cause') not in (None, ''):
        out += 'AE01%02X' % _int(fields, 'cause', 0, 0, 255)
    ti = str(fields.get('ti') or '00').strip().upper()
    if not re.fullmatch(r'[0-9A-F]{2}', ti):
        raise EventError('ti must be one hex byte')
    out += '1C01' + ti
    if fields.get('datetime') == 'now':
        out += '2607' + scts_bytes(now or _local_now(), _tz_offset_minutes()).hex().upper()
    if fields.get('mcc') not in (None, ''):
        mcc = _digits(fields, 'mcc', None, 3, 3)
        mnc = _digits(fields, 'mnc', '01', 2, 3)
        out += '1307' + plmn_hex(mcc, mnc) \
            + _hex(fields, 'lac', '0000', 4, 4) + _hex(fields, 'cell', '0001', 4, 4)
    if fields.get('tech') not in (None, ''):
        out += 'BF01%02X' % _int(fields, 'tech', 0, 0, 255)
    out += '9B01%02X' % _int(fields, 'loc_status', 0, 0, 2)
    if str(fields.get('apn') or '').strip():
        body = str(fields['apn']).encode('utf-8')
        if len(body) > 100:
            raise EventError('apn must be at most 100 bytes')
        out += 'C7%02X%s' % (len(body), body.hex().upper())
    if fields.get('pdp_type') not in (None, ''):
        out += '0B01%02X' % _int(fields, 'pdp_type', 0, 0, 255)
    return out


BUILDERS = {
    EVENT_LOCATION_STATUS: build_location_status,
    EVENT_ACCESS_TECH: build_access_tech,
    EVENT_NETWORK_REJECTION: build_network_rejection,
    EVENT_DATA_CONNECTION: build_data_connection,
}


def normalise(event, fields):
    """Validate an event's fields (returning the cleaned dict) - the same
    function the builder runs, so a script fails before the run starts."""
    fields = dict(fields or {})
    if event not in BUILDERS:
        raise EventError('no semantic builder for event 0x%02X - use the '
                         'envelope action with the data hex' % event)
    BUILDERS[event](fields)
    return fields


def build(event, fields, now=None):
    """The event-data hex for a semantic event action."""
    fields = normalise(event, fields)
    if event == EVENT_DATA_CONNECTION:
        return BUILDERS[event](fields, now=now)
    return BUILDERS[event](fields)
