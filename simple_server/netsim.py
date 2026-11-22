# coding=utf-8
"""The network-condition simulator's card adapter.

The scenario builders, the byte layouts and the step log are
:mod:`pysim_simple_server.netsim` - pysim-free and reused unchanged, so the
observed phone write patterns (``projects/UICC_NAA.md`` section 13) have one
implementation.  Its runner drives the card through pySim's runtime-channel
API (``_select_path``, the selected file's FCP metadata, UPDATE/READ, the
AUTHENTICATE transport and the event download), so this module supplies that
surface on :class:`simple_server.commands.FileOps` and
:class:`simple_server.cat_session.CatSession` instead of copying the
scenarios.

Provenance: ``pysim_simple_server/server.py``'s ``_netstate_read``/
``_netstate_after_net_sim`` were the legacy glue; the adapter here is the
equivalent for :func:`run_scenario`.

One model difference is patched explicitly: the legacy candidate paths for
EF.Kc/EF.KcGPRS (``ADF.USIM/4F20``, ``DF.GSM/6F08``) predate the current
tree, where TS 31.102 §4.4.3.1/§4.4.3.2 puts them under ``DF.GSM-ACCESS``
(``5F3B``) and the 2G SIM ones are ``6F20``/``6F52`` (TS 51 011
§10.3.3/§10.3.32).  :data:`PATH_ALIASES` redirects exactly those two keys to
the model's paths; every other legacy path resolves as written.

One limitation is shared with the monitor (:mod:`simple_server.netmon`): a
SELECT that carries no FCP (a 2G SIM's TS 51 011 §9.2.1 form) gives no file
size / record length, so the FCP-driven read cannot run.  The shim reports
:data:`NO_FCP_SW` as the read's status word - the runner's "read failed, skip"
path - instead of raising, and never guesses a length.
"""

import logging
from types import SimpleNamespace

from pysim_simple_server import netsim as legacy_netsim

from simple_server.commands import FileError, UnexpectedStatusWord

log = logging.getLogger(__name__)

__all__ = ['PATH_ALIASES', 'EVENT_LOCATION_STATUS', 'SCENARIO_SERVICE',
           'NO_FCP_SW', 'run_scenario']

#: The status word reported for a read the selected file's FCP cannot size
#: (the SELECT returned no FCP: a 2G SIM's TS 51 011 §9.2.1 form).  No APDU
#: with a guessed length is sent; the runner only distinguishes ``9000`` from
#: "the read failed", and ``6985`` (conditions of use not satisfied) says the
#: read could not be performed under the conditions.
NO_FCP_SW = '6985'

#: The reused tables, re-exported so the server reads one module: the Location
#: status event type and the service state each scenario establishes.
EVENT_LOCATION_STATUS = legacy_netsim.EVENT_LOCATION_STATUS
SCENARIO_SERVICE = legacy_netsim.SCENARIO_SERVICE

#: Legacy candidate path -> the model's path for the same file (see the
#: module docstring).  Keyed by the exact legacy path so the runner's
#: candidate order is unchanged.
PATH_ALIASES = {
    'ADF.USIM/4F20': 'ADF.USIM/5F3B/4F20',
    'ADF.USIM/4F52': 'ADF.USIM/5F3B/4F52',
    'DF.GSM/6F08': 'DF.GSM/6F20',
    'DF.GSM/6F09': 'DF.GSM/6F52',
}


def _sw_text(sw):
    """A status word from the transport -> lower-case hex (pySim's form)."""
    if isinstance(sw, (bytes, bytearray)):
        return bytes(sw).hex()
    return str(sw or '').strip().lower()


def _hex_text(value):
    """An UPDATE payload as upper-case hex (netsim builds hex strings)."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    return str(value or '').strip().upper()


class _TransportShim(object):
    """pySim's ``scc._tp``: ``send_apdu(hex) -> (data_hex, sw_hex)``."""

    def __init__(self, transport):
        self.transport = transport

    def send_apdu(self, apdu):
        data, sw = self.transport.send_apdu(bytes.fromhex(str(apdu)))
        return bytes(data or b'').hex().upper(), _sw_text(sw)


class _SccShim(object):
    """The card's command class as the runner reads it (AUTHENTICATE)."""

    def __init__(self, transport, cla):
        self.cla_byte = str(cla or '00').upper()
        self._tp = _TransportShim(transport)


class _LchanShim(object):
    """The selected file and its I/O, on ``FileOps`` + the file model.

    pySim's ``RuntimeLchan`` surface the runner uses: ``selected_file_size``/
    ``selected_file_record_len``/``selected_file_num_of_rec`` (from the FCP),
    ``update_binary``/``update_record``/``read_binary``/``read_record`` (the
    pySim ``(data_hex, sw_hex)`` pair).  A non-``9000`` answer is returned,
    not raised - the runner records it as a failed step.  A read the FCP
    cannot size (no FCP at all, ``sel_fcp=False``) is :data:`NO_FCP_SW` with
    empty data: never a guessed length.
    """

    def __init__(self, ops, model):
        self.ops = ops
        self.model = model
        self.fcp = None

    # -- selection --

    def select(self, path):
        """Select a full path (a legacy path is redirected to the model's)."""
        target = PATH_ALIASES.get(str(path), str(path))
        if target != str(path):
            log.debug('net-sim path %s -> %s (the model tree)', path, target)
        self.fcp = self.ops.select_path(self.model.path_chain(target))
        return self.fcp

    # -- FCP metadata --

    def selected_file_size(self):
        return self.fcp.file_size if self.fcp is not None else None

    def selected_file_record_len(self):
        return self.fcp.record_len if self.fcp is not None else None

    def selected_file_num_of_rec(self):
        return self.fcp.num_of_rec if self.fcp is not None else None

    # -- updates --

    def update_binary(self, data):
        try:
            sw = self.ops.update_binary(0, data)
        except UnexpectedStatusWord as exc:
            # A refused update is a failed step, not an exception (pySim
            # returns the status word; the runner records ok = sw == 9000).
            return _hex_text(data), exc.sw
        return _hex_text(data), _sw_text(sw)

    def update_record(self, record, data):
        try:
            sw = self.ops.update_record(record, data)
        except UnexpectedStatusWord as exc:
            return _hex_text(data), exc.sw
        return _hex_text(data), _sw_text(sw)

    # -- reads --

    def read_binary(self):
        """The whole selected transparent file as ``(data_hex, sw)``.

        A refused READ returns the card's status word; a SELECT without an
        FCP (a 2G SIM, TS 51 011 §9.2.1) carries no file size, so nothing is
        sent and :data:`NO_FCP_SW` stands for the failed read (the monitor
        reports such files absent - see :mod:`simple_server.netmon`).
        """
        return self._read()

    def read_record(self, record):
        """Record ``record`` as ``(data_hex, sw)`` (see :meth:`read_binary`)."""
        return self._read(rec=record)

    def _read(self, rec=None):
        try:
            data, sw = self.ops.read_file(self.fcp, rec=rec)
        except UnexpectedStatusWord as exc:
            # The card refused the READ: its own status word is the answer.
            return '', _sw_text(exc.sw)
        except (ValueError, FileError) as exc:
            # No FCP / no record length in the FCP: the read cannot be sized,
            # so it is a failed read for the runner - never a guessed length.
            log.debug('net-sim read failed, no size in the FCP: %s', exc)
            return '', NO_FCP_SW
        return bytes(data or b'').hex().upper(), _sw_text(sw)


class _ServerShim(object):
    """The legacy server hooks the runner calls, on ``ServerState``.

    ``_select_path`` selects through the lchan shim (the path is resolved by
    the file model); ``_send_event_download`` is the ported CAT session's
    ENVELOPE; ``net_state`` is the monitor dict the FPLMN rule reads.
    """

    def __init__(self, state, cat, scc):
        self.state = state
        self.cat = cat
        self.scc = scc
        self._server_ref = SimpleNamespace(scc=scc)

    @property
    def net_state(self):
        """The live monitor dict (the runner reads it for the FPLMN rule)."""
        return self.state.netstate

    def _select_path(self, lchan, path, app):
        lchan.select(path)
        return None, None

    def _send_event_download(self, scc, event_type, event_data=None,
                             drain=True, src=None, log_response=True):
        data, sw = self.cat.send_event_download(
            event_type, event_data, drain=drain, src=src)
        return bytes(data or b'').hex().upper(), _sw_text(sw)


def run_scenario(state, scenario, params=None):
    """Run one net-sim scenario against the equipped card.

    ``state`` is a :class:`~simple_server.server.ServerState`; the caller
    holds the card lock.  Returns the legacy result dict
    ``{scenario, success, error, steps}`` (the caller attaches ``net_state``
    after patching the monitor).  A :class:`ValueError` for an unknown
    scenario.
    """
    cat = state.cat
    ops = state._file_ops()
    lchan = _LchanShim(ops, state.files)
    # The command class is the file-access CLA ('00' UICC / 'a0' SIM), which
    # is what pySim's ``scc.cla_byte`` meant to the runner - not the CAT CLA.
    scc = _SccShim(state.transport, ops.cla)
    server = _ServerShim(state, cat, scc)
    app = SimpleNamespace(rs=SimpleNamespace(lchan=[lchan]))
    event_list = list(cat.event_list or []) if cat is not None else []
    return legacy_netsim.run_scenario(server, app, scenario, params,
                                      event_list=event_list)
