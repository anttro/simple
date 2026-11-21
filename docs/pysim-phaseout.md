# pysim phase-out — plan & worklog

> **Canonical record** for removing the `pysim` dependency from SIMple.
> Share this file with any subagent working on the phase-out — it is the single
> source of truth for scope, decisions, dependency inventory, status and open
> ideas.  Update the status table and the open-ideas list as work progresses.

## Status at a glance

| Phase | What | Status |
|---|---|---|
| 0 | `simple_server/` foundation: `util` / `tlv` / `text` / `transport` + tests | **done** (72 tests; full suite 1184 green) |
| 1 | card-type detection + file model + SELECT/READ/WRITE + card init | not started |
| 2 | SCP80/OTA + SMS/CAT/ENVELOPE | not started |
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
