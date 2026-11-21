# pysim phase-out — plan & worklog

> **Canonical record** for removing the `pysim` dependency from SIMple.
> Share this file with any subagent working on the phase-out — it is the single
> source of truth for scope, decisions, dependency inventory, status and open
> ideas.  Update the status table and the open-ideas list as work progresses.

## Status at a glance

| Phase | What | Status |
|---|---|---|
| 0 | `simple_server/` foundation: `util` / `tlv` / `text` / `transport` + tests | **done** (72 tests; full suite 1184 green) |
| 1 | card-type detection + file model + SELECT/READ/WRITE + card init | **done** (50 tests; full suite 1234 green) |
| 2 | SCP80/OTA + SMS/CAT/ENVELOPE | **done** (116 tests; full suite 1350 green) |
| 3 | eUICC ES10 + HTTP handler/CAT + `simple-server` entry point + cutover | not started |

## Phase 0 outcome

`simple_server/` now has `util.py` (`h2b`/`b2h`/`rpad`/`all_subclasses`/
`parse_command_apdu`), `tlv.py` (`BerTlv`/`ComprTlv` + BER/COMPR length+tag
codecs + `ber_tlv_list`/`tlv_map`/`parse_one`), `text.py` (`pack_gsm7`/
`unpack_gsm7`, `decode_stk_text` with all three Annex A variants, `decode_dcs_text`,
`encode_text`) and `transport.py` (`PcscLink` PC/SC link with the T=0 rules —
case transform, `61xx`/`9fxx` GET RESPONSE, `62xx`/`63xx` dummy, `6cxx` re-send,
T=1 pass-through — plus `ApduTracer`/`ProactiveHandler` hooks).  No
`pySim`/`osmocom`/`construct` import.  Note: `decode_stk_text` defaults to the
8-bit GSM default alphabet (matching pySim's `GsmString`) with `packed=True`
for 7-bit packed data; `send_apdu_checksw` is exact-match only for now (masks
come with the Phase 1 file model).

## Phase 1 outcome

`simple_server/` gained `fcp.py` (FCP/FCI parsing: file size, the Table 11.5
file descriptor - shareable b7, file type, structure incl. the BER-TLV row -
record length/count, FID, DF name; `62` and `6F` templates; malformed input
yields all-`None` fields), `files.py` (`FileInfo`/`FileModel` over the
`frontend/uicc_files.json` entries, with a synthetic MF root; case-insensitive
name/FID/AID/path resolution, `AmbiguousFile` on a bare FID/name that matches
several files, custom-file injection flagged `source='custom'` and an unknown
parent flagged `parent_known=False`, `path_chain()` for SELECT),
`commands.py` (`FileOps`: SELECT by FID/AID, READ/UPDATE BINARY/RECORD,
STATUS, and the FCP-driven `read_file()` port of `_read_selected_ef`) and
`cards.py` (`detect_card` - USIM AID, then the ISO MF select, then the TS 51
011 §9.2.1 SIM form `A0A4 0000 02 3F00`, ISD-R best-effort; `init_card` =
connect/reset + ATR + detect + MF select, with a reset retry when probing left
an ADF selected).  `util.sw_matches()` adds the `?` SW mask
(`9???`/`6A??`) used by `send_apdu_checksw` and the file ops.  Card-type
decision: the ISO MF form succeeding is **UICC**, not SIM - only the
TS 51 011 command form (CLA `A0`, P1/P2 `00`) means a 2G SIM, so a UICC with
no selectable USIM is not misclassified and keeps the FCP-returning SELECT.
TERMINAL PROFILE + the proactive drain remain Phase 3 (documented in
`cards.init_card`).  50 new tests (`tests/test_simple_files.py` + two transport
tests); full suite 1234 green.  Not done: `simple_server` is not in
`pyproject.toml`'s package list yet (it ships with the Phase 3 entry point).

## Phase 2 outcome

`simple_server/` gained `scp80.py` (SCP80 secured packets: the TS 102 225
coding tables, `spi_from_bytes`, `Keyset.from_bytes` (algorithm nibble +
keyset number, `00` → `implicit`, an unknown nibble refused only when the
algorithm is used), the `CryptoAlgo`/`AuthAlgo` classes over `pycryptodomex`
(DES/3DES/AES-CBC, CBC-MAC and AES-CMAC-8), `encode_cmd` (any size - the
`_encode_cmd_unlimited` coding plus the unciphered CPL octets of
`_build_secured_packet`, so a >140-octet packet can be split),
`build_secured_packet`, `split_secured_packet` + the `SCP80_*` capacities,
`decode_resp` (RPI check, response header, optional deciphering with the
PCNTR strip, CC/RC verification), `parse_compact` (TS 102 226 §5.1.2 Table
5.1), `counter_tracked` and `increment_counter_hex`), `sms.py` (the TS 23.040
`UserDataHeader` with the concat IEs `00`/`08` and RPI `71`, `SmsSubmit` and
`SmsDeliver` parse/build, `encode_oa`/`encode_tp_address`/
`decode_tp_address`(+`_value`), `encode_scts`/`decode_scts`,
`decode_ud_text`, the SMS-PP download `build_sms_tpdu`, `find_sms_tpdu`,
`ud_offset` and `parse_sms_concat`) and `cat.py` (`DeviceIdentities`,
`Address`, the `D1` `SmsPpDownload` template and `envelope_apdu`).

**Parity with the existing server** was checked with a throwaway script that
imported both implementations: every pinned command vector
(`_build_secured_packet` × 13 parameter sets + 10 random C-APDU lengths per
SPI), all 256×256 SPI combinations, the six pinned response packets
(`decode_resp` vs `_decode_por`: status/TAR/CNTR/PCNTR/RPL/RHL/RC-CC/secured
data), the four `_build_sms_tpdu` layouts, the ENVELOPE bytes against pySim's
`SMSPPDownload` (with and without the address object) and `find_sms_tpdu` on
the real FETCH captures are **byte-identical**.  Three deliberate deviations:
`ud_offset` fixes the existing `_calc_ud_offset` DELIVER branch (it omits the
address type octet, so its TP-UD offset is one octet short and
`parse_sms_concat` cannot read an SMS-DELIVER TPDU - the download direction);
the response RC (`por_rc_cc_ds = 'rc'`) is verified as CRC-32 over the same
range as the CC (pySim has a TODO there and refuses); and `decode_resp` walks
the actual UDH length rather than pySim's hard-coded 3 octets (identical for
the conforming `02 71 00` header).  Documented gaps: the DS signature coding
is refused (the specs leave the scheme open), the response-scripting content
parse (TS 102 226 §5.2.2) is left to the caller, and no card vector exists in
the corpus for an RC-only response - that branch is pinned by a
self-consistency test only.

116 new tests (`tests/test_simple_scp80.py` 62, `tests/test_simple_sms.py`
54); full suite 1350 green.  Still not in `pyproject.toml`'s package list
(same as Phase 1 - it ships with the Phase 3 entry point).

## Goal & scope

- A parallel, self-contained server in `simple_server/` (sibling of
  `pysim_simple_server/`).  `pysim_simple_server/` stays untouched as the
  fallback until feature parity and cutover.
- **PC/SC readers only.**  Serial readers are out of scope (rare; they'd need a
  software T=0 protocol — reset/ATR/PPS/procedure bytes).  `pyserial` dropped.
- **Dependency floor:** `pyscard` (PC/SC), `pycryptodomex` (DES/3DES/AES/CMAC
  for SCP80), `gsm0338` (GSM 03.38 codec).  Optional later: inline `gsm0338`'s
  ~128-char table to reach exactly two dependencies.
- **Hard constraint:** no `import pySim`, `osmocom`, or `construct` anywhere in
  `simple_server/` — we have our own TLV constructor and text codecs.

## Decisions (locked)

- PC/SC only; serial readers out of scope.
- Drop `osmocom` and `construct` too (our own TLV + text codecs).
- Shared pysim-free modules (below): **import them from `pysim_simple_server`
  for now** (its `__init__.py` is empty, so no pysim is pulled in); decide a
  move to `simple_server` once the new server runs.
- Runtime selection: **default = a separate `simple-server` console script**
  (recommended); `--backend` flag left as an open question if the user prefers
  one entry point.

## Dependency inventory

### pysim modules we use (the surface to replace)

| Area | pysim modules | What we call | ~size |
|---|---|---|---|
| Transport / APDU | `transport.__init__` (403L), `transport.pcsc` (143L) | `init_reader`, `LinkBase.send_apdu` (T=0 `61xx`/GET RESPONSE), `ApduTracer`, `ProactiveHandler`, `parse_command_apdu` | ~550L |
| Card commands | `commands.SimCardCommands` (806L) | `select_file`, `read/update_binary`, `read/update_record`, `get_atr`, `reset_card`, `status`, `verify_chv` | |
| Card type + file model | `cards` (193L), `filesystem` (1683L), `runtime` (685L), `profile`, `ts_102_221` (994L), `ts_51_011` (1344L), `ts_31_102` (1853L), `ts_31_103` (243L), `ts_31_102_telecom` (311L) | `card_detect`, `CardProfileUICC/SIM`, `CardModel.apply_matching_models`, `CardApplication`+`CardDF`, `RuntimeState`/`RuntimeLchan` | **~6300L** |
| Shell | `pySim-shell.py` (loaded via `shell.py`), `card_handler.CardHandler` (146L) | `PysimApp` (cmd2): `onecmd_plus_hooks` (`/api/command`), `option_parser`, `app.card/rs/lchan/sl/stdout`, `equip`/`do_equip`/`do_reset` | large |
| SCP80/OTA | `ota.OtaKeyset`, `ota.OtaDialectSms` (500L) | `encode_cmd`, `decode_resp`, `SmsResponsePacket.parse` | 500L |
| SMS / CAT / ENVELOPE | `sms` (447L), `cat` (1404L), `ts_31_102.SMSPPDownload` | `SMS_SUBMIT.from_bytes`, `UserDataHeader.from_bytes`, `DeviceIdentities`, `Address`, `ProactiveCommand` | ~1800L |
| eUICC ES10 | `euicc` (672L) | `CardApplicationISDR.store_data_tlv`/`get_eid`, ~15 Req/Resp classes | 672L |
| log/exceptions/utils | `log.PySimLogger`, `exceptions`, `utils` | `NoCardError`/`ReaderError`/`SwMatchError`/`ProtocolError`, `h2b`/`b2h`/`all_subclasses` | trivial |

### pysim-free modules (already reusable, 12 of them)

`presets.py`, `testscript.py`, `events.py`, `capmem.py`, `catrelease.py`,
`netsim.py`, `netstate.py`, `outfile.py`, `scp81.py`, `httpota.py`,
`test_scripts.py`, `test_suites.py`

### `osmocom` / `construct` surface we use (to reimplement, ~200-300L)

`BER_TLV_IE`, `COMPR_TLV_IE`, `bertlv_parse_one_rawtag`, `flatten_dict_lists`,
`rpad`, `h2b`, `b2h`, `all_subclasses`, `GsmOrUcs2Adapter`, `GreedyBytes`.

### Dependency drop matrix

**Keep:** `pyscard`, `pycryptodomex`, `gsm0338`.
**Drop:** `cmd2`, `pyserial`, `pytlv`, `jsonpath-ng`, `bidict`, `pyyaml`,
`colorlog`, `termcolor`, `packaging`, `smpp.pdu`/`smpplib`/`smpp.twisted3`,
`asn1tools`, `cryptography`, `osmocom`, `construct`.

### What we already own (reduces the work)

EF decoders (~30 files, client-side) + `EF_ENCODERS`; `frontend/uicc_files.json`
(committed asset) + `SUPPLEMENT`; `_read_selected_ef`, `_select_path_minimal`,
`_fcp_value`, `_pbr_parse`/`_pbr_inject_children`; `_encode_cmd_unlimited`
(SCP80 encode); `_por_header_parse` (SCP80 response header); ENVELOPE builders;
`_decode_dcs_text`/`_decode_stk_text`/`_pack_gsm7`; FCP/TLV helpers
(`parse_ber_len`, `_ber_tlv_list`, `_tlv_map`).

## Phase plan (steps)

**Phase 0 — foundation.** `simple_server/util.py` (hex/APDU helpers),
`tlv.py` (BER-TLV + comprehension-TLV), `text.py` (GSM 7-bit/UCS2/Annex A),
`transport.py` (PC/SC link + T=0 APDU exchange).  Pure, card-free-testable.
Acceptance: unit tests green; `grep` proves zero forbidden imports; full suite
still green.

**Phase 1 — card + file model.**  Card-type detection (UICC/SIM/eUICC via ADF
AID probe), the file model (name↔FID↔path from `uicc_files.json` +
`SUPPLEMENT` + custom files), SELECT/READ/WRITE, and card initialisation
(own the ATR/reset/probe/MF-restore/TP-drain sequence; `--no-card-init` = skip
a phase).

**Phase 2 — SCP80/OTA + SMS/CAT.**  Secured-packet encode/decode (port
`_encode_cmd_unlimited`, own `OtaDialectSms` + DES/3DES/AES), `SMS_SUBMIT`
parse, `UserDataHeader`, ENVELOPE builders.

**Phase 3 — eUICC ES10 + HTTP.**  ES10 TLV classes on our own `BerTlv`, the
HTTP handler/CAT logic, then a `simple-server` entry point and cutover.

## Helper scripts at cutover

When the pysim-free server becomes the default, split the helper scripts
(both `.sh` and `.bat`):

- **Legacy pysim** — rename the current scripts to `setup-pysim` / `start-pysim`
  / `update-pysim`.  Unchanged behaviour: create the venv and `pip install`
  pysim + its deps (the current `setup.sh`/`setup.bat` + `requirements-pysim.txt`).
- **Pysim-free** — the new `setup` / `start` / `update`.  `setup` creates the
  venv and installs **only our dependencies** — `pyscard`, `pycryptodomex`,
  `gsm0338` — from a small `requirements.txt` (no pysim, no
  `requirements-pysim.txt`), then `pip install -e .`.  Reuse the venv-validation
  + recovery logic (v3.38.1) and the Windows Python-3.13 / Linux
  `libpcsclite-dev` notes: **pyscard still builds from source on Linux and needs
  the PC/SC headers**, so `setup` keeps that prerequisite.  `start` keeps the
  PC/SC access pre-flight and tee-to-log, minus pysim.

## Design principles & benefits

- **Custom files are first-class.**  A file is `{name, fid, parent_df, kind,
  source}` with `source ∈ {standard, custom, pbr, supplement}` — "custom" is a
  tag on one tree, not a special case.  Replaces the Profiler localStorage
  customs, the `allow_probe` probe/detach, `_pbr_inject_children`, and
  `SUPPLEMENT` (four workarounds → one data structure).
- **Card init is a phase, not a fight with upstream.**  Own the whole sequence;
  skip = don't call a phase.  Removes `do_equip`/`do_reset` monkeypatching.
- **Proactive sessions are fully ours.**  pysim was only *negatively* involved
  (`_DefaultProactiveHandler` disables its auto-handler).  Deleting that stub is
  the whole point, not a capability loss.
- **Free deletions:** the tracer-nulling saga (v3.40.10/3.40.11,
  `_keep_tracer_attached`, `_onchange_apdu_trace`) and the stdout/StringIO
  juggling (`_capture_app_output`, `_LineFilter`, `app.stdout`/`sys.stderr`,
  `PySimLogger.setup`) both disappear because their cause — pySim's lifecycle —
  is gone.

## Licensing & repo split plan

Current license is **GPL-2.0-or-later**, for two reasons the repo itself
documents (`LICENSE` / `THIRD-PARTY-NOTICES`):

1. the server **imports pysim** (GPL-2.0-or-later) — a runtime dependency; under
   the FSF's reading a program that links to a GPL library is a derivative work
   while so linked;
2. the **client-side EF decoders** are *ported/adapted from simtrace-analyser*
   (GPL-2.0-or-later; see `frontend/index.html`).

The two anchors unblock on different timelines:

- **pysim** is third-party (osmocom) and cannot be relicensed — the only way out
  is this phase-out: stop importing it and write the replacement from the pinned
  specs, not adapted from pySim code.  In progress.
- **simtrace-analyser is ours** (we hold the copyright) — we *can* relicense it, so
  the frontend decoders are an unblocked **coordination step**, not a
  re-implementation.

### Rollout (staged)

1. **Stay on GPL while `pysim-simple-server` is bundled.**  The server package
   that imports pysim is the GPL anchor; until it is out of the shipped artifact
   the whole repo stays GPL.
2. **Complete the pysim phase-out** into `simple_server` (the phases above),
   written from the specs.
3. **Relicense simtrace-analyser** (our own action) — or re-author the ~30
   client-side EF decoders from the specs.  Either clears the second GPL anchor.
4. **Split the repos at cutover:**
   - `simple-pysim` (or keep `pysim-simple-server`) — the legacy server that
     imports pysim; stays **GPL-2.0-or-later**.
   - `simple` — the new pysim-free server + PWA; licensed **MIT** (or the chosen
     permissive license).
5. Update `LICENSE`, `THIRD-PARTY-NOTICES`, `pyproject.toml`, the READMEs and the
   phase-out notes to match.

### Notes & caveats

- **Derivative-work boundary is subtle:** formats/protocols are facts, but *code*
  is expression.  The non-GPL `simple` must be written from the specs, not copied
  or closely adapted from pySim or simtrace-analyser code (the "behavioural port of
  pySim" / "byte-pinned to pySim's output" phrasing in the Phase-0 docstrings is a
  thing to keep an eye on for this reason).
- Remaining deps are license-compatible with MIT on our own code: **pyscard**
  (LGPL-2.1), **pycryptodomex** (BSD + public domain), **gsm0338** (MIT).
- Relicensing needs the consent of *all* copyright holders — if there are
  contributors beyond the owner, factor that in.
- `GPL-2.0-or-later` only allows later *GPL*, never a permissive license.
- This is a plan, not legal advice.

## Open ideas & questions (running list)

- Cutover strategy: when/how to switch the default entry point from
  `pysim_simple_server` to `simple_server` (dual-run, then flag-flip, then
  archive?).
- Whether to later **move** the 12 pysim-free modules into `simple_server/`
  (single source) instead of importing from `pysim_simple_server`.
- Whether to inline `gsm0338` to reach exactly two dependencies.
- `/api/command`: provide our own command set, or retire the pySim-shell
  passthrough in favour of the typed endpoints?
- Runtime selection: separate `simple-server` script (default) vs `--backend`
  flag.

## How to use this file with subagents

When spawning a subagent for a phase-out task, include:
1. a pointer to this file (read it first), and
2. the specific task + acceptance criteria + verification commands.

The subagent needs no other context about scope/decisions/inventory — it is all
here.
