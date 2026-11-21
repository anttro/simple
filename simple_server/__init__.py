# coding=utf-8
"""Foundational, pysim-independent modules for the simple server.

Phase 0: transport, TLV, text and hex/APDU helpers.
Phase 1: FCP parsing, the file model, file operations and card detection.

Nothing here imports ``pySim``, ``osmocom`` or ``construct``; only
:mod:`simple_server.transport` touches ``pyscard``.
"""
