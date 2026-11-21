# coding=utf-8
"""Card-type detection and the minimal card initialisation.

Detection is a spec probe ladder, never a card-model branch:

1. ``SELECT ADF.USIM`` (AID ``A0000000871002``) - a ``9000`` means the card
   carries the UICC application: kind ``uicc``, CAT CLA ``80``
   (TS 102 221 §11.2 - UICC/USIM CAT commands use CLA ``80``).
2. otherwise ``SELECT MF`` - by the ISO form first (TS 102 221 Table 11.2),
   then by the TS 51 011 §9.2.1 form (CLA ``A0``, P1/P2 ``00``) for a 2G SIM:
   kind ``sim``, CAT CLA ``A0`` (TS 151 011).
3. on a UICC, a best-effort ``SELECT`` of the eUICC ISD-R AID
   (``A0000005591010FFFFFFFF8900000100``) flags ``euicc`` - still UICC-based
   (CLA ``80``) per SGP.22.

:func:`init_card` is the minimal init: connect/reset, read the ATR, detect,
select MF.  The TERMINAL PROFILE and the proactive-command drain are
deliberately **not** part of this phase - they own the CAT session and belong
to the CAT/HTTP phase, so this module must not start one.

Pure apart from the transport it is handed (a
:class:`simple_server.transport.PcscLink` or a fake with
``send_apdu``/``get_atr``/``reset_card``).
"""

from dataclasses import dataclass, field

from simple_server.commands import FileError, FileOps

__all__ = [
    'AID_USIM', 'AID_ISD_R', 'CardError', 'CardType', 'detect_card',
    'init_card', 'read_atr',
]

AID_USIM = 'A0000000871002'
AID_ISD_R = 'A0000005591010FFFFFFFF8900000100'

# CAT CLA per card class (TS 102 221 §11.2 / TS 151 011): a UICC session uses
# 80, a 2G SIM session A0.  This is **not** the file-access CLA, which stays
# 00 for both (see FileOps).
CLA_UICC = '80'
CLA_SIM = 'a0'


class CardError(Exception):
    """The card could not be classified or initialised."""


@dataclass
class CardType(object):
    """The detected card class.

    ``kind`` is ``uicc``/``sim``/``euicc`` (``unknown`` for a
    ``skip_init`` placeholder), ``cla`` the CAT class byte (``80``/``a0``) and
    ``aids`` the application identifiers the probe found present.
    """

    kind: str
    cla: str
    aids: list = field(default_factory=list)
    atr: bytes = None


def read_atr(transport):
    """The card's ATR as bytes (``b''`` when the transport cannot report it)."""
    getter = getattr(transport, 'get_atr', None)
    if getter is None:
        return b''
    atr = getter()
    return bytes(atr) if atr is not None else b''


def _connect(transport):
    """Reset/connect the link; returns True when it had a way to do so."""
    reset = getattr(transport, 'reset_card', None)
    if reset is not None:
        reset()
        return True
    connect = getattr(transport, 'connect', None)
    if connect is not None:
        connect()
        return True
    return False


def detect_card(transport):
    """Classify the card: ``uicc``/``sim``/``euicc`` plus its CAT CLA.

    Raises :class:`CardError` when neither the USIM nor any SELECT MF form is
    accepted - a card that answers nothing is not silently treated as a SIM.
    """
    aids = []
    kind = None
    try:
        FileOps(transport).select_adf(AID_USIM)
    except FileError:
        pass
    else:
        aids.append(AID_USIM)
        kind = 'uicc'

    if kind is None:
        try:
            FileOps(transport).select_mf()
        except FileError:
            # A 2G SIM answers the TS 51 011 §9.2.1 form: CLA A0, P1/P2 00.
            try:
                FileOps(transport, cla='a0', sel_fcp=False).select_file('3F00')
            except FileError as exc:
                raise CardError(
                    'cannot classify the card: ADF.USIM, SELECT MF (ISO) and '
                    'SELECT MF (TS 51 011) were all refused (%s)'
                    % exc) from exc
            kind = 'sim'
        else:
            kind = 'uicc'

    if kind == 'uicc':
        # An eUICC is UICC-based; the ISD-R probe is best-effort (a card
        # without it answers 6A82 and stays a plain UICC).
        try:
            FileOps(transport).select_adf(AID_ISD_R)
        except FileError:
            pass
        else:
            aids.append(AID_ISD_R)
            kind = 'euicc'

    return CardType(kind=kind,
                    cla=CLA_UICC if kind in ('uicc', 'euicc') else CLA_SIM,
                    aids=aids)


def init_card(transport, skip_init=False):
    """The minimal init: connect/reset, read the ATR, detect, select MF.

    ``skip_init`` still connects and reads the ATR but skips detection and the
    MF select, returning a placeholder :class:`CardType` (``kind='unknown'``,
    UICC CAT CLA) - the ``--no-card-init`` case.

    Not done here by design (a later phase): the TERMINAL PROFILE and the
    proactive-command drain.  Starting a CAT session needs the terminal
    profile and a FETCH/TERMINAL RESPONSE loop, so doing half of it here would
    leave the card with a session nobody answers.
    """
    _connect(transport)
    atr = read_atr(transport)
    if skip_init:
        return CardType(kind='unknown', cla=CLA_UICC, aids=[], atr=atr)
    card = detect_card(transport)
    card.atr = atr
    uicc = card.kind in ('uicc', 'euicc')
    ops = FileOps(transport, cla='00' if uicc else 'a0',
                  status_cla=card.cla, sel_fcp=uicc)
    try:
        ops.select_mf()
    except FileError:
        # Probing may leave an ADF (ISD-R) selected and some cards refuse the
        # MF select by FID from within it.  The generic recovery is a physical
        # reset - never a card-specific branch (see pysim_simple_server's
        # fastinit `_restore_mf_after_probe`).
        if not _connect(transport):
            raise
        ops.select_mf()
    return card
