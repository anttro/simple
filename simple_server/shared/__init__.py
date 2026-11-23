# coding=utf-8
"""The spec-only modules ``simple_server`` carries in its own package.

These are the modules (the preset and test-script stores, the test-script
engine, the event builders, the CAP memory analyzer, the CAT release table,
the network simulation and monitor state, the outfile parser, the BIP terminal
and the SCP81 listener) that never needed pySim - the 4.0.0 cut-over moved
them here, so ``simple_server`` installs and runs without the pySim-based 3.x
tree.  That legacy copy is preserved in the ``simple-pysim`` fork.

The modules import each other inside this package (``testscript`` ->
``events``, ``netstate`` -> ``netsim``, ``outfile`` -> ``presets``), exactly
as they did in the legacy tree; there is no cycle and no import-time work
beyond reading the module-level tables.

No module here may import ``pySim``, ``osmocom`` or
``construct`` - the package-import hygiene guard enforces it.
"""
