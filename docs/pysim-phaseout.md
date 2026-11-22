# pysim phase-out — plan & worklog

> **Canonical record** for removing the `pysim` dependency from SIMple.
> Share this file with any subagent working on the phase-out — it is the single
> source of truth for scope, decisions, dependency inventory, status and open
> ideas.  Update the status table and the open-ideas list as work progresses.

## Status at a glance

| Phase | What | Status |
|---|---|---|
| 0 | `simple_server/` foundation: `util` / `tlv` / `text` / `transport` + tests | **done** (72 tests; full suite 1184 green then) |
| 1 | card-type detection + file model + SELECT/READ/WRITE + card init | **done** (50 tests; full suite 1234 green then) |
| 2 | SCP80/OTA + SMS/CAT/ENVELOPE | **done** (116 tests; full suite 1350 green then, 1367 after the review fixes) |
| 3 | eUICC ES10 + HTTP handler/CAT + `simple-server` entry point + cutover | **in progress** — 3a (ES10) and 3b first half (proactive/TR layer) done; HTTP handler/CAT, the entry point and cutover remain |

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
54); full suite 1350 green then (1367 after the review fixes below; the suite is
1513 today - the counts in this worklog are the count at the time, not a
currency claim).  Still not in `pyproject.toml`'s package list
(same as Phase 1 - it ships with the Phase 3 entry point).

**Review fixes** (the pysim-free code review, same series): `transport._send_t0`
bounds the T=0 GET RESPONSE chaining (`T0_MAX_ROUNDS` = 8; when exhausted it
logs a warning and returns the last `(data, sw)`, so a card that keeps
answering `61xx`/`62xx`/`63xx` can no longer hold the caller - the byte
behaviour of a conforming 1-2 round exchange is unchanged); `files._find_parent`
resolves a mixed name/FID parent path (`MF/DF.TELECOM`) segment by segment and
accepts a DF/ADF, so such a custom file is selectable; `scp80.decode_resp`
turns a misaligned ciphered response (Cryptodome's `ValueError`) into
`Scp80Error`; `sms.build_sms_tpdu`/`SmsDeliver.to_bytes`/`SmsSubmit.to_bytes`
accept the hex-string forms of `scts`/`vp` (a `str` used to raise `TypeError`);
`tlv.ber_tlv_list` stops at a truncated/indefinite length form (`8b 81` -> `[]`
instead of a fabricated `(tag, b'')`); the duplicated `scp80` SPI1 comment and
the unreachable `increment_counter_hex` branch are gone.  17 new tests
(transport case 1/3, `91 00`, `63xx`, `6Cxx` after a GET RESPONSE round and the
chaining bound; the name/FID/EF parent paths; the ciphered-PoR and TPDU forms;
TLV truncation; a pin that the watchdog's thread name matches
`_test_watchdog_start`); full suite 1367 green.

## Phase 3b outcome (first half: the pure proactive/TR layer)

`simple_server/proactive.py` (command decode) and `simple_server/tr.py`
(TERMINAL RESPONSE build + decode) exist.  They are behavioural ports of the
existing server's private helpers - `parse_proactive_header` (`_parse_...`),
`decode_proactive_command` (`_decode_cmd` + the DISPLAY TEXT / SELECT ITEM /
SET UP MENU / GET INKEY / GET INPUT / BIP / TIMER / SEND SHORT MESSAGE /
event-list sub-decoders), `decode_poll_negotiation`, `build_tr`, `decode_tr`,
`tr_data_only`, `extract_tr_result` and the §8.12.0/§8.12.2 result tables -
with the I/O switched to bytes and the `pySim`/`osmocom` surface replaced by
`simple_server.tlv`/`text`/`sms`/`cat` (the BIP `Other address`/transport-level
decoders and their tags came along from `httpota`, which stays in the legacy
package).  Everything is pure: no transport, no card state, no module globals.

Deliberate deviations from the legacy helpers - each pinned by a fixture in
`tests/test_simple_proactive.py` (68 tests), with the duplicated tables and the
shared decoders cross-checked against the legacy implementation by
`tests/test_simple_drift.py` (18 tests; the one module allowed to import both):

- **The command TLV region is everything after the `D0` length**, not clipped
  to it: a card's template length is sometimes off by a byte, and the walk stops
  at a truncated TLV anyway (the tolerance `sms.find_sms_tpdu` documents).
  Pinned by `TemplateToleranceTests` with a template length one octet short and
  one octet long.
- **Objects are walked as TLVs where the legacy code byte-scanned**
  (`find_duration` and the SET UP EVENT LIST decode): the legacy
  `raw.find(b'\x99')` read a `0x99` octet *inside a value* as the event-list
  tag, while the walk sees the real object (pinned by a fixture with a `0x99`
  in the Alpha identifier).  The BER long-form lengths the walks must survive
  are pinned by the live `D0 81 ab … 8B 81 96 <150-octet TPDU>` capture (the
  v3.6.57 `IndexError` in the FETCH path) and the `8D 81 90` DISPLAY TEXT.
- **`decode_tr` accepts the complete TERMINAL RESPONSE payload**, not only its
  data part (a superset of the legacy `_decode_tr`, which takes the data part):
  the boilerplate is stripped in both cases.  Its fallback row prints the
  *stripped* bytes (the legacy printed the hex it was handed - the same string
  on the real call path) and a data part too short for one object has no rows.
  Harmless for the real callers, pinned by `DecodeTrTests`.
- **Address digits stay uppercase**: a TON 5 (alphanumeric) address is not BCD,
  so the nibbles are printed as hex - uppercase, the form
  `simple_server.sms.decode_tp_address` documents (pinned by a fixture; the
  legacy went through pySim's `Address`, which lowercases).
- **`decode_proactive_command` never raises on a hex string**: `raw` that does
  not parse as hex is treated like an empty buffer (the proactive log renders
  whatever the FETCH returned).
- **`build_tr` takes the terminal's own answer objects** as `answer_tlvs`
  (bytes, hex or `(tag, value)` pairs) instead of reading the PLI dictionary /
  poll interval, and selects the Result object - the general `83 02 R I` form
  by default (the scripted-TR shape) or the basic `03 01 R` form the terminal
  chain uses (both byte-identical to the respective legacy builders).  Its
  `dev_src`/`dev_dst` are the *fetched command's* identities and the TR swaps
  them (§6.8.0 "Direction: terminal to UICC"; §8.7 orders the object source,
  destination), so a UICC -> terminal command's answer reads `02 02 82 81`.

No new captures were taken for the port: every fixture is an existing live
capture (the Alfa SET UP MENU and its long-form SEND SHORT MESSAGE, the
RemMobileID GET INPUT) or a reference trace (OPEN CHANNEL / SEND DATA), so the
decoders are measured against card data, never against a re-encode of
themselves.

Not done (the second half): wiring these into the FETCH/TERMINAL RESPONSE chain,
the STATUS poll, the menu state machine and the PLI dictionary/clock answers.

## Phase 3c-1 outcome (the CAT session engine)

`simple_server/cat_session.py` exists: one `CatSession` per card owns the CAT
conversation - `send_status`, `send_terminal_profile` (plus
`resend_terminal_profile`, the v3.23.7 boundary-marker path), `handle_chain`
(the FETCH/TERMINAL RESPONSE loop with `on_fetch` pause/exit, the poll-interval
adoption, the injected BIP handler/flush, the test-run abort and the round
bound), `send_envelope` (the SMS-PP download template, the `61xx`/`62xx`/`63xx`
GET RESPONSE quirk, the `91xx` chain that keeps a delivered PoR, the inline and
SEND-SHORT-MESSAGE PoR capture via `PorSubmitCapture`, the late-PoR STATUS
poll), `send_event_download`, the PLI dictionary + host-clock answer, the
interactive menu (`menu_fetch_handler`, `menu_send_response`,
`finish_pending_menu`, the `menu_timeout` watchdog), TIMER MANAGEMENT
(`handle_timer_command`, the expiry ENVELOPE with the `93 00` retry,
`cancel_timers`) and the proactive log (`log_proactive`, `record_tr`,
`entries`, `mark_session`, `reset`).

Deliberate, documented deviations from the legacy server - each one is
cross-checked against the legacy bytes by `tests/test_simple_drift.py`
(byte parity for the STATUS/TP/FETCH/TR/ENVELOPE/timer APDUs, the PLI
dictionary, the input coding and the timer expiry envelope):

- **The status word is bytes** (`b'\x91\x12'`) on the wire API instead of the
  legacy hex strings; the log entry's `tr_sw` and the menu response's `sw` stay
  hex because they are API fields the PWA renders.
- **No stderr writes**: the legacy's per-exchange trace lines are `log.debug`/
  `log.info` records (the server's console/tracer layer owns presentation).
- **The terminal-profile chain is bounded** by `chain_rounds` (the legacy loop
  was unbounded; the same safety the v3.25.1/v3.37.0 chain bound gave) - it
  keeps the legacy's `0xFF` FETCH length.
- **The session owns sim_menu/event_list**: `send_terminal_profile` caches what
  it saw (the legacy caller did that assignment), and a background-poll timer is
  not created here - `poll_interval`/`poll_suspended` record the card's wishes
  (§6.4.6/§6.4.14) for the server's poll.
- **`busy`** replaces `_PROACTIVE_BUSY`, and the timer expiry waits on it as
  well as on `stk_pending` (the session has no card lock).
- **Refusals raise `CatSessionError`** where the legacy helpers returned an
  HTTP-shaped `(payload, 400)` or an `UnboundLocalError`.
- **Kept, and flagged for review**: `handle_timer_command` emits the timer
  objects before the Result (the legacy form the live cards accepted) while
  §6.8.0 puts the Result third - a behavioural change needs card evidence, so
  the port pins the legacy bytes and documents the deviation.

Not done (later sub-phases): the BIP command handlers (injected), the HTTP
endpoints and the card-session wiring.

## Spec compliance (spec-checker)

Every spec-sensitive module is verified against the pinned PDFs
(`/home/catarrh/kb/uicc-esim-kb/`) by the spec-checker before it is relied on.

- **Phase 0/1 (all CONFIRMED):** the FCP file-descriptor byte (TS 102 221
  Table 11.5 — shareable b7, file type b6–b4, structure b3–b1, BER-TLV = b6–b4
  111 + b3–b1 001), the three Annex A alpha-field variants (TS 102 221 Annex A),
  the CAT CLA (80 UICC / A0 SIM), the SELECT forms (Le 00 for the full FCP; the
  TS 51 011 §9.2.1 SIM form) and the T=0 rules (TS 102 221 §7.3.1.1 — case
  transform, GET RESPONSE on 61xx/62xx/63xx, 6Cxx resend).
- **Phase 2 (CONFIRMED, one fix):** the SPI coding (TS 102 225 §5.1.1), KIc/KID
  algorithm nibble + keyset number (§5.1.2/A.2), the CNTR/PCNTR/CHL/CPL header,
  the SMS-SUBMIT TPDU + UDH concatenation (TS 23.040) and the SMS-PP download
  ENVELOPE (TS 31.111 §7.1.1.2 — source Network 83 / destination UICC 81).
  Fixed: SPI1.b5b4=11 is "one higher", not "lower" (the legacy server keeps
  pySim's misnomer enum key — it is passed straight to pySim's construct Enum;
  `scp80` uses the spec label).  Documented limitations: RC is CRC-32 only
  (CRC-16 not offered), and KID's meaning is context-dependent (CRC for RC,
  CBC-MAC/CMAC for CC) — `scp80` computes RC via `zlib.crc32` directly, so the
  flat auth-algo map is only used for CC.
- **Phase 3a (ES10, spec-checked + live-tested):** the `profileIdentifier`
  `A0` wrapper is spec-correct — SGP.22 §2.8 `AUTOMATIC TAGS` supplies the
  SEQUENCE's `[0]` tag around the `4F` AID / `5A` ICCID CHOICE (the
  "untagged" reading was wrong); `profileInfoListError undefinedError(127)`
  CONFIRMED (pySim's `2` is wrong, spec wins).  `refreshFlag` is a `BOOLEAN`,
  DER-encoded (TRUE = `FF`), so `RefreshFlag` now encodes `FF` (pySim writes
  `01`).  **Live destructive switch test (user-approved, real eUICC):** the
  read path (EID, EUICCInfo1/2, addresses, GetProfilesInfo, ListNotification)
  is verified; `EnableProfileReq` is byte-identical to pySim's form and the
  switch **succeeds with `refreshFlag` FALSE** (a silent switch) but is
  **refused with an undefined `enableResult` 7 when TRUE** (both `01` and
  `FF`) — a card deviation.  The server re-initializes after a switch anyway
  (`_esim_reinit`), so the Phase 3c switch orchestration should prefer
  `refreshFlag` FALSE; the legacy server still defaults TRUE/`01`.

## Phase 3c API surface (locked decisions)

The pysim-free server must not expose pySim's interpreter.  The PWA's
Card reader tab currently has four pills (File manager / Services / **pySim
command line** / Raw APDU) and a status block fed by `/api/status`:

- **Remove the pySim command line.**  No `/api/command` passthrough, no
  `/api/help`, no `/api/commands`, no `/api/cardinfo` (the PWA never used
  `cardinfo`; it exists only as an API surface).  The PWA's *pySim command
  line* pill, its `pysimExec`/`pysimLoadCommands`/`pysimCmdInput`/
  `pysimCmdKeydown` helpers, the command-suggestion/help UI and the
  `pysim-cmdline` help section go at cutover (the legacy server keeps its
  endpoints; the PWA just stops calling them).  The three actions the pill
  was used for get dedicated endpoints instead: `POST /api/equip`,
  `POST /api/reset`, and the state from `GET /api/status`.
- **Keep raw APDU, with the automatic proactive handler on `91XX` by
  default.**  `POST /api/apdu {apdu}` stays (core workbench feature),
  implemented directly on our `transport.send_apdu` with the same
  `{response, sw}` contract.  Like the legacy
  `scc.send_apdu_checksw`/`_DefaultProactiveHandler` path, a `91XX` answer
  auto-fetches the command and answers it with a TERMINAL RESPONSE (the
  full proactive chain - the PLI dictionary/clock, BIP, timer and the
  display/menu auto-answers), so a card that queues a command as a side
  effect is never left with an unanswered FETCH.  It is off only when
  something else owns the card: a test script (the run owns it; `/api/apdu`
  answers 409, `_TEST_BLOCKED_PATHS`) or a running BIP session (the BIP
  terminal answers its own commands; the auto-handler must not shortcut
  them).  "Raw" means the APDU is sent exactly as given; the handler acts
  only on the `91XX` answer.
- **Replace pySim's status block with our own.**  The PWA's status line
  today prints pySim internals — `Card: <name>`, `Profile: <rs.profile>`,
  `CLA`/`SEL` (the `SimCardCommands` parameters), logical `Channels`, and a
  pySim-shaped `current_selection` (`name/type/fid/path/file_size/record_len/
  num_of_rec`).  The new `/api/status` defines its own shape — no pySim
  concepts — e.g. `reader`, `atr`, `card` (our class: UICC/SIM/eUICC, plus a
  human name), `iccid`, `connected`/`card_present`/`equipping`/
  `card_session`, and a `selection` object we own (`path`, `name`, `fid`,
  `kind`, `size`, `record_len`, `num_records`).  The PWA renders that shape
  instead of the pySim one; all the information the UI actually needs stays,
  the pySim internals go.
- **Drop "Verify vs pySim" and do NOT replace it with "Verify vs server".**
  The SCP80 tab's `/api/sp-verify` cross-checks the PWA's JS packet builder
  against pySim's `OtaDialectSms.encode_cmd`; with no pySim there is no external
  reference left.  The JS and Python packet builders are the same logic, so a
  cross-check is a symmetry check, not a correctness proof.  The
  cross-implementation drift is already covered by tests: the **same byte
  vector** `00201506091515B00000C08F58C38860ACB3A362FFFE670AD13759A2A6B4C1A91116`
  is pinned in `frontend/tests/sp.test.js`, `tests/test_ota_helpers.py` and
  `tests/test_simple_scp80.py` (plus the other shared vectors), so a builder
  that drifts breaks its own suite.  Keep that pinning (a shared fixture would
  be a nice-to-have, not required).  The PWA's own packet preview stays.
- **Keep, but back with our model:** `/api/tree`, `/api/select`, `/api/read`,
  `/api/write` (our `files`/`commands`/`fcp` modules), `/api/apdu`, the
  presets/test-script stores (already pysim-free) and `/api/esim/*` (already
  reimplemented on `simple_server.esim`).

**Deferred idea (user, 2026-11-22): two SCP80 send buttons.**  Split "Send to
Card" into (a) *send the secured packet as built in the frontend* and (b) *send
the C-APDU + settings and let the server build the packet*.  The server already
supports both forms today - `/api/send-ota` takes `sp` (pre-built) or `apdu` +
the SPI/keys/counter parameters - but the PWA only ever sends `sp`, so this is
a UI affordance, not new capability.  Note the counter semantics differ: the
`sp` path uses the packet's own counter (the caller's bookkeeping), while the
`apdu` path seeds from the preset's keyset and persists the consumed counter
(server-owned since v3.20.0), so the two buttons are not interchangeable.
Not implemented yet - the single "send exactly what is previewed" flow is the
clearest mental model for a workbench; add the second button if a concrete
need appears (e.g. suspecting the JS builder, or sending a C-APDU without
building it in the UI).

These are Phase 3c (server) + cutover (PWA) work; the legacy server and the
current PWA stay untouched until the cutover.

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

The phase is split so each piece is reviewable:

- **3a — ES10 (done):** `simple_server/esim.py`, spec-checked + live-tested.
- **3b — pure proactive/TR layer (done):** `simple_server/proactive.py` and
  `simple_server/tr.py`, no card state.
- **3c-1 — the CAT session engine:** `simple_server/cat_session.py` — the
  `CatSession` state (proactive log, menu, pending command, timers, PLI
  dictionary), the FETCH/TERMINAL RESPONSE chain, STATUS / TERMINAL PROFILE /
  ENVELOPE exchanges (incl. the 61xx/62xx/63xx GET RESPONSE quirk), event
  downloads, menu responses/timeout and the timer-expiry ENVELOPE.  BIP is
  injectable (a `bip_handler` callable) so the next piece slots in.
- **3c-2 — BIP:** `simple_server/bip.py` — the BIP command handling
  (OPEN/CLOSE CHANNEL, SEND/RECEIVE DATA, GET CHANNEL STATUS) on
  `pysim_simple_server.httpota.BipTerminal`, the Data-available event and the
  Channel-status event.
- **3c-3 — HTTP handler + state:** `simple_server/server.py` (or `http.py`) —
  the REST endpoints on the locked API surface (no pySim command line, keep
  the raw APDU with its automatic 91xx handler, an own `/api/status` shape,
  no "Verify vs server"), the card-session state (`_CARD_LOCK` discipline),
  card init/equip/reset, file ops, presets/test-script stores, the SCP80/RAM
  operations and the eSIM endpoints.
- **3d — entry point + GSMTAP + cutover:** the `simple-server` console script,
  the true-wire-exchange GSMTAP tracer (`send_tpdu`-level), the helper-script
  split (`setup-pysim`/`start-pysim` vs `setup`/`start`/`update`), the PWA
  changes (remove the pySim command line, the new status block, drop "Verify vs
  pySim") and the 4.0.0 major release.

## Helper scripts at cutover

The repo split removes the need for two script sets in `simple`.  Each repo
ships **one** set:

- **`simple` (pysim-free)** — `setup` / `start` / `update`.  `setup` creates
  the venv and installs **only our dependencies** — `pyscard`, `pycryptodomex`,
  `gsm0338` — from a small `requirements.txt` (no pysim, no
  `requirements-pysim.txt`), then `pip install -e .`.  Reuse the venv-validation
  + recovery logic (v3.38.1) and the Windows Python-3.13 / Linux
  `libpcsclite-dev` notes: **pyscard still builds from source on Linux and needs
  the PC/SC headers**, so `setup` keeps that prerequisite.  `start` keeps the
  PC/SC access pre-flight and tee-to-log, minus pysim.  The `setup-pysim` /
  `start-pysim` names are **not** needed — the legacy lives in its own repo.
- **`simple-pysim` (the fork)** — keeps the current `setup`/`start`/`update`
  scripts and `requirements-pysim.txt` unchanged (venv + `pip install` pysim
  and its dependencies).  Frozen at the final 3.x tree; see the repo split
  below.

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

## Tracing, logging & GSMTAP (design)

Owning the transport is the chance to redesign the APDU tracing stack, which
today is split across pySim's `apdu_tracer`, our `StderrApduTracer` /
`_LoggingApduTracer`, the `_keep_tracer_attached` wrapper and `gsmtap.py`.

- **Log every APDU.**  The ISO 7816 C-APDU/R-APDU (raw, at the TPDU level) *and*
  the SCP80 plaintext C-APDU/R-APDU (decoded out of the secured packet) are all
  logged — one logging path, no per-path special cases.
- **Stream the whole ISO 7816 exchange to GSMTAP** (when requested), including
  the init phase.  Today `gsmtap.py` *reconstructs* wire TPDUs from the logical
  APDU (pySim's tracer only exposes the logical level) and the init phase was
  missed entirely (the v3.40.10/3.40.11 saga).  Our transport exposes
  `send_tpdu` — the raw TPDU — so the true wire exchange (including the `61xx`
  / GET RESPONSE TPDUs and the init phase) streams directly, no reconstruction.

**This is a headline release-note item.**  The log and GSMTAP data exist for
card testing, and the true wire exchange (real TPDUs, `61xx`/GET RESPONSE and
all, including the init phase) is what makes that trustworthy — pysim's
reconstruction + missed init phase was a real gap.  It is one of the major
reasons to phase out pysim.

Implications:

- `simple_server/transport.py` already has both hook levels: `ApduTracer`
  (`trace_command`/`trace_response` around `send_apdu`) and the raw `send_tpdu`.
  A GSMTAP tracer attaches at `send_tpdu` for the true wire stream.
- `pysim_simple_server/gsmtap.py` imports pySim (`ApduTracer`,
  `parse_command_apdu`); it is reimplemented on `simple_server` (Phase 3) with
  the same GSMTAP-SIM packet format but streaming the real TPDUs.
- The `_keep_tracer_attached` wrapper and the `_onchange_apdu_trace` monkeypatch
  disappear: the tracer is always attached, because the transport owns its own
  lifecycle.
- This is a Phase 3 concern (wired with the HTTP entry point), but it shapes the
  transport API *now*: `send_tpdu` is the raw hook, `send_apdu` the logical one.
- **Major version bump:** the cutover to the pysim-free server ships as a major
  release (4.0.0) — a new package/entry point, the GSMTAP true-wire-exchange
  behaviour, and the dependency change are all breaking.

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
4. **Fork `simple-pysim` at cutover** — a fresh repo from the **final
   published 3.x tree** (no history; the v1.7.0 consolidation's "fresh move +
   archive" recipe): `pysim_simple_server/`, the pysim-era `frontend/`, the
   tests, the scripts, `requirements-pysim.txt`, the GPL `LICENSE`, the
   READMEs.  It is a **frozen legacy tool** (maintenance mode: little or no
   update), GPL-2.0-or-later, with a README pointing at `simple` as the
   maintained tool.  Its PWA keeps the pysim-era UI (the pySim command line,
   the old status block, "Verify vs pySim").
5. **`simple` becomes fully pysim-free** — `pysim_simple_server/` is removed
   (the 12 pysim-free modules move into `simple_server/`), the
   `requirements-pysim.txt` / pysim install steps are gone, the PWA loses the
   pySim-specific UI (see the Phase 3c API surface), and the license moves to
   **MIT** once the simtrace-analyser relicensing lands.  One server, one
   script set, one API, one PWA code path.  Version 4.0.0.
6. Update `LICENSE`, `THIRD-PARTY-NOTICES`, `pyproject.toml`, the READMEs, the
   workspace `AGENTS.md` (which still describes `pysim_simple_server` as the
   server) and the phase-out notes to match.

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

- **Settled (2026-11-22): fork `simple-pysim` at cutover.**  The legacy is a
  fresh repo from the final 3.x tree (no history), frozen and little-updated;
  `simple` drops `pysim_simple_server/` entirely.  This replaces the earlier
  "both servers + two script sets in one repo" plan — one repo per tool, one
  script set each, one PWA code path in `simple`.
- **Settled (2026-11-22): move the 12 pysim-free modules into `simple_server/`
  at the fork** (so `simple` does not import `pysim_simple_server`).  Until
  then they stay in `pysim_simple_server/` and `simple_server` imports them.
- **Settled (2026-11-22): `/api/command` is retired** — no pySim-shell
  passthrough; dedicated `equip`/`reset` endpoints and the state from
  `/api/status` (see the Phase 3c API surface).
- Whether to inline `gsm0338` to reach exactly two dependencies.
- Runtime selection: the separate `simple-server` console script (default).

## How to use this file with subagents

When spawning a subagent for a phase-out task, include:
1. a pointer to this file (read it first), and
2. the specific task + acceptance criteria + verification commands.

The subagent needs no other context about scope/decisions/inventory — it is all
here.
