# coding=utf-8
"""The network-state monitor and the MCC/MNC operator list.

Two things the Simulator's "Network state" panel needs, both reused rather
than re-derived:

* :mod:`simple_server.shared.netstate` (pure, moved from the legacy tree
  unchanged) - the cached per-card view of the network-related EFs, the
  simulated service state and the derived location/roaming view.  This
  module only supplies its card side (:func:`read_files`, the port of the
  legacy ``_netstate_read``) and the operator list (:func:`load_mcc_mnc`,
  :func:`compute`).
* the bundled worldwide MCC/MNC list (``simple_server/data/
  mcc-mnc-list.json``, MIT, from pbakondy/mcc-mnc-list) and the search /
  random-pick helpers behind ``GET /api/mcc-mnc`` (the legacy
  ``_mcc_mnc_load``/``_mcc_mnc_is_mvno``/``_mcc_mnc_search``/
  ``_mcc_mnc_random``, ported name for name).

Provenance: ``pysim_simple_server/server.py``'s ``_netstate_read`` and the
four ``_mcc_mnc_*`` helpers.  Differences, both deliberate:

* the read is driven by :class:`~simple_server.commands.FileOps` and the file
  model (``FileModel.path_chain``) instead of pySim's runtime channel, so a
  candidate path that the model does not know is skipped exactly like an
  absent file;
* the monitor state is created and owned by
  :class:`~simple_server.server.ServerState`, not by module globals.
"""

import json
import logging
import os
import random
import time

import simple_server
from simple_server.shared import netstate

log = logging.getLogger(__name__)

__all__ = [
    'DEFAULT_MCC_MNC', 'default_mcc_mnc_path', 'load_mcc_mnc', 'is_mvno',
    'search', 'random_entry', 'read_files', 'compute',
]

#: The operator fields an API row carries (the legacy projection).
ENTRY_FIELDS = ('countryName', 'countryCode', 'mcc', 'mnc', 'brand',
                'operator', 'status')


def default_mcc_mnc_path():
    """The bundled operator list (the legacy ``_default_mcc_mnc_list``).

    Resolved from this package, not the legacy tree, so an installed
    ``simple_server`` carries its own copy (``pyproject.toml``'s package
    data).
    """
    package = os.path.dirname(os.path.abspath(simple_server.__file__))
    return os.path.join(package, 'data', 'mcc-mnc-list.json')


DEFAULT_MCC_MNC = default_mcc_mnc_path()

#: One loaded list per path (the legacy cache; the file is immutable at run
#: time, so a re-read buys nothing).
_CACHE = {'path': None, 'data': None}


def load_mcc_mnc(path):
    """Load the optional MCC/MNC operator list (JSON) once; ``None`` if absent."""
    if not path:
        return None
    if _CACHE['path'] == path:
        return _CACHE['data']
    data = None
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            loaded = json.load(handle)
        if isinstance(loaded, list):
            data = loaded
        else:
            log.warning('MCC/MNC list %s is not a JSON array', path)
    except Exception as exc:
        log.warning('MCC/MNC list %s could not be read: %s', path, exc)
        data = None
    _CACHE['path'] = path
    _CACHE['data'] = data
    return data


def is_mvno(entry):
    """True for MVNO entries: ``bands`` marks them (e.g. 'MVNO', 'Satellite MVNO').

    MVNOs do not operate their own radio network, so the network-simulation
    picker hides them; the full list stays available for operator-name
    resolution (``netstate.compute_network``).
    """
    return 'mvno' in str(entry.get('bands') or '').lower()


def _row(entry):
    return {key: entry.get(key) for key in ENTRY_FIELDS}


def search(data, query, limit=50):
    """Compact substring search over country/brand/operator/MCC/MNC."""
    text = (query or '').strip().lower()
    if not text:
        return []
    out = []
    for entry in data or []:
        if is_mvno(entry):
            continue
        haystack = ' '.join(
            str(entry.get(key) or '')
            for key in ('countryName', 'countryCode', 'mcc', 'mnc', 'brand',
                        'operator')).lower()
        if text not in haystack:
            continue
        out.append(_row(entry))
        if len(out) >= limit:
            break
    return out


def random_entry(data, exclude=None):
    """One random operator entry (optionally excluding ``'<mcc><mnc>'``)."""
    pool = [entry for entry in (data or [])
            if (str(entry.get('mcc') or '') + str(entry.get('mnc') or ''))
            != (exclude or '') and not is_mvno(entry)]
    if not pool:
        pool = [entry for entry in (data or []) if not is_mvno(entry)]
    if not pool:
        return None
    return _row(random.choice(pool))


# ---- the card side of the monitor --------------------------------------

def read_files(ops, model, keys=None):
    """Read the monitored network-state EFs (best effort per file).

    Port of the legacy ``_netstate_read``: each entry of
    :data:`netstate.FILE_DEFS` is looked for at its candidate paths in order
    (a USIM is served from ADF.USIM, a GSM SIM from DF.GSM/DF.TELECOM), the
    first one that selects is read (record files record by record,
    transparent files whole) and every failure is skipped - a file the card
    does not have is reported as ``present: False``, never as an error.
    ``keys`` filters the definition list (the net-sim/event paths re-read
    only EF.IMSI).

    ``ops`` is the equipped card's :class:`simple_server.commands.FileOps`,
    ``model`` the :class:`simple_server.files.FileModel` for path resolution.
    A 2G SIM's SELECT carries no FCP (TS 51 011 §9.2.1), so a SIM's entries
    stay absent here - the same limitation the file endpoints' reads have
    until a card-independent length source exists.
    """
    out = {}
    for definition in netstate.FILE_DEFS:
        key = definition['key']
        if keys and key not in keys:
            continue
        entry = {'name': definition['name'], 'fid': definition['fid'],
                 'present': False, 'kind': None, 'source': 'refresh',
                 'updated': time.time()}
        for path in definition['paths']:
            try:
                fcp = ops.select_path(model.path_chain(path))
                if fcp.structure in ('linear_fixed', 'cyclic'):
                    count = fcp.num_of_rec or 1
                    records = []
                    for index in range(1, count + 1):
                        data, _sw = ops.read_file(fcp, rec=index)
                        records.append({'num': index,
                                        'data': bytes(data or b'').hex().upper()})
                    entry.update({'path': path, 'present': True,
                                  'kind': 'record', 'records': records})
                else:
                    data, _sw = ops.read_file(fcp)
                    entry.update({'path': path, 'present': True,
                                  'kind': 'transparent',
                                  'data': bytes(data or b'').hex().upper()})
                break
            except Exception as exc:
                # Absent file, an FCP without a size, a refused read: the
                # next candidate (or "absent") - never a failed refresh.
                log.debug('network-state read %s (%s) failed: %s',
                          definition['name'], path, exc)
                continue
        out[key] = entry
    return out


def compute(state, mcc_mnc_data=None):
    """Derive the monitor header (service, location, roaming) from the cache.

    ``state`` is a :func:`simple_server.shared.netstate.new_state` dict; the
    derived block lands in ``state['network']`` and is returned.
    """
    return netstate.compute_network(state, mcc_mnc_data)


# Forget the loaded operator list, for tests that swap the bundled path.
def _reset_cache():
    """Forget the loaded operator list (tests only)."""
    _CACHE['path'] = None
    _CACHE['data'] = None
