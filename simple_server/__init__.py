# coding=utf-8
"""Foundational, pysim-independent modules for the simple server.

Phase 0: transport, TLV, text and hex/APDU helpers.
Phase 1: FCP parsing, the file model, file operations and card detection.
Phase 2: SCP80 secured packets, SMS TPDUs and the SMS-PP download ENVELOPE.
Phase 3: the eUICC ES10 (SGP.22) data objects and transport functions.
Phase 3b: proactive command decoding and TERMINAL RESPONSE building.

Nothing here imports ``pySim``, ``osmocom`` or ``construct``; only
:mod:`simple_server.transport` touches ``pyscard``, and
:mod:`simple_server.scp80` uses ``pycryptodomex`` for the ciphers.
"""
