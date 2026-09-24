"""Fast card initialization for pysim-simple-server.

pySim's ``init_card()`` performs several physical card resets: one per profile
candidate tried by ``CardProfile.pick()`` plus one at the end of
``RuntimeState.__init__``, and ``PysimApp.equip()`` resets yet again. On common
readers each disconnect/connect costs around a second, so the stock path spends
most of its time re-establishing a clean state (MF selected) that can also be
restored in software.

This module mirrors ``pySim.app.init_card()`` with those resets removed: all
profile probes run back-to-back on the same connection and the runtime state
uses a software reset. It is the default init/equip path; ``--full-pysim-init``
restores pysim's stock behavior, and the explicit ``equip``/``reset`` commands
keep a real reconnect/physical reset.
"""

import operator
import sys

from smartcard.Exceptions import CardConnectionException, NoCardException

from pySim.cards import CardBase, SimCardBase, UiccCardBase, card_detect
from pySim.commands import SimCardCommands
from pySim.exceptions import ProtocolError, SwMatchError
from pySim.filesystem import CardApplication, CardModel
from pySim.profile import CardProfile
from pySim.runtime import RuntimeState
from pySim.ts_102_221 import CardProfileUICC
from pySim.utils import all_subclasses

import pySim.euicc

from .server import _tlog


class FastRuntimeState(RuntimeState):
    """RuntimeState whose reset() restores software state (selects MF) instead
    of power-cycling the card. Use hard_reset() for an explicit reset."""

    def reset(self, cmd_app=None):
        try:
            return self.soft_reset(cmd_app)
        except (SwMatchError, ProtocolError) as e:
            sys.stderr.write('FAST-RESET: soft reset failed (%s), falling back to physical reset\n' % e)
            return self.hard_reset(cmd_app)

    def soft_reset(self, cmd_app=None):
        for lchan_nr in list(self.lchan.keys()):
            self.lchan[lchan_nr].scc.scp = None
            if lchan_nr == 0:
                continue
            del self.lchan[lchan_nr]
        self.adm_verified = False
        try:
            atr = self.card._scc.get_atr()
        except Exception:
            atr = None
        if cmd_app:
            cmd_app.lchan = self.lchan[0]
        self.lchan[0].select('MF', cmd_app)
        self.lchan[0].selected_adf = None
        self.identity['ATR'] = atr
        return atr

    def hard_reset(self, cmd_app=None):
        return super().reset(cmd_app)


def pick_profile_no_reset(scc):
    """Like CardProfile.pick(), but without a physical reset between
    candidates. Each probe selects its own discriminating file, so a reset only
    costs a reconnect without changing the outcome."""
    original_reset = scc.reset_card
    scc.reset_card = lambda: None
    try:
        profiles = sorted(all_subclasses(CardProfile), key=operator.attrgetter('ORDER'))
        for p in profiles:
            if p.match_with_card(scc):
                return p()
        return None
    finally:
        scc.reset_card = original_reset


def _restore_mf_after_probe(scc):
    """Probing can leave an ADF (e.g. ISD-R on an eUICC) selected.  The
    RuntimeState construction selects MF by FID, which some cards refuse from
    within an ADF (6A82), so restore it here: the cheap select keeps the
    reset-free path for normal cards, the physical reset covers the rest."""
    try:
        scc.select_file('3f00')
    except SwMatchError:
        sys.stderr.write('FAST-INIT: MF restore after probing failed; physical reset\n')
        scc.reset_card()


def init_card_fast(sl, skip_card_init=False, wait=True):
    """Replacement for pySim.app.init_card() that avoids redundant resets.

    ``wait`` performs the single disconnect/connect of this init (explicit
    equip passes True; startup already connects via wait_for_card). If probing
    leaves the card in a state the software reset cannot clear, retry once
    after a physical reset."""
    try:
        return _init_card_once(sl, skip_card_init, wait)
    except (SwMatchError, ProtocolError) as e:
        sys.stderr.write('FAST-INIT: %s; retrying after physical reset\n' % e)
        sl.reset_card()
        return _init_card_once(sl, skip_card_init, wait=False)
    except (CardConnectionException, NoCardException) as e:
        # PC/SC link error during the first exchange (e.g. the card was
        # swapped a moment ago): release the connection, wait for the card
        # and retry once.
        sys.stderr.write('FAST-INIT: link error (%s); retrying after reconnect\n' % e)
        try:
            sl.disconnect()
        except Exception:
            pass
        return _init_card_once(sl, skip_card_init, wait=True)


def _init_card_once(sl, skip_card_init, wait):
    scc = SimCardCommands(transport=sl)
    if wait:
        sl.wait_for_card(3)
    if skip_card_init:
        return None, CardBase(scc)

    generic_card = False
    card = card_detect(scc)
    if card is None:
        card = SimCardBase(scc)
        generic_card = True

    profile = pick_profile_no_reset(scc)
    if profile is None:
        return None, card

    # A successful probe may leave an ADF selected (e.g. ISD-R on an eUICC);
    # RuntimeState selects MF by FID and would fail on cards that refuse that
    # from within an ADF.
    _restore_mf_after_probe(scc)

    if generic_card and isinstance(profile, CardProfileUICC):
        card._adm_chv_num = 0x0A

    if isinstance(profile, CardProfileUICC):
        for app_cls in all_subclasses(CardApplication):
            if hasattr(app_cls, '_' + app_cls.__name__ + '__intermediate'):
                continue
            profile.add_application(app_cls())
        if generic_card:
            card = UiccCardBase(scc)

    rs = FastRuntimeState(card, profile)

    CardModel.apply_matching_models(scc, rs)

    sl.set_sw_interpreter(rs)

    isd_r = rs.mf.applications.get(pySim.euicc.AID_ISD_R.lower(), None)
    if isd_r:
        rs.lchan[0].select_file(isd_r)
        try:
            rs.identity['EID'] = pySim.euicc.CardApplicationISDR.get_eid(scc)
        except SwMatchError:
            pass
        finally:
            # rs.reset() tries the reset-free soft reset first and escalates
            # to a physical reset when MF cannot be selected from the ADF.
            rs.reset()

    return rs, card


def do_equip_fast(app):
    """Explicit equip: one real reconnect (wait_for_card) then reset-free init.
    PysimApp.equip() unregisters the old command sets itself after the new init
    succeeds, so a failed init leaves the previous card state intact."""
    rs, card = init_card_fast(app.sl, wait=True)
    app.equip(card, rs)


def do_reset_fast(app):
    """Explicit reset: always a physical card reset."""
    if app.rs is None:
        app.card._scc.reset_card()
        atr = app.card._scc.get_atr()
    else:
        atr = app.rs.hard_reset(app)
    app.poutput('Card ATR: %s' % atr)


def install(app):
    """Route the pySim-shell equip/reset commands through the fast paths."""
    def _do_equip(statement):
        _tlog('do_equip_fast: start')
        do_equip_fast(app)
        _tlog('do_equip_fast: done')

    def _do_reset(statement):
        _tlog('do_reset_fast: start')
        do_reset_fast(app)
        _tlog('do_reset_fast: done')

    app.do_equip = _do_equip
    app.do_reset = _do_reset
