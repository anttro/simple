# coding=utf-8
"""SCP80 operation helpers: keyset/TAR resolution, counters and the PoR decode.

This module is the pysim-free port of the legacy server's operation logic
(the legacy 3.x ``server``): the preset TAR table and its per-TAR default
keyset (v3.26.0), the MSL-derived SPI1 (TS 102 226 §8.2.1.3.2.4), the
server-owned counter (v3.20.0), the response-packet (PoR) decode with its
remote-management response forms (TS 102 225 §5.2, TS 102 226 §5.1.2/§5.2.2)
and the bounded counter-sync probe.  Everything except :func:`counter_probe`
(which takes the caller's one-attempt send function) is pure and card-free
testable; the APDU exchange lives in :mod:`simple_server.server`.

Ported legacy helpers, name for name::

    _kvn_of                  -> kvn_of
    _counter_valid           -> counter_valid
    _cntr_low_fields         -> cntr_low_fields
    _ram_next_cntr           -> next_counter
    _tar_is_rm               -> tar_is_rm (RM_TAR_ALLOCATIONS)
    _tar_probe_verdict       -> tar_probe_verdict
    _preset_keysets          -> preset_keysets
    _preset_keyset_by_kvn    -> preset_keyset_by_kvn
    _preset_keyset_check     -> preset_keyset_check
    _resolve_operation_keyset-> resolve_operation_keyset
    _op_keyset_resolve       -> op_keyset_resolve
    _preset_counter_seed     -> counter_seed
    _preset_counter_persist  -> counter_persist
    _spi1_for_tar            -> spi1_for_tar
    _wrap_expanded_apdu      -> wrap_expanded_apdu
    _ram_format_apdu         -> format_apdu
    _ram_normalize_format    -> normalise_ram_format
    _parse_response_scripting-> parse_response_scripting
    _decode_por              -> decode_por
    _counter_probe_candidates-> counter_probe_candidates
    _counter_probe_params    -> counter_probe_params
    _counter_probe           -> counter_probe

Spec anchors: TS 102 225 §5.1.1 (the SPI bits), §5.1.2/A.2 (the key version
nibble of KIc/KID and the dedicated counter per key version), §5.2 Table 3 (the
response packet and its status byte); TS 102 226 §4 (a non-RM TAR's payload is
application data), §5.1.2 Table 5.1 (the compact response), §5.2.2 (the Response
Scripting template and the Bad format TLV), §8.2.1.3.2.4 (the Minimum Security
Level); TS 101 220 V18.3.0 Annex D Table D.1 (the ISD/RFM TAR allocations).
"""

import logging
import re

from simple_server.shared import presets

from simple_server import scp80
from simple_server import tlv

log = logging.getLogger(__name__)

__all__ = [
    'RM_TAR_ALLOCATIONS', 'BAD_FORMAT_NAMES', 'COUNTER_PROBE_DEFAULT_APDU',
    'COUNTER_PROBE_CEILING', 'COUNTER_PROBE_MAX_ATTEMPTS', 'COUNTER_PROBE_SPI2',
    'kvn_of', 'counter_valid', 'cntr_low_fields', 'next_counter', 'tar_is_rm',
    'tar_probe_verdict',
    'preset_keysets', 'preset_keyset_by_kvn', 'preset_keyset_check',
    'resolve_operation_keyset', 'op_keyset_resolve', 'counter_seed',
    'counter_persist', 'spi1_for_tar', 'normalise_ram_format',
    'format_apdu', 'wrap_expanded_apdu', 'parse_response_scripting',
    'decode_por', 'counter_probe_candidates', 'counter_probe_params',
    'counter_probe',
]

#: The TARs treated as remote-management targets: the ISD/RFM allocations this
#: tool recognises as remote management by default - a curated subset of
#: TS 101 220 V18.3.0 Annex D Table D.1 (a preset's role entries are added at
#: call time - a card may use non-standard TARs).  A command sent to any other
#: TAR goes to a receiving application whose data format is
#: application-specific (TS 102 226 §4), so its response is reported raw -
#: never decoded as an RM response with a fabricated status word.
RM_TAR_ALLOCATIONS = ('000000', 'B20100', 'B00000', 'B00001', 'B00010',
                      'B00120', 'B00130', 'B00140')

#: TS 102 226 §5.2.2 / Table 5.12 (via TS 101 220 Table 7.20): the Bad format
#: TLV's error types of a Response Scripting answer.
BAD_FORMAT_NAMES = {
    '01': 'unknown tag',
    '02': 'wrong length',
    '03': 'length not found',
}

# ---- the counter-sync probe (v3.9.x) --------------------------------------
#
# A bounded upward search for a counter value the card accepts.  The card only
# accepts a counter *above* its own (TS 102 225 §5.1.1 b5b4 = 10) and updates it
# only when the packet is accepted, so rejected attempts change nothing; the
# search walks a doubling ladder and stops below the 40-bit maximum, where the
# counter would get blocked.

#: The probe command: read-only SELECT MF (a literal - the TAR probe's own
#: constant lives with the RAM code).
COUNTER_PROBE_DEFAULT_APDU = '00A40000023F00'
COUNTER_PROBE_CEILING = 'FFFFFFFF'      # 32-bit default
COUNTER_PROBE_MAX_ATTEMPTS = 40         # doubling covers 40 bits
#: The probe's SPI2 default: the RAM listing transport (PoR via SMS-SUBMIT).
#: The probe cannot work without the PoR verdict - with b1 clear a card that
#: honours the bit sends nothing at all, and the live cards answer ``01`` with
#: an ``actual_response_sms_submit`` that never arrives.
COUNTER_PROBE_SPI2 = '21'


# ---- the keyset/TAR rules --------------------------------------------------

def kvn_of(kic_hex, kid_hex):
    """The keyset number of a packet's KIc/KID bytes; ``(kvn, error)``.

    TS 102 225 §5.1.2/A.2: the number is the b8..b5 nibble of KIc/KID and the
    two shall be identical when different from 0 (the card rejects a mismatch
    with "Unidentified security error").  ``kvn`` is 0 for the spec's
    no-security ``00``/``00`` form; ``error`` names a malformed byte or a
    mismatch.
    """
    kic = str(kic_hex or '').strip().upper()
    kid = str(kid_hex or '').strip().upper()
    for name, value in (('KIc', kic), ('KID', kid)):
        if value and not re.fullmatch(r'[0-9A-F]{2}', value):
            return None, '%s must be one hex byte: %r' % (name, value)
    kic_v = int(kic, 16) >> 4 if kic else 0
    kid_v = int(kid, 16) >> 4 if kid else 0
    if kic_v and kid_v and kic_v != kid_v:
        return None, ('KIc and KID must carry the same keyset number '
                      '(TS 102 225 A.2): %s / %s' % (kic, kid))
    return (kic_v or kid_v), None


def counter_valid(cntr):
    """True when an SCP80 counter is usable (1-10 hex digits).

    A pre-built packet may carry no counter at all; the empty value must never
    reach ``int()``.
    """
    return bool(re.fullmatch(r'[0-9A-Fa-f]{1,10}', str(cntr or '')))


def cntr_low_fields(status):
    """The card's ``cntr_low`` verdict as a flag, or ``{}`` for any other.

    The card's own counter is deliberately not reported: the response packet's
    CNTR is "a copy of the contents of the CNTR in the Command Packet"
    (TS 102 225 §5.2 Table 3) and a rejection leaves it zeroed.  A low counter
    is not recoverable by retrying - the caller raises the preset counter
    (the counter probe can do that in bounded steps).
    """
    if str(status or '') != 'cntr_low':
        return {}
    return {'cntr_low': True}


def next_counter(cntr, advance=True):
    """The counter after a packet: +1 only when the card accepted it.

    The SCP80 counter is 5 octets / 40 bits (TS 102 225 §5.1.1), so it wraps
    at ``FFFFFFFFFF``; a rejected packet (``cntr_low``, a PoR error) leaves
    the card's expectation and the preset counter untouched.
    """
    if not advance:
        return cntr
    return '%010X' % ((int(cntr, 16) + 1) % (2 ** 40))


def tar_is_rm(preset, tar):
    """True for a remote-management TAR, False for any other, None if empty.

    A preset's role entries are RM targets whatever their value; the standard
    ISD/RFM allocations cover a preset-less send.  The TAR decides the response
    form: only an RM TAR answers with the compact/scripting structures.
    """
    t = str(tar or '').strip().upper()
    if not t:
        return None
    for entry in (preset or {}).get('tars') or []:
        if entry.get('role') and str(entry.get('tar') or '').strip().upper() == t:
            return True
    return t in RM_TAR_ALLOCATIONS


def tar_probe_verdict(step):
    """Classify one TAR probe step: the card's ENVELOPE verdict plus the PoR.

    * ``registered`` - the ENVELOPE was accepted (``9000``) **and** the
      application answered with its own status word or data: a remote
      management application owns that TAR;
    * ``no_answer`` - accepted, but the application answered with nothing
      (an empty PoR): a registered address whose application stays silent;
    * ``no_por`` - accepted, no response packet at all (the SPI2 asked for
      one and the card dropped it);
    * ``refused`` - the ENVELOPE itself failed (typically ``6200``), so the
      card has no application registered on that TAR.

    ``step`` is a step record from the RAM sender (``por_status``,
    ``por_sw``, ``por_data``).
    """
    status = str((step or {}).get('por_status') or '')
    if status == 'por_ok':
        return ('registered' if (step.get('por_sw') or step.get('por_data'))
                else 'no_answer')
    if status == 'no_por':
        return 'no_por'
    return 'refused'


def preset_keysets(preset):
    """The keysets of a preset dict.

    A v3.8.0 flat preset (card-wide kic/kid/kicKey/kidKey/counter) counts as a
    single keyset, so old data keeps working.  The store always serves the
    normalised ``keysets`` list; the fallback is belt and braces.
    """
    preset = preset or {}
    keysets = [ks for ks in (preset.get('keysets') or [])
               if isinstance(ks, dict)]
    if not keysets and (preset.get('kic') or preset.get('kid')):
        keysets = [{'kic': preset.get('kic'), 'kid': preset.get('kid'),
                    'kicKey': preset.get('kicKey'),
                    'kidKey': preset.get('kidKey'),
                    'cntr': preset.get('counter') or preset.get('cntr') or ''}]
    return keysets


def preset_keyset_by_kvn(preset, kvn):
    """The preset's keyset dict with number ``kvn``, or None."""
    return next((ks for ks in preset_keysets(preset)
                 if presets.keyset_kvn(ks) == kvn), None)


def preset_keyset_check(store, preset_id, kic, kid):
    """The "the keyset number must be defined in the preset" rule.

    Returns an error string when the packet's (non-zero) keyset number has no
    keyset in the named preset, else None.  Without a ``preset_id`` (a raw API
    caller) there is nothing to check; the ``00``/``00`` (no security) form
    needs no keyset either.
    """
    if not preset_id or store is None:
        return None
    kvn, err = kvn_of(kic, kid)
    if err:
        return err
    if not kvn:
        return None
    if store.find_keyset(preset_id, kvn) is None:
        preset = store.get(preset_id) or {}
        return ('keyset %d is not defined in preset %s - add it in the '
                'Cards tab' % (kvn, preset.get('name') or preset_id))
    return None


def resolve_operation_keyset(store, body, tar):
    """Fill an operation's keyset fields from the preset's keyset for ``tar``.

    An explicit KIc/KID in the request is an API override and stays as it is;
    otherwise the TAR's default keyset supplies kic/kid/kicKey/kidKey (v3.26.0)
    - and, for an MSL 00 entry without a keyset, the spec's no-security
    ``00``/``00`` form (TS 102 225 A.2).  A TAR whose MSL requires security
    must have one: the operation refuses instead of sending a keyless packet
    the card would reject.  Returns ``(kvn, error)``.
    """
    explicit_kic = str((body or {}).get('kic') or '').strip().upper()
    explicit_kid = str((body or {}).get('kid') or '').strip().upper()
    if explicit_kic or explicit_kid:
        return (kvn_of(explicit_kic, explicit_kid)[0] or None), None
    preset = store.get((body or {}).get('preset_id')) if (
        store is not None and (body or {}).get('preset_id')) else None
    if preset is None:
        return None, None
    kvn = presets.tar_keyset(preset, tar)
    if not kvn:
        msl = presets.tar_msl(preset, tar)
        if msl and msl != '00':
            return None, ('TAR %s needs a default keyset (its MSL %s requires '
                          'security) - set it in the Cards tab' % (tar, msl))
        if presets.tar_entry(preset, tar) is None:
            # A TAR outside the preset: the caller must name its keyset - a
            # silent default would be arbitrary.
            return None, ('TAR %s is not in the preset - add it in the Cards '
                          'tab or pass the keyset (KIc/KID) explicitly' % tar)
        # An MSL 00 entry without a default keyset: the spec's no-security
        # values - the packet is built keyless.
        body['kic'] = '00'
        body['kid'] = '00'
        body['kicKey'] = ''
        body['kidKey'] = ''
        return None, None
    keyset = preset_keyset_by_kvn(preset, kvn)
    if keyset is None:
        return None, ('keyset %02X is not defined in preset %s - add it in the '
                      'Cards tab' % (kvn, preset.get('name') or
                                     (body or {}).get('preset_id')))
    for field in ('kic', 'kid', 'kicKey', 'kidKey'):
        body[field] = str(keyset.get(field) or '').strip()
    return kvn, None


def op_keyset_resolve(store, body, tar=None):
    """Resolve an operation's keyset: preset TAR table, then the KIc/KID rules.

    The TAR defaults to the request's; an operation without one uses the
    preset's ISD role entry (v3.26.0, the RAM operations).  The numbers must
    agree (TS 102 225 A.2) and a named preset must define the number ("the
    keyset must exist").  An explicit request KIc/KID stays an API override.
    Returns ``(kvn, kic, kid, error)`` with the effective values; ``kvn`` is
    None when the packet carries no number (``00``/``00``, legal per A.2).
    """
    t = str(tar if tar is not None else (body.get('tar') or '')).strip().upper()
    if not t and body.get('preset_id') and store is not None:
        preset = store.get(body.get('preset_id'))
        t = presets.role_tar(preset, 'isd') if preset else ''
    _kvn, err = resolve_operation_keyset(store, body, t)
    if err:
        return None, '', '', err
    kic = str(body.get('kic') or '').strip().upper()
    kid = str(body.get('kid') or '').strip().upper()
    kvn, err = kvn_of(kic, kid)
    if err:
        return None, '', '', err
    err = preset_keyset_check(store, body.get('preset_id'), kic, kid)
    return kvn, kic, kid, err


def counter_seed(store, body, kvn):
    """The effective SCP80 counter of an operation (v3.20.0).

    An explicit ``cntr`` in the request wins (a hand send or a probe may
    deliberately pick one); otherwise the counter the preset store holds for
    the packet's keyset - the operation persists the next value on acceptance,
    so the store *is* the next counter.  ``''`` when neither is available (the
    packet then carries 0 and nothing is tracked).
    """
    explicit = str((body or {}).get('cntr') or '').strip().upper()
    if explicit:
        return explicit
    preset = (store.get((body or {}).get('preset_id'))
              if store is not None and (body or {}).get('preset_id') else None)
    if preset is None or not kvn:
        return ''
    keyset = preset_keyset_by_kvn(preset, kvn)
    return str((keyset or {}).get('cntr') or '').strip().upper()


def counter_persist(store, preset_id, cntr, source, kvn=None):
    """Persist the counter an operation accepted into the preset store.

    The counter is card state, so the server writes it with the operation that
    consumed it - a closed tab, a lost response or a second window can no
    longer lose an increment.  The store itself is monotonic.  A keyset number
    is mandatory (each keyset has its own dedicated counter, TS 102 225 A.1);
    a packet with no number has nothing to write.  Best effort: a store
    failure must never fail the card operation.
    """
    if not preset_id or not cntr or not kvn or store is None:
        return
    if not hasattr(store, 'set_counter'):
        return
    try:
        store.set_counter(preset_id, cntr, source, kvn)
    except Exception as exc:                    # never fail the operation
        log.warning('counter persist failed (%s): %s', source, exc)


# ---- the SPI1 derivation ---------------------------------------------------

def _hex_lt(a, b):
    """Numeric ``a < b`` for two hex byte strings; invalid input never is."""
    try:
        return int(a, 16) < int(b, 16)
    except (TypeError, ValueError):
        return False


def spi1_for_tar(preset, tar_hex, explicit='', prefer_msl=False):
    """Resolve the SPI1 of a packet addressed to ``tar_hex``; ``(spi1, warning)``.

    Each TAR carries its **MSL** (Minimum SPI1, TS 102 226 §8.2.1.3.2.4): the
    card checks it before the security processing and answers response status
    0A "Insufficient security level" below it, so the MSL is the natural value
    for packets to that TAR.

    ``prefer_msl=False`` (a hand send): the caller's explicit SPI1 wins - the
    operator may deliberately ask for more security than the minimum - and a
    value below the TAR's MSL only produces a warning.  ``prefer_msl=True``
    (an operation): the TAR's MSL wins, the explicit value is only the
    fallback for a TAR the preset does not carry.  Raises ValueError when
    neither exists - guessing would either be refused by the card or send more
    security than the application expects.
    """
    explicit = str(explicit or '').strip().upper()
    tar = str(tar_hex or '').strip().upper()
    msl = presets.tar_msl(preset, tar)
    if prefer_msl and msl:
        return msl, ''
    if explicit:
        warning = ''
        if msl and _hex_lt(explicit, msl):
            warning = ('SPI1 %s is below the MSL %s of TAR %s - the card may '
                       'answer 0A (insufficient security level)'
                       % (explicit, msl, tar))
        return explicit, warning
    if msl:
        return msl, ''
    if not tar:
        raise ValueError('no TAR for the packet and no spi1 given - pass the '
                         'TAR (add its MSL to the card preset) or spi1')
    raise ValueError('no MSL for TAR %s in the card preset - set it in the '
                     'Cards tab or pass spi1' % tar)


# ---- the RAM command format (TS 102 226 §5.2.1) ----------------------------
#
# The bare C-APDU (compact) or the Command TLV '22' inside the 'AA' scripting
# template (expanded).  The per-operation format *detection* belongs to the RAM
# operation sender (it sends a probe packet and consumes counters); the plain
# SCP80 send only rewrites an explicitly named format.

def wrap_expanded_apdu(apdu_hex, form='definite'):
    """Wrap a C-APDU in the expanded remote-management format (TS 102 226 §5.2.1).

    ``definite``   -> ``AA <len> 22 <len> <apdu>`` (TS 101 220 Table 7.19; the
                      reference trace's ``AA0A220880F24000024F0000``)
    ``indefinite`` -> ``AE 80 22 <len> <apdu> 00 00`` (indefinite length
                      coding, the variant the spec recommends for RAM/RFM
                      over HTTPS; some cards accept only this one)
    """
    cmd = '22' + _ber_len_hex(len(apdu_hex) // 2) + apdu_hex
    if form == 'indefinite':
        return ('AE80' + cmd + '0000').upper()
    return ('AA' + _ber_len_hex(len(cmd) // 2) + cmd).upper()


def _ber_len_hex(length):
    """A BER length as hex (short form below 128, else ``81 nn``...)."""
    return tlv.encode_ber_len(length).hex().upper()


def normalise_ram_format(value):
    """'auto' | 'compact' | 'expanded' | 'expanded-ae'; anything else 'auto'."""
    v = str(value or 'auto').strip().lower()
    return v if v in ('auto', 'compact', 'expanded', 'expanded-ae') else 'auto'


def format_apdu(apdu_hex, ram_format):
    """Apply the RAM command format: compact (bare) or an expanded template.

    ``auto`` is the caller's format detection, which the RAM operation sender
    owns: it sends a read-only probe packet per operation (the legacy
    ``_ram_detect_format`` - it consumes counters, so it belongs to a RAM step,
    not to a plain send; ``ServerState._ram_detect_format``).  **Decision
    (3c-3c-1):** the plain send does not probe - it builds the compact form and
    reports the form it actually sent; the RAM endpoints (3c-3c-2a) do probe.
    If a caller asks a plain send for ``auto`` on a card that only accepts an
    expanded form, the RAM operation is the path to use.
    """
    if ram_format == 'expanded':
        return wrap_expanded_apdu(apdu_hex, 'definite')
    if ram_format == 'expanded-ae':
        return wrap_expanded_apdu(apdu_hex, 'indefinite')
    return apdu_hex


# ---- the response packet (PoR) --------------------------------------------

def parse_response_scripting(data):
    """Parse a Response Scripting template (TS 102 226 §5.2.2).

    ``AB <len>`` (definite) or ``AF 80 ... 00 00`` (indefinite) containing the
    executed-command-count TLV ``80``, one or more R-APDU TLVs ``23`` (the last
    two bytes are SW1 SW2) and/or the Bad format TLV ``90`` (TS 101 220
    Table 7.20; error types per TS 102 226 Table 5.12).

    Returns ``{'count', 'sw', 'data', 'bad_format'}`` or None when the data is
    not a scripting template.  ``bad_format`` is the error type hex when the
    card aborted the script on a malformed command (then ``sw`` is None and
    ``data`` ''); an R-APDU is not required for a bad-format answer.
    """
    if not data:
        return None
    data = bytes(data)
    if data[0] == 0xAF:
        if len(data) < 4 or data[1] != 0x80 or data[-2:] != b'\x00\x00':
            return None
        body = data[2:-2]
    elif data[0] == 0xAB:
        ln, voff = tlv.parse_ber_len(data, 1)
        if ln <= 0 or voff + ln > len(data):
            return None
        body = data[voff:voff + ln]
    else:
        return None
    count = None
    last = None
    bad = None
    off = 0
    while off < len(body) - 1:
        tag = body[off]
        ln, voff = tlv.parse_ber_len(body, off + 1)
        if ln < 0 or voff + ln > len(body):
            break
        val = body[voff:voff + ln]
        if tag == 0x80 and val:
            count = int.from_bytes(val, 'big')
        elif tag in (0x23, 0xA3) and len(val) >= 2:
            # both tag styles: the spec pins the CR flag to 0, cards mix them
            last = (val[-2:].hex().upper(), val[:-2].hex().upper())
        elif tag in (0x90, 0x10) and val:
            bad = val[:1].hex().upper()
        off = voff + ln
    if last is None and bad is None:
        return None
    return {'count': count,
            'sw': last[0] if last else None,
            'data': last[1] if last else '',
            'bad_format': bad}


def _compact_or_none(secured, cmd_len):
    """The compact RM response (TS 102 226 §5.1.2 Table 5.1), or None.

    ``parse_compact`` refuses a structure too short to be one; the command
    count cannot exceed the plaintext command script's length (``cmd_len``
    octets - each command needs at least one), so a larger count proves the
    secured data is the receiving application's own.
    """
    compact = scp80.parse_compact(secured)
    if compact is None:
        return None
    if cmd_len is not None and compact['number_of_commands'] > int(cmd_len):
        return None
    return compact


def decode_por(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex,
               response_hex, cmd_len=None, rm=None):
    """Decode a response packet (the PoR), or None.

    ``rm`` tells whether the command was addressed to a remote-management TAR
    (TS 102 226 §4: only those answer with the compact/scripting structures):

    * ``True``  - decode as an RM response;
    * ``False`` - any other TAR: the secured data is the receiving
      application's own (application specific, "not defined" in the spec) and
      is reported raw; no status word is invented;
    * ``None`` (unknown) - decode, falling back to raw when the compact parse
      cannot apply.

    ``cmd_len`` is the length in bytes of the plaintext command script the
    packet carried (None when unknown): a compact response's command count
    cannot exceed it (TS 102 226 §5.1.2 Table 5.1), so a larger count means the
    data is the application's own and is reported as ``raw``.

    The packet's own verification (UDH/RPI, deciphering, PCNTR padding, RC/CC)
    is :func:`simple_server.scp80.decode_resp`; a malformed packet or a failed
    check is "not a PoR" here.
    """
    if not response_hex:
        return None
    try:
        keyset = scp80.Keyset.from_bytes(
            spi1, spi2, kic, kid, cntr_hex if cntr_hex else 0,
            kic_key_hex, kid_key_hex)
        spi = scp80.spi_from_bytes(spi1, spi2)
    except (scp80.Scp80Error, ValueError, TypeError):
        # a malformed counter/key/hex (e.g. a hand-made request) is not a PoR
        return None
    try:
        res = scp80.decode_resp(keyset, spi, response_hex)
    except (scp80.Scp80Error, ValueError, TypeError):
        return None
    raw_hex = (bytes(response_hex).hex()
               if isinstance(response_hex, (bytes, bytearray))
               else str(response_hex).strip())
    out = {
        'response_status': str(res.response_status),
        'tar': res.tar_hex,
        'cntr': res.cntr_hex,
        'pcntr': res.pcntr,
        'rpl': res.rpl,
        'rhl': res.rhl,
        'cc_rc': res.cc_rc_hex,
        'raw': raw_hex,
    }
    secured_hex = res.secured_hex
    if secured_hex:
        out['secured_data'] = secured_hex
    if res.response_status == 'por_ok' and res.secured_data:
        scripted = (parse_response_scripting(res.secured_data)
                    if rm is not False else None)
        if scripted is not None:
            out['response_type'] = 'scripting'
            out['decoded'] = {
                'number_of_commands': scripted['count'],
                'last_status_word': scripted['sw'],
                'last_response_data': scripted['data'],
            }
            if scripted['bad_format']:
                # the card parsed the scripting template but aborted on a
                # malformed command (TS 102 226 §5.2.2 Bad format TLV)
                out['bad_format'] = scripted['bad_format']
                out['bad_format_name'] = BAD_FORMAT_NAMES.get(
                    scripted['bad_format'], 'unknown error type')
        else:
            compact = (_compact_or_none(res.secured_data, cmd_len)
                       if rm is not False else None)
            if compact is not None:
                out['response_type'] = 'compact'
                out['decoded'] = {
                    'number_of_commands': compact['number_of_commands'],
                    'last_status_word': compact['last_status_word'],
                    'last_response_data': compact['last_response_data'],
                }
            else:
                # Not a compact remote response - the application's own bytes
                # (e.g. a third-party applet's TAR); expose them as the
                # response data, with no status word.
                out['response_type'] = 'raw'
                out['decoded'] = {
                    'number_of_commands': None,
                    'last_status_word': '',
                    'last_response_data': secured_hex,
                }
    else:
        out['response_type'] = 'none'
    return out


# ---- the counter-sync probe ------------------------------------------------

def counter_probe_candidates(start_hex, ceiling_hex, max_attempts):
    """The cautious counter ladder: start + 1, +2, +4, ... (doubling).

    Returns ``(candidates, reason)``: reason is ``'ceiling'`` when the next
    value would exceed the ceiling, else ``'attempts'`` when the attempt budget
    ran out.  Doubling keeps the packet count low (<= ~40 across the whole
    40-bit range) and the accepted value below ~2x the card's counter - only
    the accepted packet advances anything, every rejected one is a no-op.
    """
    try:
        value = int(str(start_hex or '0'), 16)
        ceiling = int(str(ceiling_hex or COUNTER_PROBE_CEILING), 16)
        max_attempts = int(max_attempts)
    except (TypeError, ValueError):
        return [], 'attempts'
    candidates = []
    step = 1
    while len(candidates) < max_attempts:
        value += step
        if value > ceiling:
            return candidates, 'ceiling'
        candidates.append('%010X' % value)
        step *= 2
    return candidates, 'attempts'


def counter_probe_params(preset, body):
    """Resolve a counter probe's parameters from the named preset.

    The keyset (by ``kvn``, else the TAR's default keyset, else the first),
    its keys and counter, the preset's TAR and MSL-derived SPI1 (overridable),
    the ceiling and the attempt budget.  SPI2 always carries b1 (a PoR is
    required - the probe is blind without the verdict); the default is the RAM
    listing transport.  Raises ValueError with a user-facing message.
    """
    keysets = preset_keysets(preset)
    if not keysets:
        raise ValueError('the preset defines no keyset')
    tar = str(body.get('tar') or presets.role_tar(preset, 'isd')
              or '').strip().upper()
    want = body.get('kvn')
    if want not in (None, ''):
        try:
            want = int(want)
        except (TypeError, ValueError):
            raise ValueError('invalid keyset number')
        keyset = next((k for k in keysets if presets.keyset_kvn(k) == want),
                      None)
        if keyset is None:
            raise ValueError('keyset %d is not defined in the preset' % want)
    else:
        tar_kvn = presets.tar_keyset(preset, tar)
        keyset = (next((k for k in keysets
                        if presets.keyset_kvn(k) == tar_kvn), None)
                  if tar_kvn else None)
        if keyset is None:
            keyset = keysets[0]
    kic = str(keyset.get('kic') or '').strip().upper()
    kid = str(keyset.get('kid') or '').strip().upper()
    kic_key = str(keyset.get('kicKey') or '').strip()
    kid_key = str(keyset.get('kidKey') or '').strip()
    if not (kic and kid and kic_key and kid_key):
        raise ValueError('the keyset needs KIc, KID and both keys')
    kvn, err = kvn_of(kic, kid)
    if err:
        raise ValueError(err)
    start = str(body.get('cntr') or keyset.get('cntr') or '').strip().upper()
    if not counter_valid(start):
        raise ValueError('the keyset needs a counter to start from')
    ceiling = str(body.get('ceiling') or COUNTER_PROBE_CEILING).strip().upper()
    try:
        ceiling_value = int(ceiling, 16)
    except ValueError:
        raise ValueError('invalid ceiling')
    if ceiling_value < int(start, 16):
        raise ValueError('the ceiling is below the current counter')
    if ceiling_value > 0xFFFFFFFFFE:
        raise ValueError('the ceiling must stay below the 40-bit maximum')
    try:
        max_attempts = int(body.get('max_attempts')
                           or COUNTER_PROBE_MAX_ATTEMPTS)
    except (TypeError, ValueError):
        raise ValueError('invalid attempt budget')
    max_attempts = max(1, min(64, max_attempts))
    spi1, _warning = spi1_for_tar(preset, tar, body.get('spi1'),
                                  prefer_msl=True)
    if not scp80.counter_tracked(spi1):
        raise ValueError('the probe needs a counter check - SPI1 %s has none '
                         '(b5b4 = 00)' % spi1)
    spi2 = str(body.get('spi2') or COUNTER_PROBE_SPI2).strip().upper()
    if not re.fullmatch(r'[0-9A-F]{2}', spi2):
        raise ValueError('invalid SPI2')
    # b1 (PoR required) is forced - the probe is blind without the verdict;
    # the caller's transport bits are kept
    spi2 = '%02X' % (int(spi2, 16) | 0x01)
    return {
        'kvn': kvn, 'kic': kic, 'kid': kid,
        'kic_key': kic_key, 'kid_key': kid_key,
        'start': start, 'ceiling': ceiling,
        'max_attempts': max_attempts,
        'spi1': spi1,
        'spi2': spi2,
        'tar': tar,
        # the probe command is fixed: a read-only SELECT MF, which every
        # applet answers (an error SW is fine - the counter advances on the
        # packet's security acceptance, not on the command's own result)
        'apdu': COUNTER_PROBE_DEFAULT_APDU,
    }


def counter_probe(store, preset, body, send_fn):
    """Sync a preset's counter with the card: ``counter_probe_params`` + ladder.

    ``send_fn(params, cntr)`` performs one attempt and returns
    ``(send_result, por)`` - the :class:`~simple_server.server.ServerState`
    builds and delivers the packet.  Every rejected attempt is a no-op on the
    card; the ladder only advances on a ``cntr_low`` verdict.  On acceptance
    the probe persists ``accepted + 1`` (monotonic).  A *post*-``cntr_low``
    security error (e.g. ``rc_cc_ds_failed``) proves the value is above the
    card's counter: the value is saved anyway but the result is an error
    ("something is likely wrong with the security settings"), because the
    packet did not verify.  A verdict without a prior ``cntr_low`` saves
    nothing.

    Returns the result dict with the attempt list - the caller (UI) retries
    the failed operation afterwards.
    """
    p = counter_probe_params(preset, body)
    candidates, stop = counter_probe_candidates(p['start'], p['ceiling'],
                                                p['max_attempts'])
    out = {'success': False, 'preset_id': preset.get('id'),
           'preset_name': preset.get('name'), 'kvn': p['kvn'],
           'start': p['start'], 'ceiling': p['ceiling'],
           'max_attempts': p['max_attempts'], 'tar': p['tar'],
           'spi1': p['spi1'], 'spi2': p['spi2'], 'apdu': p['apdu'],
           'packets': 0, 'attempts': [], 'stopped': stop}
    if not candidates:
        out['error'] = 'the counter is already at the ceiling %s' % p['ceiling']
        return out
    saw_low = False
    for cntr in candidates:
        send, por = send_fn(p, cntr)
        pstatus = str((por or {}).get('response_status') or '')
        out['packets'] += 1
        attempt = {'cntr': cntr, 'por_status': pstatus or None,
                   'sw': send.get('sw'), 'bytes': send.get('bytes'),
                   'segments': send.get('segments')}
        remote_sw = _por_remote_sw(por)
        if remote_sw:
            attempt['remote_sw'] = remote_sw
        if (por or {}).get('bad_format'):
            attempt['bad_format'] = por['bad_format']
        out['attempts'].append(attempt)
        log.info('counter probe: %s -> %s', cntr,
                 pstatus or ('ENVELOPE %s' % (send.get('sw') or '?')))
        if pstatus == 'cntr_low':
            # the only verdict that keeps the ladder going: the card says the
            # attempted counter is not above its own
            saw_low = True
            continue
        if pstatus in ('por_ok', 'actual_response_sms_submit',
                       'actual_response_ussd'):
            # accepted (an actual-response status means the response travels
            # via SMS/USSD - the packet itself was accepted)
            stored = next_counter(cntr, True)
            counter_persist(store, preset.get('id'), stored, 'counter-probe',
                            p['kvn'])
            out.update({'success': True, 'stopped': 'accepted',
                        'verdict': pstatus, 'counter_saved': True,
                        'accepted_cntr': cntr, 'stored_cntr': stored})
            log.info('counter probe: accepted %s (%s), preset stores %s',
                     cntr, pstatus, stored)
            return out
        out['stopped'] = 'error'
        if pstatus:
            if saw_low:
                stored = next_counter(cntr, True)
                counter_persist(store, preset.get('id'), stored,
                                'counter-probe', p['kvn'])
                out.update({'counter_saved': True, 'verdict': pstatus,
                            'accepted_cntr': cntr, 'stored_cntr': stored,
                            'error': 'card verdict: %s - the counter value was '
                                     'still saved (accepted %s, stored %s); '
                                     'something is likely wrong with the '
                                     'security settings'
                                     % (pstatus, cntr, stored)})
                log.info('counter probe: %s -> %s, value saved (preset stores '
                         '%s)', cntr, pstatus, stored)
            else:
                out['error'] = send.get('error') or (
                    'card verdict: %s - the counter was not synced '
                    '(the search never saw a cntr_low rejection)' % pstatus)
        else:
            out['error'] = send.get('error') or (
                'card verdict: no PoR (the card sent none for SPI2 %s)'
                % p['spi2'])
        return out
    if stop == 'ceiling':
        out['error'] = ('no counter up to the ceiling %s was accepted - the '
                        "card's counter is above it, raise the preset counter "
                        'manually' % p['ceiling'])
    else:
        out['error'] = ('no counter above %s was accepted in %d attempts'
                        % (p['start'], p['max_attempts']))
    return out


def _por_remote_sw(por):
    """The last remote-command status word of a decoded PoR, '' when none."""
    dec = (por or {}).get('decoded') or {}
    sw = str(dec.get('last_status_word') or '').upper()
    if sw:
        return sw
    for response in reversed((por or {}).get('responses') or []):
        if response.get('status_word'):
            return str(response['status_word']).upper()
    return ''
