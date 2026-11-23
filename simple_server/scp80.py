# coding=utf-8
"""SCP80 secured packets (TS 102 225 / TS 31.115, the former GSM 03.48).

A secured packet is the SCP80 command and response transport the OTA
operations use:

* :func:`encode_cmd` builds a command packet (TS 102 225 §5.1.1 / TS 31.115
  §4.2): ``CPL CHL SPI KIc KID TAR | CNTR PCNTR [RC/CC] secured data``, with
  the data ciphered and padded when the SPI asks for it;
* :func:`decode_resp` parses a response packet (TS 102 225 §5.2 Table 3):
  ``UDHL RPI | RPL RHL TAR CNTR PCNTR STS [RC/CC] secured data``, deciphering
  and verifying the RC/CC/DS it carries;
* :func:`split_secured_packet` cuts a command packet into the SMS user-data
  parts of a concatenated SMS-PP download (TS 31.115 §4.3) - the parts are
  handed to :func:`simple_server.sms.build_sms_tpdu`.

Everything here is a behavioural port of the byte-exact implementation the
legacy 3.x server carries (``_spi_from_bytes``,
``_ota_keyset``, ``_encode_cmd_unlimited``, ``_build_secured_packet``,
``_split_secured_packet``, ``_por_header_parse`` and ``_decode_por``'s header
part), with the crypto written against ``pycryptodomex`` instead of pySim's
dialect classes.  The functions are pure and card-free testable; the tests pin
them to the same vectors the legacy server asserts, so both produce
identical bytes.

Cryptographic algorithms (TS 102 225 §5.1.2/§5.1.3, keyed by the low nibble
of KIc/KID): ``1`` single DES, ``5`` triple DES (two-key, 16-byte key), ``9``
triple DES (three-key, 24-byte key) and ``2`` AES; the ciphering modes are CBC
with the all-zero IV, the CC is the trailing CBC-MAC block (8 octets) or an
8-octet AES-CMAC, and the RC is the CRC-32 of the same range (4 octets).

Not implemented: the DS (digital signature) coding of RC/CC/DS - the specs
leave the signature scheme open, so a packet that asks for it is refused
instead of building an unsigned packet.  The response-scripting content parse
(TS 102 226 §5.2.2) is left to the caller: :func:`decode_resp` returns the
secured data and :func:`parse_compact` reads the compact remote-management
form.
"""

import zlib
from dataclasses import dataclass

from Cryptodome.Cipher import AES, DES, DES3
from Cryptodome.Hash import CMAC

from simple_server import sms

__all__ = [
    'RC_CC_DS', 'CNTR_REQ', 'POR_REQ', 'CRYPT_ALGO', 'AUTH_ALGO',
    'RESPONSE_STATUS', 'SCP80_SINGLE_BYTES', 'SCP80_FIRST_BYTES',
    'SCP80_NEXT_BYTES', 'SCP80_CONCAT_REF', 'SCP80_MAX_SEGMENTS',
    'Scp80Error', 'CryptoAlgo', 'AuthAlgo', 'Keyset', 'Response',
    'spi_from_bytes', 'spi_to_bytes', 'sig_len_for', 'get_padding',
    'pad_to_multiple',
    'encode_cmd', 'build_secured_packet', 'split_secured_packet',
    'decode_resp', 'parse_compact', 'counter_tracked',
    'increment_counter_hex',
]


class Scp80Error(ValueError):
    """A malformed secured packet, or an unsupported/refused parameter."""


# ---- TS 102 225 coding tables ---------------------------------------------

# TS 102 225 §5.1.1: SPI1.b2b1 (command) and SPI2.b4b3 (response)
RC_CC_DS = {0: 'no_rc_cc_ds', 1: 'rc', 2: 'cc', 3: 'ds'}
# TS 102 225 §5.1.1: SPI1.b5b4 - no counter / no replay or sequence check /
# the counter must be higher / the counter must be one higher than the stored
# one.  (pySim's enum calls the last value 'counter_must_be_lower', a misnomer;
# we use the spec wording here - the label is our own API, not pySim's.)
CNTR_REQ = {0: 'no_counter', 1: 'counter_no_replay_or_seq',
            2: 'counter_must_be_higher', 3: 'counter_must_be_one_higher'}
# TS 102 225 §5.1.1: SPI2.b2b1 - no PoR / a PoR is required / only on error
POR_REQ = {0: 'no_por', 1: 'por_required', 2: 'por_only_when_error'}
# TS 102 225 §5.1.2 (KIc) / §5.1.3.1 (KID with CC): low nibble of the byte
CRYPT_ALGO = {1: 'single_des', 5: 'triple_des_cbc2', 9: 'triple_des_cbc3',
              2: 'aes_cbc'}
AUTH_ALGO = {1: 'single_des', 5: 'triple_des_cbc2', 9: 'triple_des_cbc3',
             2: 'aes_cmac'}

# the reverse lookups the SPI/keyset byte encoders use
CNTR_REQ_REVERSE = {name: nib for nib, name in CNTR_REQ.items()}
RC_CC_DS_REVERSE = {name: nib for nib, name in RC_CC_DS.items()}
POR_REQ_REVERSE = {name: nib for nib, name in POR_REQ.items()}

# TS 102 225 §5.2 Table 3 / TS 31.115 §7: the response status byte
RESPONSE_STATUS = {
    0x00: 'por_ok',
    0x01: 'rc_cc_ds_failed',
    0x02: 'cntr_low',
    0x03: 'cntr_high',
    0x04: 'cntr_blocked',
    0x05: 'ciphering_error',
    0x06: 'undefined_security_error',
    0x07: 'insufficient_memory',
    0x08: 'more_time_needed',
    0x09: 'tar_unknown',
    0x0A: 'insufficient_security_level',
    0x0B: 'actual_response_sms_submit',
    0x0C: 'actual_response_ussd',
}

# SMS user-data capacities of the SMS-PP download (TS 31.115 §4.3): an SM's
# user data is 140 octets, minus the UDH (UDHL octet + IEs).
SCP80_SINGLE_BYTES = 137    # 140 - UDHL(1) - CPI IE(2)
SCP80_FIRST_BYTES = 132     # 140 - UDHL(1) - concat IE(5) - CPI IE(2)
SCP80_NEXT_BYTES = 134      # 140 - UDHL(1) - concat IE(5)
SCP80_CONCAT_REF = sms.CONCAT_REF     # the fixed UDH reference of the splitter
SCP80_MAX_SEGMENTS = 5      # the card's concatenation buffer (server cap)


# ---- padding ---------------------------------------------------------------

def get_padding(in_len, multiple):
    """Zero padding up to the next multiple (empty when already aligned).

    TS 102 225 §5.1.1: the padding octets are ``00`` and the count is carried
    in PCNTR.  A length that is already a multiple of ``multiple`` gets *no*
    full block of padding - port of pySim's ``OtaAlgo._get_padding``.
    """
    if in_len % multiple == 0:
        return b''
    return b'\x00' * (multiple - (in_len % multiple))


def pad_to_multiple(data, multiple):
    """``data`` plus :func:`get_padding` for its length."""
    data = bytes(data)
    return data + get_padding(len(data), multiple)


# ---- ciphering / authentication algorithms ---------------------------------

class CryptoAlgo(object):
    """CBC ciphering with the all-zero IV (TS 102 225 §5.1.1).

    ``encrypt`` pads its input to the block size (the command builder does
    that padding itself and passes an aligned block, but the algorithm stays
    usable on its own); ``decrypt`` does not unpad - the PCNTR field says how
    many octets to drop.
    """

    blocksize = 8

    def __init__(self, key):
        self.key = bytes(key)
        if not self.key:
            raise Scp80Error('the ciphering key is empty')

    @property
    def iv(self):
        return bytes(self.blocksize)

    def encrypt(self, data):
        return self._cipher().encrypt(pad_to_multiple(data, self.blocksize))

    def decrypt(self, data):
        return self._cipher().decrypt(bytes(data))

    def _cipher(self):
        raise NotImplementedError

    def __repr__(self):
        return '%s(%d-bit key)' % (type(self).__name__, len(self.key) * 8)


class DesCrypto(CryptoAlgo):
    """Single DES-CBC (8-byte block, 8-byte key)."""

    def _cipher(self):
        if len(self.key) != 8:
            raise Scp80Error('single DES needs an 8-byte key (%d given)'
                             % len(self.key))
        return DES.new(self.key, DES.MODE_CBC, self.iv)


class Des3Crypto(CryptoAlgo):
    """Triple DES-CBC (8-byte block, 16-byte two-key or 24-byte three-key)."""

    def _cipher(self):
        if len(self.key) not in (16, 24):
            raise Scp80Error('triple DES needs a 16 or 24-byte key (%d given)'
                             % len(self.key))
        return DES3.new(self.key, DES3.MODE_CBC, self.iv)


class AesCrypto(CryptoAlgo):
    """AES-CBC (16-byte block, 16/24/32-byte key)."""

    blocksize = 16

    def _cipher(self):
        if len(self.key) not in (16, 24, 32):
            raise Scp80Error('AES needs a 16, 24 or 32-byte key (%d given)'
                             % len(self.key))
        return AES.new(self.key, AES.MODE_CBC, self.iv)


class AuthAlgo(object):
    """The CC of a secured packet: a CBC-MAC or an AES-CMAC.

    ``sign`` pads to ``blocksize`` and returns the trailing ``sig_len``
    octets; ``check_sig`` recomputes and compares, raising
    :class:`Scp80Error` on a mismatch (TS 102 225 §5.1.1).
    """

    blocksize = 8
    sig_len = 8

    def __init__(self, key):
        self.key = bytes(key)
        if not self.key:
            raise Scp80Error('the authentication key is empty')

    def sign(self, data):
        return self._sign(pad_to_multiple(data, self.blocksize))

    def check_sig(self, data, cc_received):
        cc = self.sign(data)
        if bytes(cc_received) != cc:
            raise Scp80Error('received CC/RC %s != computed %s'
                             % (bytes(cc_received).hex(), cc.hex()))

    def _sign(self, padded):
        raise NotImplementedError

    def __repr__(self):
        return '%s(%d-bit key)' % (type(self).__name__, len(self.key) * 8)


class DesCbcMac(AuthAlgo):
    """DES-CBC-MAC: the last 8 octets of the CBC encryption (zero IV)."""

    def _sign(self, padded):
        if len(self.key) != 8:
            raise Scp80Error('single DES needs an 8-byte key (%d given)'
                             % len(self.key))
        ciph = DES.new(self.key, DES.MODE_CBC, bytes(self.blocksize)).encrypt(padded)
        return ciph[-self.sig_len:]


class Des3CbcMac(AuthAlgo):
    """3DES-CBC-MAC: the last 8 octets of the CBC encryption (zero IV)."""

    def _sign(self, padded):
        if len(self.key) not in (16, 24):
            raise Scp80Error('triple DES needs a 16 or 24-byte key (%d given)'
                             % len(self.key))
        ciph = DES3.new(self.key, DES3.MODE_CBC,
                        bytes(self.blocksize)).encrypt(padded)
        return ciph[-self.sig_len:]


class AesCmac(AuthAlgo):
    """AES-CMAC truncated to 8 octets (``mac_len=8``).

    CMAC needs no block padding of its own, so ``blocksize`` is 1 and
    :meth:`AuthAlgo.sign` pads nothing.
    """

    blocksize = 1

    def _sign(self, padded):
        if len(self.key) not in (16, 24, 32):
            raise Scp80Error('AES needs a 16, 24 or 32-byte key (%d given)'
                             % len(self.key))
        cmac = CMAC.new(self.key, ciphermod=AES, mac_len=self.sig_len)
        cmac.update(padded)
        return cmac.digest()[-self.sig_len:]


CRYPT_ALGOS = {
    'single_des': DesCrypto,
    'triple_des_cbc2': Des3Crypto,
    'triple_des_cbc3': Des3Crypto,
    'aes_cbc': AesCrypto,
}

AUTH_ALGOS = {
    'single_des': DesCbcMac,
    'triple_des_cbc2': Des3CbcMac,
    'triple_des_cbc3': Des3CbcMac,
    'aes_cmac': AesCmac,
}

_CRYPT_NIBBLES = {name: nib for nib, name in CRYPT_ALGO.items()}
_AUTH_NIBBLES = {name: nib for nib, name in AUTH_ALGO.items()}
# The no-security '00' byte codes no algorithm at all; TS 102 225 §5.1.2 gives
# the nibble 0 the name 'implicit'.
_CRYPT_NIBBLES['implicit'] = 0
_AUTH_NIBBLES['implicit'] = 0


# ---- SPI / keyset ----------------------------------------------------------

def _as_int(value, what):
    """An ``int`` from an ``int`` or a hex string (bytes are raw octets)."""
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(bytes(value), 'big')
    try:
        return int(str(value).strip(), 16)
    except (TypeError, ValueError):
        raise Scp80Error('%s must be an integer or a hex string: %r'
                         % (what, value))


def spi_from_bytes(spi1, spi2):
    """Decode the two SPI octets into the SCP80 field map.

    ``spi1``/``spi2`` are integers (``0x16``) or their hex string (``'16'``).
    The map has the keys the command builder and the response decoder use:
    ``counter``, ``ciphering``, ``rc_cc_ds``, ``por_in_submit``,
    ``por_shall_be_ciphered``, ``por_rc_cc_ds``, ``por``.

    Operation bit order (TS 102 225 §5.1.1, Table 1): SPI1.b5b4 = counter
    handling, b3 = ciphering, b2b1 = RC/CC/DS; SPI2.b6 = PoR in an SMS-SUBMIT,
    b5 = the PoR shall be ciphered, b4b3 = the response RC/CC/DS, b2b1 = the
    PoR request (whose value ``11`` is reserved and refused).
    """
    spi1 = _as_int(spi1, 'spi1')
    spi2 = _as_int(spi2, 'spi2')
    return {
        'counter': CNTR_REQ[(spi1 >> 3) & 0x03],
        'ciphering': bool(spi1 & 0x04),
        'rc_cc_ds': RC_CC_DS[spi1 & 0x03],
        'por_in_submit': bool(spi2 & 0x20),
        'por_shall_be_ciphered': bool(spi2 & 0x10),
        'por_rc_cc_ds': RC_CC_DS[(spi2 >> 2) & 0x03],
        'por': _por_request(spi2 & 0x03),
    }


def _por_request(value):
    """The PoR request name; SPI2.b2b1 = ``11`` is reserved (TS 102 225 §5.1.1)."""
    try:
        return POR_REQ[value]
    except KeyError:
        raise Scp80Error('invalid PoR request value %d (reserved)' % value)


def sig_len_for(spi):
    """The RC/CC/DS length of a packet: 0 none, 4 RC (CRC-32), 8 CC.

    A ``ds`` (digital signature) packet has no length defined by the specs, so
    it is refused - exactly like pySim's ``OtaDialect._compute_sig_len``.
    """
    mode = spi['rc_cc_ds']
    if mode == 'no_rc_cc_ds':
        return 0
    if mode == 'rc':
        return 4
    if mode == 'cc':
        return 8
    raise Scp80Error('Invalid rc_cc_ds: %s' % mode)


@dataclass
class Keyset(object):
    """The SCP80 key material and counter of one packet.

    ``kic``/``kid`` are the keys, ``kic_idx``/``kid_idx`` the keyset number
    (bits b8-b5 of the KIc/KID byte) and ``algo_crypt``/``algo_auth`` the
    algorithm names from :data:`CRYPT_ALGO`/:data:`AUTH_ALGO` (``'implicit'``
    for the no-security ``00`` byte).  :meth:`from_bytes` is the constructor
    the request paths use.
    """

    kic: bytes = b''
    kid: bytes = b''
    kic_idx: int = 0
    kid_idx: int = 0
    cntr: int = 0
    algo_crypt: str = 'implicit'
    algo_auth: str = 'implicit'

    def __post_init__(self):
        self.kic = bytes(self.kic)
        self.kid = bytes(self.kid)

    @classmethod
    def from_bytes(cls, spi1, spi2, kic, kid, cntr, kic_key, kid_key):
        """Build a keyset from the two KIc/KID bytes and their keys.

        TS 102 225 §5.1.2/A.2: the low nibble of the byte names the
        algorithm, the high nibble the keyset number.  ``'00'`` is a legal
        KIc byte when no ciphering is applied (SPI1.b3=0) and a legal KID byte
        when no RC/CC/DS is applied (SPI1.b2b1=00) - such a packet carries no
        algorithm at all, which is coded ``'implicit'``.  An unknown nibble is
        refused only when the algorithm is actually used, because otherwise
        its key is never touched.
        """
        kic_b = _as_int(kic, 'kic')
        kid_b = _as_int(kid, 'kid')
        spi = spi_from_bytes(spi1, spi2)
        algo_crypt = CRYPT_ALGO.get(kic_b & 0x0F)
        algo_auth = AUTH_ALGO.get(kid_b & 0x0F)
        needs_crypt = spi['ciphering'] or spi['por_shall_be_ciphered']
        needs_auth = (spi['rc_cc_ds'] != 'no_rc_cc_ds'
                      or spi['por_rc_cc_ds'] != 'no_rc_cc_ds')
        if algo_crypt is None:
            if needs_crypt or (kic_b & 0x0F):
                raise Scp80Error('Unsupported KIc algorithm nibble %02X'
                                 % (kic_b & 0x0F))
            algo_crypt = 'implicit'
        if algo_auth is None:
            if needs_auth or (kid_b & 0x0F):
                raise Scp80Error('Unsupported KID algorithm nibble %02X'
                                 % (kid_b & 0x0F))
            algo_auth = 'implicit'
        cntr = _as_int(cntr, 'cntr') if cntr not in (None, '') else 0
        return cls(kic=_key_bytes(kic_key, 'kic_key'), kid=_key_bytes(kid_key, 'kid_key'),
                   kic_idx=kic_b >> 4, kid_idx=kid_b >> 4, cntr=cntr,
                   algo_crypt=algo_crypt, algo_auth=algo_auth)

    @property
    def crypt(self):
        """The ciphering algorithm instance (raises when none is coded)."""
        return _algo(CRYPT_ALGOS, self.algo_crypt, self.kic, 'crypt')

    @property
    def auth(self):
        """The authentication algorithm instance (raises when none is coded)."""
        return _algo(AUTH_ALGOS, self.algo_auth, self.kid, 'auth')

    @property
    def kic_byte(self):
        """The KIc octet: keyset number b8-b5, algorithm b4-b1."""
        return (self.kic_idx << 4) | _CRYPT_NIBBLES[self.algo_crypt]

    @property
    def kid_byte(self):
        """The KID octet: keyset number b8-b5, algorithm b4-b1."""
        return (self.kid_idx << 4) | _AUTH_NIBBLES[self.algo_auth]


def _key_bytes(value, what):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return bytes.fromhex(str(value or '').strip())
    except ValueError:
        raise Scp80Error('%s is not valid hex: %r' % (what, value))


def _algo(registry, name, key, kind):
    if name not in registry:
        raise Scp80Error('no %s algorithm for key byte nibble %r'
                         % (kind, name))
    return registry[name](key)


# ---- command packet --------------------------------------------------------

def _encode_cmd(keyset, spi, tar, apdu):
    """The bare SCP80 command packet (no unciphered CPL fix-up).

    Port of the legacy 3.x server's ``_encode_cmd_unlimited`` - the coding
    of pySim's ``OtaDialectSms.encode_cmd`` without its single-SMS length
    refusal.  Layout (TS 102 225 §5.1.1, TS 31.115 §4.2)::

        CPL(2) CHL(1) SPI(2) KIc(1) KID(1) TAR(3) CNTR(5) PCNTR(1)
        [RC(4) | CC(8)] secured data

    ``CHL`` counts SPI..RC/CC/DS (13 + the signature length); the RC/CC is
    computed over ``CPL CHL SPI KIc KID TAR CNTR PCNTR secured data``
    (including the ciphering padding, TS 31.115 §4.2); when ciphering, only
    ``CNTR PCNTR [RC/CC] secured data`` is encrypted and the CPL is re-wrapped
    around the header plus the ciphertext.
    """
    tar = bytes(tar)
    if len(tar) != 3:
        raise Scp80Error('a TAR is 3 octets (%d given)' % len(tar))
    len_sig = sig_len_for(spi)
    pad_cnt = 0
    apdu = bytes(apdu)
    if spi['ciphering']:
        # The padding makes CNTR..secured data a whole number of blocks; the
        # count is carried in PCNTR (TS 102 225 §5.1.1).
        len_cipher = 6 + len_sig + len(apdu)
        padding = get_padding(len_cipher, keyset.crypt.blocksize)
        pad_cnt = len(padding)
        apdu += padding

    # CHL = octets from (and including) SPI to the end of RC/CC/DS:
    # 13 == SPI(2) + KIc(1) + KID(1) + TAR(3) + CNTR(5) + PCNTR(1).
    chl = 13 + len_sig
    part_head = (bytes([chl]) + spi_to_bytes(spi)
                 + bytes([keyset.kic_byte, keyset.kid_byte]) + tar)
    part_cnt = keyset.cntr.to_bytes(5, 'big') + bytes([pad_cnt])
    cpl_body = part_head + part_cnt + apdu
    cpl = len(cpl_body) + len_sig
    cpl_body = cpl.to_bytes(2, 'big') + cpl_body

    if spi['rc_cc_ds'] == 'cc':
        cc = keyset.auth.sign(cpl_body)
        body = part_cnt + cc + apdu
    elif spi['rc_cc_ds'] == 'rc':
        crc32 = zlib.crc32(cpl_body) & 0xffffffff
        body = part_cnt + crc32.to_bytes(4, 'big') + apdu
    elif spi['rc_cc_ds'] == 'no_rc_cc_ds':
        body = part_cnt + apdu
    else:
        raise Scp80Error('Invalid rc_cc_ds: %s' % spi['rc_cc_ds'])

    if spi['ciphering']:
        out = part_head + keyset.crypt.encrypt(body)
        return len(out).to_bytes(2, 'big') + out
    return part_head + body


def spi_to_bytes(spi):
    """The two SPI octets of a field map (the inverse of :func:`spi_from_bytes`).

    Useful for a caller that changes one field of a decoded SPI (e.g. asking
    for a PoR) before building a packet.
    """
    return bytes([_spi1_byte(spi), _spi2_byte(spi)])


def _spi1_byte(spi):
    """Re-encode the first SPI octet from the field map."""
    return (((CNTR_REQ_REVERSE[spi['counter']] & 0x03) << 3)
            | (0x04 if spi['ciphering'] else 0x00)
            | (RC_CC_DS_REVERSE[spi['rc_cc_ds']] & 0x03))


def _spi2_byte(spi):
    """Re-encode the second SPI octet from the field map."""
    return ((0x20 if spi['por_in_submit'] else 0x00)
            | (0x10 if spi['por_shall_be_ciphered'] else 0x00)
            | ((RC_CC_DS_REVERSE[spi['por_rc_cc_ds']] & 0x03) << 2)
            | (POR_REQ_REVERSE[spi['por']] & 0x03))


def encode_cmd(keyset, spi, tar, apdu):
    """A SCP80 command packet of any size (bytes).

    :func:`_encode_cmd` plus the CPL octets of the unciphered forms: pySim's
    encoder drops them, but CPL is part of the RC/CC/DS input (TS 31.115
    §4.2) and is required for a concatenated download (Table 1 NOTE / §4.3),
    where the ``CHL``-to-end range exceeds one SM.  CPL counts the octets
    from the CHL octet to the last octet of the secured data, so it equals
    the length of the unciphered ``CHL..secured data`` range.

    The result is byte-identical to the existing server's
    ``_build_secured_packet`` for the same keyset/SPI/TAR/C-APDU.
    """
    out = _encode_cmd(keyset, spi, tar, apdu)
    if not spi['ciphering'] and (spi['rc_cc_ds'] != 'no_rc_cc_ds'
                                 or len(out) > SCP80_SINGLE_BYTES):
        out = len(out).to_bytes(2, 'big') + out
    return out


def build_secured_packet(spi1, spi2, kic, kid, tar, cntr, apdu,
                         kic_key, kid_key):
    """Command packet from hex/int parameters: ``(packet bytes, spi dict)``.

    The convenience form of :func:`encode_cmd` for callers that hold the hex
    strings the request/API layer deals in (the existing server's
    ``_build_secured_packet``, same argument order and semantics).
    """
    keyset = Keyset.from_bytes(spi1, spi2, kic, kid, cntr, kic_key, kid_key)
    spi = spi_from_bytes(spi1, spi2)
    return encode_cmd(keyset, spi, _as_bytes(tar, 'tar'),
                      _as_bytes(apdu, 'apdu')), spi


def split_secured_packet(pkt, include_cpi=True):
    """Split a command packet into SMS user-data parts (TS 31.115 §4.3).

    Every part plus its UDH stays within the 140-octet SMS user data: a
    single SM may carry :data:`SCP80_SINGLE_BYTES`, the first part of a
    concatenated command is smaller because its UDH also carries the CPI IE,
    the following ones get :data:`SCP80_NEXT_BYTES`.

    ``include_cpi=False`` is the form without the OTA CPI IE (the capacities
    then are the full 140 for a single SM and the concat-only UDH size
    afterwards) - a single unprotected SM keeps the CHL-first form.
    """
    pkt = bytes(pkt)
    single_max = SCP80_SINGLE_BYTES if include_cpi else 140
    if len(pkt) <= single_max:
        return [pkt]
    first = SCP80_FIRST_BYTES if include_cpi else SCP80_NEXT_BYTES
    parts = [pkt[:first]]
    rest = pkt[first:]
    while rest:
        parts.append(rest[:SCP80_NEXT_BYTES])
        rest = rest[SCP80_NEXT_BYTES:]
    return parts


# ---- response packet -------------------------------------------------------

@dataclass
class Response(object):
    """A parsed SCP80 response packet (TS 102 225 §5.2 Table 3).

    ``response_status`` is the name from :data:`RESPONSE_STATUS` (the raw
    integer for an unassigned value), ``tar``/``cntr``/``cc_rc``/
    ``secured_data`` are the packet's byte fields (with a ``*_hex`` spelling
    available for each), ``pcntr`` the number of padding octets that were
    stripped from ``secured_data``, and ``rpl``/``rhl`` the two length fields.
    """

    response_status: object
    tar: bytes = b''
    cntr: bytes = b''
    pcntr: int = 0
    rpl: int = 0
    rhl: int = 0
    cc_rc: bytes = b''
    secured_data: bytes = b''
    raw: bytes = b''

    @property
    def tar_hex(self):
        return self.tar.hex().upper()

    @property
    def cntr_hex(self):
        return self.cntr.hex().upper()

    @property
    def cc_rc_hex(self):
        return self.cc_rc.hex().upper()

    @property
    def secured_hex(self):
        return self.secured_data.hex().upper()

    def __getitem__(self, key):
        """Dict-style access, for callers written against the old dict."""
        if not hasattr(self, key):
            raise KeyError(key)
        return getattr(self, key)


def _as_bytes(value, what):
    """Bytes from a bytes-like object or a hex string."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return bytes.fromhex(str(value or '').strip())
    except ValueError:
        raise Scp80Error('%s is not valid hex: %r' % (what, value))


def decode_resp(keyset, spi, data):
    """Parse and verify a response packet (the PoR).

    ``data`` is the whole response packet as bytes or a hex string, starting
    with the UDH (``02 71 00`` = UDHL 2, the RPI IE ``71`` with length 0)::

        UDHL RPI IEDL | RPL(2) RHL(1) TAR(3) CNTR(5) PCNTR(1) STS(1)
        [RC(4) | CC(8)] secured data

    With ``spi['por_shall_be_ciphered']`` the octets from CNTR on are
    deciphered and the ``PCNTR`` padding is stripped from the secured data;
    ``spi['por_rc_cc_ds']`` selects the verification - no check (an RC/CC must
    then be absent), the CC (CBC-MAC/CMAC over UDH + RPL..STS + secured data)
    or the RC (CRC-32 of the same range).  A malformed packet or a failed
    check raises :class:`Scp80Error`; the caller decides whether that means
    "no PoR" (the existing server's ``_decode_por`` returns None then).

    Not parsed here: the response-scripting template of TS 102 226 §5.2.2
    (``AB``/``AF``) - the caller inspects :attr:`Response.secured_data`, e.g.
    with :func:`parse_compact`.
    """
    data = _as_bytes(data, 'response')
    if not data or data[0] != 0x02:
        raise Scp80Error('the response packet does not start with UDHL 2')
    try:
        udh, remainder = sms.UserDataHeader.from_bytes(data)
    except sms.SmsError as exc:
        raise Scp80Error('malformed response UDH: %s' % exc) from exc
    if not udh.has_ie(0x71):
        raise Scp80Error('the RPI IE (0x71) is missing from the UDH')

    rpl, rhl, tar, cntr, pcntr, status, cc_rc, secured = _parse_resp_header(remainder)
    if spi['por_shall_be_ciphered']:
        # RPL/RHL/TAR stay plain, everything from CNTR on is ciphered.  A
        # ciphertext that is not a whole number of blocks is a malformed
        # packet, not a caller error: the cipher raises ValueError there, and
        # our contract is Scp80Error (Scp80Error itself is a ValueError, so
        # the algorithm lookup happens outside the try).
        crypt = keyset.crypt
        try:
            plain = remainder[:6] + crypt.decrypt(remainder[6:])
        except ValueError as exc:
            raise Scp80Error('the ciphered response is %d octets from CNTR on '
                             'and cannot be deciphered: %s'
                             % (len(remainder) - 6, exc)) from exc
        rpl, rhl, tar, cntr, pcntr, status, cc_rc, secured = _parse_resp_header(plain)
        if pcntr:
            if pcntr > len(secured):
                raise Scp80Error('PCNTR %d exceeds the secured data (%d octets)'
                                 % (pcntr, len(secured)))
            secured = secured[:-pcntr]
        remainder = plain

    len_sig = rhl - 10
    if len_sig < 0:
        raise Scp80Error('RHL %d is below the response header length 10' % rhl)
    if spi['por_rc_cc_ds'] == 'no_rc_cc_ds':
        if len_sig:
            raise Scp80Error('no RC/CC/DS was requested, but %d octets are '
                             'present' % len_sig)
    elif spi['por_rc_cc_ds'] == 'cc':
        # UDH + RPL/RHL/TAR/CNTR/PCNTR/STS are part of the CC input, and so is
        # the secured data as it travelled (padding included).
        keyset.auth.check_sig(data[:len(data) - len(remainder)] + remainder[:13]
                              + remainder[13 + len_sig:], cc_rc)
    elif spi['por_rc_cc_ds'] == 'rc':
        crc = zlib.crc32(data[:len(data) - len(remainder)] + remainder[:13]
                         + remainder[13 + len_sig:]) & 0xffffffff
        if cc_rc != crc.to_bytes(4, 'big'):
            raise Scp80Error('received RC %s != computed %s'
                             % (cc_rc.hex(), crc.to_bytes(4, 'big').hex()))
    else:
        raise Scp80Error('Invalid por_rc_cc_ds: %s' % spi['por_rc_cc_ds'])

    return Response(response_status=RESPONSE_STATUS.get(status, status),
                    tar=tar, cntr=cntr, pcntr=pcntr, rpl=rpl, rhl=rhl,
                    cc_rc=cc_rc, secured_data=secured, raw=data)


def _parse_resp_header(data):
    """Peel RPL/RHL/TAR/CNTR/PCNTR/STS/RC-CC and the secured data."""
    data = bytes(data)
    if len(data) < 13:
        raise Scp80Error('the response packet is %d octets (13 needed for the '
                         'header)' % len(data))
    rpl = int.from_bytes(data[0:2], 'big')
    rhl = data[2]
    tar = data[3:6]
    cntr = data[6:11]
    pcntr = data[11]
    status = data[12]
    len_sig = rhl - 10
    if len_sig < 0 or 13 + len_sig > len(data):
        raise Scp80Error('RHL %d does not fit the %d-octet packet'
                         % (rhl, len(data)))
    cc_rc = data[13:13 + len_sig]
    return rpl, rhl, tar, cntr, pcntr, status, cc_rc, data[13 + len_sig:]


def parse_compact(secured_data, cmd_len=None):
    """The compact remote-management response (TS 102 226 §5.1.2 Table 5.1).

    ``number_of_commands`` (the number of commands executed inside the command
    script), ``last_status_word`` and ``last_response_data``, both as
    lowercase hex like pySim's ``CompactRemoteResp``.  Returns ``None`` when
    the data cannot be that structure: fewer than three octets, or a command
    count above ``cmd_len`` - each command needs at least one octet, so a
    larger count proves the secured data is the receiving application's own
    (TS 102 226 §4) rather than a compact response.
    """
    data = _as_bytes(secured_data, 'secured data')
    if len(data) < 3:
        return None
    count = data[0]
    if cmd_len is not None and count > int(cmd_len):
        return None
    return {
        'number_of_commands': count,
        'last_status_word': data[1:3].hex(),
        'last_response_data': data[3:].hex(),
    }


# ---- counters --------------------------------------------------------------

def counter_tracked(spi1):
    """Whether the packet asks the card to check the counter.

    TS 102 225 §5.1.1: SPI1.b5b4 = ``01``/``10``/``11`` request a counter
    check; with ``00`` the field is "present, ignored, never updated" - the
    card keeps no replay value, so no counter may be advanced or persisted
    (a keyless ``SPI1 00`` send must leave every stored counter untouched).
    """
    try:
        return ((_as_int(spi1, 'spi1') >> 3) & 0x03) != 0
    except Scp80Error:
        return False


def increment_counter_hex(counter_hex):
    """``counter + 1`` in the same hex width, wrapping at that width.

    The SCP80 counter is 5 octets / 10 hex digits (TS 102 225 §5.1.1), so a
    10-digit counter wraps after ``FFFFFFFFFF``.  A value that is not a plain
    hex string is returned unchanged (the caller's input is echoed, never
    silently replaced).
    """
    text = str(counter_hex or '').strip().upper()
    try:
        value = int(text, 16)
    except ValueError:
        return counter_hex
    width = len(text)
    return '%0*X' % (width, (value + 1) & ((1 << (4 * width)) - 1))
