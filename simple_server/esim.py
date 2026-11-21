# coding=utf-8
"""eUICC ES10 (SGP.22) interface: the TLV types and the transport helpers.

ES10a/b/c all travel in one generic APDU - STORE DATA, CLA ``80``, INS ``E2``,
P1 ``91`` ("last block, BER-TLV command data field, case 4"), P2 ``00`` (the
first block) and Le ``00`` (SGP.22 v2.6 §5.7.2, Tables 47/48) - whose data
field is one DER-encoded request data object and whose response data field is
one DER-encoded response data object.  This module owns both sides of that
exchange:

* the typed data objects (:class:`TlvIe` subclasses) with a round-trip
  ``encode`` / ``decode`` for every object the server uses;
* :func:`store_data` / :func:`store_data_tlv` / :func:`get_eid`;
* the ES10c result-code mapping (:data:`RESULT_MESSAGES`,
  :func:`result_text`) and the :class:`EsimError` exception shape;
* the raw EUICCInfo1/2, configured-addresses and GetRat decoders the chip view
  needs.  SGP.22's ``EUICCInfo2`` is a long OPTIONAL SEQUENCE and the classes
  here deliberately keep the capability objects raw (``85``/``88``) and
  preserve every unmodelled object, so :func:`decode_info1` /
  :func:`decode_info2` render the fields rather than trusting a partial model.

The transport is duck-typed: anything with ``send_apdu(bytes) -> (bytes, sw)``
(:class:`simple_server.transport.PcscLink` in production, a fake in the tests).
The module is otherwise pure and card-free testable.

Scope: the consumer/IoT ES10 profile-management subset - EID, EUICCInfo1/2,
configured addresses, profiles (GetProfilesInfo, Enable/DisableProfile),
notifications (ListNotification) and GetRat.  Profile download
(BoundProfilePackage), notification retrieval/removal and SM-DP+ interaction
are out of scope, like the existing server's eSIM support.

Deviation worth knowing: SGP.22 v2.6 §5.7.16/§5.7.17 print
``profileIdentifier`` as a ``CHOICE`` inside ``BF31``/``BF32``, but pySim -
and therefore the existing server's ``build_switch_apdu`` - wraps it in an
``A0``.  :class:`ProfileIdentifier` keeps that ``A0`` form.  The spec-checker
confirmed this is correct, not a deviation: SGP.22 uses ``AUTOMATIC TAGS``
(§2.8), so the SEQUENCE's ``[0]`` wrapper (tag ``A0``) is supplied by the
encoding rules around the selected ``4F`` AID / ``5A`` ICCID CHOICE.

``refreshFlag`` is typed ``BOOLEAN`` and DER-encoded, so TRUE is ``FF`` (not
the ``01`` octet pySim writes); see :class:`RefreshFlag`.  Live note (one test
eUICC): a switch with ``refreshFlag`` TRUE is refused (an undefined
``enableResult`` 7), while FALSE (a silent switch, no REFRESH) is accepted -
the server re-initializes the card after a switch either way, so the caller
should prefer FALSE when it performs its own reset.
"""

import re

from simple_server.tlv import BerTlv, ber_tlv_list
from simple_server.util import sw_matches

__all__ = [
    # errors and result codes
    'EsimError', 'RESULT_MESSAGES', 'result_text', 'PROFILE_MGMT_OPERATIONS',
    # value codecs
    'swap_nibbles', 'decode_bcd', 'encode_bcd', 'decode_plmn', 'encode_plmn',
    'decode_der_int', 'encode_der_int', 'decode_bit_string', 'decode_version',
    # TLV framework
    'TlvIe', 'RawTlv', 'EnumIe', 'Utf8Ie', 'VersionIe',
    # AIDs
    'AID_ISD_R', 'AID_ECASD', 'AID_ISD_P_FILE', 'AID_ISD_P_MODULE',
    # profile management
    'Iccid', 'IsdpAid', 'TagList', 'RefreshFlag', 'ProfileIdentifier',
    'EnableProfileReq', 'EnableProfileResp', 'EnableResult',
    'DisableProfileReq', 'DisableProfileResp', 'DisableResult',
    'ProfileInfoListReq', 'ProfileInfoListResp', 'ProfileInfoSeq',
    'ProfileInfo', 'ProfileState', 'ProfileNickname', 'ServiceProviderName',
    'ProfileName', 'IconType', 'Icon', 'ProfileClass', 'ProfilePolicyRules',
    'NotificationConfigurationInfo', 'ProfileOwner', 'ProfileOwnerPLMN',
    'ProfileOwnerGID1', 'ProfileOwnerGID2', 'SMDPPProprietaryData',
    'ProfileInfoListError', 'profile_info_tag_list',
    # notifications
    'ListNotificationReq', 'ListNotificationResp', 'ProfileMgmtOperation',
    'SeqNumber', 'NotificationAddress', 'NotificationMetadata',
    'NotificationMetadataList', 'ListNotificationsResultError',
    'profile_operations',
    # eUICC information
    'EuiccConfiguredAddresses', 'DefaultDpAddress', 'RootDsAddress',
    'EuiccInfo1', 'EuiccInfo2', 'SVN', 'ProfileVersion', 'EuiccFirmwareVer',
    'ExtCardResource', 'UiccCapability', 'TS102241Version',
    'GlobalPlatformVersion', 'RspCapability', 'EuiccCategory', 'PpVersion',
    'SsAcreditationNumber', 'IpaMode', 'IotVersion', 'IotVersionSeq',
    'IotSpecificInfo', 'EuiccCiPkiListForVerification',
    'EuiccCiPkiListForSigning', 'SubjectKeyIdentifier',
    'decode_info1', 'decode_info2', 'decode_addresses', 'decode_rat',
    'UICC_CAPABILITY_BITS', 'RSP_CAPABILITY_BITS', 'PPR_ID_BITS',
    'PPR_FLAG_BITS', 'TRE_PROPERTY_BITS', 'EUICC_CATEGORIES',
    # ES10 functions
    'GetEuiccData', 'EidValue', 'store_data_apdu', 'store_data',
    'store_data_tlv', 'get_eid',
]

# Application identifiers of the SGP.02 Annex H ES10 applications.
AID_ISD_R = 'A0000005591010FFFFFFFF8900000100'
AID_ECASD = 'A0000005591010FFFFFFFF8900000200'
AID_ISD_P_FILE = 'A0000005591010FFFFFFFF8900000D00'
AID_ISD_P_MODULE = 'A0000005591010FFFFFFFF8900000E00'


# ---- errors and result codes ---------------------------------------------

class EsimError(Exception):
    """An eSIM operation failed; ``code`` is a stable identifier for the API.

    ``message`` is a human-readable fallback (the PWA localizes by ``code``).
    Extra keyword arguments land in ``details`` - the STORE DATA helper puts
    the card's status word there as ``sw``.
    """

    def __init__(self, code, message=None, **details):
        super().__init__(message or code)
        self.code = code
        self.details = details


# ES10c result codes -> short English fallback (the PWA localizes by code).
# The names are the SGP.22 v2.6 enumerants of EnableResult/DisableResult
# (§5.7.16/§5.7.17); ProfileInfoListError and ListNotificationsResultError
# reuse the same vocabulary where their names overlap.
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


def result_text(result):
    """Short English text for a decoded ES10 result (ints included).

    An unknown enumerant (decoded as its integer) and the ES10c
    ``undefinedError`` both render as "undefined error", like the existing
    server's ``_error_text``.
    """
    if isinstance(result, int):
        return RESULT_MESSAGES['undefinedError']
    return RESULT_MESSAGES.get(result, result)


# ---- value codecs --------------------------------------------------------

def swap_nibbles(text):
    """Swap the nibbles of every octet of a hex string (BCD/binary order)."""
    text = str(text)
    if len(text) % 2:
        raise ValueError('a hex string needs an even number of digits: %r'
                         % (text,))
    return ''.join(text[i + 1] + text[i] for i in range(0, len(text), 2))


def decode_bcd(data):
    """Packed BCD octets -> digit string (the trailing ``F`` pad is dropped).

    SGP.22 ``Iccid ::= OCTET STRING (SIZE(10))``: the digits are packed two
    per octet, low nibble first (TS 102 221 §13.2), and an odd digit count is
    padded with an ``F`` nibble.
    """
    digits = swap_nibbles(bytes(data).hex())
    if digits.endswith('f'):
        digits = digits[:-1]
    return digits


def encode_bcd(text):
    """Digit string -> packed BCD octets (odd counts get an ``F`` pad)."""
    digits = re.sub(r'\s', '', str(text)).lower()
    if not re.match(r'^[0-9a-f]*$', digits):
        raise ValueError('a BCD value holds hex digits only: %r' % (text,))
    if len(digits) % 2:
        digits += 'f'
    return bytes.fromhex(swap_nibbles(digits))


def decode_plmn(data):
    """A 3-octet PLMN -> ``MCC-MNC`` (TS 24.008 §10.5.1.3, digit 4 = ``F``).

    A 2-digit MNC leaves ``F`` in the high nibble of the third octet; a
    3-digit MNC swaps the last two digits.
    """
    data = bytes(data)
    if len(data) != 3:
        raise ValueError('a PLMN is 3 octets, got %d' % len(data))
    digits = swap_nibbles(data.hex())
    if digits[3] == 'f':
        return '%s-%s' % (digits[:3], digits[4:])
    return '%s-%s' % (digits[:3], digits[4:6] + digits[3])


def encode_plmn(text):
    """``MCC-MNC`` -> the 3-octet PLMN coding (TS 24.008 §10.5.1.3)."""
    parts = str(text).split('-')
    if (len(parts) != 2 or len(parts[0]) != 3
            or len(parts[1]) not in (2, 3)
            or not re.match(r'^[0-9]+$', parts[0] + parts[1])):
        raise ValueError('a PLMN is "MCC-MNC": %r' % (text,))
    mcc, mnc = parts
    digits = mcc + 'f' + mnc if len(mnc) == 2 else mcc + mnc[2] + mnc[:2]
    return bytes.fromhex(swap_nibbles(digits))


def decode_der_int(data):
    """ASN.1 DER INTEGER content -> ``int`` (two's complement, minimal)."""
    data = bytes(data)
    if not data:
        raise ValueError('an ASN.1 INTEGER is at least one octet')
    return int.from_bytes(data, 'big', signed=True)


def encode_der_int(value):
    """``int`` -> the minimal two's-complement octets of an ASN.1 INTEGER."""
    value = int(value)
    length = 1
    while length <= 8:
        try:
            return value.to_bytes(length, 'big', signed=True)
        except OverflowError:
            length += 1
    raise ValueError('ASN.1 INTEGER out of range: %r' % (value,))


def decode_bit_string(data, names):
    """ASN.1 BIT STRING content -> the list of set bit names.

    The first octet is the number of unused bits in the final octet; bits are
    numbered MSB-first within each octet (SGP.22 v2.6 §5.7.8), so bit index
    ``n`` is ``0x80 >> (n % 8)`` of octet ``n // 8``.  Names beyond the list
    are dropped; bits with no name are ignored.
    """
    data = bytes(data)
    if not data:
        return []
    unused = data[0]
    body = data[1:]
    out = []
    for j, byte in enumerate(body):
        b = byte
        if j == len(body) - 1 and 0 < unused < 8:
            b &= (0xFF << unused) & 0xFF
        for i in range(8):
            idx = j * 8 + i
            if idx >= len(names):
                break
            if b & 0x80:
                out.append(names[idx])
            b = (b << 1) & 0xFF
    return out


def decode_version(data):
    """SGP.22 ``VersionType`` (3 octets) -> ``'major.minor.revision'``."""
    data = bytes(data)
    if len(data) != 3:
        return None
    return '%d.%d.%d' % (data[0], data[1], data[2])


def _hex_bytes(value, what='value'):
    """Bytes-like or hex text -> bytes (colons/spaces allowed, even length)."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    text = re.sub(r'[\s:]', '', str(value))
    if len(text) % 2 or not re.match(r'^[0-9a-fA-F]*$', text):
        raise ValueError('%s is not an even-length hex value: %r'
                         % (what, value))
    return bytes.fromhex(text)


def _sw_hex(sw):
    """A returned status word as a lowercase hex string."""
    if isinstance(sw, (bytes, bytearray)):
        return bytes(sw).hex()
    return str(sw).strip().lower()


# ---- the typed TLV framework --------------------------------------------

_UNSET = object()


def _snake(name):
    """Class name -> the snake_case key the decoded dicts use (pySim shape)."""
    return re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', name).lower()


class TlvIe(BerTlv):
    """A typed BER-TLV information element with a round-trip codec.

    A *primitive* IE decodes its value field with :meth:`decode_value` and
    encodes :attr:`decoded` back with :meth:`encode_value`; the defaults pass
    the raw octets through unchanged.  A *constructed* IE (``nested`` is a
    non-empty tuple) parses its value field as a BER-TLV sequence: a child
    whose tag matches one of ``nested`` (or that class's ``alt_tags``)
    becomes an instance of that class in the card's order, and an unmodelled
    child is preserved verbatim as a :class:`RawTlv` - never dropped.

    Construction mirrors :class:`simple_server.tlv.BerTlv`:
    ``Iccid(decoded='8949…')`` builds a primitive IE from its decoded value,
    ``ProfileIdentifier(children=[…])`` builds a constructed one, and the
    ``tag=``/``value=`` pair is what the parser uses internally.
    """

    #: Child IE classes accepted inside a constructed IE, in the order the
    #: specs list them (parsing matches by tag, so it is documentation).
    nested = ()
    #: Extra tags accepted when parsing (kept as-is on re-encode).
    alt_tags = ()

    def __init__(self, tag=None, value=None, children=None, decoded=_UNSET):
        super(TlvIe, self).__init__(tag=tag, value=value, children=children)
        if decoded is not _UNSET and self.children:
            raise ValueError(
                '%s: pass children or a decoded value, not both'
                % type(self).__name__)
        self.decoded = decoded

    # -- encoding --

    def to_bytes(self):
        """The value field: the children's TLVs or the decoded value."""
        if self.children:
            return b''.join(child.to_tlv() for child in self.children)
        if self.decoded is not _UNSET:
            return self.encode_value(self.decoded)
        return self.value

    def encode_value(self, decoded):
        """A primitive decoded value -> its value octets (raw by default)."""
        if isinstance(decoded, (bytes, bytearray)):
            return bytes(decoded)
        return _hex_bytes(decoded)

    # -- decoding --

    @classmethod
    def from_tlv(cls, data):
        """Decode one TLV at ``data[0]``; returns ``(instance, rest)``.

        The wire tag must be the class's own tag (or one of its
        ``alt_tags``), so a response carrying a different data object is an
        error rather than a silently mis-typed instance.
        """
        obj, rest = super(TlvIe, cls).parse(data)
        if cls.tag is not None and obj.tag not in (cls.tag,) + tuple(
                cls.alt_tags):
            raise ValueError('%s: tag 0x%X is not 0x%X'
                             % (cls.__name__, obj.tag, cls.tag))
        obj._decode()
        return obj, rest

    @classmethod
    def from_bytes(cls, data):
        """Decode exactly one TLV; an all-``FF`` trailer is padding.

        The classic SIM file convention pads with ``FF``; anything else after
        the data object is a malformed answer and raises :class:`ValueError`
        (the callers wrap that into :class:`EsimError`).
        """
        obj, rest = cls.from_tlv(data)
        if rest.strip(b'\xff'):
            raise ValueError('%d trailing byte(s) after the TLV'
                             % len(rest))
        return obj

    def _decode(self):
        """Turn the parsed value field into children or a decoded value."""
        if self.nested:
            self.children = _parse_children(self.value, self.nested,
                                            self.alt_tags)
            self.value = b''
            self.decoded = _UNSET
        else:
            self.decoded = self.decode_value(self.value)

    def decode_value(self, data):
        """The value octets -> a decoded value (raw octets by default)."""
        return bytes(data)

    # -- accessors --

    def child_by_type(self, cls):
        """The first child of class ``cls`` (``None`` when absent)."""
        for child in self.children:
            if isinstance(child, cls):
                return child
        return None

    def children_by_type(self, cls):
        """Every child of class ``cls`` (empty list when absent)."""
        return [child for child in self.children if isinstance(child, cls)]

    def to_val_dict(self):
        """The JSON-able value part (hex strings for octet values)."""
        if self.nested:
            return [child.to_dict() for child in self.children]
        if self.decoded is _UNSET:
            return None
        if isinstance(self.decoded, (bytes, bytearray)):
            return bytes(self.decoded).hex()
        return self.decoded

    def to_dict(self):
        """``{snake_case_class_name: value}`` - one level, pySim's shape."""
        return {_snake(type(self).__name__): self.to_val_dict()}


class RawTlv(TlvIe):
    """A child data object this module does not model, preserved verbatim."""

    def to_dict(self):
        return {'unknown_%X' % self.tag: self.to_bytes().hex()}


def _parse_children(data, nested, alt_tags=()):
    """Walk a constructed IE's value field into child IE instances."""
    by_tag = {}
    for cls in nested:
        by_tag[cls.tag] = cls
        for tag in cls.alt_tags:
            by_tag[tag] = cls
    children = []
    for tag, value in ber_tlv_list(data):
        cls = by_tag.get(tag)
        if cls is None:
            children.append(RawTlv(tag=tag, value=value))
            continue
        child = cls(tag=tag, value=value)
        child._decode()
        children.append(child)
    return children


class EnumIe(TlvIe):
    """A one-octet enumerated IE; an unassigned code decodes to its integer."""

    #: ``{code: name}`` - the spec's enumerants.
    values = {}

    def decode_value(self, data):
        data = bytes(data)
        if len(data) != 1:
            raise ValueError('%s: expected one octet, got %d'
                             % (type(self).__name__, len(data)))
        return self.values.get(data[0], data[0])

    def encode_value(self, decoded):
        if isinstance(decoded, str):
            by_name = {name: code for code, name in self.values.items()}
            if decoded not in by_name:
                raise ValueError('%s: unknown value %r'
                                 % (type(self).__name__, decoded))
            return bytes([by_name[decoded]])
        return bytes([int(decoded) & 0xFF])


class VersionIe(TlvIe):
    """An SGP.22 ``VersionType`` IE (3 octets, ``M.m.r``)."""

    def decode_value(self, data):
        data = bytes(data)
        if len(data) != 3:
            raise ValueError('%s: a version is 3 octets, got %d'
                             % (type(self).__name__, len(data)))
        return decode_version(data)

    def encode_value(self, decoded):
        parts = str(decoded).split('.')
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError('%s: a version is "M.m.r": %r'
                             % (type(self).__name__, decoded))
        return bytes(int(p) for p in parts)


# ---- the common ES10 data objects ----------------------------------------

class Iccid(TlvIe, tag=0x5A):
    """ICCID, packed BCD (SGP.22 ``Iccid ::= OCTET STRING (SIZE(10))``)."""

    def decode_value(self, data):
        return decode_bcd(data)

    def encode_value(self, decoded):
        return encode_bcd(decoded)


class IsdpAid(TlvIe, tag=0x4F):
    """An ISD-P AID (``OctetTo16``); decoded as a lowercase hex string."""

    def decode_value(self, data):
        return bytes(data).hex()

    def encode_value(self, decoded):
        return _hex_bytes(decoded, 'ISD-P AID')


class TagList(TlvIe, tag=0x5C):
    """The GetProfilesInfo tag list: a concatenation of tag octets (§5.7.15).

    A multi-octet tag is flattened into its octets (``9F70`` -> ``9F 70``),
    which is the form the spec's own example prints.
    """

    def decode_value(self, data):
        return list(bytes(data))

    def encode_value(self, decoded):
        if isinstance(decoded, (bytes, bytearray)):
            return bytes(decoded)
        return bytes(int(octet) & 0xFF for octet in decoded)


class RefreshFlag(TlvIe, tag=0x81):
    """The DER BOOLEAN refresh flag of Enable-/DisableProfileReq.

    SGP.22 types ``refreshFlag`` as ``BOOLEAN`` and encodes its data objects
    with DER (v2.6 §2.8), so TRUE is ``FF`` and FALSE is ``00`` (X.690) -
    not the ``01``/``00`` octet pySim writes.  Decoding accepts any non-zero
    octet as TRUE for robustness.
    """

    def decode_value(self, data):
        data = bytes(data)
        if len(data) != 1:
            raise ValueError('RefreshFlag: expected one octet, got %d'
                             % len(data))
        return data[0] & 0x01

    def encode_value(self, decoded):
        return bytes([0xFF if decoded else 0x00])


class ProfileIdentifier(TlvIe, tag=0xA0):
    """The profile identifier choice wrapper used by Enable/DisableProfile.

    The ``A0`` wrapper is pySim's (and the existing server's) form; the SGP.22
    ASN.1 leaves the CHOICE untagged - see the module docstring.
    """

    nested = (IsdpAid, Iccid)


# ---- profile management --------------------------------------------------

class EnableResult(EnumIe, tag=0x80):
    """SGP.22 v2.6 §5.7.16 ``enableResult``."""

    values = {
        0: 'ok',
        1: 'iccidOrAidNotFound',
        2: 'profileNotInDisabledState',
        3: 'disallowedByPolicy',
        4: 'wrongProfileReenabling',
        5: 'catBusy',
        127: 'undefinedError',
    }


class DisableResult(EnumIe, tag=0x80):
    """SGP.22 v2.6 §5.7.17 ``disableResult``."""

    values = {
        0: 'ok',
        1: 'iccidOrAidNotFound',
        2: 'profileNotInEnabledState',
        3: 'disallowedByPolicy',
        5: 'catBusy',
        127: 'undefinedError',
    }


class EnableProfileReq(TlvIe, tag=0xBF31):
    """SGP.22 v2.6 §5.7.16 EnableProfileRequest."""

    nested = (ProfileIdentifier, RefreshFlag)


class EnableProfileResp(TlvIe, tag=0xBF31):
    """SGP.22 v2.6 §5.7.16 EnableProfileResponse."""

    nested = (EnableResult,)


class DisableProfileReq(TlvIe, tag=0xBF32):
    """SGP.22 v2.6 §5.7.17 DisableProfileRequest."""

    nested = (ProfileIdentifier, RefreshFlag)


class DisableProfileResp(TlvIe, tag=0xBF32):
    """SGP.22 v2.6 §5.7.17 DisableProfileResponse."""

    nested = (DisableResult,)


class ProfileState(EnumIe, tag=0x9F70):
    """SGP.22 v2.6 §5.7.15 ``ProfileState``."""

    values = {0: 'disabled', 1: 'enabled'}


class Utf8Ie(TlvIe):
    """Shared codec of the ES10 UTF8String objects."""

    def decode_value(self, data):
        return bytes(data).decode('utf-8', 'replace')

    def encode_value(self, decoded):
        return str(decoded).encode('utf-8')


class ProfileNickname(Utf8Ie, tag=0x90):
    """Profile nickname (UTF8String)."""


class ServiceProviderName(Utf8Ie, tag=0x91):
    """Service provider name (UTF8String)."""


class ProfileName(Utf8Ie, tag=0x92):
    """Profile name (UTF8String)."""


class IconType(EnumIe, tag=0x93):
    """SGP.22 v2.6 §5.7.15 ``IconType``."""

    values = {0: 'jpg', 1: 'png'}


class Icon(TlvIe, tag=0x94):
    """The profile icon octets (kept raw; the API hexes them)."""


class ProfileClass(EnumIe, tag=0x95):
    """SGP.22 v2.6 §5.7.15 ``ProfileClass``."""

    values = {0: 'test', 1: 'provisioning', 2: 'operational'}


class ProfilePolicyRules(TlvIe, tag=0x99):
    """PPR identifiers (an ASN.1 BIT STRING, kept raw)."""


class NotificationConfigurationInfo(TlvIe, tag=0xB6):
    """Notification configuration (SEQUENCE OF, kept raw)."""


class ProfileOwnerPLMN(TlvIe, tag=0x80):
    """The operator's PLMN inside ``ProfileOwner`` (``MCC-MNC``)."""

    def decode_value(self, data):
        return decode_plmn(data)

    def encode_value(self, decoded):
        return encode_plmn(decoded)


class ProfileOwnerGID1(TlvIe, tag=0x81):
    """The operator's GID1 (hex)."""

    def decode_value(self, data):
        return bytes(data).hex()


class ProfileOwnerGID2(TlvIe, tag=0x82):
    """The operator's GID2 (hex)."""

    def decode_value(self, data):
        return bytes(data).hex()


class ProfileOwner(TlvIe, tag=0xB7):
    """SGP.22 v2.6 §5.7.15 ``OperatorId``."""

    nested = (ProfileOwnerPLMN, ProfileOwnerGID1, ProfileOwnerGID2)


class SMDPPProprietaryData(TlvIe, tag=0xB8):
    """SM-DP+ proprietary data (kept raw)."""


class ProfileInfo(TlvIe, tag=0xE3):
    """SGP.22 v2.6 §5.7.15 ``ProfileInfo``.

    The field list and order are pySim's; the spec additionally allows
    ``serviceSpecificDataStoredInEuicc`` (tag ``BF22``), which an eUICC may
    return - it lands in :class:`RawTlv` and is preserved.
    """

    nested = (
        Iccid, IsdpAid, ProfileState, ProfileNickname, ServiceProviderName,
        ProfileName, IconType, Icon, ProfileClass, ProfilePolicyRules,
        NotificationConfigurationInfo, ProfileOwner, SMDPPProprietaryData,
    )


class ProfileInfoSeq(TlvIe, tag=0xA0):
    """The OK branch of ProfileInfoListResponse: SEQUENCE OF ProfileInfo."""

    nested = (ProfileInfo,)


class ProfileInfoListError(EnumIe, tag=0x81):
    """SGP.22 v2.6 §5.7.15 ``ProfileInfoListError``.

    The spec assigns ``undefinedError(127)``; pySim's class says ``2``, which
    the pinned spec contradicts - the spec wins here.
    """

    values = {1: 'incorrectInputValues', 127: 'undefinedError'}


class ProfileInfoListReq(TlvIe, tag=0xBF2D):
    """SGP.22 v2.6 §5.7.15 GetProfilesInfo request (search criterion + tags).

    Only the tag list is modelled; a search criterion object (an ``A0`` with
    ``4F``/``5A``/``95`` inside) is preserved as :class:`RawTlv` and can be
    appended by a caller that needs it.
    """

    nested = (TagList,)


class ProfileInfoListResp(TlvIe, tag=0xBF2D):
    """SGP.22 v2.6 §5.7.15 GetProfilesInfo response."""

    nested = (ProfileInfoSeq, ProfileInfoListError)


def profile_info_tag_list():
    """A TagList asking for every ProfileInfo object (the ``--all`` set).

    Flattens multi-octet tags into their octets (``9F70`` -> ``9F 70``), the
    coding §5.7.15 uses in its own example.
    """
    octets = []
    for cls in ProfileInfo.nested:
        tag = cls.tag
        if tag <= 0xFF:
            octets.append(tag)
        elif tag <= 0xFFFF:
            octets.append(tag >> 8)
            octets.append(tag & 0xFF)
        # a 3-octet tag cannot appear in the one-octet-per-tag list coding
    return TagList(decoded=octets)


# ---- notifications -------------------------------------------------------

# ProfileMgmtOperation flags in bit order (SGP.22 v2.6 §5.7.9
# ``NotificationEvent ::= BIT STRING {notificationInstall(0), ...}``), with
# the ASN.1 bit index counted MSB-first inside the flags octet.
_NOTIFICATION_FLAGS = (
    ('install', 0x80),
    ('enable', 0x40),
    ('disable', 0x20),
    ('delete', 0x10),
)

PROFILE_MGMT_OPERATIONS = ('install', 'enable', 'disable', 'delete')


class ProfileMgmtOperation(TlvIe, tag=0x81):
    """``NotificationEvent``: a BIT STRING of profile-management operations.

    The content is the ASN.1 BIT STRING form - a padding-bits octet followed
    by the flags octet - and decodes to ``{operation: bool}`` for all four
    operations, like the existing server's notification view expects.
    """

    def decode_value(self, data):
        data = bytes(data)
        if len(data) < 2:
            raise ValueError('ProfileMgmtOperation: expected 2 octets, got %d'
                             % len(data))
        flags = data[1]
        return {name: bool(flags & mask) for name, mask in _NOTIFICATION_FLAGS}

    def encode_value(self, decoded):
        if isinstance(decoded, dict):
            flags = 0
            for name, mask in _NOTIFICATION_FLAGS:
                if decoded.get(name):
                    flags |= mask
            return bytes([0, flags])
        return bytes([0, int(decoded) & 0xFF])


class SeqNumber(TlvIe, tag=0x80):
    """A notification sequence number (ASN.1 INTEGER)."""

    def decode_value(self, data):
        return decode_der_int(data)

    def encode_value(self, decoded):
        return encode_der_int(decoded)


class NotificationAddress(Utf8Ie, tag=0x0C):
    """The notification's FQDN (UTF8String)."""


class NotificationMetadata(TlvIe, tag=0xBF2F):
    """SGP.22 v2.6 §5.7.9 ``NotificationMetadata``."""

    nested = (SeqNumber, ProfileMgmtOperation,
                               NotificationAddress, Iccid)


class NotificationMetadataList(TlvIe, tag=0xA0):
    """SEQUENCE OF NotificationMetadata."""

    nested = (NotificationMetadata,)


class ListNotificationsResultError(EnumIe, tag=0x81):
    """SGP.22 v2.6 §5.7.9 ``listNotificationsResultError`` (only 127)."""

    values = {127: 'undefinedError'}


class ListNotificationReq(TlvIe, tag=0xBF28):
    """SGP.22 v2.6 §5.7.9 ListNotification request."""

    nested = (ProfileMgmtOperation,)


class ListNotificationResp(TlvIe, tag=0xBF28):
    """SGP.22 v2.6 §5.7.9 ListNotification response."""

    nested = (NotificationMetadataList,
                               ListNotificationsResultError)


def profile_operations(op):
    """A decoded ``profileManagementOperation`` -> the set operation names.

    Accepts the dict this module decodes and the ``{'pmo': {...}}`` shape a
    pySim-style nested decode produces; anything else yields ``[]``.
    """
    if not isinstance(op, dict):
        return []
    flags = op.get('pmo') if isinstance(op.get('pmo'), dict) else op
    return [name for name in PROFILE_MGMT_OPERATIONS if flags.get(name)]


# ---- eUICC information ---------------------------------------------------

class DefaultDpAddress(Utf8Ie, tag=0x80):
    """The default SM-DP+ address (UTF8String FQDN, OPTIONAL)."""


class RootDsAddress(Utf8Ie, tag=0x81):
    """The Root SM-DS address (UTF8String FQDN)."""


class EuiccConfiguredAddresses(TlvIe, tag=0xBF3C):
    """SGP.22 v2.6 §5.7.3 GetEuiccConfiguredAddresses request/response."""

    nested = (DefaultDpAddress, RootDsAddress)


class SVN(VersionIe, tag=0x82):
    """``SVN``: the profile package version the eUICC supports."""


class ProfileVersion(VersionIe, tag=0x81):
    """EUICCInfo2 ``profileVersion``."""


class EuiccFirmwareVer(VersionIe, tag=0x83):
    """EUICCInfo2 ``euiccFirmwareVer``."""


class TS102241Version(VersionIe, tag=0x86):
    """EUICCInfo2 ``ts102241Version``."""


class GlobalPlatformVersion(VersionIe, tag=0x87):
    """EUICCInfo2 ``globalplatformVersion``."""


class PpVersion(VersionIe, tag=0x04):
    """EUICCInfo2 ``ppVersion`` (a bare ASN.1 tag inside the DO)."""


class SsAcreditationNumber(Utf8Ie, tag=0x0C):
    """EUICCInfo2 ``sasAcreditationNumber`` (UTF8String)."""


class IpaMode(EnumIe, tag=0x90):
    """SGP.32 ``ipaMode`` (an IoT eUICC's IPA flavour)."""

    values = {0: 'ipad', 1: 'ipea'}


class ExtCardResource(TlvIe, tag=0x84):
    """EUICCInfo2 ``extCardResource`` (TS 102 226; kept raw)."""


class UiccCapability(TlvIe, tag=0x85):
    """EUICCInfo2 ``uiccCapability`` - an ASN.1 BIT STRING, kept raw.

    :func:`decode_info2` renders the set bits with
    :data:`UICC_CAPABILITY_BITS`; the object itself stays raw because the
    capability list differs per release.
    """


class RspCapability(TlvIe, tag=0x88):
    """EUICCInfo2 ``rspCapability`` - an ASN.1 BIT STRING, kept raw."""


class EuiccCategory(EnumIe, tag=0x8B):
    """EUICCInfo2 ``euiccCategory`` (§5.7.8 Table 4).

    The existing decoder also accepted the explicit-tagged form ``AB``, so the
    class accepts it while re-encoding as ``8B``.
    """

    values = {0: 'other', 1: 'basicEuicc', 2: 'mediumEuicc',
              3: 'contactlessEuicc'}
    alt_tags = (0xAB,)


class SubjectKeyIdentifier(TlvIe, tag=0x04):
    """A CI SubjectKeyIdentifier octet string (kept raw)."""


class EuiccCiPkiListForVerification(TlvIe, tag=0xA9):
    """SEQUENCE OF SubjectKeyIdentifier (verification keys)."""

    nested = (SubjectKeyIdentifier,)


class EuiccCiPkiListForSigning(TlvIe, tag=0xAA):
    """SEQUENCE OF SubjectKeyIdentifier (signing keys)."""

    nested = (SubjectKeyIdentifier,)


class IotVersion(VersionIe, tag=0x80):
    """SGP.32 ``iotVersion``."""


class IotVersionSeq(TlvIe, tag=0xA0):
    """SGP.32 ``iotVersionSeq`` (SEQUENCE OF iotVersion)."""

    nested = (IotVersion,)


class IotSpecificInfo(TlvIe, tag=0x94):
    """SGP.32 ``iotSpecificInfo``."""

    nested = (IotVersionSeq,)


class EuiccInfo1(TlvIe, tag=0xBF20):
    """SGP.22 v2.6 §5.7.8 GetEUICCInfo(1) request/response."""

    nested = (SVN, EuiccCiPkiListForVerification,
                     EuiccCiPkiListForSigning)


class EuiccInfo2(TlvIe, tag=0xBF22):
    """SGP.22 v2.6 §5.7.8 GetEUICCInfo(2) request/response.

    The field list is pySim's (which stops at the SGP.32 ``iotSpecificInfo``).
    The eUICC may return further objects (the certification data object
    ``AC``, the TRE objects ``AD``/``AE``, the additional package versions
    ``AF``, ...) - they are preserved as :class:`RawTlv`, and
    :func:`decode_info2` decodes them.
    """

    nested = (
    ProfileVersion, SVN, EuiccFirmwareVer, ExtCardResource, UiccCapability,
    TS102241Version, GlobalPlatformVersion, RspCapability,
    EuiccCiPkiListForVerification, EuiccCiPkiListForSigning, EuiccCategory,
    PpVersion, SsAcreditationNumber, IpaMode, IotSpecificInfo,
)


# ---- GetEID and our own GetRat request -----------------------------------

class EidValue(TlvIe, tag=0x5A):
    """SGP.22 v2.6 §5.7.20 ``eidValue`` (16 octets), decoded as hex."""

    def decode_value(self, data):
        return bytes(data).hex()


class GetEuiccData(TlvIe, tag=0xBF3E):
    """SGP.22 v2.6 §5.7.20 GetEuiccData (the EID) request/response."""

    nested = (TagList, EidValue)


class _GetRatRequest(TlvIe, tag=0xBF43):
    """ES10b GetRat request (SGP.22 v2.6 §5.7.22): no input data.

    Our own class - pySim's ``euicc`` module has none - used by the chip-info
    view; the response is decoded by :func:`decode_rat`.
    """


# ---- raw EUICCInfo / addresses / RAT decoders ---------------------------

# SGP.22 v2.6 §5.7.8.  The bit names follow the spec's UICCCapability and
# RSPCapability BIT STRINGs; unknown trailing bits are ignored.
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


def _raw_bytes(value):
    """``bytes``/hex text/``None`` -> bytes (the info decoders accept both)."""
    if not value:
        return b''
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return bytes.fromhex(re.sub(r'\s', '', str(value)))


def _tlv_value(data, tag):
    """Value octets of the first ``tag`` TLV in ``data`` (``b''`` if none)."""
    for t, value in ber_tlv_list(_raw_bytes(data)):
        if t == tag:
            return value
    return b''


def _decode_ski_list(data):
    """SEQUENCE OF SubjectKeyIdentifier -> hex strings."""
    return [value.hex() for _tag, value in ber_tlv_list(_raw_bytes(data))]


def _decode_ext_card_resource(data):
    """TS 102 226 Extended Card Resource Information (inner 81/82/83)."""
    out = {}
    raw = {}
    for tag, value in ber_tlv_list(_raw_bytes(data)):
        if tag == 0x81:
            out['installed_application'] = int.from_bytes(value, 'big')
        elif tag == 0x82:
            out['free_non_volatile_memory'] = int.from_bytes(value, 'big')
        elif tag == 0x83:
            out['free_volatile_memory'] = int.from_bytes(value, 'big')
        else:
            raw['%02X' % tag] = value.hex()
    if raw:
        out['raw_tlvs'] = raw
    return out


def _decode_certification_data_object(data):
    """CertificationDataObject (§5.7.8): platform label + DLOA URL."""
    out = {}
    raw = {}
    for tag, value in ber_tlv_list(_raw_bytes(data)):
        if tag == 0x80:
            out['platform_label'] = value.decode('utf-8', 'replace')
        elif tag == 0x81:
            out['discovery_base_url'] = value.decode('utf-8', 'replace')
        else:
            raw['%02X' % tag] = value.hex()
    if raw:
        out['raw_tlvs'] = raw
    return out


def decode_info1(raw):
    """EUICCInfo1 (``BF20``) -> SVN and the CI PKI lists.

    ``raw`` is the response data object as bytes or hex; unknown objects are
    preserved in ``raw_tlvs`` instead of being dropped.
    """
    out = {'svn': None, 'euicc_ci_pki_list_for_verification': [],
           'euicc_ci_pki_list_for_signing': []}
    raw_tlvs = {}
    for tag, value in ber_tlv_list(_tlv_value(raw, 0xBF20)):
        if tag == 0x82:
            out['svn'] = decode_version(value)
        elif tag == 0xA9:
            out['euicc_ci_pki_list_for_verification'] = _decode_ski_list(value)
        elif tag == 0xAA:
            out['euicc_ci_pki_list_for_signing'] = _decode_ski_list(value)
        else:
            raw_tlvs['%02X' % tag] = value.hex()
    if raw_tlvs:
        out['raw_tlvs'] = raw_tlvs
    return out


def decode_info2(raw):
    """EUICCInfo2 (``BF22``) with every SGP.22 v2.6 field decoded.

    The capability objects are rendered here (the TLV classes keep them raw):
    ``uiccCapability``/``rspCapability`` as bit-name lists, the category as
    its name, the resource object numerically - and unknown objects in
    ``raw_tlvs``.
    """
    out = {}
    raw_tlvs = {}
    for tag, value in ber_tlv_list(_tlv_value(raw, 0xBF22)):
        if tag == 0x81:
            out['profile_version'] = decode_version(value)
        elif tag == 0x82:
            out['svn'] = decode_version(value)
        elif tag == 0x83:
            out['euicc_firmware_ver'] = decode_version(value)
        elif tag == 0x84:
            out['ext_card_resource'] = _decode_ext_card_resource(value)
        elif tag == 0x85:
            out['uicc_capability'] = decode_bit_string(
                value, UICC_CAPABILITY_BITS)
        elif tag == 0x86:
            out['ts102241_version'] = decode_version(value)
        elif tag == 0x87:
            out['globalplatform_version'] = decode_version(value)
        elif tag == 0x88:
            out['rsp_capability'] = decode_bit_string(
                value, RSP_CAPABILITY_BITS)
        elif tag == 0xA9:
            out['euicc_ci_pki_list_for_verification'] = _decode_ski_list(value)
        elif tag == 0xAA:
            out['euicc_ci_pki_list_for_signing'] = _decode_ski_list(value)
        elif tag in (0x8B, 0xAB):   # implicit and explicit category encodings
            out['euicc_category'] = EUICC_CATEGORIES.get(
                int.from_bytes(value, 'big') if value else 0, 'other')
        elif tag == 0x99:
            out['forbidden_profile_policy_rules'] = decode_bit_string(
                value, PPR_ID_BITS)
        elif tag == 0x04:           # ppVersion has no context tag
            out['pp_version'] = decode_version(value)
        elif tag == 0x0C:           # sasAcreditationNumber: a bare UTF8String
            out['ss_acreditation_number'] = value.decode('utf-8', 'replace')
        elif tag == 0xAC:
            out['certification_data_object'] = \
                _decode_certification_data_object(value)
        elif tag == 0xAD:
            out['tre_properties'] = decode_bit_string(
                value, TRE_PROPERTY_BITS)
        elif tag == 0xAE:
            out['tre_product_reference'] = value.decode('utf-8', 'replace')
        elif tag == 0xAF:
            out['additional_euicc_profile_package_versions'] = [
                decode_version(v) for _t, v in ber_tlv_list(value)]
        else:
            raw_tlvs['%02X' % tag] = value.hex()
    if raw_tlvs:
        out['raw_tlvs'] = raw_tlvs
    return out


def decode_addresses(raw):
    """ES10a GetEuiccConfiguredAddresses (``BF3C``)."""
    out = {'default_dp_address': None, 'root_ds_address': None}
    raw_tlvs = {}
    for tag, value in ber_tlv_list(_tlv_value(raw, 0xBF3C)):
        if tag == 0x80:
            out['default_dp_address'] = value.decode('utf-8', 'replace')
        elif tag == 0x81:
            out['root_ds_address'] = value.decode('utf-8', 'replace')
        else:
            raw_tlvs['%02X' % tag] = value.hex()
    if raw_tlvs:
        out['raw_tlvs'] = raw_tlvs
    return out


def decode_rat(raw):
    """ES10b GetRat (``BF43``): the Rules Authorisation Table (§5.7.13)."""
    out = []
    table = _tlv_value(_tlv_value(raw, 0xBF43), 0xA0)
    for _tag, rule in ber_tlv_list(table):
        entry = {'ppr_ids': [], 'allowed_operators': [], 'ppr_flags': []}
        for tag, value in ber_tlv_list(rule):
            if tag == 0x80:
                entry['ppr_ids'] = decode_bit_string(value, PPR_ID_BITS)
            elif tag == 0xA1:
                operators = []
                for _t, op in ber_tlv_list(value):
                    ident = {'plmn': None, 'gid1': None, 'gid2': None}
                    for t2, v2 in ber_tlv_list(op):
                        if t2 == 0x80:
                            ident['plmn'] = v2.hex()
                        elif t2 == 0x81:
                            ident['gid1'] = v2.hex()
                        elif t2 == 0x82:
                            ident['gid2'] = v2.hex()
                    operators.append(ident)
                entry['allowed_operators'] = operators
            elif tag == 0x82:
                entry['ppr_flags'] = decode_bit_string(value, PPR_FLAG_BITS)
        out.append(entry)
    return out


# ---- the ES10 transport functions ---------------------------------------

def store_data_apdu(tx_do):
    """The ES10 STORE DATA command APDU for one data object (SGP.22 §5.7.2).

    ``CLA 80``, ``INS E2``, P1 ``91`` = last block + BER-TLV data field +
    case 4, P2 ``00`` = the first block (Table 47/48), Lc, the DER data
    object, Le ``00``.  ``tx_do`` may be bytes or hex text; nothing is sent.
    """
    data = _hex_bytes(tx_do, 'STORE DATA data object')
    if len(data) > 0xFF:
        raise EsimError(
            'do_too_long',
            'a single STORE DATA block carries at most 255 bytes, got %d'
            % len(data))
    return b'\x80\xe2\x91\x00' + bytes([len(data)]) + data + b'\x00'


def store_data(transport, cmd_do, exp_sw='9000'):
    """Send one ES10 STORE DATA and return ``(data_bytes, sw_hex)``.

    ``exp_sw`` is a status-word pattern (``simple_server.util.sw_matches``:
    ``'9???'`` accepts the ``91XX`` "proactive command pending" answer of a
    profile switch); a mismatch raises :class:`EsimError` with the card's
    status word in ``details['sw']``.  ``False``/``None`` skips the check.
    """
    data, sw = transport.send_apdu(store_data_apdu(cmd_do))
    data = bytes(data or b'')
    sw = _sw_hex(sw)
    if exp_sw and not sw_matches(sw, exp_sw):
        raise EsimError('unexpected_sw',
                        'STORE DATA answered SW %s, expected %s'
                        % (sw, exp_sw),
                        sw=sw, expected=exp_sw)
    return data, sw


def store_data_tlv(transport, cmd_do, resp_cls=None, exp_sw='9000'):
    """Transceive one ES10 data object, encoding the request and decoding
    the response.

    ``cmd_do`` is a :class:`TlvIe` instance (``to_tlv()`` is called), raw
    bytes or ``None``/``b''`` for a function without command data.  The
    response is parsed with ``resp_cls`` when given (an instance is returned),
    else its raw octets are returned; an empty response data field yields
    ``None``, like pySim's helper.  The ES10 transport is single-block here,
    so a data object over 255 octets raises :class:`EsimError` (SGP.22 §5.7.2
    would need the ``11``/``91`` block sequence) - never a silent truncation.
    A response that is not exactly one well-formed data object raises
    :class:`EsimError` instead of leaking a parser traceback.
    """
    if cmd_do is None or cmd_do == b'':
        tx_do = b''
    elif isinstance(cmd_do, (bytes, bytearray)):
        tx_do = bytes(cmd_do)
    else:
        tx_do = cmd_do.to_tlv()
    data, _sw = store_data(transport, tx_do, exp_sw=exp_sw)
    if not data:
        return None
    if resp_cls is None:
        return data
    try:
        return resp_cls.from_bytes(data)
    except (ValueError, IndexError) as error:
        raise EsimError('decode_error',
                        '%s: %s' % (resp_cls.__name__, error))


def get_eid(transport, exp_sw='9000'):
    """The eUICC's EID as lowercase hex (SGP.22 v2.6 §5.7.20).

    The request is ``BF3E 03 5C 01 5A`` ("give me the EID object"); the
    response's ``5A`` object is returned raw - the SGP.29 §10 check digits are
    the caller's business.
    """
    request = GetEuiccData(children=[TagList(decoded=[0x5A])])
    resp = store_data_tlv(transport, request, GetEuiccData, exp_sw=exp_sw)
    if resp is None:
        raise EsimError('empty_response',
                        'the card returned no data for GetEuiccData')
    eid = resp.child_by_type(EidValue)
    if eid is None or not eid.decoded:
        raise EsimError('eid_missing',
                        'the GetEuiccData response carries no EID (5A)')
    return eid.decoded
