# coding=utf-8
"""Foundational, pysim-independent modules for the simple server.

Phase 0: transport, TLV, text and hex/APDU helpers.
Phase 1: FCP parsing, the file model, file operations and card detection.
Phase 2: SCP80 secured packets, SMS TPDUs and the SMS-PP download ENVELOPE.
Phase 3: the eUICC ES10 (SGP.22) data objects and transport functions.
Phase 3b: proactive command decoding and TERMINAL RESPONSE building.
Phase 3c-1: the CAT session engine (FETCH/TERMINAL RESPONSE chain, ENVELOPE,
the interactive menu, TIMER MANAGEMENT, the PLI dictionary and the proactive
log).
Phase 3c-2: the BIP terminal (the five BIP commands, the session control and
the Data available / Channel status events).
Phase 3c-3a: the server state (the card lifecycle, the presence monitor, the
background STATUS poll) and the HTTP handler with the static PWA serving and
the first endpoints (version/status/readers/reader/equip/reset).
Phase 3c-3b: the file endpoints (tree/select/read/write) on the file model,
the FCP parser and :class:`~simple_server.commands.FileOps`, the card-free
EF.ARR/EF.PBR session caches, and :mod:`simple_server.pbr` (the EF.PBR
phonebook decoder and the tree injection).
Phase 3c-3c-1: the preset store endpoints, ADM verification and the plain
SCP80 send/counter-probe endpoints, and :mod:`simple_server.ota` (the per-TAR
keyset and counter rules, the RAM command format helpers and the PoR decode).
Phase 3c-3c-2: the CAP analysis and the single-card RAM installs
(:mod:`simple_server.capram`), the CAP compatibility test, the TAR probe and
the batch install.
Phase 3c-3d-1: the test-script store endpoints and the single-script run
(:mod:`simple_server.testrun`, the per-state runner).
Phase 3c-3d-2: the test-suite store endpoints, the cross-store rules
(:mod:`simple_server.suites`) and the suite execution (the ordered
setup/members/teardown, the per-member ``on_fail``, the start reset, the
shared SCP80 counter state) - also in :mod:`simple_server.testrun`.
Phase 3c-3e-1: the CAT/Simulator endpoints on the CAT session engine (the STK
menu browser, the event downloads, the runtime TERMINAL PROFILE, the manual
STATUS poll and the poll switch, the proactive log and the PLI dictionary),
the server-side event builders of :mod:`pysim_simple_server.events` and the
raw APDU endpoint.

Nothing here imports ``pySim``, ``osmocom`` or ``construct``; only
:mod:`simple_server.transport` touches ``pyscard``, and
:mod:`simple_server.scp80` uses ``pycryptodomex`` for the ciphers.
"""
