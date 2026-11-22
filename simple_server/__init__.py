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

Nothing here imports ``pySim``, ``osmocom`` or ``construct``; only
:mod:`simple_server.transport` touches ``pyscard``, and
:mod:`simple_server.scp80` uses ``pycryptodomex`` for the ciphers.
"""
