# coding=utf-8
"""The test-script runner: one run per server, no module globals.

A script is a list of **actions** (send an ENVELOPE, a raw APDU, a secured
packet, read/write a file, poll STATUS, drain the proactive queue, select a
menu item) and **expectations** (fetch the command the card announced, check
it and answer it with a scripted TERMINAL RESPONSE).  The run executes in a
worker thread and *owns the card* while it runs: the other card endpoints
answer ``409`` (``TEST_BLOCKED_PATHS``), the background STATUS poll skips
(:meth:`simple_server.server.ServerState.poll_once` watches
``state.test_running``) and a batch install refuses to start (and vice
versa).

Provenance: a behavioural port of the legacy server's test runner
(the legacy 3.x ``server.py``: ``_test_run_*``, ``_test_step_*``,
``_test_*_entry``, ``_test_log``/``_test_heartbeat``, ``_test_watchdog_*``,
``_test_load_*``, ``_ensure_adm``, ``_test_preset_error``,
``_TEST_BLOCKED_PATHS``).  The module-level state (``_TEST_RUN``,
``_TEST_LOCK``, ``_TEST_THREAD``, ``_TEST_RUNNING``, the watchdog globals)
became one :class:`TestRunner` per :class:`~simple_server.server.ServerState`,
so two servers in one process are independent and the tests need no global
reset.  The pure engine (validation, matching, the scripted TERMINAL
RESPONSE) stays in :mod:`simple_server.shared.testscript`, the semantic event
builders in :mod:`simple_server.shared.events`, the script store in
:mod:`simple_server.shared.test_scripts` - the pysim-free modules moved
verbatim from the legacy tree, never re-derived.  The card exchanges reuse the ported machinery:
:class:`~simple_server.cat_session.CatSession` (STATUS/FETCH/TERMINAL
RESPONSE, ENVELOPE, the menu cache, the proactive log),
:func:`simple_server.ota.decode_por` / ``spi1_for_tar`` / ``format_apdu``,
:func:`simple_server.scp80.build_secured_packet` /
``counter_tracked`` / ``increment_counter_hex``,
:class:`simple_server.commands.FileOps` and
:meth:`ServerState._send_secured_packet`.

Scope: the **single-script** path (3c-3d-1) and the **suites** (3c-3d-2) -
the ordered ``setup``/``members``/``teardown`` references executed in one
card session, with the per-member ``on_fail``, the suite ``require_adm``, the
optional ``start_reset`` and the shared SCP80 counter state.  The cross-store
rules (references, move/copy, the reconciliation) live in
:mod:`simple_server.suites`; the stores themselves are
:mod:`simple_server.shared.test_suites` and
:mod:`simple_server.shared.test_scripts`.  Documented
simplifications vs the legacy, each also named where it applies:

* ``file-read`` of a transparent EF whose FCP carries no file size fails the
  step (the legacy read the whole file through pySim, which fell back to the
  selected file's size); the explicit-length read fallback is the 3d item
  recorded in ``docs/pysim-phaseout.md``.
* The start-of-suite card reset (``start_reset``) calls the state's own
  :meth:`~simple_server.server.ServerState.reset` - the physical reset plus
  the full re-initialization of this build (the legacy's
  ``_card_reset_reinit`` also probed the pySim-shell command-set
  registration, which this build has no shell for).
* The run-log lines go through :mod:`logging` (the module logger) rather than
  a raw ``sys.stderr.write``; the buffered excerpt in the status payload is
  byte-identical.  Nothing here configures logging (no ``basicConfig`` in the
  module), so until the 3d entry point configures INFO for the
  ``simple_server`` logger - the recorded decision that also covers the
  RAM/OTA diagnostics - these lines do not reach ``simple_lastrun.log`` at the
  default WARNING level.

Spec references (the same the legacy runner cites): TS 102 221 §11.2 (the CAT
command APDUs), §11.2.3 (FETCH), §11.2.4 (TERMINAL RESPONSE), §7.4.2.1 (the
``91XX`` "command pending" announce); TS 102 223 §6.3 (the proactive
dialogue), §6.8.0/§6.8.1 (the TERMINAL RESPONSE structure and the echoed
command details), §6.8.4 (the POLL INTERVAL echo), §6.4.15 (PROVIDE LOCAL
INFORMATION), §8.12.0 (the result grid), §8.18 (the REFRESH File List);
TS 102 225 §5.1.1/A.1/A.2 (the SPI1 counter check, the per-keyset counters,
the KIc/KID keyset number), §5.2 Table 3 (the response packet); TS 102 226
§5.1.2 (the compact response) and §5.2.1 (the Command Scripting template);
TS 31.115 §4.2/§4.4 (the PoR transport: the ENVELOPE exchange or
SEND SHORT MESSAGE); TS 23.040 §9.2.2.2 (the SMS-SUBMIT the applet's PoR
travels in).
"""

import json
import logging
import re
import threading
import time

from simple_server.shared import events
from simple_server.shared import presets as presets_store
from simple_server.shared import testscript

from simple_server import cat
from simple_server import cat_session
from simple_server import commands
from simple_server import ota
from simple_server import proactive
from simple_server import scp80
from simple_server import sms
from simple_server import text as text_codec
from simple_server import tr

log = logging.getLogger(__name__)

__all__ = [
    'TestRunner', 'TestRunBusy', 'TestRunError', 'TestRunNotFound',
    'TestAdmError', 'TEST_LOG_MAX', 'TEST_STEP_TIMEOUT', 'WATCHDOG_TICK',
    'TEST_LOAD_MAX', 'TEST_MEMBER_LOG_MAX', 'TEST_KIND_LABELS',
    'TEST_BLOCKED_PATHS',
    'empty_state', 'load_state', 'load_iterations', 'check_result', 'entry',
    'step_label', 'action_checks', 'por_check_results', 'packet_rejected',
    'counter_consumed', 'POR_REJECTED', 'sms_tpdu_fields', 'parse_file_list',
    'command_type', 'validate_script', 'preset_error', 'request_blocked',
    'member_prefix', 'member_status', 'preset_counters_snapshot', 'suite_state',
]

#: Buffered run-log lines kept for the report's excerpt (the stderr stream -
#: here the logger - stays complete).
TEST_LOG_MAX = 600

#: Run-log lines kept per suite member in the report (the legacy
#: ``_TEST_MEMBER_LOG_MAX``): the shared buffer is trimmed, so a member's
#: excerpt is bounded on its own too.
TEST_MEMBER_LOG_MAX = 120

#: Seconds without runner progress that mark the card stuck (the legacy
#: ``_TEST_STEP_TIMEOUT``).  pyscard's ``SCardTransmit`` has no timeout, so a
#: silent card would hold the runner inside a step; the watchdog turns that
#: into a failed run and tries to cancel the transmit.
TEST_STEP_TIMEOUT = 30

#: The watchdog's tick (seconds).
WATCHDOG_TICK = 1.0

#: The load test's iteration cap: 1,000,000,000 is inside the JS safe-integer
#: range (2^53-1), so the PWA's counters and ETA stay exact; the practical
#: ceiling is the run time.
TEST_LOAD_MAX = 1000000000

#: The report label of each action kind (the legacy ``_TEST_KIND_LABELS``).
TEST_KIND_LABELS = {
    'envelope': 'ENVELOPE(Event Download)', 'event': 'ENVELOPE(Event Download)',
    'menu-select': 'ENVELOPE(Menu Selection)',
    'file-write': 'UPDATE FILE', 'file-read': 'READ FILE', 'apdu': 'APDU',
    'scp80': 'SCP80', 'status': 'STATUS', 'cleanup': 'CLEANUP',
    'proactive-drain': 'DRAIN',
}

#: Card-touching endpoints refused while a script owns the card (the legacy
#: ``_TEST_BLOCKED_PATHS``, verbatim - the paths this build does not serve yet
#: are listed too, so the guard is already right when they land).
TEST_BLOCKED_PATHS = frozenset([
    '/api/command', '/api/cardinfo', '/api/tree', '/api/select', '/api/read',
    '/api/write', '/api/apdu', '/api/verify-adm', '/api/send-ota',
    '/api/ram-install', '/api/ram-install-app', '/api/cap-compat',
    '/api/tar-probe', '/api/counter-probe',
    '/api/sp-verify', '/api/menu-select',
    '/api/menu-respond', '/api/event-send', '/api/net-sim',
    '/api/net-state-refresh', '/api/status-poll', '/api/rescue',
    '/api/terminal-profile', '/api/poll-toggle', '/api/esim/chip',
    '/api/esim/profiles', '/api/esim/notifications', '/api/esim/profile',
    '/api/scp81/bip', '/api/scp81/queue', '/api/scp81/psk-map',
    '/api/scp81/gen-install', '/api/scp81/log-clear',
    '/api/bip/control', '/api/bip/log-clear',
])

#: The proactive command names -> type codes, for a script's name form (the
#: legacy ``_test_command_type``).
COMMAND_TYPES = {name.upper(): code
                 for code, name in proactive.PROACTIVE_TYPE_NAMES.items()}

#: The BIP proactive command types the terminal answers itself (OPEN/CLOSE
#: CHANNEL, SEND/RECEIVE DATA, GET CHANNEL STATUS) - TS 102 223 §6.4.27-31.
_BIP_CMD_TYPES = cat_session.BIP_COMMAND_TYPES

#: The drain's short result aliases for the report line (TS 102 223 §8.12.0;
#: 0x12 is "no response from the user", which the legacy mislabelled).
_DRAIN_ALIAS = {0x00: 'ok', 0x10: 'cancel', 0x11: 'back', 0x12: 'timeout',
                0x22: 'refused'}


class TestRunBusy(RuntimeError):
    """A run cannot start: another run or a batch install owns the card (409)."""


class TestRunNotFound(RuntimeError):
    """The request names a script id the store does not hold (404)."""


class TestRunError(ValueError):
    """A refused run request (validation, a missing preset, the ADM gate).

    A ``ValueError`` on purpose: the handler's dispatch already answers that
    class ``400``, and the PWA shows the message verbatim.
    """


class TestAdmError(TestRunError):
    """The ADM gate refused the run; ``adm`` carries the structured state."""

    def __init__(self, message, adm=None):
        super().__init__(message)
        self.adm = dict(adm or {})


# ---- the pure helpers (the legacy module functions) ------------------------

def command_type(name):
    """A proactive command name -> its type code (``None`` when unknown)."""
    return COMMAND_TYPES.get((name or '').upper())


def validate_script(raw):
    """The script store's validator: the runner's own engine pass.

    The store calls this on every mutation, so what is stored is what the
    runner accepts (a load keeps an entry that no longer validates - it is
    served, fails at run start with this message and can be fixed).
    """
    return testscript.normalise_script(raw, command_type)


def load_state(iterations, suite=False):
    """The ``load`` block of a run: disabled for a normal single run.

    ``suite`` lets the PWA label a suite load test even after its state is
    cleared (a successful load run keeps no suite report, v3.37.0).
    """
    enabled = iterations > 1
    return {'enabled': enabled, 'suite': bool(suite),
            'iterations': iterations if enabled else None,
            'iteration': 0, 'done': 0, 'failed_iteration': None,
            'started': time.time() if enabled else None, 'finished': None,
            'elapsed_ms': None, 'avg_ms': None}


def load_iterations(body):
    """The run's iteration count (1 when omitted); ``ValueError`` when bad.

    The endpoint answers 400: a bad count must not silently start one
    iteration (or a million) instead of what the operator typed.
    """
    raw = (body or {}).get('iterations')
    if raw is None or raw == '':
        return 1
    try:
        count = int(raw)
    except (TypeError, ValueError):
        raise ValueError('iterations must be an integer')
    if isinstance(raw, float) and raw != count:
        raise ValueError('iterations must be an integer')
    if count < 1 or count > TEST_LOAD_MAX:
        raise ValueError('iterations must be between 1 and %d' % TEST_LOAD_MAX)
    return count


def empty_state():
    """A run state with no run (the status payload the PWA renders).

    ``kind`` discriminates the run: ``script`` for a single-script run,
    ``suite`` for the suite runner; ``suite`` itself is the member-report
    block (``None`` for a single script).
    """
    return {
        'running': False, 'stop': False, 'kind': None, 'name': None,
        'status': None, 'session': None, 'index': 0, 'total': 0, 'steps': [],
        'preset': None, 'script_id': None,
        'scp80_counter': None, 'scp80_counters': {},
        'started': None, 'finished': None, 'error': None,
        # the stuck-card watchdog: `heartbeat` is the last runner progress
        # mark; `stuck` makes the final status an error even when the stop
        # flag ended the loop
        'stuck': False, 'heartbeat': None,
        # the suite runs: None for a single script
        'suite': None, 'log': [], 'log_prefix': '', 'adm': None,
        'load': load_state(1),
    }


# ---- the suite report helpers (the legacy suite functions) ------------------

def member_prefix(index, total, member):
    """The run-log prefix of a suite member: its position, role and name, so
    the shared log stream (and the report's excerpt) stays readable."""
    total = total or 1
    tag = member.get('name') or member.get('script_id') or 'script'
    role = member.get('role') or 'member'
    return '[%d/%d %s %s] ' % (index + 1, total, role, tag)


def member_status(steps, stopped, stuck=False):
    """A suite member's verdict from its step levels.

    An ``error`` stops that script; a stop request marks the member
    ``stopped``.  A stuck card is an ``error`` even though the watchdog's
    stop flag ended the script (a stuck card is not a user stop).
    """
    if stuck:
        return 'error'
    if stopped:
        return 'stopped'
    levels = [step.get('status') for step in steps]
    if 'error' in levels:
        return 'error'
    if 'warning' in levels:
        return 'warning'
    return 'ok'


def preset_counters_snapshot(preset):
    """``{kvn: counter}`` of a preset's keysets - the suite report's SCP80
    counter before/after columns (the store persists what the card consumed)."""
    out = {}
    for keyset in ota.preset_keysets(preset or {}):
        kvn = presets_store.keyset_kvn(keyset)
        if kvn:
            out[str(kvn)] = str(keyset.get('cntr') or '').upper()
    return out


def suite_state(suite, members, preset, preset_id, session, reset_info=None):
    """The run's ``suite`` report skeleton: one pending member record per
    resolved ``(entry, script)`` plus the run-level metadata (preset, session,
    the SCP80 counters before, the start reset).  Shared by the starter and
    the tests."""
    return {
        'id': suite.get('id'),
        'name': suite.get('name') or 'test suite',
        'require_adm': bool(suite.get('require_adm')),
        'start_reset': bool(suite.get('start_reset')),
        'reset': reset_info or None,
        'started': time.time(),
        'session_start': session, 'session_end': None,
        'session_changed': False, 'preset_id': preset_id,
        'preset': (preset or {}).get('name') or (preset or {}).get('iccid'),
        'counters_before': preset_counters_snapshot(preset),
        'counters_after': None, 'summary': None,
        'members': [
            {'script_id': entry['script_id'], 'name': (script or {}).get('name'),
             'role': entry['role'], 'on_fail': entry['on_fail'],
             'status': 'pending', 'steps': [], 'log': [],
             'log_truncated': False, 'note': None,
             'started': None, 'finished': None, 'counters_after': {}}
            for entry, script in members],
    }


def check_result(label, ok, expected, actual, level, detail=None):
    """One check row of a step entry (the legacy ``_test_check_result``)."""
    res = {'label': label, 'ok': bool(ok), 'expected': str(expected),
           'actual': str(actual), 'level': level}
    if detail:
        res['detail'] = detail
    return res


def step_label(step):
    """``(kind, label)`` of a step as the report shows it."""
    if step['type'] == 'expect':
        ctype = step['command'].get('type')
        kind = (step['command'].get('name')
                or (('0x%02X' % ctype) if ctype is not None else 'ANY'))
        return kind, 'EXPECT ' + kind
    kind = step['kind']
    return kind, (step.get('label') or TEST_KIND_LABELS.get(kind, kind))


def entry(step, index):
    """A fresh step entry (the live row the status endpoint serves)."""
    kind, label = step_label(step)
    return {'index': index, 'type': step['type'], 'kind': kind,
            'label': label, 'status': 'running', 'checks': [], 'note': None,
            'sent': None, 'sw': None, 'data': None, 'ms': None,
            'started': time.time()}


def por_check_results(spec, por, level, prefix='PoR'):
    """The decoded-PoR assertion rows (``status``/``sw``/``data``).

    Shared by the scp80 action's ``check.por`` object and the expectation's
    ``por`` content check: both assert the decoded PoR of *this* exchange,
    whether it arrived in the ENVELOPE answer or in a SEND SHORT MESSAGE.
    """
    checks = []
    if por is None:
        checks.append(check_result(
            prefix, False, 'a decoded PoR',
            'no PoR in this command (or no scp80 step before it)', level))
        return checks
    decoded = por.get('decoded') or {}
    if 'status' in spec:
        actual = str(por.get('response_status') or '')
        want = spec['status']
        ok = (actual.lower() == want.lower()
              or (want.lower() == 'ok' and actual == 'por_ok'))
        checks.append(check_result(prefix + ' status', ok, want,
                                   actual or '(none)', level))
    if 'sw' in spec:
        actual = str(decoded.get('last_status_word') or '')
        ok = testscript.match_value(spec['sw'], actual)
        detail = None
        if not ok and por.get('response_type') == 'raw':
            detail = ("the response is application data - an applet's own TAR "
                      "has no R-APDU SW; assert it with 'data'")
        checks.append(check_result(prefix + ' SW', ok, spec['sw']['value'],
                                   actual or '(none)', level, detail=detail))
    if 'data' in spec:
        actual = str(decoded.get('last_response_data') or '')
        checks.append(check_result(
            prefix + ' data', testscript.match_value(spec['data'], actual),
            spec['data']['value'], actual or '(none)', level))
    return checks


def action_checks(step, sw, data, por, kind):
    """The action step's checks: the SW, the optional data and the PoR.

    A data check is skipped (reported ``ok``) when the SW check already
    failed: the response bytes of a refused exchange are not the step's
    business.  The PoR check is scp80-only: ``any`` accepts no PoR, ``none``
    demands its absence, ``ok`` names ``por_ok`` and an object asserts the
    decoded fields.
    """
    check = step['check']
    level = step['on_fail']
    checks = []
    sw_ok = testscript.match_value(check['sw'], sw)
    checks.append(check_result('SW', sw_ok, check['sw']['value'],
                               sw or '(none)', level))
    if check.get('data'):
        if sw_ok:
            checks.append(check_result('Data',
                                       testscript.match_value(check['data'],
                                                              data),
                                       check['data']['value'], data or '(none)',
                                       level))
        else:
            checks.append(check_result('Data', True, check['data']['value'],
                                       '(not checked - SW mismatch)', 'ok'))
    if kind == 'scp80':
        want = check.get('por')
        if isinstance(want, dict):
            checks.extend(por_check_results(want, por, level))
        elif want != 'any':
            if want == 'none':
                ok = por is None
                actual = 'none' if por is None else 'present'
            else:
                ok = bool(por) and por.get('response_status') == 'por_ok'
                actual = (por or {}).get('response_status') or 'none'
            checks.append(check_result('PoR', ok, want, actual, level))
    return checks


#: The decoded-PoR verdicts that **prove** the card refused the packet: the
#: counter checks (TS 102 225 §5.2 Table 3 status bytes ``02``-``04``), the
#: security and verification failures (``01``, ``05``-``07``) and the
#: TAR/security-level refusals (``09``, ``0A``).  Only these block the counter
#: advance (:func:`counter_consumed`); ``more_time_needed`` (``08``) is not a
#: refusal - it stays "unclear" and advances.
POR_REJECTED = frozenset([
    'rc_cc_ds_failed', 'cntr_low', 'cntr_high', 'cntr_blocked',
    'ciphering_error', 'undefined_security_error', 'insufficient_memory',
    'tar_unknown', 'insufficient_security_level',
])


def packet_rejected(por):
    """True when the decoded PoR **proves** the card refused the packet."""
    if por is None:
        return False
    return str(por.get('response_status') or '') in POR_REJECTED


def counter_consumed(por, sw):
    """Whether the SCP80 counter advances after this packet (three-way rule).

    The counter moves unless there is **positive evidence of rejection**
    (TS 102 225 Annex A.1 - the card associates one counter with each keyset):

    * clear acceptance - a ``por_ok`` / ``actual_response_sms_submit``
      verdict, or a ``9000``/``91XX`` ENVELOPE SW - advances;
    * a **clear rejection** - a decoded PoR naming a counter/security refusal
      (:data:`POR_REJECTED`) - does not (the card did not consume it);
    * an **unclear** outcome - no PoR, an undecodable PoR, an ambiguous status
      word (``6Axx``/``6Fxx``, a warning ``62xx``/``63xx`` without data) or a
      transport error after the packet may have reached the card - advances
      too: an unnecessarily skipped counter value costs one value the card
      accepts, while not advancing a counter the card *did* consume leaves
      every later packet answering ``cntr_low`` and the operation stuck.

    ``sw`` is the ENVELOPE's status word; an empty one means no exchange
    happened at all (a pre-send failure), so nothing is consumed.
    """
    if not _sw(sw):
        return False
    return not packet_rejected(por)


def keyset_complete(keyset):
    """A usable keyset: both key bytes, both keys and a counter."""
    return bool(keyset.get('kic') and keyset.get('kid')
                and keyset.get('kicKey') and keyset.get('kidKey')
                and keyset.get('cntr'))


def preset_error(script, preset):
    """The SCP80 precondition, or ``None``.

    The SCP80 steps need at least one complete keyset in the card preset; a
    step may pick the keyset (``kvn``), the TAR, SPI1 and SPI2 - the KIc/KID
    keys and the counters stay preset-owned (TS 102 225 Annex A: the counters
    are keyset-linked).
    """
    preset = preset or {}
    keysets = ota.preset_keysets(preset)
    if not keysets:
        return 'SCP80 preset is incomplete: no keyset is defined'
    if not [ks for ks in keysets if keyset_complete(ks)]:
        return ('SCP80 preset is incomplete: a keyset needs KIc, KID, both keys '
                'and a counter')
    for index, step in enumerate(script['steps']):
        if step['type'] != 'action' or step['kind'] != 'scp80':
            continue
        params = step['params']
        if params.get('kvn') is not None:
            want = int(params['kvn'])
            keyset = next((ks for ks in keysets
                           if presets_store.keyset_kvn(ks) == want), None)
            if keyset is None:
                return ('step %d: keyset %d is not defined in the preset'
                        % (index + 1, want))
            if not keyset_complete(keyset):
                return ('step %d: keyset %d is incomplete (KIc, KID, both '
                        'keys and a counter)' % (index + 1, want))
        else:
            # The step's TAR picks its default keyset (v3.26.0): it must exist
            # and be complete; a TAR whose MSL requires security must have one.
            tar = params.get('tar') or presets_store.role_tar(preset, 'isd')
            tar_kvn = presets_store.tar_keyset(preset, tar)
            if tar_kvn:
                keyset = next((ks for ks in keysets
                               if presets_store.keyset_kvn(ks) == tar_kvn),
                              None)
                if keyset is None:
                    return ('step %d: keyset %d (the default of TAR %s) is not '
                            'defined in the preset'
                            % (index + 1, tar_kvn, tar))
                if not keyset_complete(keyset):
                    return ('step %d: keyset %d (the default of TAR %s) is '
                            'incomplete (KIc, KID, both keys and a counter)'
                            % (index + 1, tar_kvn, tar))
            elif (presets_store.tar_msl(preset, tar) or '') not in ('', '00'):
                return ('step %d: TAR %s needs a default keyset (its MSL %s '
                        'requires security) - set it in the Cards tab'
                        % (index + 1, tar, presets_store.tar_msl(preset, tar)))
        tar = params.get('tar') or presets_store.role_tar(preset, 'isd')
        if not tar:
            return 'step %d: no TAR (neither in the step nor in the preset)' \
                % (index + 1)
        if not (params.get('spi1') or presets_store.tar_msl(preset, tar)):
            return ('step %d: no SPI1 for TAR %s (neither in the step nor as '
                    'its MSL in the preset)' % (index + 1, tar))
    return None


def request_blocked(path, running):
    """True when a card endpoint must answer 409: a run owns the card."""
    if not running:
        return False
    return path.split('?', 1)[0] in TEST_BLOCKED_PATHS


def sms_tpdu_fields(raw):
    """The SEND SHORT MESSAGE TPDU's assertable fields (TS 23.040 §9.2.2).

    ``{mti, tpdu, [da, pid, dcs, udl, ud]}``; ``None`` when the command
    carries no TPDU, ``{mti, tpdu}`` for a non-SUBMIT TPDU.  ``da`` is the
    digit string (the odd-count pad stripped), ``ud`` the whole TP-UD hex.
    """
    tpdu = sms.find_sms_tpdu(raw)
    if not tpdu:
        return None
    mti = tpdu[0] & 0x03
    out = {'mti': mti, 'tpdu': bytes(tpdu).hex().upper()}
    if mti != 1:
        return out
    try:
        submit = sms.SmsSubmit.from_bytes(tpdu)
    except Exception:
        return None
    out.update({
        'da': str(submit.da or '').rstrip('fF'),
        'pid': '%02X' % submit.pid,
        'dcs': '%02X' % submit.dcs,
        'udl': int(submit.udl),
        'ud': bytes(submit.ud or b'').hex().upper(),
    })
    return out


def parse_file_list(raw):
    """The REFRESH File List (TS 102 223 §8.18; tag ``12``/``92``) as paths.

    The value is the file count followed by concatenated 2-byte FIDs; every
    path starts with the MF (``3F``), which delimits the entries.  ``[]`` when
    the command carries no File List.
    """
    try:
        value = proactive.cmd_tlv(proactive.command_tlv_map(raw), 0x12)
    except Exception:
        return []
    if not value:
        return []
    data = value[1:]
    paths = []
    current = bytearray()
    for index in range(0, len(data) - len(data) % 2, 2):
        fid = data[index:index + 2]
        if len(fid) < 2:
            break
        if fid[0] == 0x3F and current:
            paths.append(bytes(current).hex().upper())
            current = bytearray()
        current += fid
    if current:
        paths.append(bytes(current).hex().upper())
    return paths


def _hex(value):
    """Bytes (or nothing) as upper-case hex - the report's data form."""
    return (bytes(value or b'')).hex().upper()


def _sw(value):
    """A status word (bytes or hex) as the report's upper-case hex."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    return str(value or '').strip().upper()


def _pending_length(sw):
    """The FETCH length a ``91XX`` answer names (``None`` when it is not 91xx)."""
    sw = _sw(sw)
    if not sw.startswith('91') or len(sw) != 4:
        return None
    return int(sw[2:], 16)


# ---- the runner ------------------------------------------------------------

class TestRunner(object):
    """One server's test-script run: the state, the worker and the executors.

    :class:`~simple_server.server.ServerState` owns one runner; nothing here
    is a module global.  ``state`` supplies the card session
    (``transport``/``cat``), the card lock, ``card_session``, the preset
    store, the ADM latch and ``_send_secured_packet``.

    **Lifecycle.**  :meth:`start` resets the run state and spawns the worker
    (or, with ``spawn=False``, leaves the seeded state for a synchronous
    :meth:`execute` - the tests' seam); the worker finalises the status and
    clears ``state.test_running`` in every exit path.  :meth:`snapshot` is the
    JSON-safe status payload, :meth:`stop` sets the stop flag,
    :meth:`clear` drops a finished report.

    **The card lock.**  Every step runs under ``state.card_lock`` (the
    endpoints take it too), while the run state is guarded by its own
    :attr:`_lock` - the status endpoint must answer while a step is blocked
    inside a card exchange.

    **The stuck-card watchdog.**  pyscard's transmit has no timeout, so a
    silent card would hold the runner (and the lock) inside a step.  The
    watchdog watches :attr:`_run`'s ``heartbeat`` (beaten at every step
    start/finish and per attempt of a bounded loop); after
    :attr:`step_timeout` seconds it sets ``stuck``/``error``/``stop`` and
    tries to cancel the transmit by disconnecting the PC/SC link on a daemon
    thread (pcsc-lite serializes the disconnect behind the pending transmit,
    so a synchronous call would block the watchdog).  A stuck run finalises
    as ``error``, never ``stopped``.
    """

    def __init__(self, state, step_timeout=TEST_STEP_TIMEOUT):
        self.state = state
        self.step_timeout = step_timeout
        self._lock = threading.RLock()
        self._run = empty_state()
        self._thread = None
        self._watchdog = None
        self._watchdog_stop = threading.Event()
        self._watchdog_aborted = None
        #: The current suite member's log lines (a private buffer beside the
        #: shared, trimmed ``_run['log']`` - the shared buffer may be full
        #: when a member starts, which used to slice the member's excerpt
        #: empty).  ``_member_truncated`` records a drop from the front.
        self._member_log = None
        self._member_truncated = False

    # ---- plumbing -----------------------------------------------------

    def _cat(self):
        """The CAT session of the equipped card (an error when cardless)."""
        session = self.state.cat
        if session is None:
            raise RuntimeError('no card session: equip a card first')
        return session

    @property
    def running(self):
        """True while a run owns the card (the endpoints' guard)."""
        with self._lock:
            return bool(self._run.get('running'))

    def snapshot(self):
        """The status payload: a JSON-safe copy of the whole run state."""
        with self._lock:
            return json.loads(json.dumps(self._run))

    def stop(self):
        """Request a stop: the runner finishes the current step, drains a
        pending command with ``ok`` and reports the run as ``stopped``."""
        with self._lock:
            if self._run.get('running'):
                self._run['stop'] = True
        return self.snapshot()

    def clear(self):
        """Drop a finished report; ``False`` while a run is active."""
        with self._lock:
            if self._run.get('running'):
                return False
            self._run = empty_state()
        return True

    def close(self):
        """Stop the watchdog at server shutdown (bounded join)."""
        self._watchdog_stop.set()
        thread, self._watchdog = self._watchdog, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # ---- the run state helpers ---------------------------------------

    def _heartbeat(self):
        """Mark runner progress for the watchdog.  A bounded STATUS/drain loop
        may legitimately run longer than one exchange, so it beats once per
        attempt; every step beats when it starts and when it finishes."""
        with self._lock:
            self._run['heartbeat'] = time.time()

    def _log(self, message):
        """One semantic run-log line, prefixed with the step number.

        The verification trail (the plaintext C-APDU, the built secured
        packet, the PoR R-APDU, the FETCH/TR of an expectation, the menu
        selection's ENVELOPE) buffered for the report's excerpt; the raw
        transport view stays with the APDU tracer.
        """
        index = self._run.get('index')
        number = (index + 1) if isinstance(index, int) else 0
        prefix = self._run.get('log_prefix') or ''
        line = 'TEST-RUN %sstep %d: %s' % (prefix, number, message)
        log.info('%s', line)
        try:
            with self._lock:
                buffer = self._run.setdefault('log', [])
                buffer.append(line)
                if len(buffer) > TEST_LOG_MAX:
                    del buffer[:len(buffer) - TEST_LOG_MAX]
                if self._member_log is not None:
                    self._member_log.append(line)
                    if len(self._member_log) > TEST_MEMBER_LOG_MAX:
                        del self._member_log[:
                                            len(self._member_log)
                                            - TEST_MEMBER_LOG_MAX]
                        self._member_truncated = True
        except Exception:               # never let a log line fail a run
            pass

    def _entry_update(self, row, fields):
        """Mutate a step entry under the lock (``snapshot`` serializes it)."""
        with self._lock:
            row.update(fields)

    def _finish_entry(self, row, result):
        fields = {'status': result.get('status', 'ok'),
                  'checks': result.get('checks', []),
                  'ms': int((time.time() - (row.get('started')
                                           or time.time())) * 1000)}
        for key in ('sent', 'sw', 'data', 'note', 'por', 'sms', 'counter',
                    'command', 'drained'):
            if result.get(key) is not None:
                fields[key] = result[key]
        self._heartbeat()               # the exchange finished: progress
        self._entry_update(row, fields)

    # ---- the ADM gate --------------------------------------------------

    def _adm_verified_session(self):
        """True when the ADM was verified for the current card session."""
        return (getattr(self.state, 'adm_session', None)
                == getattr(self.state, 'card_session', 0))

    def ensure_adm(self, preset, needed):
        """The run's ADM prerequisite; ``(ok, info, error)``.

        With ``needed`` the ADM must be verified for the current card
        session: a successful verify (the ``/api/verify-adm`` badge or an
        earlier gated run) passes, otherwise the matched preset's ADM key is
        tried **once**.  A failure is never retried automatically - the
        session is stamped (``adm_failed_session``) and the operator verifies
        manually (the badge's verify clears the stamp); every wrong key
        consumes one of the card's attempts.
        """
        session = getattr(self.state, 'card_session', 0)
        info = {'required': bool(needed), 'verified': False}
        if not needed:
            return True, info, None
        if self._adm_verified_session():
            info['verified'] = True
            return True, info, None
        if getattr(self.state, 'adm_failed_session', None) == session:
            info['failed'] = True
            return False, info, ('ADM is required and its verification failed '
                                 'earlier in this card session - verify it '
                                 'manually (the ADM badge) and retry')
        key = re.sub(r'\s', '', str((preset or {}).get('adm') or '')).upper()
        if not key:
            return False, info, ('ADM is required but the matching card preset '
                                 'has no ADM key')
        if not self.state.connected or self.state.transport is None:
            return False, info, 'ADM is required but no card is equipped'
        try:
            with self.state.card_lock:
                result = self.state.verify_adm({'adm': key})
        except Exception as exc:
            return False, info, 'ADM verification failed: %s' % exc
        if result.get('ok'):
            self.state.adm_session = session
            self.state.adm_failed_session = None
            info['verified'] = True
            info['checked'] = True
            log.info('TEST-RUN: ADM verified for this card session')
            return True, info, None
        self.state.adm_failed_session = session
        info['result'] = result
        if result.get('attempts_left') is not None:
            info['attempts_left'] = result['attempts_left']
        if result.get('blocked'):
            info['blocked'] = True
        extra = ''
        if result.get('attempts_left') is not None:
            extra = ' (%d attempt(s) left)' % result['attempts_left']
        elif result.get('blocked'):
            extra = ' (the ADM is blocked - the unblock key is needed)'
        return False, info, ('ADM verification failed (SW %s)%s - verify it '
                             'manually and retry'
                             % (result.get('sw'), extra))

    # ---- the lifecycle -------------------------------------------------

    def _reset(self, script, preset, script_id=None, adm_info=None,
               iterations=1):
        """Seed the run state (the legacy ``_test_run_start``'s update)."""
        with self._lock:
            self._run = empty_state()
            self._run.update({
                'running': True, 'kind': 'script', 'name': script['name'],
                'status': None,
                'session': getattr(self.state, 'card_session', 0),
                'index': 0, 'total': len(script['steps']),
                'started': time.time(), 'heartbeat': time.time(),
                'script_id': script_id,
                'preset': ((preset or {}).get('name')
                           or (preset or {}).get('iccid')),
                'adm': dict(adm_info) if adm_info else {'required': False},
                'load': load_state(iterations),
            })

    def start(self, script, preset, script_id=None, adm_info=None,
              iterations=1, spawn=True):
        """Seed the run state and start the worker (``spawn=False``: tests).

        With ``spawn=False`` the state is seeded and ``state.test_running``
        set, but no thread/watchdog is started - the caller drives
        :meth:`execute` (the synchronous seam).  The run owns the card from
        here: a paused interactive command is answered (the legacy
        ``_finish_pending_menu``) and the background poll skips while
        ``state.test_running`` is true.
        """
        # The ownership flag first: the guard must never be open while the run
        # state is being seeded (a concurrent card request would slip through).
        self.state.test_running = True
        self._reset(script, preset, script_id, adm_info, iterations)
        if iterations > 1:
            log.info('TEST-LOAD: script %r, %d iterations',
                     script.get('name') or script_id or 'script', iterations)
            target, args, name = (self._load_worker,
                                  (script, preset, iterations), 'test-load')
        else:
            target, args, name = (self._worker, (script, preset),
                                  'test-script')
        if not spawn:
            return self.snapshot()
        try:
            self._watchdog_start()
            self._spawn(target, args, name)
        except Exception:
            # A run that never started must not leave the card owned.
            self._resume_poll()
            with self._lock:
                self._run['running'] = False
            raise
        return self.snapshot()

    def _spawn(self, target, args, thread_name):
        """Start the worker: answer a paused command, spawn the thread.

        A run that never starts must not leave the card blocked (the 409
        guard keys off ``test_running``, and no worker would clear it).
        """
        with self.state.card_lock:
            try:
                session = self._cat()
                session.finish_pending_menu()
            except Exception as exc:
                log.warning('TEST-RUN: finishing pending menu failed: %s', exc)
        try:
            self._thread = threading.Thread(target=target, args=args,
                                            name=thread_name, daemon=True)
            self._thread.start()
        except Exception:
            self._thread = None
            self._resume_poll()
            with self._lock:
                self._run['running'] = False
                self._run['status'] = 'error'
                self._run['error'] = 'could not start the run worker'
                self._run['finished'] = time.time()
            raise

    def _resume_poll(self):
        """The run ended: let the background poll run again.

        The poll loop watches ``state.test_running`` itself (nothing to
        re-arm, unlike the legacy's canceled ``threading.Timer``), so this
        only clears the flag and re-syncs the card's polling wishes the run
        may have changed (a POLLING OFF / POLL INTERVAL during a script).
        """
        self.state.test_running = False
        session = self.state.cat
        if session is not None:
            self.state.poll_suspended = bool(
                getattr(session, 'poll_suspended', False))
            self.state._adopt_poll_interval()

    # ---- the worker ----------------------------------------------------

    def execute(self, script, preset):
        """Run the steps and finalise the run state (the worker body).

        Port of the legacy ``_test_run_execute``: the status is the worst step
        level; a stuck card is an ``error`` even though the watchdog's stop
        flag ended the loop, a user stop is ``stopped``.
        """
        try:
            _ctx, stopped = self.run_script(script, preset)
        except Exception as exc:            # a raised runner error
            log.exception('TEST-RUN: the script failed')
            with self._lock:
                self._run['error'] = str(exc)
                self._run['running'] = False
                self._run['finished'] = time.time()
                if not self._run['status']:
                    self._run['status'] = 'error'
            self._resume_poll()
            return
        with self._lock:
            self._run['running'] = False
            self._run['finished'] = time.time()
            if self._run['status'] is None:
                levels = [step.get('status')
                          for step in self._run['steps']]
                if self._run.get('stuck'):
                    # the watchdog's stop flag ended the loop; the run still
                    # failed (a stuck card is not a user stop)
                    self._run['status'] = 'error'
                elif stopped:
                    self._run['status'] = 'stopped'
                elif 'error' in levels:
                    self._run['status'] = 'error'
                elif 'warning' in levels:
                    self._run['status'] = 'warning'
                else:
                    self._run['status'] = 'ok'
        self._resume_poll()

    def _worker(self, script, preset):
        """The single-script worker thread body."""
        try:
            self.execute(script, preset)
        except Exception as exc:
            log.exception('TEST-RUN: the worker failed')
            with self._lock:
                self._run['error'] = str(exc)
                self._run['running'] = False
                self._run['finished'] = time.time()
                if not self._run['status']:
                    self._run['status'] = 'error'
            self._resume_poll()

    # ---- the suites ----------------------------------------------------

    def _reset_suite(self, suite, members, preset, preset_id=None,
                     adm_info=None, reset_info=None, iterations=1):
        """Seed the run state for a suite run (the ``kind`` discriminator)."""
        with self._lock:
            session = getattr(self.state, 'card_session', 0)
            self._run = empty_state()
            self._run.update({
                'running': True, 'kind': 'suite',
                'name': suite.get('name') or 'test suite', 'status': None,
                'session': session, 'index': 0, 'total': 0, 'steps': [],
                'started': time.time(), 'heartbeat': time.time(),
                'script_id': None,
                'preset': ((preset or {}).get('name')
                           or (preset or {}).get('iccid')),
                'suite': suite_state(suite, members, preset, preset_id,
                                     session, reset_info=reset_info),
                'adm': dict(adm_info) if adm_info else {'required': False},
                'load': load_state(iterations, suite=True),
            })

    def start_suite(self, suite, members, preset, preset_id=None,
                    adm_info=None, reset_info=None, iterations=1,
                    spawn=True):
        """Seed a suite run and start the worker (``spawn=False``: tests).

        ``members`` is the resolved, normalised ``[(entry, script)]`` list in
        run order (setup first, teardown last - the store's order);
        ``reset_info`` records the start-of-suite card reset the caller
        already performed.  With ``spawn=False`` the caller drives
        :meth:`execute_suite` (the synchronous seam).
        """
        self.state.test_running = True
        self._reset_suite(suite, members, preset, preset_id, adm_info,
                          reset_info, iterations)
        if iterations > 1:
            log.info('TEST-LOAD: suite %r, %d iterations',
                     suite.get('name') or 'suite', iterations)
            target, args, name = (self._load_suite_worker,
                                  (suite, members, preset, preset_id,
                                   iterations), 'test-load-suite')
        else:
            target, args, name = (self._suite_worker,
                                  (suite, members, preset, preset_id),
                                  'test-suite')
        if not spawn:
            return self.snapshot()
        try:
            self._watchdog_start()
            self._spawn(target, args, name)
        except Exception:
            # A run that never started must not leave the card owned.
            self._resume_poll()
            with self._lock:
                self._run['running'] = False
            raise
        return self.snapshot()

    def suite_execute_once(self, suite, members, preset, preset_id, state,
                           shared=None):
        """One suite iteration; ``(status, stopped, session_changed)``.

        setup -> members in order (per-member ``on_fail``) -> teardown, in
        **one card session, without resets and without resume**.  A stop
        request or a failed member ends the member loop (the rest is marked
        skipped with the reason); a card session change stops the suite and
        skips the rest (flagged); the teardown runs on every stop/failure -
        its purpose is to leave the card ready for the next test - and is
        skipped only when the card is gone.  ``state`` is the
        :func:`suite_state` dict; a load test calls this per iteration.
        """
        session_start = getattr(self.state, 'card_session', 0)
        stopped = False
        session_changed = False
        setup_i = next((i for i, (entry, _script) in enumerate(members)
                        if entry['role'] == 'setup'), None)
        teardown_i = next((i for i, (entry, _script) in enumerate(members)
                           if entry['role'] == 'teardown'), None)
        member_idx = [i for i, (entry, _script) in enumerate(members)
                      if entry['role'] == 'member']
        # The counter state is per card session: one shared dict for the whole
        # suite, so a second SCP80 script starts from what the first consumed
        # (the store persists each packet's counter; the preset dict here is
        # the suite-start snapshot).
        shared = shared if shared is not None else {}

        def run_member(i):
            entry, script = members[i]
            member = state['members'][i]
            with self._lock:
                member['status'] = 'running'
                member['started'] = time.time()
                self._run['steps'] = []
                self._run['index'] = 0
                self._run['heartbeat'] = time.time()
                self._run['total'] = len(script['steps'])
                self._run['log_prefix'] = member_prefix(i, len(members), member)
                self._member_log = []
                self._member_truncated = False
            ctx, member_stopped = self.run_script(script, preset,
                                                  shared=shared)
            with self._lock:
                steps = json.loads(json.dumps(self._run['steps']))
                lines = list(self._member_log or [])
                member.update({
                    'status': member_status(
                        steps, member_stopped,
                        stuck=bool(self._run.get('stuck'))),
                    'steps': steps,
                    'log': lines[-TEST_MEMBER_LOG_MAX:],
                    'log_truncated': bool(self._member_truncated),
                    'finished': time.time(),
                    'counters_after': dict(ctx.get('counters') or {}),
                })
                return member['status'], member_stopped

        def skip_pending(reason):
            with self._lock:
                for member in state['members']:
                    if member['status'] == 'pending':
                        member['status'] = 'skipped'
                        member['note'] = reason

        abort = False
        if setup_i is not None:
            status, setup_stopped = run_member(setup_i)
            if setup_stopped:
                stopped = True
                abort = True
            elif status not in ('ok', 'warning'):
                abort = True
                skip_pending('the setup script failed')
        for i in member_idx:
            if abort:
                break
            with self._lock:
                stop_req = bool(self._run['stop'])
            if stop_req:
                stopped = True
                skip_pending('stopped')
                break
            if getattr(self.state, 'card_session', 0) != session_start:
                session_changed = True
                skip_pending('card session changed (card removed or '
                             're-equipped)')
                break
            status, member_stopped = run_member(i)
            if member_stopped:
                stopped = True
                abort = True
                break
            if (status not in ('ok', 'warning')
                    and members[i][0]['on_fail'] == 'stop'):
                abort = True
        if stopped:
            skip_pending('stopped')
        elif abort:
            skip_pending('a previous script failed (on_fail: stop)')
        if teardown_i is not None:
            if not getattr(self.state, 'card_present', False):
                with self._lock:
                    state['members'][teardown_i]['status'] = 'skipped'
                    state['members'][teardown_i]['note'] = 'no card in the reader'
            else:
                # The teardown runs on every stop - its purpose is to leave
                # the card ready for a new test - so a pending stop request
                # does not cut it short (the run is finishing anyway).
                with self._lock:
                    stop_saved = self._run['stop']
                    self._run['stop'] = False
                try:
                    run_member(teardown_i)
                finally:
                    with self._lock:
                        # A Stop pressed *during* the teardown is a real
                        # request: it must not be discarded with the cleared
                        # flag (a load suite would start another iteration).
                        self._run['stop'] = bool(stop_saved
                                                 or self._run['stop'])
        with self._lock:
            session_end = getattr(self.state, 'card_session', 0)
            state['session_end'] = session_end
            state['session_changed'] = bool(session_changed
                                            or session_end != session_start)
            after = None
            store = getattr(self.state, 'presets', None)
            if preset_id and store is not None:
                after = preset_counters_snapshot(store.get(preset_id))
            state['counters_after'] = after
            statuses = [member['status'] for member in state['members']]
            state['summary'] = {
                'ok': statuses.count('ok'),
                'warning': statuses.count('warning'),
                'error': statuses.count('error'),
                'skipped': statuses.count('skipped'),
                'stopped': bool(stopped or statuses.count('stopped')),
                'session_changed': state['session_changed'],
                'wall_ms': int((time.time()
                                - (state.get('started')
                                   or self._run.get('started')
                                   or time.time())) * 1000),
                'counters_before': state.get('counters_before'),
                'counters_after': after,
                'setup': (state['members'][setup_i]['status']
                          if setup_i is not None else None),
                'teardown': (state['members'][teardown_i]['status']
                             if teardown_i is not None else None),
            }
            if 'error' in statuses:
                status = 'error'
            elif 'warning' in statuses:
                status = 'warning'
            elif stopped or 'stopped' in statuses:
                status = 'stopped'
            else:
                status = 'ok'
        return status, stopped, session_changed

    def execute_suite(self, suite, members, preset, preset_id=None,
                      shared=None):
        """One suite run: one iteration, then the run state is finalised.

        The status is the worst member verdict (a stop request makes it
        ``stopped`` when nothing failed).  A load test calls
        :meth:`suite_execute_once` itself.
        """
        state = self._run.get('suite')
        if state is None:
            state = suite_state(suite, members, preset, preset_id,
                                getattr(self.state, 'card_session', 0))
            with self._lock:
                self._run['suite'] = state
        status, _stopped, _session_changed = self.suite_execute_once(
            suite, members, preset, preset_id, state, shared)
        with self._lock:
            self._run['status'] = status
            self._run['running'] = False
            self._run['finished'] = time.time()
            self._run['log_prefix'] = ''
        self._resume_poll()

    def _suite_error(self, exc):
        """Close a suite run that raised, keeping the report renderable.

        The open member records are closed (a ``running`` one becomes an
        ``error`` with the reason, the ``pending`` ones ``skipped``) and a
        summary is filled, so the PWA can render the report instead of a run
        that never finishes.
        """
        with self._lock:
            self._run['error'] = str(exc)
            self._run['running'] = False
            self._run['finished'] = time.time()
            if not self._run['status']:
                self._run['status'] = 'error'
            self._run['log_prefix'] = ''
            state = self._run.get('suite')
            if state is not None and state.get('summary') is None:
                statuses = []
                for member in state['members']:
                    if member['status'] == 'running':
                        member['status'] = 'error'
                        member['note'] = (member.get('note')
                                          or ('the run failed: %s' % exc))
                    elif member['status'] == 'pending':
                        member['status'] = 'skipped'
                    statuses.append(member['status'])
                state['session_end'] = getattr(self.state, 'card_session', 0)
                state['summary'] = {
                    'ok': statuses.count('ok'),
                    'warning': statuses.count('warning'),
                    'error': statuses.count('error'),
                    'skipped': statuses.count('skipped'),
                    'stopped': False,
                    'session_changed': bool(state.get('session_changed')),
                    'wall_ms': int((time.time()
                                    - (self._run.get('started')
                                       or time.time())) * 1000),
                    'counters_before': state.get('counters_before'),
                    'counters_after': None,
                    'setup': None, 'teardown': None,
                }

    def _suite_worker(self, suite, members, preset, preset_id):
        """The suite worker thread body."""
        try:
            self.execute_suite(suite, members, preset, preset_id)
        except Exception as exc:
            log.exception('TEST-RUN: the suite failed')
            self._suite_error(exc)
        finally:
            self._resume_poll()

    # ---- the load test -------------------------------------------------

    def _load_finalise(self, status, done, iterations, failed, attempted=None):
        """Close a load run: the counters, the timing and the final status.

        Only the failing iteration's steps/log stay (the successful ones were
        discarded).  ``attempted`` counts the started iterations (the
        failing/partial one included), so ``avg_ms`` is a real per-iteration
        time.
        """
        with self._lock:
            now = time.time()
            load = self._run['load']
            started = load.get('started') or self._run.get('started') or now
            elapsed = int((now - started) * 1000)
            attempted = attempted if attempted is not None else done
            load.update({'done': done, 'failed_iteration': failed,
                         'finished': now, 'elapsed_ms': elapsed,
                         'avg_ms': (int(elapsed / attempted)
                                    if attempted else None)})
            self._run['running'] = False
            self._run['finished'] = now
            self._run['status'] = status
            self._run['log_prefix'] = ''
        if failed:
            how = 'failed at iteration %d/%d' % (failed, iterations)
        elif status == 'stopped':
            how = 'stopped after %d/%d' % (done, iterations)
        else:
            how = 'done %d/%d' % (done, iterations)
        log.info('TEST-LOAD: %s (%s)', self._run.get('name') or 'run', how)

    def _load_worker(self, script, preset, iterations):
        """Repeat one script ``iterations`` times (the load test, v3.37.0).

        Successful iterations are discarded (only the counter and the timing
        grow); an error keeps that iteration's steps/log as the report and
        stops the loop; a manual stop ends it after the current iteration; a
        card session change between iterations is an error.  The SCP80
        counter state is shared by every iteration (the in-memory preset copy
        is the run-start snapshot while the store moves on).
        """
        shared = {}
        session = getattr(self.state, 'card_session', 0)
        done = 0
        attempted = 0
        failed = None
        final = 'ok'
        worst = 'ok'
        try:
            for index in range(1, iterations + 1):
                with self._lock:
                    if self._run['stop']:
                        final = 'stopped'
                        break
                    self._run.update({'steps': [], 'index': 0, 'status': None,
                                      'error': None, 'stuck': False,
                                      'heartbeat': time.time(), 'log': [],
                                      'log_prefix': ''})
                    self._run['load'].update({'iteration': index, 'done': done})
                attempted = index
                if getattr(self.state, 'card_session', 0) != session:
                    with self._lock:
                        self._run['error'] = (
                            'the card session changed during the load test '
                            '(card removed or re-equipped)')
                    failed = index
                    final = 'error'
                    break
                _ctx, stopped = self.run_script(script, preset, shared=shared)
                with self._lock:
                    levels = [step.get('status')
                              for step in self._run['steps']]
                    if self._run.get('stuck'):
                        status = 'error'
                    elif stopped:
                        status = 'stopped'
                    elif 'error' in levels:
                        status = 'error'
                    elif 'warning' in levels:
                        status = 'warning'
                    else:
                        status = 'ok'
                    if status in ('error', 'stopped'):
                        if status == 'error':
                            failed = index
                            if not self._run.get('error'):
                                self._run['error'] = ('iteration %d failed'
                                                      % index)
                        final = status
                    else:
                        if status == 'warning':
                            worst = 'warning'
                        done = index
                        self._run['steps'] = []
                        self._run['log'] = []
                        self._run['load']['done'] = done
                if final != 'ok':
                    break
            if final == 'ok':
                final = worst if done == iterations else 'stopped'
            self._load_finalise(final, done, iterations, failed, attempted)
        except Exception as exc:
            # a raised runner error must not leave the run "running" forever
            # (the PWA would poll a dead run and Clear would stay refused)
            log.exception('TEST-LOAD: the worker failed')
            with self._lock:
                self._run['error'] = str(exc)
            if failed is None:
                failed = self._run['load'].get('iteration') or (done + 1)
            self._load_finalise('error', done, iterations, failed, attempted)
        finally:
            self._resume_poll()

    def _load_suite_worker(self, suite, members, preset, preset_id, iterations):
        """Repeat a whole suite ``iterations`` times (the load test, v3.37.0).

        setup/members/teardown per iteration with one shared counter state;
        the same success/failure policy as the script load test: successful
        iterations are discarded (only ``load.done``/timing grow, the suite
        report stays ``None``), the first error keeps that iteration's report
        and stops the loop (an error wins over the stop flag - a stuck card
        sets both), a manual stop ends it after the current iteration, and a
        card session change is an error.
        """
        shared = {}
        session = getattr(self.state, 'card_session', 0)
        done = 0
        attempted = 0
        failed = None
        final = 'ok'
        worst = 'ok'
        try:
            for index in range(1, iterations + 1):
                with self._lock:
                    if self._run['stop']:
                        final = 'stopped'
                        break
                    # the report's counters_before is this iteration's start;
                    # the protocol state (keys/TARs) stays the run-start
                    # snapshot
                    fresh = preset
                    store = getattr(self.state, 'presets', None)
                    if preset_id and store is not None:
                        fresh = store.get(preset_id) or preset
                    state = suite_state(suite, members, fresh, preset_id,
                                        session)
                    self._run.update({'suite': state, 'steps': [], 'index': 0,
                                      'total': 0, 'status': None, 'error': None,
                                      'stuck': False, 'heartbeat': time.time(),
                                      'log': [], 'log_prefix': ''})
                    self._run['load'].update({'iteration': index, 'done': done})
                attempted = index
                if getattr(self.state, 'card_session', 0) != session:
                    with self._lock:
                        self._run['error'] = (
                            'the card session changed during the load test '
                            '(card removed or re-equipped)')
                    failed = index
                    final = 'error'
                    break
                status, stopped, session_changed = self.suite_execute_once(
                    suite, members, preset, preset_id, state, shared)
                if session_changed:
                    with self._lock:
                        self._run['error'] = (
                            'the card session changed during the load test '
                            '(card removed or re-equipped)')
                    failed = index
                    final = 'error'
                    break
                # an error wins over the stop flag: a stuck card sets both,
                # and the report must say it failed, not that the operator
                # stopped it
                if status == 'error':
                    failed = index
                    final = 'error'
                    with self._lock:
                        if not self._run.get('error'):
                            self._run['error'] = 'iteration %d failed' % index
                    break
                if stopped or status == 'stopped':
                    final = 'stopped'
                    break
                if status == 'warning':
                    worst = 'warning'
                with self._lock:
                    done = index
                    self._run['load']['done'] = done
                    self._run['steps'] = []
                    self._run['log'] = []
                    # successful iterations are not tracked (a failure keeps
                    # its own state for the report)
                    self._run['suite'] = None
            if final == 'ok':
                final = worst if done == iterations else 'stopped'
            self._load_finalise(final, done, iterations, failed, attempted)
        except Exception as exc:
            log.exception('TEST-LOAD: the suite worker failed')
            with self._lock:
                self._run['error'] = str(exc)
            if failed is None:
                failed = self._run['load'].get('iteration') or (done + 1)
            self._load_finalise('error', done, iterations, failed, attempted)
        finally:
            self._resume_poll()

    # ---- the watchdog --------------------------------------------------

    def _watchdog_start(self):
        """Start the watchdog thread (idempotent; :meth:`close` stops it)."""
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                          name='test-run-watchdog',
                                          daemon=True)
        self._watchdog.start()

    def _watchdog_loop(self):
        while not self._watchdog_stop.wait(WATCHDOG_TICK):
            try:
                self.watchdog_check()
            except Exception as exc:
                log.warning('TEST-WATCHDOG: %s', exc)

    def watchdog_check(self, now=None):
        """One watchdog pass; the stuck error string when it acted, else None.

        The heartbeat is the authority (beaten at every step start/finish and
        inside the bounded drains), so a stale one means the runner is stuck
        inside a card exchange.  Acts once per heartbeat value, so a second
        tick (or a test's own call) cannot abort twice.
        """
        now = time.time() if now is None else now
        with self._lock:
            if not self._run.get('running'):
                self._watchdog_aborted = None
                return None
            heartbeat = self._run.get('heartbeat')
            if not heartbeat:
                return None
            idle = now - heartbeat
            if idle < self.step_timeout:
                return None
            if self._watchdog_aborted == heartbeat:
                return None
            self._watchdog_aborted = heartbeat
            steps = self._run.get('steps') or []
            current = steps[-1] if steps else None
            index = (current.get('index') if current is not None
                     else self._run.get('index'))
            message = ('the card stopped responding (step %d, no answer for '
                       '%d s)' % ((index or 0) + 1, int(idle)))
            self._run['stuck'] = True
            self._run['error'] = message
            self._run['stop'] = True
        log.warning('TEST-WATCHDOG: %s - trying to cancel the pending '
                    'transmit', message)
        self.state.abort_card_transport()
        return message

    # ---- the step executors -------------------------------------------

    def _terminal_response(self, cmd_num, cmd_type, dev_dst, dev_src, cmd_qual,
                           respond, cd_tag=None):
        """The runner's TERMINAL RESPONSE: the scripted answer plus the
        terminal's own command-specific objects.

        The same objects the terminal's own answers carry, unless the script
        supplied ``raw`` TLVs (which win): a successful PROVIDE LOCAL
        INFORMATION answer takes the PLI dictionary entry for its qualifier
        (or the host clock for the date/time qualifier, TS 102 223 §6.4.15);
        a POLL INTERVAL answer echoes the current interval (§6.8.4).  Without
        this a scripted expectation silently stalled an applet that waited
        for its data (v3.22.2 review).

        The POLL INTERVAL echo keeps the legacy ``84`` Duration tag
        byte-for-byte: the terminal's own builder (``CatSession.build_chain_tr``)
        uses the base ``04`` form per §6.8.0, but the runner's TR has shipped
        the CR-set form since v3.22.2 and the card accepts it.  §6.8.0 says the
        response objects "should" use the CR=0 form (not "shall"), and the
        behaviour's anchors are the legacy test pin (``840201<interval>``) and
        card acceptance - no recorded or golden TERMINAL RESPONSE compares
        these bytes, so the deviation stays documented rather than changed.
        """
        respond = respond or {}
        answer = None
        if not respond.get('raw') and respond.get('result', 0) == 0x00:
            session = self._cat()
            if cmd_type == 0x26 and cmd_qual is not None:
                answer = session.pli_data_hex(cmd_qual) or None
            elif cmd_type == 0x03:
                answer = '840201%02X' % (session.poll_interval & 0xFF)
        return testscript.build_tr(cmd_num, cmd_type, dev_dst, dev_src,
                                   respond, answer_tlvs=answer,
                                   cmd_qual=cmd_qual, cd_tag=cd_tag)

    def _step_tr(self, cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag,
                 respond, raw):
        """The TR of a step for a fetched command, its own BIP commands included.

        The card's BIP commands (0x40-0x44) are handled by the terminal, so
        its channels keep working while a run owns the card (v3.25.3).  The
        script overrides the answer when it carries ``raw`` TLVs or a non-ok
        result (an applet error path to test); everything else goes through
        the scripted answer.
        """
        answer = respond or {}
        if (cmd_type in _BIP_CMD_TYPES and not answer.get('raw')
                and answer.get('result', 0) == 0x00):
            tr = self.state._bip_handler(cmd_num, cmd_type, cmd_qual, raw,
                                         dev_src, dev_dst, cd_tag)
            if tr:
                self._log('BIP: the terminal answered %s (0x%02X)'
                          % (proactive.PROACTIVE_TYPE_NAMES.get(cmd_type, '?'),
                             cmd_type))
                return bytes(tr)
        return self._terminal_response(cmd_num, cmd_type, dev_dst, dev_src,
                                       cmd_qual, respond, cd_tag=cd_tag)

    def _run_status(self, step):
        """The STATUS action: ``attempts`` polls at ``interval_ms``, stopping
        on the step's SW check (so ``91??`` polls until a command appears)."""
        session = self._cat()
        params = step['params']
        data, sw = b'', b''
        used = 0
        for index in range(params['attempts']):
            self._heartbeat()
            data, sw = session.send_status()
            used = index + 1
            self._log('STATUS %d/%d -> %s'
                      % (used, params['attempts'], _sw(sw)))
            if testscript.match_value(step['check']['sw'], _sw(sw)):
                break
            if index + 1 < params['attempts'] and params['interval_ms']:
                time.sleep(params['interval_ms'] / 1000.0)
        return (_hex(data), _sw(sw),
                'STATUS x%d' % used if used > 1 else 'STATUS')

    def _run_file(self, step):
        """READ/UPDATE a file by path; ``(data, sw, sent)``.

        The path resolves through the server's file model (parent-scoped, a
        model-unknown 4-hex FID is the custom-file probe), then
        ``mode``/the FCP's structure picks READ/UPDATE BINARY or
        READ/UPDATE RECORD.  A card-level refusal is reported as its status
        word (the step's SW check names it) instead of raising - that is what
        the legacy ``lchan`` calls did; a resolution failure (a missing file,
        a refused SELECT) still raises and lands in the step's ``on_fail``
        handling.
        """
        state = self.state
        params = step['params']
        ops, _timed = state._file_endpoint_ops()
        info, probe, fcp, _path = state._select_node(
            {'path': params['path']}, ops)
        is_record = fcp.structure in ('linear_fixed', 'cyclic')
        use_record = (params['mode'] == 'record'
                      or (params['mode'] == 'auto' and is_record))
        record = params.get('record') or 1
        sent = params['path']
        try:
            if step['kind'] == 'file-write':
                if use_record:
                    sent = 'UPDATE RECORD %d %s' % (record, params['path'])
                    sw = ops.update_record(record, params['data'])
                else:
                    sent = 'UPDATE BINARY %s' % params['path']
                    sw = ops.update_binary(0, params['data'])
                state._refresh_arr_after_write(info, probe)
                return '', _sw(sw), sent
            if use_record:
                sent = 'READ RECORD %d %s' % (record, params['path'])
                data, sw = ops.read_file(fcp, rec=record)
                return _hex(data), _sw(sw), sent
            sent = 'READ BINARY %s' % params['path']
            data, sw = ops.read_file(fcp)
            return _hex(data), _sw(sw), sent
        except commands.UnexpectedStatusWord as exc:
            return '', _sw(exc.sw), sent

    def _run_scp80(self, step, ctx):
        """The SCP80 action: build/deliver one secured packet, decode the PoR.

        The keyset resolves from the step's ``kvn`` or the TAR's default
        (TS 102 225 Annex A.1/A.2 - the counters are keyset-linked); the
        KIc/KID keys and the counter stay preset-owned, a step may override
        the TAR, SPI1 and SPI2 only.  The packet is delivered with the
        ENVELOPE chain **not** consumed (``handle_proactive=False``): a
        ``91XX`` answer stays pending for the next expectation, which is how a
        script asserts the applet's own command (a PoR in SEND SHORT MESSAGE
        included).  The counter follows the **three-way rule**
        (:func:`counter_consumed`): it advances unless a decoded PoR proves the
        card refused the packet (``cntr_low``, a failed RC/CC/DS, an undefined
        security error, ...); a clear acceptance advances, and so does an
        unclear outcome (no PoR, an ambiguous status word, a transport error
        after the packet may have reached the card) - losing one counter value
        is harmless, leaving the card expecting a higher one is not.
        """
        state = self.state
        preset = ctx['preset']
        params = step['params']
        keysets = ota.preset_keysets(preset)
        keyset = None
        if params.get('kvn') is not None:
            want = int(params['kvn'])
            keyset = next((ks for ks in keysets
                           if presets_store.keyset_kvn(ks) == want), None)
            if keyset is None:
                raise testscript.ScriptError(
                    'keyset %d is not defined in the selected card preset'
                    % want)
        elif keysets:
            tar = str(params.get('tar')
                      or presets_store.role_tar(preset, 'isd')
                      or '').strip().upper()
            tar_kvn = presets_store.tar_keyset(preset, tar)
            if tar_kvn:
                keyset = next((ks for ks in keysets
                               if presets_store.keyset_kvn(ks) == tar_kvn),
                              None)
                if keyset is None:
                    raise testscript.ScriptError(
                        'keyset %d (the default of TAR %s) is not defined in '
                        'the selected card preset' % (tar_kvn, tar))
            else:
                msl = presets_store.tar_msl(preset, tar)
                if msl and msl != '00':
                    raise testscript.ScriptError(
                        'TAR %s needs a default keyset (its MSL %s requires '
                        'security) - set it in the Cards tab' % (tar, msl))
                keyset = keysets[0]
        kic = (keyset or {}).get('kic') or ''
        kid = (keyset or {}).get('kid') or ''
        kic_key = (keyset or {}).get('kicKey') or ''
        kid_key = (keyset or {}).get('kidKey') or ''
        kvn, kvn_error = ota.kvn_of(kic, kid)
        if kvn_error:
            raise testscript.ScriptError(kvn_error)
        tar = params.get('tar') or presets_store.role_tar(preset, 'isd') or ''
        try:
            # a step may override the SPI1 deliberately; the TAR's MSL is the
            # default when absent (TS 102 226 §8.2.1.3.2.4)
            spi1, _warning = ota.spi1_for_tar(preset, tar, params.get('spi1'))
        except ValueError as exc:
            raise testscript.ScriptError(str(exc))
        spi2 = params.get('spi2') or '01'
        counters = ctx.setdefault('counters', {})
        if kvn and kvn not in counters:
            counters[kvn] = (keyset or {}).get('cntr') or '00000000'
        counter = (counters.get(kvn) if kvn
                   else (ctx.get('counter') or '00000000'))
        counter = counter or '00000000'
        # The PoR of this exchange may arrive as a SEND SHORT MESSAGE
        # (PoR-in-submit); the expect step decodes it with the parameters the
        # packet was built from.  `cmd_len` is the plaintext command script's
        # length - the compact PoR's command-count bound, None for a
        # pre-built packet.
        cmd_len = len(params.get('apdu') or '') // 2 or None
        rm = ota.tar_is_rm(preset, tar)
        ctx['last_scp80'] = {'spi1': spi1, 'spi2': spi2, 'kic': kic,
                             'kid': kid, 'counter': counter,
                             'kicKey': kic_key, 'kidKey': kid_key,
                             'tar': tar, 'cmd_len': cmd_len, 'rm': rm}
        fmt = params.get('format') or 'compact'
        apdu_hex = params.get('apdu') or ''
        if params.get('sp'):
            sp_hex = params['sp']
            source = 'sp'
            self._log('SCP80 SP=%s' % sp_hex)
        else:
            source = 'apdu'
            if fmt != 'compact':
                # TS 102 226 §5.2.1: wrap the C-APDU in the Command Scripting
                # template (the expanded remote-management format)
                apdu_hex = ota.format_apdu(apdu_hex, fmt)
                self._log('SCP80 C-APDU=%s WRAPPED=%s (%s)'
                          % (params['apdu'], apdu_hex, fmt))
            else:
                self._log('SCP80 C-APDU=%s' % apdu_hex)
            try:
                packet, _spi = scp80.build_secured_packet(
                    spi1, spi2, kic, kid, tar, counter, apdu_hex,
                    kic_key, kid_key)
            except scp80.Scp80Error as exc:
                raise testscript.ScriptError(str(exc))
            sp_hex = bytes(packet).hex()
        self._log('SCP80 SECURED=%s (%d B) TAR=%s SPI1=%s SPI2=%s CNTR=%s'
                  % (sp_hex, len(sp_hex) // 2, tar or '-', spi1, spi2,
                     counter))
        # No submit capture: with ``handle_proactive=False`` the SEND SHORT
        # MESSAGE chain never runs, so a PorSubmitCapture could never be
        # filled - the *expect* step decodes the applet's own submit instead
        # (``_expect_por``).
        try:
            result = state._send_secured_packet(sp_hex, include_cpi=True,
                                               handle_proactive=False)
        except Exception:
            # The transport failed mid-exchange: the packet may already have
            # reached the card, so the outcome is "unclear" and the counter
            # advances (the three-way rule) - an unnecessarily skipped value
            # is harmless, a card left expecting a higher one is not.  The
            # step still fails with the transport error.
            if scp80.counter_tracked(spi1):
                self._advance_scp80_counter(ctx, preset, kvn, counter)
            raise
        sw = str(result.get('sw') or '').upper()
        data = result.get('response_data') or ''
        segments = result.get('segments') or 0
        self._log('SCP80 -> SW=%s%s'
                  % (sw or '(none)',
                     ' (%d segment(s))' % segments if segments > 1 else ''))
        por = None
        if data:
            por = ota.decode_por(spi1, spi2, kic, kid, counter, kic_key,
                                 kid_key, data, cmd_len=cmd_len, rm=rm)
        if por:
            decoded = por.get('decoded') or {}
            extra = ''
            if por.get('bad_format'):
                extra = ' bad_format=%s (%s)' % (por['bad_format'],
                                                por.get('bad_format_name') or '')
            self._log('PoR[inline] status=%s TAR=%s CNTR=%s%s raw=%s'
                      % (por.get('response_status'), por.get('tar'),
                         por.get('cntr'), extra, por.get('raw') or data))
            if por.get('response_type') == 'raw':
                self._log('APP DATA=%s (no R-APDU SW)'
                          % (decoded.get('last_response_data') or '(none)'))
            else:
                self._log('R-APDU SW=%s data=%s'
                          % (decoded.get('last_status_word') or '(none)',
                             decoded.get('last_response_data') or '(none)'))
        elif data:
            self._log('PoR[inline] undecodable raw=%s' % data)
        else:
            self._log('PoR[inline] none')
        sent = 'SCP80 %s TAR=%s SPI1=%s SPI2=%s cntr=%s%s %s%s' % (
            source, tar or '-', spi1, spi2, counter,
            (' kvn=%d' % kvn) if kvn else '',
            ('SP=%s' % sp_hex) if source == 'sp'
            else ('C-APDU=%s' % apdu_hex),
            (' format=%s' % fmt)
            if (source == 'apdu' and fmt != 'compact') else '')
        if scp80.counter_tracked(spi1) and counter_consumed(por, sw):
            # The packet asks for a counter check and the card did not prove
            # it refused the packet (the three-way rule): advance the working
            # value and persist it into the server-side preset (the server
            # owns the counters, so the run survives a lost response).
            self._advance_scp80_counter(ctx, preset, kvn, counter)
        return data, sw, sent, por, counter

    def _advance_scp80_counter(self, ctx, preset, kvn, counter):
        """Consume the counter of this packet: the run's value and the store.

        Shared by the decoded-result path and the transport-error path (both
        "advanced" per the three-way rule); ``kvn`` selects the keyset-linked
        counter, the keyless step uses the run's plain ``counter``.
        """
        counters = ctx.setdefault('counters', {})
        if kvn:
            counters[kvn] = scp80.increment_counter_hex(counter)
            ctx['counter'] = counters[kvn]
            ota.counter_persist(self.state.presets, preset.get('id'),
                                counters[kvn], 'test-run', kvn)
        else:
            ctx['counter'] = scp80.increment_counter_hex(counter)
            ota.counter_persist(self.state.presets, preset.get('id'),
                                ctx['counter'], 'test-run')

    def _menu_item_by_text(self, params):
        """Resolve a ``menu-select`` text against the cached SET UP MENU items.

        The item ids vary with the applet's install parameters, so a script
        matches the text the card actually shows; the match is
        case-insensitive unless the step sets ``case_sensitive: true``.  A
        miss names the available items (and, for a strict-case miss, the
        case-insensitive candidates).
        """
        menu = getattr(self._cat(), 'sim_menu', None) or {}
        items = menu.get('items') or []
        spec = {'mode': params.get('mode', 'exact'), 'value': params['text'],
                'case_sensitive': params.get('case_sensitive', False)}
        matches = [item for item in items
                   if testscript.match_text(spec, item.get('text') or '')]
        if not matches:
            listing = ', '.join('%s=%r' % (item.get('id'), item.get('text'))
                                for item in items)
            hint = ''
            if spec.get('case_sensitive') and items:
                # A strict-case miss is a common authoring slip with
                # non-ASCII menus: name the items a loose match would find.
                loose = [item for item in items
                         if testscript.match_text(
                             dict(spec, case_sensitive=False),
                             item.get('text') or '')]
                if loose:
                    hint = (' - a case-insensitive match exists (%s): set '
                            '"case_sensitive": false or fix the case'
                            % ', '.join('%s=%r' % (item.get('id'),
                                                   item.get('text'))
                                        for item in loose))
            raise testscript.ScriptError(
                'menu-select: no menu item matches %r (%s)%s'
                % (params['text'],
                   'menu: ' + listing if listing
                   else 'no menu cached - fetch the menu (a SET UP MENU '
                        'expectation) first', hint))
        if len(matches) > 1:
            raise testscript.ScriptError(
                'menu-select: %d items match %r (%s) - use the item id'
                % (len(matches), params['text'],
                   ', '.join('%s=%r' % (item.get('id'), item.get('text'))
                             for item in matches)))
        return matches[0]['id']

    def _run_action(self, step, ctx):
        """One action step; ``(result, pending)``.

        ``pending`` is the FETCH length of a ``91XX`` answer (``None`` when
        the card is idle) - the next expectation's command.
        """
        session = self._cat()
        kind = step['kind']
        params = step['params']
        por = None
        counter = None
        if kind == 'envelope':
            data, sw = session.send_event_download(
                params['event'],
                bytes.fromhex(params['data']) if params['data'] else None,
                drain=False, src=params.get('src'))
            sent = 'ENVELOPE(Event Download) type=0x%02X' % params['event']
            self._log('%s src=%s data=%s -> SW=%s%s'
                      % (sent, params.get('src') or '82',
                         params['data'] or '(none)', _sw(sw) or '(none)',
                         (' RESP=%s' % _hex(data)) if data else ''))
        elif kind == 'event':
            # The semantic event download: the server builds the events it
            # models (at run time, so the data-connection host clock is
            # fresh); every other event carries the PWA form's built hex
            # (validated at load).
            if params.get('data') is not None:
                data_hex = params['data']
            else:
                data_hex = events.build(params['event'],
                                        params.get('fields') or {})
            data, sw = session.send_event_download(
                params['event'], bytes.fromhex(data_hex), drain=False,
                src=params.get('src'))
            sent = ('ENVELOPE(Event Download) type=0x%02X (%s) data=%s'
                    % (params['event'], events.event_name(params['event']),
                       data_hex))
            self._log('%s src=%s -> SW=%s%s'
                      % (sent, params.get('src') or '82',
                         _sw(sw) or '(none)',
                         (' RESP=%s' % _hex(data)) if data else ''))
        elif kind == 'menu-select':
            item_id = params.get('item_id')
            if item_id is None:
                item_id = self._menu_item_by_text(params)
            tlv = bytes([0xD3, 0x07, 0x02, 0x02, 0x01, 0x81, 0x90, 0x01,
                         item_id])
            apdu = cat.envelope_apdu(tlv, int(session.cat_cla, 16))
            sent = apdu.hex().upper()
            session.menu_active = True
            data, sw = session.transport.send_apdu(apdu)
            if not _sw(sw).startswith('91'):
                session.menu_active = False
            self._log('MENU-SELECT ENVELOPE=%s item=%d%s -> SW=%s%s'
                      % (sent, item_id,
                         (' (%r)' % params['text']) if 'text' in params else '',
                         _sw(sw) or '(none)',
                         (' RESP=%s' % _hex(data)) if data else ''))
        elif kind == 'apdu':
            sent = params['apdu']
            data, sw = session.transport.send_apdu(bytes.fromhex(sent))
            self._log('APDU TX=%s -> SW=%s%s'
                      % (sent, _sw(sw) or '(none)',
                         (' RESP=%s' % _hex(data)) if data else ''))
        elif kind == 'status':
            data, sw, sent = self._run_status(step)
        elif kind in ('file-write', 'file-read'):
            data, sw, sent = self._run_file(step)
            self._log('%s -> SW=%s%s'
                      % (sent, sw or '(none)',
                         (' RESP=%dB' % (len(data) // 2)) if data else ''))
        elif kind == 'scp80':
            data, sw, sent, por, counter = self._run_scp80(step, ctx)
        elif kind == 'proactive-drain':
            return self._run_drain(step)
        else:
            raise testscript.ScriptError('unknown action kind %r' % kind)
        # The action executors return bytes for the data/SW; the checks and
        # the report use the hex forms.
        if isinstance(data, (bytes, bytearray)):
            data = _hex(data)
        if isinstance(sw, (bytes, bytearray)):
            sw = _sw(sw)
        checks = action_checks(step, sw, data, por, kind)
        status = testscript.combine_levels(
            [check['level'] if not check['ok'] else 'ok'
             for check in checks])
        result = {'status': status, 'sw': sw or '', 'data': data or '',
                  'sent': sent, 'checks': checks, 'por': por}
        if counter:
            result['counter'] = counter
        return result, _pending_length(sw)

    def _run_drain(self, step):
        """The proactive-drain action: consume every announced command.

        Polls STATUS and answers each ``91XX``-announced command - the TR is
        ``first`` for the first one, ``respond`` for the rest - until the card
        answers an idle SW, a non-``91XX`` SW appears or the attempt budget is
        spent.  The final SW is the step's SW check; an optional ``require``
        asserts what was drained (at least one match - an empty drain fails
        it).
        """
        session = self._cat()
        params = step['params']
        consumed = []
        sw = ''
        polls = 0
        for index in range(params['attempts']):
            self._heartbeat()
            _data, sw_raw = session.send_status()
            sw = _sw(sw_raw)
            polls = index + 1
            self._log('STATUS %d/%d -> %s' % (polls, params['attempts'], sw))
            if not sw.startswith('91'):
                break
            length = int(sw[2:], 16)
            fetch_apdu = session.fetch_apdu(length).hex().upper()
            raw_data, fetch_sw = session.fetch(length)
            if not raw_data:
                self._log('FETCH=%s -> SW=%s (no data)'
                          % (fetch_apdu, _sw(fetch_sw)))
                sw = _sw(fetch_sw)
                break
            raw = bytes(raw_data)
            self._log('FETCH=%s -> SW=%s %s'
                      % (fetch_apdu, _sw(fetch_sw), raw.hex().upper()))
            cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag = \
                proactive.parse_proactive_header(raw)
            name = proactive.PROACTIVE_TYPE_NAMES.get(cmd_type, 'UNKNOWN')
            self._log('CMD 0x%02X %s qual=%s'
                      % (cmd_type, name,
                         ('%02X' % cmd_qual) if cmd_qual is not None else '-'))
            if cmd_type == 0x25:
                # keep the cached menu (text-based selection) current
                menu = proactive.parse_setup_menu_command(raw)
                if menu:
                    session.sim_menu = menu
            respond = (params['first']
                       if (not consumed and params.get('first'))
                       else params['respond'])
            tr = self._step_tr(cmd_num, cmd_type, dev_src, dev_dst, cmd_qual,
                               cd_tag, respond, raw)
            _tr_data, tr_sw = session.send_terminal_response(tr)
            self._log('TR=%s -> SW=%s' % (tr.hex().upper(), _sw(tr_sw)))
            self.state._bip_flush()
            consumed.append({
                'type_hex': '%02X' % cmd_type, 'type_name': name,
                'qualifier': ('%02X' % cmd_qual) if cmd_qual is not None
                else None,
                'tr_sw': _sw(tr_sw), 'raw': raw.hex().upper()})
            sw = _sw(tr_sw)
            if not sw.startswith('91') and sw != '9000':
                # a TERMINAL RESPONSE the card rejected: stop and let the SW
                # check name it
                break
            if index + 1 < params['attempts'] and params['interval_ms']:
                time.sleep(params['interval_ms'] / 1000.0)
        checks = [check_result('SW',
                               testscript.match_value(step['check']['sw'], sw),
                               step['check']['sw']['value'], sw or '(none)',
                               step['on_fail'])]
        require = params.get('require')
        if require:
            matched = any(_drain_require_match(require, row)
                          for row in consumed)
            if consumed:
                actual = ', '.join(
                    '%s%s' % (row['type_name'],
                              (' q=%s' % row['qualifier'])
                              if row['qualifier'] else '')
                    for row in consumed)
            else:
                actual = '(nothing was pending)'
            checks.append(check_result('Require', matched,
                                       _drain_require_label(require), actual,
                                       step['on_fail']))
        status = testscript.combine_levels(
            [check['level'] if not check['ok'] else 'ok'
             for check in checks])
        result = {'status': status, 'sw': sw or '', 'data': '',
                  'sent': 'STATUS x%d; TR x%d' % (polls, len(consumed)),
                  'checks': checks, 'drained': consumed}
        return result, _pending_length(sw)

    def _expect_por(self, ctx, raw):
        """The PoR carried by a FETCHed SEND SHORT MESSAGE, or ``None``.

        The PoR-in-submit transport (SPI2 bit ``0x20``, TS 102 225 §5.1.1)
        delivers the response packet in the applet's own SMS-SUBMIT; the
        decoding uses the triggering scp80 step's parameters.  A concatenated
        submit is assembled across expectations (the segments accumulate in
        the run's context) before a PoR is decoded.
        """
        last = (ctx or {}).get('last_scp80')
        if not last:
            return None
        tpdu = sms.find_sms_tpdu(raw)
        if not tpdu:
            return None
        try:
            ref, total, number, payload = sms.parse_sms_concat(bytes(tpdu))
        except Exception:
            return None
        if total is not None and number is not None and total > 1:
            segments = ctx.setdefault('por_segments', [])
            segments.append((ref, total, number, bytes(payload).hex()))
            matching = sorted([seg for seg in segments if seg[0] == ref],
                              key=lambda seg: seg[2])
            if len(matching) < total:
                return None
            ud_hex = ''.join(seg[3] for seg in matching)
        else:
            ud_hex = bytes(payload).hex()
        if not ud_hex:
            return None
        por_hex = '027100' + ud_hex
        return ota.decode_por(last['spi1'], last['spi2'], last['kic'],
                              last['kid'], last['counter'], last['kicKey'],
                              last['kidKey'], por_hex,
                              cmd_len=last.get('cmd_len'),
                              rm=last.get('rm'))

    def _run_expect(self, step, pending, ctx=None):
        """One expectation: FETCH, check the command, answer it.

        The card announces a command with ``91XX`` (TS 102 221 §7.4.2.1); an
        expectation never polls - "nothing pending" and "a different command
        is pending" are check failures at the step's ``on_fail`` level (a
        ``status`` action with ``attempts > 1`` is the polling form).
        """
        session = self._cat()
        if not pending:
            want = step['command'].get('name')
            if not want and step['command'].get('type') is not None:
                want = '0x%02X' % step['command']['type']
            return {'status': step['on_fail'],
                    'checks': [check_result(
                        'Command', False, want or 'a pending command',
                        '(none)', step['on_fail'],
                        detail='no proactive command pending - the previous '
                               'step did not end with SW 91XX (the card '
                               'announces pending commands in the response to '
                               'a command, TS 102 221 7.4.2.1); a status '
                               'action with attempts > 1 polls until one '
                               'appears')]}, None
        fetch_apdu = session.fetch_apdu(pending).hex().upper()
        fetched, fetch_sw = session.fetch(pending)
        if not fetched:
            self._log('FETCH=%s -> SW=%s (no data)'
                      % (fetch_apdu, _sw(fetch_sw)))
            return {'status': step['on_fail'],
                    'checks': [check_result(
                        'Fetch', False, 'the announced command',
                        'no data (SW %s)' % _sw(fetch_sw), step['on_fail'],
                        detail='the card announced a command (SW 91XX) but '
                               'FETCH returned no data')]}, None
        raw = bytes(fetched)
        self._log('FETCH=%s -> SW=%s %s'
                  % (fetch_apdu, _sw(fetch_sw), raw.hex().upper()))
        cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag = \
            proactive.parse_proactive_header(raw)
        log_entry = session.log_proactive(cmd_type, raw, cmd_qual, cmd_num)
        type_name = proactive.PROACTIVE_TYPE_NAMES.get(cmd_type, 'UNKNOWN')
        self._log('CMD 0x%02X %s qual=%s'
                  % (cmd_type, type_name,
                     ('%02X' % cmd_qual) if cmd_qual is not None else '-'))
        sms_fields = sms_tpdu_fields(raw) if cmd_type == 0x13 else None
        if sms_fields and sms_fields.get('mti') == 1:
            self._log('SMS DA=%s PID=%s DCS=%s UDL=%s UD=%s'
                      % (sms_fields.get('da'), sms_fields.get('pid'),
                         sms_fields.get('dcs'), sms_fields.get('udl'),
                         sms_fields.get('ud')))
        # A SEND SHORT MESSAGE carries the PoR when the packet asked for it in
        # submit mode; decode it with the triggering scp80 step's context.
        por = self._expect_por(ctx, raw) if cmd_type == 0x13 else None
        if cmd_type == 0x13:
            if por is not None:
                decoded = por.get('decoded') or {}
                self._log('PoR[sms-submit] status=%s TAR=%s CNTR=%s raw=%s'
                          % (por.get('response_status'), por.get('tar'),
                             por.get('cntr'), por.get('raw') or '(none)'))
                if por.get('response_type') == 'raw':
                    self._log('APP DATA=%s (no R-APDU SW)'
                              % (decoded.get('last_response_data')
                                 or '(none)'))
                else:
                    self._log('R-APDU SW=%s data=%s'
                              % (decoded.get('last_status_word') or '(none)',
                                 decoded.get('last_response_data')
                                 or '(none)'))
            else:
                self._log('PoR[sms-submit] none (no decodable PDU or no scp80 '
                          'context)')
        if cmd_type == 0x25:
            # The card may re-send its menu (e.g. after an applet install);
            # keep the cache text-based selection resolves against current.
            menu = proactive.parse_setup_menu_command(raw)
            if menu:
                session.sim_menu = menu
        checks = []
        want_type = step['command'].get('type')
        type_ok = want_type is None or cmd_type == want_type
        if type_ok:
            checks.append(check_result(
                'Command', True, step['command'].get('name') or type_name,
                '%s (0x%02X)' % (type_name, cmd_type), 'ok'))
        else:
            # A different command pending fails like any other check: it
            # honours the step's on_fail, so on_fail "warning" expresses "the
            # applet either asks X or Y" (the command is still answered).
            checks.append(check_result(
                'Command', False,
                step['command'].get('name') or '0x%02X' % want_type,
                '%s (0x%02X)' % (type_name, cmd_type), step['on_fail']))
        if step.get('qualifier'):
            actual_qual = ('%02X' % cmd_qual) if cmd_qual is not None else None
            checks.append(check_result(
                'Qualifier',
                testscript.match_value(step['qualifier'], actual_qual),
                step['qualifier']['value'], actual_qual or '(none)',
                step['on_fail']))
        for content in step['checks']:
            if content['kind'] == 'text':
                text = proactive.parse_display_text(raw)
                checks.append(check_result(
                    'Text', testscript.match_text(content, text),
                    content['value'],
                    text if text is not None else '(none)',
                    content['on_fail']))
            elif content['kind'] == 'item':
                items = None
                if cmd_type == 0x24:
                    items = proactive.parse_select_item(raw)
                elif cmd_type == 0x25:
                    items = proactive.parse_setup_menu_items(raw)
                if items is None:
                    checks.append(check_result(
                        'Item', False,
                        'id=%s text=%r' % (content.get('id'),
                                           content.get('text')),
                        'command carries no items', content['on_fail']))
                else:
                    ok, detail = testscript.match_item(items, content)
                    checks.append(check_result(
                        'Item', ok,
                        'id=%s text=%r' % (content.get('id'),
                                           content.get('text')),
                        detail, content['on_fail']))
            elif content['kind'] == 'raw':
                actual = raw.hex().upper()
                checks.append(check_result(
                    'Raw',
                    testscript.match_value({'mode': content['mode'],
                                            'value': content['value']},
                                           actual),
                    content['value'], actual, content['on_fail']))
            elif content['kind'] == 'por':
                checks.extend(por_check_results(content, por,
                                                content['on_fail']))
            elif content['kind'] == 'alpha':
                alpha = proactive.cmd_tlv(
                    proactive.command_tlv_map(raw), 0x05)
                text = None
                if alpha:
                    try:
                        text = text_codec.decode_stk_text(alpha)
                    except Exception:
                        text = None
                checks.append(check_result(
                    'Alpha', testscript.match_text(content, text),
                    content['value'],
                    text if text is not None else '(none)',
                    content['on_fail']))
            elif content['kind'] == 'sms':
                if sms_fields is None or sms_fields.get('mti') != 1:
                    checks.append(check_result(
                        'SMS', False, 'a SUBMIT TPDU',
                        'none (not an SMS-SUBMIT)', content['on_fail']))
                else:
                    if 'da' in content:
                        checks.append(check_result(
                            'SMS DA', sms_fields.get('da') == content['da'],
                            content['da'], sms_fields.get('da') or '(none)',
                            content['on_fail']))
                    for label, key in (('PID', 'pid'), ('DCS', 'dcs')):
                        if key in content:
                            actual = sms_fields.get(key) or ''
                            checks.append(check_result(
                                'SMS ' + label,
                                testscript.match_value(content[key], actual),
                                content[key]['value'], actual or '(none)',
                                content['on_fail']))
                    if 'udl' in content:
                        actual = sms_fields.get('udl')
                        checks.append(check_result(
                            'SMS UDL', actual == content['udl'],
                            str(content['udl']),
                            '(none)' if actual is None else str(actual),
                            content['on_fail']))
                    if 'ud' in content:
                        actual = sms_fields.get('ud') or ''
                        checks.append(check_result(
                            'SMS UD',
                            testscript.match_value(content['ud'], actual),
                            content['ud']['value'], actual or '(none)',
                            content['on_fail']))
            elif content['kind'] == 'files':
                paths = parse_file_list(raw)
                want = content['files']
                checks.append(check_result(
                    'Files', sorted(paths) == sorted(want), ', '.join(want),
                    ', '.join(paths) or '(none)', content['on_fail']))
        tr = self._step_tr(cmd_num, cmd_type, dev_src, dev_dst, cmd_qual,
                           cd_tag, step['respond'], raw)
        _tr_data, tr_sw = session.send_terminal_response(tr)
        tr_sw = _sw(tr_sw)
        self._log('TR=%s -> SW=%s' % (tr.hex().upper(), tr_sw))
        session.record_tr(log_entry, tr, tr_sw)
        self.state._bip_flush()
        status = testscript.combine_levels(
            [check['level'] if not check['ok'] else 'ok'
             for check in checks])
        result = {'status': status, 'sw': tr_sw,
                  'data': raw.hex().upper(),
                  'sent': 'TR ' + tr.hex().upper(), 'checks': checks,
                  'por': por, 'sms': sms_fields,
                  'command': {'type_hex': '%02X' % cmd_type,
                              'type_name': type_name,
                              'qualifier': ('%02X' % cmd_qual)
                              if cmd_qual is not None else None,
                              'raw': raw.hex().upper()}}
        return result, _pending_length(tr_sw)

    def _drain_pending(self, fetch_len, result=0x00, max_commands=3):
        """Fetch and answer the commands the script left pending (TS 102 223 §6.3).

        The answer defaults to ``ok`` (0x00): a cancel tells the card the user
        terminated the proactive session and pushes assertive applications
        onto an error path.  Answering one command can make the application
        emit another, so the drain keeps fetching while the TERMINAL RESPONSE
        answers ``91XX`` (bounded by ``max_commands``).  A script that wants
        to test the refusal path answers explicitly (an expectation's
        ``respond`` or the ``proactive-drain`` action's ``first``).
        """
        session = self._cat()
        answered = []
        try:
            while len(answered) < max_commands:
                self._heartbeat()       # a bounded drain still makes progress
                # fetch_len 0 (a 9100 SW) means "length unknown": fetch the
                # full 256 bytes (``%02x`` of 0x100 would emit an odd APDU)
                length = fetch_len or 0xFF
                fetch_apdu = session.fetch_apdu(length).hex().upper()
                fetched, sw = session.fetch(length)
                if not fetched:
                    break
                raw = bytes(fetched)
                cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag = \
                    proactive.parse_proactive_header(raw)
                # the drained command is part of the session's proactive
                # history too - a command answered by the drain must not be
                # invisible in /api/proactive-log
                session.log_proactive(cmd_type, raw, cmd_qual, cmd_num)
                tr = self._step_tr(cmd_num, cmd_type, dev_src, dev_dst,
                                   cmd_qual, cd_tag, {'result': result}, raw)
                _tr_data, tr_sw = session.send_terminal_response(tr)
                self.state._bip_flush()
                name = proactive.PROACTIVE_TYPE_NAMES.get(cmd_type, '?')
                answered.append({'name': name, 'type': cmd_type,
                                 'raw': raw.hex().upper(),
                                 'tr': tr.hex().upper(), 'sw': _sw(tr_sw)})
                log.info('TEST-RUN drain: %s (0x%02X) TR=%s -> SW=%s',
                         name, cmd_type, tr.hex().upper(), _sw(tr_sw))
                if not _sw(tr_sw).startswith('91'):
                    break
                fetch_len = int(_sw(tr_sw)[2:], 16)
        except Exception as exc:
            return {'note': 'drain failed: %s' % exc}
        if not answered:
            return {'note': 'nothing to drain'}
        result_name = (_DRAIN_ALIAS.get(result)
                       or _tr_result_name(result) or ('0x%02X' % result))
        return {'sent': '; '.join('TR %s %s' % (result_name, row['tr'])
                                  for row in answered),
                'sw': answered[-1]['sw'], 'data': answered[0]['raw'],
                'note': '; '.join('pending %s (0x%02X) answered with %s'
                                  % (row['name'], row['type'], result_name)
                                  for row in answered)}

    # ---- the script loop ----------------------------------------------

    def run_script(self, script, preset, shared=None):
        """One script's steps; returns ``(ctx, stopped)``.

        The live steps land in the run state (the snapshot the PWA renders),
        the semantic lines in its ``log``.  ``shared`` carries the counter
        state across a load test's iterations or a suite's members: the dict
        is created once by the caller and reused, so a second SCP80 script
        starts where the first ended instead of resending the run-start
        snapshot (the store persists each packet, but the in-memory preset
        copy does not move).

        A step that raises is a step failure like any other: it honours the
        step's ``on_fail`` (v3.22.7) - an absent optional file, a refused
        SELECT or a preset/keyset error then warns and the run continues,
        with the error as a failed check; the default (``error``) stops the
        run.
        """
        shared = shared if shared is not None else {}
        session = self._cat()
        ctx = {
            'preset': dict(preset or {}),
            'counter': (shared.get('counter')
                        or str((preset or {}).get('counter')
                               or (preset or {}).get('cntr') or '').upper()),
            'counters': shared.setdefault('counters', {}),
            'uses_scp80': any(step['type'] == 'action'
                              and step['kind'] == 'scp80'
                              for step in script['steps']),
        }
        if ctx['uses_scp80']:
            with self._lock:
                self._run['scp80_counter'] = ctx['counter']
        pending = None
        stopped = False
        card_session = getattr(self.state, 'card_session', 0)
        for index, step in enumerate(script['steps']):
            with self._lock:
                if self._run['stop']:
                    stopped = True
                self._run['index'] = index
            if stopped:
                break
            row = entry(step, index)
            with self._lock:
                self._run['steps'].append(row)
                self._run['heartbeat'] = time.time()
            # A proactive-drain step is the tool that consumes a pending
            # command (it polls STATUS - the card re-announces the unfetched
            # command - then fetches and answers it), so the guard does not
            # apply to it (v3.22.4 review).
            drain_next = (step['type'] == 'action'
                          and step.get('kind') == 'proactive-drain')
            if pending is not None and step['type'] != 'expect' \
                    and not drain_next:
                self._entry_update(row, {
                    'status': 'error',
                    'note': 'unexpected proactive command pending (SW 91XX) - '
                            'add an expect step or a proactive-drain'})
                with self.state.card_lock:
                    drained = self._drain_pending(pending)
                fields = {key: value for key, value in drained.items()
                          if key != 'note'}
                fields['ms'] = int((time.time() - row['started']) * 1000)
                self._entry_update(row, fields)
                pending = None
                break
            if getattr(self.state, 'card_session', 0) != card_session:
                self._entry_update(row, {
                    'status': 'error',
                    'note': 'card session changed (card removed or re-equipped)',
                    'ms': int((time.time() - row['started']) * 1000)})
                break
            try:
                with self.state.card_lock:
                    if step['type'] == 'action':
                        result, pending = self._run_action(step, ctx)
                    else:
                        result, pending = self._run_expect(step, pending, ctx)
                self._finish_entry(row, result)
                if row['status'] == 'error':
                    # Error terminates the script; Warning and OK continue.
                    break
            except Exception as exc:
                level = step.get('on_fail') or 'error'
                check = check_result('Error', False, 'the step to run',
                                     str(exc), level, detail=str(exc))
                self._finish_entry(row, {'status': level, 'checks': [check],
                                         'note': str(exc)})
                log.warning('TEST-RUN step %d failed (%s): %s',
                            index + 1, level, exc)
                # A stuck card's transport is already being torn down:
                # draining would only raise again on the dead link.
                if pending is not None and not self._run.get('stuck'):
                    with self.state.card_lock:
                        drained = self._drain_pending(pending)
                    self._entry_update(row, {key: value
                                             for key, value in drained.items()
                                             if key != 'note'})
                    pending = None
                if level == 'error':
                    break
        if pending is not None and not self._run.get('stuck'):
            with self.state.card_lock:
                drained = self._drain_pending(pending)
            with self._lock:
                self._run['steps'].append(dict(
                    {'index': len(script['steps']), 'type': 'cleanup',
                     'kind': 'cleanup',
                     'label': TEST_KIND_LABELS['cleanup'],
                     'status': 'error' if not stopped else 'warning',
                     'checks': [], 'started': time.time(), 'ms': 0,
                     'note': None, 'sent': None, 'sw': None,
                     'data': None}, **drained))
        if ctx['uses_scp80']:
            with self._lock:
                self._run['scp80_counter'] = ctx['counter']
                self._run['scp80_counters'] = dict(ctx.get('counters') or {})
        # The no-keyset counter is a plain value in the ctx: carry it into the
        # shared state so the next iteration/member starts where this ended.
        shared['counter'] = ctx.get('counter') or ''
        return ctx, stopped


# ---- the small helpers the drain/result lines use --------------------------

def _tr_result_name(result):
    """The spec name of a TERMINAL RESPONSE result (the drain's report line)."""
    return tr.tr_result_name(result)


def _drain_require_match(require, consumed):
    """Whether one consumed command satisfies a drain's ``require`` spec.

    The command type/name (``None``/``ANY`` matches all) and an optional
    qualifier (exact/mask).
    """
    if require.get('type') is not None \
            and consumed.get('type_hex') != '%02X' % require['type']:
        return False
    qualifier = require.get('qualifier')
    if qualifier and not testscript.match_value(qualifier,
                                                consumed.get('qualifier')):
        return False
    return True


def _drain_require_label(require):
    """The ``require`` spec as the report's expected string."""
    if require.get('type') is not None:
        name = require.get('name') or '0x%02X' % require['type']
    else:
        name = 'ANY'
    qualifier = require.get('qualifier')
    return name + ((' q=%s' % qualifier['value']) if qualifier else '')
