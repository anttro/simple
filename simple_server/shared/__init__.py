# coding=utf-8
"""The pysim-free modules ``simple_server`` shares with the legacy tree.

These are the spec-only modules (the preset and test-script stores, the
test-script engine, the event builders, the CAP memory analyzer, the CAT
release table, the network simulation and monitor state, the outfile parser,
the BIP terminal and the SCP81 listener) that never needed pySim.  They were
written in and are still maintained in ``pysim_simple_server``; this
subpackage carries the copies so ``simple_server`` installs and runs without
the legacy fork present.  ``tests/test_simple_drift.py`` imports both sides
and is the one place allowed to compare them.

The modules import each other inside this package (``testscript`` ->
``events``, ``netstate`` -> ``netsim``, ``outfile`` -> ``presets``), exactly
as they did in the legacy tree; there is no cycle and no import-time work
beyond reading the module-level tables.

No module here may import ``pysim_simple_server``, ``pySim``, ``osmocom`` or
``construct`` - the package-import hygiene guard enforces it.
"""
