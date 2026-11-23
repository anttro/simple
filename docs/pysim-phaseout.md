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
| 3 | eUICC ES10 + HTTP handler/CAT + `simple-server` entry point + cutover | **in progress** — 3a-3c and 3e are done (the whole HTTP API: files, SCP80/RAM, test scripts/suites, CAT/Simulator, eSIM/net-sim and the SCP81/BIP control), 3d-1 (the entry point + the GSMTAP tracer) is done, 3d-2 (the PWA cutover) is done and 3d-3b (the shared modules + the MCC/MNC data move into `simple_server`) is done; the 3d-3 release remains |

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
011 §9.2.1 SIM form `A0A4 0000 02 3F00`, ISD-R best-effort; **the probe order
and the SIM select form were corrected after the 2026-11-23 2G SIM live test -
see "Live smoke (2026-11-23): the 2G SIM card-detection quirks"**: MF → DF.GSM
→ USIM AID, with the A0 select carrying the response-data octet; `init_card` =
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
(byte parity for the STATUS, TERMINAL PROFILE, FETCH, TERMINAL RESPONSE and
ENVELOPE APDUs - the profile chain and the 91XX chain compared APDU by APDU,
the FETCH length included -, the PLI dictionary, the input coding and the
timer expiry envelope):

- **The status word is bytes** (`b'\x91\x12'`) on the wire API instead of the
  legacy hex strings; the log entry's `tr_sw` and the menu response's `sw` stay
  hex because they are API fields the PWA renders.
- **No stderr writes**: the legacy's per-exchange trace lines are `log.debug`/
  `log.info` records (the server's console/tracer layer owns presentation).
- **The terminal-profile chain is bounded** by `chain_rounds` (the legacy loop
  was unbounded; the same safety the v3.25.1/v3.37.0 chain bound gave).  Its
  FETCH length is the legacy profile-chain form: the `91XX` length, with the
  `0xFF` fallback for a bare `91` (a fixed `0xFF` made a T=1 card answer `6Cxx`
  with its command left pending).
- **The session owns sim_menu/event_list**: `send_terminal_profile` caches what
  it saw (the legacy caller did that assignment), and a background-poll timer is
  not created here - `poll_interval`/`poll_suspended` record the card's wishes
  (§6.4.6/§6.4.14, adopted in the profile chain too) for the server's poll.
- **`busy`** replaces `_PROACTIVE_BUSY`; both the timer expiry and the menu
  watchdog run their card exchanges under the injected `card_lock` (the
  legacy `_CARD_LOCK`), waiting for `stk_pending`/`busy` *before* taking it.
- **Refusals raise `CatSessionError`** where the legacy helpers returned an
  HTTP-shaped `(payload, 400)` or an `UnboundLocalError`.
- **`stk_pending` is JSON-safe**: `raw` is the command's hex text (the legacy
  stored no raw at all), so `/api/stk-status`'s `pending_data` serialises.
- **A bare `91` in the 91XX chain fetches with Le `00` (256)**: the legacy
  formatted `0x100` into the APDU as three hex digits, an odd-length APDU the
  card cannot parse; the TP chain keeps the legacy `0xFF` fallback.
- **Kept, and flagged for review (a)**: `handle_timer_command` emits the timer
  objects before the Result (the legacy form the live cards accepted) while
  TS 102 223 §6.8.0 (p. 102) puts the Result third - a behavioural change
  needs card evidence, so the port pins the legacy bytes (and a `TODO` names
  the cutover: order fix + bench run).
- **Kept, and flagged for review (b)**: the Item identifier is sent only for a
  successful SELECT ITEM, while §6.8.6 (p. 104) also requires it for "Help
  information required by the user" - the API accepts `item_id`, so the fix
  lands at cutover together with the PWA's help path.
- **ENVELOPE lengths use the BER coding** (TS 101 220 §7.1.2 Table 7.6): the
  `8B` TPDU object and the `D1` template are long-form from 128 octets on
  (`8b 81 82`), which is what makes a concatenated SCP80/RAM segment's
  ~130..150-octet TPDU valid.  `tlv.ComprTlv`, the input Text string and
  `tr.build_tr`'s Text object follow the same rule (the legacy pySim TLV
  library always did; only this port's own builders had one-octet lengths).

Not done (later sub-phases): the HTTP endpoints and the card-session wiring.

## Phase 3c-2 outcome (the BIP terminal)

`simple_server/bip.py` exists: one `BipSession` per card owns the BIP state -
`handle_command` (the chain's `bip_handler`: OPEN/CLOSE CHANNEL, SEND/RECEIVE
DATA, GET CHANNEL STATUS, TS 102 223 §6.4.27-§6.4.31), `start`/`stop`/`status`
(the session control: `sink`/`redirect`/`passthru`, one session at a time with
the replaced session reported, `link_events`), `data_available` (the terminal's
`on_data` callback, the §7.5.10 Data available event) and
`flush_channel_events` (the chain's `bip_flush` target, the §7.5.11 Channel
status events), both under an injected `card_lock`.  The channel machinery and
the sink listener stay `httpota.BipTerminal` / `httpota.TcpDumpServer`
(pysim-free, imported as allowed until the 12-module move at the fork).

The ported legacy functions: `_bip_channel_id`, `_bip_tr`,
`_handle_bip_command`, `_bip_listener_status`, `_bip_data_available`,
`_bip_session_stop`, `_bip_status_body`, `_bip_control` and
`_bip_flush_channel_events` (plus the `_BIP*` module globals, now instance
state).  The TR bytes, the queued link-status events and the local terminal
state are compared case by case with the legacy implementation by
`tests/test_simple_drift.py::BipParityTests`; `tests/test_simple_bip.py` pins
them against the reference traces and the spec-derived expectations (a real
loopback channel for the data flow, a scripted terminal for the protocol).

Deliberate, documented deviations from the legacy server:

- **The session is an object, not globals**: `owner`/`mode`/`target`/
  `listener`/`link_events` are `BipSession` attributes (the legacy globals were
  shared by the SCP81 listener and the BIP pill); `start(body, owner='bip')`
  lets the SCP81 control name its own owner instead of two handlers writing the
  same global.
- **"The card is connected" is a hook**: the legacy read the server module's
  `_CARD_CONNECTED`; the port takes `card_connected()` (a present
  `cat_session` when unset) so the module stays card-free.
- **The CAT exchange is the injected session**: the events go through
  `cat_session.send_event_download` (bytes) instead of the legacy
  `_send_event_download(scc, ...)` (hex APDUs through pySim's transport).
- **The event logs are the terminal's log**: the legacy `_BIP.log(...)` entries
  are kept (`data-available`, `data-available-skip`, `open-relaxed`,
  `open-unavailable`, `sink-rx`, ...), while a failed ENVELOPE additionally
  emits a `logging` warning instead of the legacy `sys.stderr.write`.
- **A listener bind failure answers `{'ok': False, 'error': ...}`**: the legacy
  handler let the `OSError` escape (a 500); the session stops either way.
- **The legacy SCP81 TLS listener branch of `_bip_listener_status` is absent**:
  that listener belongs to the SCP81 module (3c-3), which will reuse this
  session; `status()` reports the sink/redirect/passthru shapes only, and the
  listener block reports the mode it was started with (`sink`; the legacy's
  `or 'dump'` fallback is unreachable there).
- **The flush takes the card lock** (the legacy relied on its caller holding
  `_CARD_LOCK`); it is the same reentrant lock the chain runs under, so a
  cutover caller that already holds it is unaffected.  **`card_lock` must be
  reentrant** (`threading.RLock`): `data_available` holds it while its
  ENVELOPE runs the chain, whose end calls `flush_channel_events` - the same
  lock on the same thread.  It defaults to the injected `cat_session`'s own
  `card_lock` when that attribute exists (one lock per card, so a BIP ENVELOPE
  cannot interleave a FETCH/TERMINAL RESPONSE pair; the legacy kept the two on
  the server's global `_CARD_LOCK`), else to a fresh `RLock`.  `start`/`stop`
  take it too: they disable the terminal (closing the channels), which must
  not race a BIP command being answered - the legacy got that from the HTTP
  handler's blanket lock.
- **A TERMINAL RESPONSE is capped to one APDU** (the legacy could build one
  that never went out).  TS 102 221 §11.2.4 makes the TR a case-3 APDU with a
  one-octet Lc, and `cat_session._terminal_response_apdu()` refuses more than
  255 payload octets *after* the FETCH - the command would stay unanswered,
  which the project's critical invariant forbids.  Two guards:
  - RECEIVE DATA asks the terminal for at most `CHANNEL_DATA_MAX` (237) octets
    and truncates a terminal that returns more, so the payload (`N + 18`) can
    never exceed 255.  TS 102 223 §6.4.29 makes that the specified behaviour:
    "if the requested number of bytes is available in the buffer but the whole
    requested data cannot be included in the TERMINAL RESPONSE because of APDU
    size limits, the terminal shall return the maximum number of bytes
    possible according to the length of other TLVs … the command has been
    successfully executed".
  - OPEN CHANNEL echoes at most `BEARER_ECHO_MAX` (232) octets of the Bearer
    Description (§6.8.20 requires that echo in a successful *and* an
    unsuccessful TR, so it is cut, never dropped), and the echo's length uses
    the BER long form from 128 octets (`tlv.encode_ber_len`, identical bytes
    below it).
- **The RECEIVE DATA result stays `00` for an empty/partial answer**: §6.4.29
  says "Command performed with missing information" (`02`) when fewer than the
  requested bytes are buffered.  The port keeps the live-tested legacy `00`
  (the card asks again when it still needs bytes); a cutover decision, pinned
  by `tests/test_simple_bip.py`.
- **`data_available` checks `busy`/`stk_pending` before taking the lock**: the
  legacy checked them only under `_CARD_LOCK`, so its monitor thread queued
  behind a long card operation only to find the card busy.  The same checks
  run again under the lock, so the ENVELOPE still never interleaves a
  FETCH/TR pair.

Not done: the SCP81 HTTP administration listener on top of the session (3c-3)
and any live-card run - the command/data flow is tested over a loopback TCP
peer, which exercises the terminal's real sockets but no card.

## Phase 3c-3a outcome (the server state + HTTP core)

`simple_server/server.py` exists: `ServerState` owns one server's card link,
detected card, `CatSession`, `BipSession`, session caches, the background
STATUS poll thread and the presence observer; `make_handler` /
`build_http_server` serve the static PWA and the first endpoints
(`GET /api/version|status|readers`, `POST /api/reader|equip|reset`) over a
`ThreadingHTTPServer`.  Every other API path is a JSON 404 until 3c-3b/3c-3c
(the file endpoints landed in 3c-3b, see the next section).
The module is named `server.py` like the legacy (the design note's `http.py`
alternative was not taken); its suite is `tests/test_simple_http.py`.

Ported legacy behaviour: `_apply_equipped_card`'s post-equip refresh
(the CAT-free EF.ICCID/EF.ARR/EF.PBR reads **before** the TERMINAL PROFILE,
then MF, the profile and the drain), `_do_status_poll` (the card's POLL
INTERVAL/POLLING OFF adoption, the bounded chain), the presence monitor
(filtered by reader, removal clears the session, insertion marks the link
stale and spawns the bounded auto-equip - never in the pyscard thread),
`_is_transport_fatal`'s separation (now `_is_transport_error`), the
`card_session`/`card_ready_session` markers and the `_CARD_LOCK` discipline
(card-free GETs are `CARD_FREE_GET`; static files never take the lock).

Deliberate, documented decisions:

- **The cache labels are the legacy FID paths** (`MF/7F10/6F06`,
  `MF/7F10/5F3A/4F30`, `ADF.USIM-NON-IMSI/5F3A/4F30`, ...): the PWA resolves
  an FCP `8B` EF.ARR reference and injects the PBR children by comparing
  `arr.path`/`pbr.path` against its FID-based file paths.  A symbolic DF name
  (`MF/DF.TELECOM/...`) would never match.  `tests/test_simple_http.py` pins
  the labels.
- **A cardless start is real**: `open()` starts the poll and the presence
  monitor even when the card initialization fails, then re-raises, so the 3d
  entry point can report the failure and still get the first insertion
  equipped.  `presence_install`/`presence_remove` are the test seams;
  `stop_monitor()` never constructs pyscard's `CardMonitor` for a
  seam-installed observer (that used to start a real polling thread).
- **A poll failure keeps `card_present`**: only a transport error
  (`ReaderError`/an `hresult` exception) tears the link down (and re-arms the
  auto-equip); a decoder/protocol error (`CatSessionError`, a TLV-walk bug)
  is logged and the session is kept.  `disconnect(stale=True,
  keep_present=True)` is the transport path.
- **A failed `equip()` marks the link stale** (the v3.32.2 Windows swap rule):
  attempts 2/3 rebuild instead of reusing a half-connected handle.
- **The ICCID gate is not ported**: the legacy read EF.ARR/EF.PBR only when
  EF.ICCID was readable (they fed the network-state monitor); here they are
  best-effort session caches for the file views and are read unconditionally.
- **`/api/reset` is one power cycle**: `cards.init_card()` already resets, so
  the endpoint is exactly `equip()` - the explicit `reset_card` would power
  cycle twice.
- **`CardBusyError` vs `ReaderError`**: the two "a test run / a BIP session
  owns the card" refusals answer 409; every real switch failure (reader gone,
  no card in the new reader) answers 503 with the merged `status()` body, so
  the PWA sees the bumped `card_session`.
- **The startup drain re-polls after a chain**: `CatSession.handle_chain`
  returns `None` unless an `on_fetch` exits, so the legacy "break when the
  chain's last SW was idle" cannot be reproduced from its return value; the
  outer STATUS loop (3 rounds) is the check.
- **`_teardown_session` stops the BIP session** (the legacy never did on
  equip/reset/switch/removal).  **Open decision for 3c-3d**: a *server-owned*
  SCP81 listener should survive a card change (it is the terminal side, not
  per-card state), while the per-card `BipSession` carries the channels and
  the queued link events.  The SCP81 listener/`BipSession` split and the
  ownership rule need a decision before 3c-3d wires SCP81 control; until then
  the teardown is the safe (listener-also-stopped) behaviour.
- **Shutdown**: `close()` sets a `_closed` event the auto-equip worker
  observes (and joins it, bounded).  A worker stuck inside a blocking
  `SCardTransmit` cannot be cancelled - the same limit the legacy stuck-card
  watchdog documents.

## Phase 3c-3b outcome (the file endpoints)

`POST /api/tree`, `POST /api/select`, `POST /api/read`, `POST /api/write` and
the card-free `GET /api/arr` / `GET /api/pbr` are served from the file model,
`FileOps` and the FCP parser - no pySim shell, no output parsing.  The new
module is `simple_server/pbr.py` (the EF.PBR decoder + the tree injection);
the endpoints live on `ServerState` (`file_tree`, `file_select`, `file_read`,
`file_write`, `arr_cache`, `pbr_cache`) so the handler stays a thin
dispatcher.  Suite: `tests/test_simple_http.py` (the file-endpoint classes) +
`tests/test_simple_drift.py` (the PBR tables/parser against the legacy copy).

The request contract is unchanged (the PWA is not touched): a full `path`, or
`fid`/`name` plus `parent_sel`/`parent_path`; responses keep the legacy field
names (`exists`, `file_type`, `file_size`, `record_len`, `num_of_rec`,
`fci_hex`, `children[{name,fid,isDir,aid?,pbr?,sfi?,custom?}]`,
`records[{num,data}]`, `success`, `sw`, `apdu_times`).

Deliberate, documented decisions:

- **`allow_probe` is gone** (always allowed).  A model-unknown 4-hex FID under
  a model-known DF is selected directly - that is exactly the PBR-injected
  phonebook file.  The legacy flag plus its probe/detach dance existed to keep
  pySim's model clean; this model is never mutated by a select.  A selector
  chain the model does not describe at all (a custom DF) is walked by FID from
  MF or from the ADF it starts with - a SELECT by FID needs no model entry.
- **The tree lists the model, not the card.**  A node the card does not have
  appears and answers `exists: false` when selected (the PWA's probe walk
  marks it absent).  ADF roots are appended under MF with their `aid`: an ADF
  is a model root beside MF (selected by its DF name, TS 102 221 §11.1.1.1)
  but the PWA's node path is rooted at the ADF and the Profiler's walk roots
  an ADF child by its AID.
- **The EF.PBR injection is request-time**, as in the legacy: the node's
  resolved FID path is normalised (`MF`/`3F00`, upper case, ADF by name, and a
  leading `MF` before an ADF dropped) and compared against the cached
  `pbr.path` labels; a full path is never answered by another DF's copy and a
  bare DF FID only when a single copy is cached.  The PBR files are not added
  to the model - this phase keeps the legacy's request-time injection
  (the Phase 0 design note's "PBR as first-class files" would make the
  injection unnecessary and remains an option for a later phase).
- **`/api/read` has no `mode: 'decoded'`.**  The legacy mode ran pySim's
  server-side EF decoders; the PWA decodes the same raw bytes client-side
  (v3.32.7 already removed the only PWA caller), so a `mode` other than
  `'raw'` answers 400 with the reason instead of being silently ignored.
- **`apdu_times` is a per-request instrumented exchange**
  (`commands.TimedTransport`), not a link tracer: it measures SELECT / READ
  BINARY / READ RECORD at the transport call, so a fake transport is timed
  like PC/SC and one request cannot see another's entries.  The shape is the
  legacy's list of `{type, ms}` (the PWA's snapshot scan filters it by type and
  aggregates the statistics client-side).
- **Error classification** (the 3c-3a rule): a resolution/selection failure is
  the PWA's 404 (`exists`/`success` false), a request parameter a 400, no card
  session a 503 with the status body, a **transport** error a 503 after
  tearing the link down and re-arming the auto-equip, and anything else a 500
  that **keeps the session** (a decoder/protocol bug must not lose a healthy
  card).  A card-level READ/WRITE refusal is a 200 with `success: false` and
  the lower-case status word (the PWA compares `6982`/`9804` for its inline
  ADM action).  A **`6982`/`9804` on SELECT** is that 404 without a `sw` (the
  legacy parity: the SELECT failure carries no status word in the body), so
  the PWA's inline Verify ADM never appears for a select refusal - a frontend
  change would be needed to offer it there.
- **A card whose SELECT returns no FCP** (a 2G SIM on the TS 51 011 form, or
  a UICC omitting the FCP) is handled with two sources, in this order:
  1. **The card's own SELECT response.**  A 2G SIM answers the TS 51 011
     §9.2.1 file information, not a BER FCP: bytes 3-4 are the **file size**,
     byte 14 the **structure** (`00` transparent, `01` linear fixed, `03`
     cyclic) and byte 15 the **record length** (bytes 1-14 mandatory).  The
     spec-checker quoted the table; `fcp.parse_gsm_select` parses it into the
     ordinary `Fcp` fields (`form: 'gsm'`, the record count computed as
     size / record length), so the size-driven read path works unchanged and
     `/api/select` reports the card's real values (live bench card: EF.ICCID
     size 10, EF.ARR 31 × 76 B).  This is the **card's own** answer - not the
     rejected nominal model size.  A card that answers a BER FCP to the SIM
     select form goes through `parse_fcp` instead.
  2. **A size-agnostic fallback** (the 2026-11-22 decision, implemented
     2026-11-23) for a card that returns nothing at all: the explicit
     `length` / `record_len` request parameters, READ BINARY `Le = 256`
     accumulating until a short read / the `6282` warning (the bytes it
     carries are kept) / the 2G SIM's `9402` out-of-range address / zero
     progress (bounded at 65 535 bytes), and READ RECORD `Le = 256` until a
     refusal; both marked `size_inferred: true` for the PWA/snapshot.  The
     spec-checker's caveat: `6B00` is "wrong P1-P2", **not** an end-of-file
     code - an immediate `6B00` is a failed read (the recorded decision's
     `6282`/`6B00` list is corrected here), while `6B00` after bytes were
     read is the end.
  The model's `structure` (regenerated into `uicc_files.json` from the fork's
  generator, per the pySim EF class) picks the transparent/record path when
  neither the card nor the FCP gives one.  **Rejected:** borrowing the nominal
  **size** from `uicc_files.json` (the structure is not a size; a spec size
  may differ per card).  Tests: `tests/test_simple_files.py`
  (`GsmSelectTests`, `read_binary_until_end`, the SIM select parse),
  `tests/test_simple_http.py` (the GSM select/read, the fallback, the explicit
  parameters); **live check (2026-11-23, server 4.0.1, the bench 2G SIM):**
  EF.ICCID size 10, EF.IMSI 9, EF.ARR 2356 B (76 × 31), EF.ADN under
  DF.TELECOM 9750 B (39 × 250) and EF.MSISDN 195 B (39 × 5) all select and
  read (`fci_form: "gsm"`); EF.SPN 17 B reads `004D656761466F6E…`
  ("MegaFon").
- **`selection`** (`/api/status`) is published by select/read/write:
  `{path, name, fid, kind, size, record_len, num_records}`.  It describes the
  last file a file endpoint addressed, not a live mirror of the card's
  selected file (the EF.ARR refresh after a write leaves an ARR copy selected
  on the card).
- **An EF.ARR write refreshes the cache** (`_arr_refresh_after_write`): the
  `8B` references the file manager resolves may have changed.  A failed
  refresh is logged, never fatal.
- **A `record_nr` on a transparent file is a 400** (a caller error, not the
  card's); `data` and `record_nr` are validated before the SELECT, so a
  malformed request spends no APDU.

Not ported / open: the legacy's `mode: 'decoded'` (gone by design), the
advisory `allow_probe: false` (ignored - see above), and no card has run these
endpoints yet - the fixture is a fake transport, so the first bench run is a
cutover gate (a DF's real FCI, a card whose SELECT returns no FCP, and a
phonebook whose PBR record is longer than one record).  The no-FCP read is
**known-unsupported** until the decision above; the drift guard
(`tests/test_simple_drift.py`) compares `files_for_node`/`inject_children`
against the legacy copies, not just the tables and the parser.

## Phase 3c-3c-1 outcome (the preset store + the plain SCP80 send)

`GET/POST /api/presets` + `/api/presets/update|delete|import|import-outfile`,
`POST /api/verify-adm`, `POST /api/send-ota` and `POST /api/counter-probe` are
served from `ServerState`.  The new module is `simple_server/ota.py` - the
pure operation logic: the per-TAR default keyset (v3.26.0), the MSL-derived
SPI1 (TS 102 226 §8.2.1.3.2.4), the server-owned counter (v3.20.0), the RAM
command format helpers, the response-packet (PoR) decode and the bounded
counter-sync probe.  The store and the vendor-outfile parser are reused from
`pysim_simple_server/presets.py` / `outfile.py` (both pysim-free), so no data
moves yet.  Suites: `tests/test_simple_http.py` (the preset/ADM/SCP80/probe
classes) + `tests/test_simple_drift.py` (the operation helpers against the
legacy copies, the keyset-resolution rules).

Deliberate, documented decisions (the first three are the deviations from the
legacy):

- **`ram_format: 'auto'` does not probe on the plain send.**  The legacy
  detected the card's RAM command format per operation with a read-only
  `GET STATUS [ISD]` probe (`_ram_detect_format`); that probe consumes counters
  and belongs to a RAM step.  **Decision:** the RAM operation sender (3c-3c-2)
  will probe; until it lands the plain send builds the compact form and reports
  `ram_format: 'compact'` in its answer.  If the RAM probe is not ported,
  `auto` must become a 400 pointing at the RAM operation - never a silent
  compact packet (stated in `ota.format_apdu`'s docstring).
- **The pre-built `sp` path is not keyset-resolved.**  A packet built by the
  PWA carries its own KIc/KID, counter and TAR, so the server sends it as it
  is (the legacy parity).  Only the `apdu` path resolves the preset's TAR
  keyset, seeds the counter and persists the accepted value; the `sp` path
  keeps the caller's bookkeeping counter.
- **`_por_header_parse` is folded into `scp80.decode_resp`.**  The legacy had
  a second decode entry point for a secured data too short for the compact
  Table 5.1 form (a 2-byte applet reply); here the same header, deciphering,
  PCNTR and RC/CC/DS checks run before the compact parse, so `ota.decode_por`
  has one path (a malformed packet or a failed check is still no PoR at all).
- **`/api/sp-verify` is dropped** (the locked API-surface decision): its
  reference packet came from pySim's `OtaDialectSms.encode_cmd`, which no
  longer exists in this server, and a JS-vs-Python symmetry check is not a
  correctness proof.  The builders stay pinned to the same byte vectors by the
  unit tests instead.
- **`/api/verify-adm` gains two request fields** (`preset_id`, `chv`) while
  keeping the legacy result shape: `preset_id` verifies the stored preset's
  ADM, and `chv` is the CHV number as a hex string (`'0A'`) or an integer byte
  (`10` = 0x0A, not a hex-encoded 10); the default stays `0x0A`.  The latch is
  the per-session `adm_session`, exposed as `/api/status.adm_verified` (the
  legacy's `rs.adm_verified`) and cleared by any equip/disconnect.
- **The keyset rules are the v3.26.0 review fixes** and are pinned by
  `OtaKeysetResolutionTests`: an explicit KIc/KID overrides the TAR's default
  keyset; an MSL 00 TAR without a keyset gets the spec's no-security
  `00`/`00` KIc/KID and empty keys; a security MSL without a keyset is refused
  ("set it in the Cards tab"); a TAR outside the preset needs explicit
  keys; an undefined keyset number is refused; no preset resolves nothing.
- **The store-I/O errors are a JSON 500** (`preset store write failed: ...`)
  with `success: false`/`ok: false`, while a store *validation* refusal
  (`PresetError`) stays a 400; the request log masks the top-level `keysets`
  keys as well as the `fields` ones (the outfile `text` is logged verbatim -
  it is the vendor file and carries the keys by nature).

Tests: **42 HTTP + 8 drift; the full suite is 1872**.  **No live card has run
these endpoints yet** - every exchange is a scripted fake transport, so the
fake-transport fixtures are the cutover gate: the first bench run (a real
preset-store verify, an APDU SCP80 send with a seeded counter, and a
`cntr_low` verdict) is the acceptance check.

## Phase 3c-3c-2a outcome (the CAP analysis + the single-card RAM installs)

`POST /api/cap-info` (card-free), `POST /api/ram-install` and
`POST /api/ram-install-app` are served from `ServerState`.  The new module is
`simple_server/capram.py` - the pure half: the `.cap` structural parse, the
GP APDU builders (INSTALL [for load]/LOAD/INSTALL [for install]/INSTALL [for
make selectable]/DELETE), the install-parameter composition, the capmem
analysis, the step-record assembly and the NV decode.  The card exchange (one
secured packet per GP APDU, the format probe, the silent FF21 reads, the chain
runner) lives on `ServerState` like the plain send's
`_send_secured_packet`.  Reused as-is: `ota` (the preset TAR keyset, the
MSL-derived SPI1, the server-owned counter, the PoR decode), `scp80` (the
packet build/split), `cat_session.PorSubmitCapture` and
`pysim_simple_server.capmem`.  Suites: `tests/test_simple_ram.py` (64) +
`tests/test_simple_drift.py` (`CapRamDriftTests`, 12).  The builders' positive
APDUs are also pinned to the TCA reference trace
(`samples/tca_ram_traces/test_mycard_keyset2_applet_full_load.log`, kept out of
the repo): the trace's clear `INSTALL [for load]`, first 64-octet `LOAD` block
and `INSTALL [for install]` are test constants (`ReferenceTraceTests`).

Request contract (unchanged from the legacy, so the PWA is untouched):
`cap_hex` (with `cap` accepted as an API alias), `sd_aid`, `delete_package`,
`privileges`, `install_params`, `stk_params`, `make_selectable`,
`nv_quota`/`volatile_quota`, `load_block_size` (1..240), `ram_format`
(`auto`/`compact`/`expanded`/`expanded-ae`), `includeCpi`, `spi2`, and the
preset resolution `preset_id` + an explicit `kic`/`kid`/`cntr`/`spi1` as the
API overrides; `ram-install-app` also takes `mode`
(`install`/`make_selectable`), `loadfile_aid`, `module_aid`, `instance_aid`.
The response is the legacy shape the PWA reads: `success`, `steps[]`
(`name`, `sw`, `bytes`, `segments`, `por_status`/`por_type`/`por_sw`/
`por_data`/`por_raw`, `por_bad_format`, `por_error`, `delete_not_found`),
`failed_step`, `error`, `final_cntr`, `ram_format`, `cntr_low`, the
`load_file_aid`/`module_aid`/`load_block_size*` block and the `nv_before`/
`nv_after`/`nv_delta` footprint.

Deliberate, documented decisions (deviations from the legacy):

- **`/api/cap-info` is card-free** (`CARD_FREE_POST`).  The legacy serialized
  it behind the card lock although it never touches the card; the analysis
  must stay answerable while a long chain runs (the PWA's progress poll).
- **A malformed `.cap` (and an invalid install-parameter combination) is a
  400.**  The legacy answered `/api/cap-info` `{ok: false, error}` with 200 and
  dropped a malformed archive of `/api/ram-install` into its generic 500; here
  every parse failure is a `ValueError` naming the problem (`the .cap is not a
  ZIP archive`, `the .cap Header is truncated`, ...) and the endpoint answers
  400 with `ok`/`success` false.  The PWA reads `ok`/`error` either way.
  **Scope of "validated":** the `.cap` structure, `load_block_size`, the
  install-parameter composition and the `privileges` width/hex (GP Table
  11-43/11-44 allow only 1 or 3 octets; the builders refuse anything else).
  `sd_aid`, `loadfile_aid`/`module_aid`/`instance_aid` and `spi2` are **not**
  validated at plan time - the AIDs are only stripped of spaces and
  uppercased, and a bad `spi2` fails when the first packet is encoded (a
  `success: false` step, not a 400), so the caller must send valid values.
- **`memory['components']` is re-ordered into the load-file order and lists
  only those components** (JC VM Specification v2.2.2 §6.2/Table 6-3 - the
  component load order is a JC VM rule, not GP's, and the TCA reference trace
  follows it).  capmem lists every component the archive carries in ZIP order;
  the report here follows the load file, so a component outside the order (a
  `Debug.cap`, which the JC VM spec says is not for download) is dropped while
  the legacy reported it after the load file.  The PWA maps a failed LOAD
  block to the component it covers by accumulating those sizes from the load
  file's start - the ZIP order is usually but not always the load order.
- **`cntr: null` builds the all-zero counter.**  When
  `ota.counter_seed()` finds no preset/keyset value, the body's `cntr` is read
  with `str(body.get('cntr') or '00000000')`: a JSON `"cntr": null` yields
  `'00000000'`, where the legacy left `state['cntr']` None.  The packet is
  byte-identical (the keyset builder reads None as 0: TS 102 225 §5.2 Table 3),
  but the response's `final_cntr` now carries the 8-digit zero string instead
  of `null`.
- **The RAM diagnostics are `log.info` and nothing configures INFO yet.**
  `simple_server` has no logging setup (deliberately - a module-level
  `basicConfig` would fight the embedding application), so at the default
  WARNING level `RAM C-APDU`, `RAM SECURED-PACKET`, the per-step PoR lines and
  `RAM-… complete` never reach `simple_lastrun.log`, the bench's primary
  diagnostic.  **Decision:** the 3d entry point configures INFO for the
  `simple_server` logger (with its stderr handler / the start script's
  mirror), or those lines are raised to WARNING if INFO turns out too chatty;
  the modules stay configuration-free.  The test runner's run-log lines
  (`TEST-RUN …`, `TEST-LOAD …`, `TEST-WATCHDOG …`, the semantic per-step
  trail the help/AGENTS call "read simple_lastrun.log for") are the same kind
  of diagnostic and the same decision covers them (3c-3d-1 repeats it).
- **The step record assembly, the remote-SW policy and the DELETE tolerance
  are the legacy's, unchanged**: `por_ok` + a remote SW outside
  `{9000, CAFE, 61xx, 62xx, 63xx}` fails the step, a `9000` transport SW with
  no PoR (`no_por`) does not, a `delete`-flagged step tolerates `6A88`/`6A82`
  (the object is already gone) and any other failure stops the chain.  The
  counter advances (and is persisted per step) only for an accepted packet
  and only when SPI1.b5b4 asks the card to check it (TS 102 225 §5.1.1).
- **`/api/status` gains `ram_progress`** (the legacy's `_RAM_PROGRESS`):
  the chain holds the card lock, so the install modal's live step/elapsed
  rides the card-free 2 s poll.  It is per-state here (no module global) and
  `status()` only reads it.
- **The plan is built before anything is sent** (a bad file, an invalid
  block size or an invalid install-parameter combination is a 400 with zero
  APDUs spent) and the keyset/preset resolution is the legacy operation rule
  (the preset's ISD role entry, `prefer_msl=True`).
- **Not ported in this phase (the locked API surface):** the CAP compatibility
  test (`_cap_compat_blocks`, `_probe_import_versions`, `/api/cap-compat`), the
  TAR probe, the batch plans/worker (`_ram_delete_plan`, `/api/ram-batch/*`)
  and the RAM format's use by the *plain* send (still the 3c-3c-1 decision).
  **The first three landed in 3c-3c-2b** (see its outcome section); the plain
  send's `auto` decision stands.

Tests: **64 RAM + 12 drift; the full suite is 1961**.  **No live card has run
these endpoints yet** - the fixture is a synthetic CAP ZIP and a scripted fake
transport (the response packets are built by the test), so the first bench run
is the acceptance check: a real `.cap` (the load-file parse and the block
split at the card's buffer), the card's own RAM format probe (which of the
three codings it accepts), a real `cntr_low`, an FF21 readout, and the
`INSTALL [for install]` parameter acceptance (the CA-in-one-EF case).

## Phase 3c-3c-2b outcome (the compatibility test, the TAR probe, the batch)

`POST /api/cap-compat`, `POST /api/tar-probe` and
`POST /api/ram-batch/start|stop|clear` + `GET /api/ram-batch/status` are
served from `ServerState`.  The pure half joins `simple_server/capram.py`
(the import-boundary helpers `version_pair`, `cap_import_end`,
`cap_import_aids`, `cap_compat_blocks`, `probe_import_versions`,
`probe_unmatched`, the composed `cap_compat_plan`, the `TAR_PROBE_DEFAULT_APDU`
constant and the batch `delete_plan`); `simple_server/ota.py` gains the pure
`tar_probe_verdict`.  The card exchanges reuse the existing machinery
unchanged: `_ram_plan_run` (the compat chain is an ordinary plan truncated at
the Import boundary, so it gets the format probe, the step records, the
counter seeding/persistence and the `cntr_low` verdict for free),
`_ram_send_gp_apdu` (the TAR probe's per-TAR packets), `ota.spi1_for_tar`/
`ota.preset_keyset_by_kvn`/`ota.counter_seed` and `capram.install_plan`/
`reinstall_plan`/`delete_plan` (the batch's plan, built once at Start).

Request contract (unchanged from the legacy, so the PWA is untouched):
`cap-compat` takes `cap_hex`, `probe_imports`/`probe_additions`,
`load_block_size` (1..240), `preset_id` + the usual `spi1`/`spi2`/`kic`/
`kid`/`tar`/`cntr` overrides and `ram_format`; its response is the legacy
shape (`success`/`imports_ok`, `steps`, `boundary_block`/`total_blocks`,
`load_file_aid`/`module_aid`, `probe_imports`/`probe_unmatched`,
`load_block_size*`, `final_cntr`, and on rejection `failed_step`/`error`/
`cntr_low`).  `tar-probe` takes `preset_id`, an optional `tars` subset,
`apdu`, `spi1` (the fallback only), `spi2`, `cntr` (the first row) and
`includeCpi`; it answers `results[]` (`tar`, `label`, `spi1`, `kvn`,
`verdict`, `sw`, `por_status`/`por_sw`/`por_data`, `bytes`, `segments`),
`registered`/`total`, `final_cntr`, `kvn`, `spi1`/`spi2`/`apdu`.
`ram-batch/start` takes a `kind` (`install-cap` default, `delete`,
`reinstall-app`) plus that kind's fields and answers the full status with
`ok: true`; `status` is the panel payload (`active`, `phase`, `kind`,
`attempts` (last 500), `attempts_total`, `current`, `notice`, `counts`,
`started`, `stopped`, `progress`) and `stop`/`clear` answer it / `{ok}`.

Deliberate, documented decisions (deviations from the legacy):

- **The batch state is per state, not a module global.**  `ServerState`
  owns `ram_batch`/`ram_batch_lock` (the legacy `_RAM_BATCH`/`_RAM_BATCH_LOCK`),
  so two servers in one process are independent and the tests need no global
  reset.  The worker reads the real attributes (`connected`,
  `card_ready_session`, `card_session`, `iccid`, `presets`) - there is no
  `_CARD_CONNECTED` module global to misread (the v3.28.0 audit's bug class
  cannot recur).  `close()` now stops the worker (bounded join) so a server
  shutdown never leaves a thread polling a dead state, and its card teardown
  acquires the card lock with `CLOSE_LOCK_TIMEOUT` (2 s) and **skips the
  teardown** when a batch install still holds it for its chain - shutdown is
  bounded end to end instead of blocking on a card exchange.
- **The worker's wait is `self._closed.wait(RAM_BATCH_POLL)`**, not
  `time.sleep`: shutdown returns promptly instead of after up to 0.5 s.
- **`/api/ram-batch/start` answers 409 via a typed error.**  `BatchBusyError`
  (another batch / a test run) -> 409, `ValueError` and every plan-level
  exception -> 400 (the legacy's `RuntimeError`/generic split).  The
  `/api/ram-batch/stop` endpoint is the second `CARD_FREE_POST` and
  `/api/ram-batch/status` joins `CARD_FREE_GET` (the worker holds the card lock
  for a whole install); no other endpoint's lock behaviour changed.
- **The compatibility test clears the last-block flag when the Import ends in
  the last block.**  The legacy truncation `seq[:1 + boundary]` equals the
  full LOAD list for a tiny CAP (`boundary == total`), whose final block then
  carries P1.b8 (GP Card Spec v2.3.1 Table 11-56) and would complete - and so
  commit - the load file, contradicting the "nothing is committed" invariant.
  The plan clears the flag (`cap_compat_plan`): the payload and the block
  counter are untouched, only the file is not completed.  For a real CAP the
  Import ends long before the last block, so this only affects a tiny archive;
  the flag is asserted by `CapCompatTests.test_the_last_block_flag_is_cleared`.
  **Consequence to keep in mind:** the verdict rests on the observed
  behaviour - a card that evaluates the import list only when the load file
  *completes* is never asked (the truncated chain stops at the boundary block,
  and the de-flagged `boundary == total` case never marks the file complete),
  so for such a card a true "rejected" would read as a false
  `imports_ok: true`.  No such card is known; the alternative is a real load
  followed by a cascade DELETE (`delete_plan`, which exists for the
  batch/`delete` kinds and would commit and then remove the package) - not
  implemented for the compat test, whose contract is "nothing is committed".
- **The TAR probe request is pre-flighted.**  `apdu` (even hex, 1..255
  octets) and `spi2` (one hex byte) are validated before any packet: the
  legacy let a malformed APDU fail in every row's encoder, whose `encode_error`
  record maps to the `refused` verdict - an operator typo read as "no
  application is registered".  Both are a `400` now.
- **The per-card install re-checks the card under the lock.**  The ICCID and
  the preset are matched before `card_lock` (the worker's poll is lock-free),
  so the chain verifies `card_session` and the normalised ICCID again once it
  holds the lock and records the card as `skipped` ("the card changed before
  the install started") if either moved - a swap in that window must not run
  the previous card's plan.
- **The generic compat failure text is the legacy's.**  A refused ENVELOPE
  leaves no step-level detail, so `_ram_plan_run`'s `<step> failed` fallback is
  replaced with "the card rejected the import list" (the PWA's verdict wording
  keys on the failure kind, not this string).
- **`/api/status` gains `ram_batch`** (the summary: no attempts list) so the
  panel's headline rides the 2 s poll; the detailed report is fetched only when
  the summary changed (the v3.28.7 stale-guard logic is the PWA's).
- **The deleted `probe_imports` stays out of `/api/ram-install`** (the
  v3.6.48 decision): the diagnostic lives in the compat operation.

**Pinned legacy behaviour (not a deviation):** the TAR probe decodes each row
with its **own TAR's** response form - `rm = ota.tar_is_rm(preset, tar)` - so a
free (non-role) TAR's answer stays `response_type: raw` with no fabricated
status word.  That is the legacy's per-packet `_ram_send_gp_apdu` rule and the
v3.21.0 invariant behind the PWA's "App SW" column
(`test_a_non_rm_tar_keeps_its_answer_raw`).

Tests: **57 batch/compat/tar tests (`tests/test_simple_batch.py`) + 8 drift
(`CapCompatDriftTests`); the full suite is 2026**.  **No live card has run
these endpoints yet** - the fixture is a synthetic CAP ZIP, a scripted fake
transport and a fake `_ram_plan_run` (the batch's per-card decisions), so the
first bench run is the acceptance check: a real `.cap`'s import boundary (does
the card reject exactly there?), the four TAR-probe verdicts on the live
applications, and the batch across a real card swap (the ICCID match and the
per-card counter).

## Phase 3c-3d-1 outcome (the test-script store + the single-script run)

`GET/POST /api/test/scripts`, `/api/test/scripts/update|delete|import`,
`POST /api/test/run|stop|clear` and `GET /api/test/status` are served from
`ServerState`; the run engine is the new `simple_server/testrun.py`
(`TestRunner`).  The pysim-free reuse is unchanged: the pure engine
(`normalise_script`/`normalise_step`/`build_tr`/`match_*`/`combine_levels`),
the script store (`TestScriptStore`) and the semantic event builders
(`events.py`).  The card exchanges reuse `CatSession` (STATUS/FETCH/TERMINAL
RESPONSE, ENVELOPE, the menu cache, the proactive log), `ota.decode_por`/
`spi1_for_tar`/`format_apdu`, `scp80.build_secured_packet`/
`counter_tracked`/`increment_counter_hex`, `commands.FileOps` and
`ServerState._send_secured_packet`.

Ported, function for function (the names are the legacy ones where a reviewer
would diff them): `_test_run_start`/`_test_run_spawn`/`_test_run_worker`/
`_test_run_execute`, `_test_run_script` (the step loop with the `on_fail`
rule, the pending-command guard, the cleanup drain), the executors
`_test_run_action`/`_test_run_expect`/`_test_run_scp80`/`_test_run_file`/
`_test_run_status`/`_test_run_drain`, `_test_step_tr`/`_test_terminal_response`,
`_test_drain_pending`, `_test_expect_por`, `_test_entry`/`_test_entry_update`/
`_test_finish_entry`/`_test_check_result`/`_test_action_checks`/
`_por_check_results`, `_test_log`/`_test_heartbeat`, `_ensure_adm`,
`_test_preset_error`, `_test_load_state`/`_test_load_iterations`/
`_test_load_finalise`/`_test_load_script_worker`, the watchdog
(`_test_watchdog_check`/`_test_watchdog_start`/`_abort_card_transport`) and
`_TEST_KIND_LABELS`/`_TEST_BLOCKED_PATHS`.

Deliberate, documented decisions (deviations from the legacy):

- **The run state is per state, not a module global.**  `TestRunner` owns the
  run dict (the legacy `_TEST_RUN`), its lock, the worker thread, the
  watchdog and the watchdog's "already aborted" heartbeat, so two servers in
  one process are independent and the tests need no global reset.  The run
  dict keeps the legacy shape (including `scp80_counters` int keys, which
  JSON renders as strings) plus a `kind` discriminator (`script` here,
  `suite` for 3c-3d-2) - the field the suite runner will branch on.
- **`ServerState` keeps the boolean `test_running`** (the existing seam the
  tests and `CatSession`'s `test_running` hook use) and the runner sets it for
  a run's whole life; `/api/test/run` refuses when either the runner or the
  flag says a run is active, so a hand-set flag and a real run cannot
  coexist.  The background poll needs no cancel/re-arm (`_sim_resume_poll`):
  `poll_once` skips while the flag is set, and the resumer only clears the
  flag and re-adopts the CAT session's polling wishes a script may have
  changed.
- **`/api/test/stop` and `/api/test/clear` are card-free POSTs** (the legacy
  only had the batch stop): the runner holds the card lock inside a step, so
  Stop must not queue behind a blocked exchange (the watchdog is the second
  half of that story).  `/api/test/status` and `/api/test/scripts` join
  `CARD_FREE_GET`; `_TEST_BLOCKED_PATHS` is the legacy set verbatim (the
  paths this build does not serve yet are listed too, so the guard is right
  when they land).
- **The script CRUD has no suite membership rules yet.**  The legacy
  requires an existing `suite_id` on create, detaches on delete and treats a
  changed `suite_id` as a move; without the suite store (3c-3d-2) a
  `suite_id` is stored as the entry's opaque field, an update ignores a
  changed one and the import has no target suite / no `replace` guard.  A
  stored entry that no longer validates is served (the store's behaviour) and
  fails at run start with the validator's message - pinned by
  `KeptEntryTests`.
- **A `suite_id` on `/api/test/run` is refused with an explicit message**
  rather than silently running one script; 3c-3d-2 replaces that branch.
- **`_send_secured_packet` gained `handle_proactive`** (default `True`, so
  every existing caller is unchanged).  The scp80 step passes `False`, which
  is the legacy's semantics: a `91XX` answer stays pending for the next
  expectation and the late-PoR STATUS poll is skipped (the script asserts the
  applet's own command, a PoR in SEND SHORT MESSAGE included).
- **The scp80 step reads its keys from the resolved keyset only.**  The
  legacy also looked at `params['kic']`/`['kid']`/`['kicKey']`/`['kidKey']`,
  but its normaliser refuses those parameters (the keys stay preset-owned), so
  the lookups were dead code; the port drops them and documents it.
- **`file-read` of a transparent EF needs an FCP size.**  The legacy read the
  whole file through pySim, which fell back to the selected file's size; this
  port uses `FileOps.read_file` (FCP-driven) and a card whose FCP carries no
  size fails the step.  The explicit-length read fallback is the 3d item
  already recorded in this file ("the no-FCP read fallback").
- **The watchdog lives on the state, not in the module.**  `ServerState.
  abort_card_transport()` owns the stale-link flag and the daemon-thread
  `disconnect` (the legacy `_abort_card_transport` global); `TestRunner.close()`
  stops the watchdog thread and `ServerState.close()` calls it, so a shutdown
  leaves no ticker behind.
- **The SCP80 counter follows a three-way rule (the scp80 step and the
  plain send's exception path).**  The legacy advanced (and persisted) on any
  non-empty status word, so a `6200` ENVELOPE carrying a `cntr_low` PoR still
  moved the preset counter although the card had refused the packet
  (reproduced: the retry persisted `…11`, audit source `test-run`).  The port
  moves the counter **unless there is positive evidence of rejection**
  (`testrun.counter_consumed` / `POR_REJECTED`):
  - **clear acceptance** advances: a `por_ok` / `actual_response_sms_submit`
    PoR verdict, or a `9000`/`91XX` SW when no PoR came back;
  - **clear rejection** does not: a decoded PoR naming a counter check
    (`cntr_low`/`cntr_high`/`cntr_blocked`), a failed RC/CC/DS, a ciphering /
    undefined security error, insufficient memory, or an unknown TAR /
    insufficient security level (TS 102 225 §5.2 Table 3 status bytes
    `01`-`07`, `09`, `0A`);
  - **unclear advances too**: no PoR, an undecodable PoR, an ambiguous status
    word (`6Axx`/`6Fxx`, a warning `62xx`/`63xx` without data) or a transport
    error after the packet may have reached the card.  Rationale (owner): an
    unnecessarily skipped counter value costs one value the card accepts,
    while not advancing a counter the card *did* consume leaves every later
    packet answering `cntr_low` and the operation stuck.  `more_time_needed`
    (`08`) is not a refusal and therefore advances as well.
  `/api/send-ota` follows the same rule on its **transport-exception** path
  (it persisted nothing there before, although the packet may have reached
  the card; the exception still propagates, so there is no response shape to
  keep - pinned by `SendOtaCounterTests`).  Its *returned-failure* branch
  keeps its stricter rule (a failure SW with no PoR does not advance) - that
  path has a verdict to evaluate and a PWA-visible `final_cntr` signal, so
  widening it would change the documented v3.6.58/v3.20.0 send behaviour and
  the PWA's counter sync; the difference is deliberate and recorded here.
  Pinned by `test_a_packet_and_its_por` and
  `test_a_9000_envelope_without_a_por_advances_the_counter` (acceptance),
  `test_a_rejected_packet_leaves_the_counter_untouched` (rejection),
  `test_an_unclear_refusal_still_advances_the_counter` /
  `test_a_transport_error_still_advances_the_counter` (unclear) and
  `test_the_counter_rule_is_three_way` (the pure rule).
- **The scp80 step has no submit capture.**  With `handle_proactive=False` the
  ENVELOPE's SEND SHORT MESSAGE chain never runs, so the legacy's
  `PorSubmitCapture` could never be filled; the port drops it and the PoR in
  submit mode is decoded by the following *expect* step (`_expect_por`),
  which is the only place it can arrive.
- **The TERMINAL RESPONSE's POLL INTERVAL echo keeps the legacy's `84` tag.**
  The terminal's own chain uses the base `04` form per TS 102 223 §6.8.0
  (v3.25.2), but the runner's scripted TR has shipped `84 02 01 <interval>`
  since v3.22.2 and the card accepts it.  §6.8.0 says the response objects
  "should" use the CR=0 form (not "shall"); the anchors are the legacy test
  pin (`840201<interval>`) and card acceptance - no recorded or golden TR
  compares these bytes - so the deviation stays documented in the
  `_terminal_response` docstring and pinned by
  `TerminalAnswerTests` rather than changed.
- **The run-log lines go through the module logger** (`log.info`) instead of
  a raw `sys.stderr.write`; the buffered excerpt the status payload serves is
  unchanged.  Like the RAM/OTA diagnostics (their decision is recorded in the
  3c-3c-2 section), nothing in `simple_server` configures logging yet, so at
  the default WARNING level these lines reach no handler until the 3d entry
  point configures INFO for the `simple_server` logger.

Tests: **115 runner tests (`tests/test_simple_runner.py`) + 12 drift
(`TestRunDriftTests`); the full suite is 2153**.  **No live card has run the
runner yet** - the fixture is a scripted fake transport (the UICC startup, the
STATUS/FETCH/TR queues and the ENVELOPE answers), so the first bench run is
the acceptance check: a real menu selection by text, an applet's `91XX`
hand-off to the next expectation, the SCP80 counter continuity across a load
test, and the watchdog against a card that really stops answering (the
30 s step timeout and whether the PC/SC disconnect cancels the blocking
`SCardTransmit`).

## Phase 3c-3d-2 outcome (the test suites)

`GET/POST /api/test/suites`, `/api/test/suites/update|delete|import`,
`/api/test/scripts/move|copy` and `/api/test/run {suite_id}` (the load test
included) are served from `ServerState`; the suite execution is an extension
of `simple_server/testrun.py` (the same `TestRunner` and the same step loop -
`run_script` is called per member), and the cross-store rules live in the new
`simple_server/suites.py`.  The stores stay the pysim-free
`pysim_simple_server.test_suites` (schema v2) and `test_scripts` (schema v2).

Ported, function for function: `_test_suite_state` (`suite_state`),
`_test_suite_start` (`TestRunner.start_suite`), `_test_suite_execute_once`
(`TestRunner.suite_execute_once`), `_test_suite_execute`/
`_test_suite_worker` (`execute_suite`/`_suite_worker`/`_suite_error`),
`_test_load_suite_worker` (`_load_suite_worker`), `_test_member_prefix`/
`_test_member_status` (`member_prefix`/`member_status`),
`_preset_counters_snapshot` (`preset_counters_snapshot`),
`_test_suite_run_request` (`ServerState._test_run_suite`),
`_test_suite_refs_error` (`suites.suite_refs_error`), `_test_script_detach`
(`suites.script_detach`), `_test_script_move` (`suites.script_move`),
`reconcile_scripts_to_suites`/`migrate_scripts_to_suites`, and the suite CRUD
/ move / copy / import handlers (`test_suite_add|update|delete|import`,
`test_script_move|copy`, and the suite rules of `test_script_add|update|
delete|import`).

The suite semantics are the v3.22.0 ones: the ordered setup/members/teardown
by reference, the per-member `on_fail` (`stop` default / `continue`), one card
session without resets, the teardown always on a stop/failure (skipped only
when the card is gone), a card session change stopping the suite (flagged),
the members sharing one SCP80 counter dict, and the report in the run status
(`suite: {id, name, require_adm, start_reset, reset, started, session_start,
session_end, session_changed, preset_id, preset, counters_before,
counters_after, summary, members[...]}`) with per-member status/steps/log
excerpt (120 lines) and the `counters_before`/`counters_after` per keyset -
the shape the PWA's report renderer reads.

Deliberate, documented decisions (deviations from the legacy):

- **The suite CRUD now enforces the membership.**  A script create requires
  an existing `suite_id` (400 otherwise) and joins the suite's member list; a
  delete detaches first; an update with a changed `suite_id` is the move; the
  scripts import refuses `replace` while a suite holds scripts, takes a
  target `suite_id`, and the suites import remaps the script ids (a bundle
  import also reconciles).  A suite create/import may only list scripts that
  are free: a referenced live script whose `suite_id` names another existing
  suite is a 400 (`suites.suite_owners_error`; a stale `suite_id` whose suite
  is gone is the reconciliation's repair, and a bundle's own freshly imported
  scripts are reconciled into their imported suite).  This supersedes the
  3c-3d-1 simplification ("a `suite_id` is kept as an opaque field").
- **The suite/script import modes are validated.**  An unknown `mode` is a
  400 for both imports (the v3.27.0 rule - never a silent `merge`), and a
  `mode: "replace"` suites import with nothing importable (every reference
  unresolvable, or no suite entry at all) is refused instead of silently
  wiping the store.
- **The start-of-suite reset calls `ServerState.reset()`** (the physical
  reset + the build's full re-initialization) instead of the legacy
  `_card_reset_reinit`, whose extra step probes the pySim-shell command-set
  registration - there is no shell here.  The order is kept: after the preset
  and SCP80 checks, before the ADM gate, with the post-reset ICCID compared
  against the run preset.  The ICCID-mismatch 400 therefore carries only the
  error text - the legacy attached the `reset` dict to that refusal, this
  build does not (the reset did run; the request is refused).
- **The reconciliation is exposed, not auto-run.**  `ServerState.
  migrate_scripts_to_suites()` (and `reconcile_scripts_to_suites()`) are the
  startup pass; this build has no entry point yet, so 3d calls the migration
  before serving (the legacy `__main__` call).  The suites import runs the
  reconciliation itself, exactly like the legacy.
- **The test-script/-suite store POSTs are card-free.**  The legacy queued
  them behind the card lock; the stores touch no card, so a run holding the
  lock must not freeze the suite editor (the same rationale as the already
  card-free `/api/test/stop|clear`).
- **The load-suite worker keeps the legacy's session-change detection.**
  `suite_execute_once` returns the local `session_changed` flag (set when the
  member loop observes the change) while the *report* also flags a change
  observed at the end (`session_end != session_start`); a load iteration whose
  last member bumped the session is therefore discarded as successful and the
  next iteration refuses to start on the changed session - the legacy
  behaviour, pinned by `test_a_session_change_is_an_error`.
- **`test_script_update` returns `(entry, moved)`** from the state method so
  the handler can answer `moved: true`; the endpoint adds that field, which
  the PWA ignores.  A move request that also carries `name`/`steps` applies
  only the ownership change - the move branch returns the moved entry (the
  legacy did the same; a caller renames after the move).
- **One store-failure message for both stores.**  `_test_store_failure`
  answers `test store write failed: ...` (the legacy said `test script store
  write failed`); the suites and scripts share the helper.
- **The shared run log may be full when a suite member starts.**  The member
  excerpt is now collected in a private per-member buffer (`_member_log`)
  instead of slicing `_run['log']` from its pre-member length: with the
  shared buffer already at `TEST_LOG_MAX`, that slice used to be empty and
  the member's report showed no log (`log_truncated: False`).

Tests: **62 suite tests (`tests/test_simple_suites.py`) + 9 drift
(`SuiteDriftTests`), 117 runner tests (`tests/test_simple_runner.py`); the
full suite is 2226**.  Still **no live card has run a suite**: the fake
transport covers the order/`on_fail`/teardown/counter semantics, and the
first bench run is the acceptance check - a real `start_reset` (the reset +
re-init on an eUICC switching its active profile mid-suite), the ADM latch
across the reset, and the teardown leaving a real card reusable.

## Phase 3c-3e-1 outcome (the CAT/Simulator endpoints)

The Phone/Simulator tab's whole HTTP surface is served from `ServerState` on
the ported `simple_server.cat_session.CatSession`: `GET /api/menu|stk-status|
events|poll-status|proactive-log|terminal-profile|pli-qualifiers|pli-dict`,
`POST /api/menu-select|menu-respond|event-send|terminal-profile|rescue|
status-poll|poll-toggle|pli-dict` and `POST /api/apdu`.  The CAT dialogue
itself (the FETCH/TERMINAL RESPONSE chain, the ENVELOPE exchanges, the menu
watchdog, TIMER MANAGEMENT and the PLI dictionary plumbing) was the 3c-1
engine; this phase adds the endpoints, the server-side semantic event builders
(`pysim_simple_server.events`, reused from the test runner) and the raw-APDU
automatic proactive handler.

Ported, function for function: the legacy `_do_GET`/`_do_POST` branches for
the paths above, `_make_menu_fetch_handler` (`CatSession.menu_fetch_handler`),
`_finish_pending_menu` (`CatSession.finish_pending_menu`), `_menu_send_response`
(`CatSession.menu_send_response`), `_send_event_download`
(`CatSession.send_event_download`), `_resend_terminal_profile`
(`CatSession.resend_terminal_profile`), `_send_status`/`_handle_proactive_chain`
(the session's `send_status`/`handle_chain`), `_validate_tp_hex`
(`validate_terminal_profile`), `_poll_enable`/the effective poll state
(`_poll_state`/`poll_toggle`), `_pli_data_hex`/the `_PLI_DATA` layering
(`_pli_effective`/`set_pli_dict`), and the menu text match
(`resolve_menu_item` over the shared `testscript.match_text`).

Spec anchors: TS 102 221 §11.2.1.2 (TERMINAL PROFILE), §11.2.2 (ENVELOPE),
§11.2.3/§11.2.4 (FETCH / TERMINAL RESPONSE), §14.6.2 (the idle poll);
TS 102 223 §6.3 (the `91XX` status), §6.4.6/§6.4.14 (POLL INTERVAL / POLLING
OFF), §6.4.16 (SET UP EVENT LIST), **§6.6.7 (SET UP MENU: the Alpha
identifier is mandatory - a card that omits it is tolerated as a guarded
fallback, never an error)**, §6.8.0/§6.8.1 (the TERMINAL RESPONSE structure
and the echoed command details), §7.5 (event downloads), §7.5.22/§8.97 (Poll
Interval Negotiation), §8.7 (device identities), §8.12.0 (the result codings)
and §8.15/§6.8.5 (the Text string codings).

Deliberate, documented decisions (deviations from the legacy):

- **`/api/apdu` answers `200` with the card's status word.**  The legacy ran
  the APDU through `scc.send_apdu_checksw`, which raises on a non-`9000`
  answer, and its handler answered `500 {'error': …}`.  A raw-APDU workbench
  must see the SW it asked for, so the port sends through the transport and
  returns `{response, sw}` for any SW.  The automatic proactive handler on a
  `91XX` answer stays (the legacy default); it is off only while a test run or
  a BIP session owns the card (409 / the BIP terminal answers its own
  commands).
- **A transport error is `503` with the status body.**  The legacy
  `/api/status-poll` answered `200 {'sw': None, 'error': 'card disconnected'}`
  and `/api/menu-select` had no handler at all (the connection dropped).  The
  3c-3a classification tears the stale link down, re-arms the auto-equip and
  answers `503` with `status()` (the PWA resets its card views from
  `connected`/`card_session`); a decoder/protocol error still does not lose a
  healthy card (it stays a `500`).
- **`/api/menu-select` names the item by text as well as by id.**  `item_id`
  stays (one octet, validated like `/api/menu-respond` - an out-of-range value
  is a 400, never a silent `& 0xFF` truncation); `item_name`/`text` resolve
  against the cached menu with `mode` (`exact` default / `contains`, both
  validated - an unknown mode used to degrade to `contains`) and
  `case_sensitive` (a boolean; the literal `true`/`false`/`0`/`1` spellings are
  accepted so `"false"` cannot read as true).  The legacy only took an id.
- **`/api/event-send` takes the semantic `fields` and the `event_src`
  override.**  `fields` build the event object through
  `pysim_simple_server.events` for the four modelled events (0x03/0x0B/0x12/
  0x1D; validated at request time) and **win over `event_data`** when both are
  present; every other event form travels as the raw `event_data` hex.
  `event_src` overrides the device-identities source (TS 102 223 §8.7).
  `event_type` accepts a number, a decimal digit string (decimal for
  tolerance - `'12'` is `0x0C`; the legacy only accepted a number and raised a
  `TypeError` on a string, so there is no legacy behaviour to match), the
  script hex form (`0x1D`/`1D`) and the name keywords.
- **`/api/event-send` does not attach `net_state`** (the legacy re-read the
  monitor for a Location-status event and returned it).  The network-state
  monitor is Phase 3c-3e-2 work (`simple_server.netsim`/`netstate` land with
  the net-sim endpoints), so the endpoint returns only
  `{sw, [data], [negotiation]}` for now - recorded as a 3c-3e-2 item.
  *(Superseded by 3c-3e-2a: the monitor exists, the event patches it and the
  response carries `net_state`.)*
- **`/api/stk-status` keeps the legacy shape.**  `active`/`pending`/
  `pending_type` plus `pending_data`; an earlier draft added a `type` alias of
  `pending_type`, which nothing read - it is gone rather than documented.
- **The PLI dictionary endpoints are card-free** like the other cached GETs
  (the `_CARD_FREE_GET` list), so a 2 s UI poll cannot queue behind a long
  card operation.  The overrides survive an equip (they sit on `ServerState`,
  the session's dict is the same object).

**Cutover item (PWA, not touched in this phase).**  `frontend/index.html`
(`stkMenuLabel`, around line 13981) repeats the wrong claim this phase fixed
in the server: its comment says the SET UP MENU alpha identifier is
"optional, TS 102 223 6.6.7", while §6.6.7 marks it **mandatory**.  The
behaviour there (show a translated fallback for a title-less menu) is right
and stays; only the comment (and, if a help/README repeats it, that text) is
wrong.  It is a cutover doc fix: any `index.html` change needs the `sw.js`
`CACHE` bump (two reloads), so it is deferred with the rest of the PWA
changes.

Tests: **71 CAT/Simulator tests (`tests/test_simple_simulator.py`) + 3 drift
(`CatEndpointHelperDriftTests` in `tests/test_simple_drift.py`); the full suite
is 2300**.  The fake
transport answers the whole dialogue (a scripted TERMINAL PROFILE, FETCH,
TERMINAL RESPONSE, STATUS and ENVELOPE queue) and the HTTP endpoints are
driven through a real `ThreadingHTTPServer` on an ephemeral loopback port.
**No live card has run this phase and there is no capture corpus for it**: the
endpoint parity (the menu, the ENVELOPE bytes, the TERMINAL PROFILE, the poll,
the PLI ownership, the raw-APDU gates and the error classification) is pinned
against the fake card and the legacy sources; the first bench run - a real
menu selection, an event download and a poll against the lab card - is the
cutover gate.

## Phase 3c-3e-2a outcome (the eSIM and network endpoints)

The Simulator tab's remaining HTTP surface is served from `ServerState`:
`GET /api/esim/chip|profiles|notifications`, `POST /api/esim/profile`,
`POST /api/net-sim`, `GET /api/net-state`, `POST /api/net-state-refresh` and
`GET /api/mcc-mnc`; `/api/status` gained `euicc`/`eid` and `/api/event-send`
gained the `net_state` block.  The SCP81/BIP control endpoints remain
3c-3e-2b work.

Ported, function for function: the legacy `/api/esim/*` handlers with
`_esim_refresh_chain`, `_esim_reinit`, `_esim_verify_switch` and
`_finish_pending_menu`; `_netstate_read`/`_netstate_compute`/
`_netstate_install`/`_netstate_init`/`_netstate_ensure`/
`_netstate_after_net_sim`/`_netstate_after_event`; and the four
`_mcc_mnc_*` helpers (now `simple_server/netmon.py`).
:mod:`pysim_simple_server.netsim` and :mod:`pysim_simple_server.netstate` are
**reused unchanged** - the scenario builders/step log and the monitor model
have one implementation; :mod:`simple_server.netsim` only supplies the card
surface the legacy runner expects (select by path through the file model, the
FCP metadata, UPDATE/READ, the AUTHENTICATE transport and the event download).

Spec anchors: SGP.22 v2.6 §5.7.2 (the ES10 STORE DATA), §5.7.8
(EUICCInfo1/2), §5.7.9 (ListNotification), §5.7.22 (GetRAT), §5.7.15
(GetProfilesInfo), §3.2.1 steps 6-8 (EnableProfile: "result OK before REFRESH",
the REFRESH, the reset that completes the switch) and §3.2.2 steps 5-7
(DisableProfile) - the ES10c functions are §5.7.16/§5.7.17 -, §5.7.20 (EID);
TS 102 221 §11.1.1.1 (the SELECT functional description) and §8.4.1/§8.5.1.1
(the MF selectable by FID from any last selected file, SELECT by DF name -
the observed MF-refusal is a deviation handled by a guarded fallback);
TS 31.102 §4.4.3.1/§4.4.3.2 (EF.Kc/KcGPRS under DF.GSM-ACCESS) and
TS 51 011 §10.3.3/§10.3.32 (the 2G 6F20/6F52 codings); TS 23.122 (EF.FPLMN and
attachment); TS 102 223 §7.5 / §8.27 (the Location status event).

Deliberate decisions (deviations from the legacy):

- **The eSIM endpoints select the ISD-R by AID on `FileOps`** (the legacy used
  pySim's shell application).  The MF restore is best effort with a guarded
  fallback (select ADF.USIM first, then MF) because some cards refuse the MF
  SELECT by FID from within an ADF; a card whose active profile is disabled
  has no filesystem, so a failed restore is logged, never an error.  A card
  without the ISD-R is `400` (`NotAnEuiccError`, a `ValueError` - the legacy's
  localized `not_an_euicc` answer).
- **The re-initialization is `ServerState.reset()`**: a physical reset plus the
  full `_init_session` (the legacy `_card_reset_reinit`).  A failed re-init is
  reported as `reinitialized: false` (the legacy returned False too), and no
  verification follows it.
- **`refresh_seen` keeps the legacy safety net** over the CAT log (a REFRESH
  answered outside the switch's own chain still counts) besides the chain
  callback.
- **`refresh` defaults to `true`** (the legacy's `body.get('refresh', True)`) -
  the same choice the legacy made - but it is read as a real boolean: the
  literal `"true"`/`"false"`/`"1"`/`"0"` strings are accepted and anything else
  is a `400` (`bool("false")` is truthy, so a hand-written API call used to
  ask for TRUE while saying "false").  The Phase 3a caveat (one bench eUICC
  **refused `refreshFlag` TRUE**, an undefined `enableResult` 7, and accepted
  FALSE) did not reproduce in the 4.0.0 endpoint run, which accepted TRUE in
  both directions (see the live smoke note below); the flag is kept and a card
  that refuses TRUE can be given `refresh: false`.  This flow always resets and
  re-initializes the card itself, so the flag only asks for the card's own
  REFRESH.
- **A failed post-switch verification is not a request error**: the ISD-R
  selection for the verification runs *inside* a guard, and a failure (the
  card refusing the select after the reset, a disabled active profile) leaves
  `verified`/`state_after` at `null` instead of raising `NotAnEuiccError` -
  which the dispatch would report as `400 "not an eUICC"` and throw away a
  completed, destructive switch (the legacy's
  `_esim_verify_switch` degraded with `{'verified': None, 'state_after':
  None}` too).
- **Only a Location status event patches the monitor** (`/api/event-send`
  gates `_netstate_after_event` on `event == 0x03`, the legacy `is_location`
  rule).  Any other event download issues no SELECT/READ of EF.IMSI and leaves
  the monitor's `imsi` entry untouched - the earlier port patched on every
  event (three extra APDUs and a `source: read` flip).
- **The decoders keep Phase 3a's lower-case hex** (`decode_info1/2`,
  `decode_addresses`, `decode_rat`) where the legacy uppercased.  The PWA
  upper-cases display values; the profile rows' `isdp_aid`/`icon` are
  upper-cased to match the legacy API.  `EsimDecodeDriftTests` compares both
  implementations case-folded and pins the port's lower-case form.
- **The net-sim adapter carries a path table** (`PATH_ALIASES`) for the two
  keys whose legacy candidate paths predate the current tree: EF.Kc/EF.KcGPRS
  sit under `ADF.USIM/DF.GSM-ACCESS` (`5F3B`), and the 2G SIM files are
  `6F20`/`6F52` (the legacy list's `DF.GSM/6F08`/`6F09` are stale).  The
  scenario log keeps the legacy `path` string; the exchange uses the model
  path.
- **The adapter's command class is the file-access CLA** (`00`/`a0`), which is
  what pySim's `scc.cla_byte` meant to the runner - not the CAT CLA.
- **The network-state read needs an FCP** for a file's length, so a 2G SIM
  (whose SELECT carries no FCP, TS 51 011 §9.2.1) reports its files absent -
  the same limitation `/api/read` had until a card-independent length source
  landed.  **Fixed in 4.0.1**: the TS 51 011 SELECT file information carries
  the size/structure/record length (`fcp.parse_gsm_select`), so the monitor
  reads the SIM's files (live: IMSI/LOCI/FPLMN/SMSS present, SPDI absent).
  The net-sim adapter's reads (`simple_server/netsim.py`) get the same sizes
  from the parsed SELECT; its `_LchanShim` `NO_FCP_SW` path (`6985`, empty
  data - the runner's "read failed, skip", its optional writes fall back, e.g.
  EF.FPLMN to `FF`×12) remains the guard for a card that returns no
  information at all, instead of aborting the whole scenario with a
  `ValueError`.  A card-refused READ returns the card's own status word the
  same way.
- **`/api/net-state` and `/api/mcc-mnc` are card-free GETs**
  (`CARD_FREE_GET`); the eSIM GETs stay card-locked like the legacy's, and
  `/api/esim/*`, `/api/net-sim` and `/api/net-state-refresh` are refused
  `409` while a test run owns the card (the existing `TEST_BLOCKED_PATHS`).
- **A pre-flight validation before touching the card**: an invalid
  `/api/esim/profile` request (action, profile identifier) is a `400` before
  the ISD-R selection or `finish_pending_menu` - a bad request must not
  answer a pending proactive command.  Likewise `/api/net-sim`'s `scenario`
  and `/api/net-state-refresh`'s `files` shape are validated up front.

Tests: **26 eSIM (`tests/test_simple_esim_api.py`) + 34 net-sim/monitor/
operator-list (`tests/test_simple_netsim_api.py`) + 6 drift
(`EsimDecodeDriftTests`/`MccMncDriftTests` in `tests/test_simple_drift.py`);
the full suite is 2366**.  The eUICC fake answers the whole ES10 dialogue
(chip/profile/notification objects, the switch's `91XX` + REFRESH chain, the
post-reset re-read) and the net-sim fake a filesystem + AUTHENTICATE +
ENVELOPE; both run the HTTP endpoints through a real `ThreadingHTTPServer`.
The review round added the regressions for the non-Location event gate, the
REFRESH-log safety net, the no-STATUS-after-REFRESH rule, the failed
post-switch verification, the model-unknown candidate path and the FCP-less
(2G SIM) read.
**Live smoke (2026-11-23, server 4.0.0 on :8080, the bench eUICC EID
`89086030202200000026000024920451`):** the read path ran through the HTTP
endpoints - `/api/esim/chip` (EID, EUICCInfo1 `svn 2.2.2` + the CI PKI lists,
EUICCInfo2 with 14 fields, the configured addresses with the root DS
`testrootsmds.gsma.com` and no default DP, one GetRAT rule, `errors: {}`),
`/api/esim/profiles` (2 profiles, `error: null`; the card returns **no
`profileNickname` (0x90)** - the "Alfa" labels are `serviceProviderName`
(0x91) and `profileName` (0x92), so `nickname: null` is correct and the PWA
title falls back to provider + name) and `/api/esim/notifications` (16
pending, `error: null`).  On the UICC `/api/esim/chip` answered `400`
`not_an_euicc`.  The switch ran both directions: `enable` `…4002667` with
`refresh: true` → `ok`, `reinitialized: true`, `verified: true`,
`state_after: "enabled"`, the ICCID re-read, session 2; `enable` `…393648`
with `refresh: false` → the same, session 3 (the original state restored).
`refresh_seen` was false in both runs - this card returned its result inline
and sent no REFRESH, so the Phase 3a "TRUE is refused" observation did not
reproduce here.  Remaining live checks: the simulator's writes on a real card
and the monitor's operator resolution on real files.

### Live smoke (2026-11-23): the 2G SIM card-detection quirks

The bench 2G SIM (ATR `3B1695D0006B2C0D00`, T=0) failed the first cutover's
`detect_card` with SW `6999` on every probe.  Two card behaviours were
measured (the old tree's trace as the reference):

- **An A0 `SELECT MF` is refused with `6999` until a DF has been selected.**
  The old tree (pySim's `SimCardBase.probe()`) probes **DF.GSM (`7F20`)** with
  CLA A0 first and only then selects MF; that order works.
- **An unknown-USIM-AID select poisons the card**: `00A4 0404 07
  A0000000871002` answers `6999` and every further command (A0 MF, A0 DF.GSM,
  the ISO MF form) answers `6999` until a physical reset.  The old tree never
  sent that AID before the SIM probe.

Fix: `detect_card` probes **MF (CLA 00) → DF.GSM (CLA A0, `7F20`) → USIM
ADF**, the AID probe strictly last; the TS 51 011 `select_file` form carries
the response-data octet (`A0A4 0000 02 <fid> 00`, pySim's form - the T=0
transform strips it on the wire).  Live result: `init_card` classifies `sim`
(CLA `a0`, no AIDs), and EF.ICCID reads `980701721274117946FF` →
`897010272147119764`.  The no-FCP read limitation is addressed below (the
GSM SELECT-response parse + the size-agnostic fallback).

## Phase 3c-3e-2b outcome (the SCP81/BIP control endpoints)

The SCP81 tab's listener and scripts and the Simulator's BIP pill are served
from `ServerState`: `POST /api/scp81/bip`, `POST /api/bip/control`,
`GET /api/scp81/status|bip/status`, `GET /api/scp81/log|bip/log` (`?after=`),
`POST /api/scp81/log-clear|bip/log-clear`, `GET /api/scp81/script` and
`POST /api/scp81/queue|psk-map|gen-install`.  `simple_server/scp81.py` holds
the control and the script engine; the listener/terminal stays
`pysim_simple_server.scp81.PskTlsServer` and `httpota.TcpDumpServer`, the BIP
session `simple_server.bip.BipSession` (3c-2) and the APDU builders
`simple_server.capram` (the same code the RAM install uses).

Ported, function for function: `_scp81_bip_control`, `_bip_control`,
`_bip_session_stop`/`_bip_status_body`/`_bip_listener_status`,
`_scp81_restart_run`/`_scp81_script_mid_run`/`_scp81_reset_script`/
`_scp81_queue_script`/`_scp81_script_state`/`_scp81_script_responder`,
`_scp81_command_body`/`_scp81_parse_response`/`_scp81_continuation`,
`_parse_psk_map`/`_scp81_update_psk_map` and `_scp81_gen_install` - the
`_SCP81_*`/`_BIP_*` globals became `Scp81Control`/`ScriptEngine` attributes of
one server.  The port keeps the legacy's split of duties exactly: the
administration script **runs on the card** (the SD fetches the C-APDU and
reports the R-APDUs in its next POST), so these endpoints do no card I/O of
their own - the card exchange is the BIP terminal's, and only the session
start/stop touches the terminal under the card lock.

Spec anchors: GPC v2.2 Amendment B §4.3 (the TLS-PSK dialect: the Table 4-2
suites, TLS 1.0-1.2, no resumption) and §4.4/§4.4.2 (the `X-Admin-*`
administration dialog; 204 ends the session), TS 102 226 §5.2.1 (the Command
Scripting template `AA`/`AE 80`), §5.2.2/Table 5.12 (the Response Scripting
template, the executed count `80` and the R-APDU `23`), §8.2.1.7.2 (GET DATA
FF21), GP Card Specification v2.3.1 §11.4.2 (GET STATUS, `63 10` = more
occurrences) and §11.5/§11.6 (INSTALL/LOAD).

Deliberate, documented decisions (deviations from the legacy):

- **The session is server-owned and survives a card change.**  The legacy kept
  one global terminal and never reset it on equip/removal; this build has one
  persistent `BipSession` per server (`state.bip_session`) and
  `attach_card`/`detach_card` swap the CAT session on a re-init/removal: the
  old card's channels are closed and their queued Channel status events
  dropped, while the listener, mode, target, owner and `link_events` stay - a
  listener started before a card (the normal "start, then trigger" workflow)
  or kept across a swap serves the next card's OPEN CHANNEL.  The detach
  closes the old channels first and drains the queued Channel status events
  afterwards, with every channel marked `closed_reported` before it is
  removed - the terminal's 250 ms monitor may still hold a snapshot of the
  channel list, and its peer-close branch would otherwise queue a link-status
  event for the gone card into the next card's session.  `state.bip` stays
  the alias for the *current card session* (`None` when cardless), so the CAT
  hooks and `/api/status` keep their per-card meaning.
- **`start_listener` is the new seam**: the SCP81 control builds its TLS/dump
  listener and hands it to the session, so the one-session rule (and the
  `replaced` report) covers both controls without either handler writing the
  other's globals.
- **`_listener_status` no longer assumes an `accepted` counter**: the TLS
  server has `conns` instead, so `/api/status`'s `bip` block reports
  mode/host/port for it and the SCP81 control adds the TLS block (identities,
  last handshake, framing) to its own status/log/start responses.  The SCP81
  status body also carries three **additive** fields (`mode`, `target`,
  `script` - the listener's mode, the channels' target and the script state)
  so one poll drives the whole panel; `owner`/`bip`/`listener` stay the legacy
  contract.
- **A validation refusal is a `400`** (`{ok: false, error}`), not the legacy's
  `200`.  The control endpoints' own `ok` mirrors the legacy field, so the PWA
  reads both shapes; `gen-install` keeps the legacy `200 {ok: false}` (its
  refusals are a file/parameter problem, and the docs pin that contract).
- **Validation is stricter where a silent fallback hides a mistake**: the TLS
  version and the `Connection` header are enums (a misspelled pin used to
  silently widen the protocol window to `auto`), `chunk_size`/`answer_delay`/
  `port` are validated as numbers, `link_events`/`chunked`/`compact_headers`/
  `cr_tag`/`force`/`make_selectable` read the literal `"false"` as FALSE (the
  v3.22.0 rule), the cipher list is checked before the listener binds (a bad
  cipher used to raise inside the constructor *after* the socket was bound,
  leaking it), and a start's script list is APDU-validated (the legacy queued
  any string).
- **The script state is locked.**  The listener answers on its connection
  thread while the HTTP endpoints read/queue, so the engine state is guarded
  by an `RLock`; the legacy relied on the blanket card lock, which the
  listener thread never took (a pre-existing race).
- **`/api/scp81/log|bip/log` and the status/script GETs are card-free**
  (`CARD_FREE_GET`): the views poll them, and the listener may run cardless.
  The control POSTs stay card-locked (the legacy order) and are in
  `TEST_BLOCKED_PATHS` (already ported with the runner; a drift test now pins
  the two sets equal).  **The two log clears joined `CARD_FREE_POST`**: they
  only touch the terminal's log (the terminal side, which may run cardless),
  so the Clear button need not queue behind a long card operation;
  `TEST_BLOCKED_PATHS` is checked first, so a test run still refuses them.
- **`_redact_psk_fields` is not ported**: the shared `redact_body` already
  masks `psk_hex` and `psk_map`, so the request log is safe with one rule -
  pinned by an `/api/scp81/bip` request-log assertion in
  `tests/test_simple_scp81_api.py`.
- **The previous session stops before the listener binds** (the legacy's
  `_bip_session_stop` ran before the bind, unlike its validation).  A Start
  on a port the old listener holds therefore rebinds it - the PWA's Start is
  the normal way to apply the Options - and the script reset/rewrite runs
  only after a successful bind, so a refused Start never swaps the running
  script's progress.  Trade-off (documented in `Scp81Control.start` and
  `docs/api.md`): a bind failure at a *foreign* port leaves no session at
  all; only a validation refusal preserves the previous one.
- **A listener construction error is the control's `{'ok': False,
  'error': ...}`** (a 400): a taken port (`OSError`), `PskTlsServer`'s
  Python-3.13 `RuntimeError` and an `ssl.SSLError` from `set_ciphers` are all
  caught, never an escaped exception reaching the 500 catch-all (the legacy
  handler happened to turn a bind failure into a 200).  The legacy listener
  classes are used **unmodified**: the partially built `TcpDumpServer` of a
  failed bind is finalized by the refcount as its traceback clears (CPython
  reports a `ResourceWarning`; the socket is closed and no session is
  created).
- **The reader-switch guard keys on the persistent session**: `switch_reader`
  refuses the switch while `bip_session.owner` is set (the SCP81 listener or
  the BIP pill), not the per-card `state.bip` alias - a cardless listener
  would otherwise slip through while the switch resets the card and kills the
  channels.

Tests: **57 API (`tests/test_simple_scp81_api.py`) + 11 drift
(`Scp81DriftTests` in `tests/test_simple_drift.py`) + 3 lifecycle
(`tests/test_simple_bip.py::CardSwapTests`); the full suite is 2438**.
The API suite runs a cardless state through a real `ThreadingHTTPServer`
(each listener mode, the PSK table and its reuse, the one-session replacement
in both directions, the validation, the status/log cursor and clear, the
script queue/state, `gen-install`, the shutdown and the card-change rule) and
a **real TLS-PSK handshake** with a loopback client over the listener the
endpoint started - the same wire path a card's BIP channel uses - including
the 200/204 administration dialog, the R-APDU report, the resume/restart rule,
an unknown identity and a live PSK-table update.  The drift tests pin the
template bytes, the response parse (including BER long forms and truncation),
the continuation, the PSK table, the script run/state machine and the
`gen-install` list against the legacy copies.
**No live card has run these endpoints**: a real card's OPEN CHANNEL through
the started listener, the SD fetching the scripted C-APDUs and reporting the
R-APDUs (the reference session's framing knobs), and the `63 10`/`CA FE`
listing continuation on hardware remain the bench acceptance checks
(`docs/scp81-findings.md` tracks the live outcomes).

## Phase 3d-1 outcome (the entry point + the GSMTAP tracer)

`simple_server/__main__.py` is the pysim-free entry point: `python -m
simple_server` or the `simple-server` console script (`pyproject.toml`
`[project.scripts]`).  It imports pyscard only through
`simple_server.transport` and its own PC/SC pre-flight; the module-import path
stays pysim-free (`--help` needs no reader and no pySim).

**The CLI.**  Ported: `-r/--reader NAME`, `--http-host`, `--http-port`,
`--web-dir`, `--mcc-mnc-list`, `--card-presets`, `--test-scripts`,
`--test-suites`, `--sms-oa`, `--sms-sm-sc`, `--terminal-profile`,
`--poll-interval`, `--menu-timeout`, `--no-card-init`, `--no-auto-equip`,
`--apdu-trace`, `--gsmtap [HOST[:PORT]]`, `--log-requests`, `--timing`.  The
defaults equal the legacy ones (the TERMINAL PROFILE literal is pinned against
`simple_server.server.DEFAULT_TERMINAL_PROFILE` by `tests/test_simple_main.py`).
`--http-port` validates 0-65535 at parse time (an out-of-range value made
`socket.bind` raise `OverflowError`, which the friendly bind-failure catch
missed and which escaped as a traceback); the bind catch itself now takes
`(OSError, OverflowError)`, and the same range rule applies to the GSMTAP
target's port (`sendto` raises `OverflowError`, not `OSError`, so the
fire-and-forget sender catches both).

**Not ported (deliberate).**  `-d/--device` (the serial/FTDI transport; the
port is PC/SC-only, and the pre-flight's access/service/reader classification
replaces the legacy's silent serial fallback), `--fast-init` and
`--full-pysim-init` (both are pySim init paths; `simple_server.cards` is the
one init) and the legacy `-p/--pcsc-device` slot form (`-r` takes the reader
*name*, and "the first reader" is the default).  **The cutover must rewrite
`start.sh`/`start.bat` (`-p 0` -> the first reader / `-r <name>`) and the
README option table together** - the packaged scripts still pass the legacy
flags.

**Startup.**  `_guard_console_streams()` is the first statement (the v3.30.3
code-page rule) and `configure_logging()` the second: INFO for `simple_server`
(the `RAM C-APDU`/`OTA SEND`/`TEST-RUN` bench diagnostics must reach
`simple_lastrun.log`), WARNING for third parties.  Then the parser, the
`--terminal-profile` validation (the endpoint's own rule), the tracers, the
PC/SC pre-flight (the v3.38.1 classification: access/service/reader, guidance,
exit 2), the `ServerState`, `_startup`, the HTTP factory and `_serve`.
`main()` resets the module-level `--timing` state, so a repeated call in one
process cannot inherit it.

**`--no-card-init` skips a phase, not the plumbing.**  `ServerState.open(...,
skip_card_init=True)` still builds the link and reads the ATR, and
`start_poll()`/`start_monitor()` still run; `_init_session(skip_card_init=True)`
runs `cards.init_card(transport, skip_init=True)` (the placeholder `unknown`
card, the UICC CAT CLA) and returns before the detection/MF select, the CAT-free
reads (EF.ICCID/EF.ARR/EF.PBR), the TERMINAL PROFILE and the drain.  The
session is `connected` and carries a CAT session (so `/api/poll-toggle` and
the operator's polling work and a later Equip initializes the card fully), but
`card_ready_session` is **not** set - the marker means "a full initialization
finished", so the batch worker does not act on the placeholder.  The
`--no-card-init`/`--no-auto-equip` pairing is unchanged (auto-equip off).
The tests run the real `ServerState` through this path: the poll thread and
the presence observer exist, `poll_once()` sends a STATUS, a removal clears the
session and `equip()` then initializes the card properly.

**A card removed during the startup init** is classified as a transport error
(`_is_transport_error`: a `ReaderError` or a pyscard `hresult`) and reported as
the v3.32.5 one-liner (`INIT: card removed during initialization - server
ready...`) without a traceback; only a decoding/protocol failure logs
"card initialization failed" and the traceback.

**GSMTAP at the transmit level.**  `simple_server/gsmtap.py` replaces the
legacy tracer: `SendTpduTracer.attach(link, tracer)` wraps the link's
`send_tpdu` (the one method every exchange goes through), its `connect` (for
the ATR) and its `reset_card` (a reset marker **before** the power cycle, so
the trace reads RESET then the post-reset ATR).  The packet form is unchanged
and still pinned against the legacy builder
(`tests/test_simple_drift.py::GsmtapDriftTests`, moved there so
`tests/test_simple_main.py` stays pysim-free): the 16-byte libosmocore header
(version 2, `hdr_len` 4, type 0x04), sub-type 0x00 for an APDU and 0x01 for the
ATR, the payload raw.  `--apdu-trace` is the same tracer's stderr sink and
combines with `--gsmtap` through a `Fanout`; one failing sink never breaks the
other or the transmit, and the sender is non-blocking and swallows
`(OSError, OverflowError)`.

**Reader-mode caveat (the honest wording).**  The trace is what
`SCardTransmit` carries, not necessarily the card's wire TPDUs: on a
**TPDU-mode** reader the packets are the card's TPDUs (a case-4 command without
`Le`, the `61XX`, then the `00C00000<Le>` GET RESPONSE - the wire capture); on
an **APDU-mode reader running T=0** the reader does the T=0 mapping itself, so
the packet is the command *without* its `Le` plus the response data and the
final SW - neither a wire TPDU nor the logical C-APDU (the `61XX` and the GET
RESPONSE never reach the application); on T=1 it is the APDU plus data and SW.
The docstrings, the `--apdu-trace` help and `simple_server/__init__.py` say
this; the earlier "the wire TPDUs the reader transmits" wording overstated it.
A guaranteed wire capture is simtrace2 hardware.

**Release package.**  `tools/build_release.py` now ships `simple_server/`
(the console script's module; `setup.sh` runs `pip install -e .` on the
packaged tree, and without it the installed `simple-server` died with
`ModuleNotFoundError`).  `tests/test_release_package.py` parses
`[project.scripts]` from the shipped `pyproject.toml` and asserts every
declared module (and its package `__init__.py`) is in the built zip.

**The MCC/MNC data coupling is a 3d-3 item - completed in 3d-3b.**
`simple_server.netmon.default_mcc_mnc_path()` used to resolve
`pysim_simple_server/data/mcc-mnc-list.json`; the operator list and its
`.LICENSE` now live in `simple_server/data` (shipped by
`[tool.setuptools.package-data]` and by `tools/build_release.py`, pinned by
`tests/test_release_package.py`).

Tests: the full suite is **2524** (`tests/test_simple_main.py` 81,
`GsmtapDriftTests` in `tests/test_simple_drift.py` 4, the new console-script
guard in `tests/test_release_package.py`).  `tests/test_simple_main.py` is
pysim-free: the legacy packet comparison lives in the drift module.  Nothing
here has run against a live card beyond the earlier bench sessions; the
--no-card-init path and the trace emission are covered with the fake link.

## Phase 3d-2 outcome (the PWA cutover)

The PWA now speaks the pysim-free API; the legacy package is untouched.  The
top-level tabs are Remote APDU, SCP80, SCP81, Cards, Profiler, **File
manager**, **Status** and Simulator (the "Card reader" label is gone).  The
**File manager** tab carries the file manager (`#pysim-sub-files`, unchanged
IDs), the **Services** editor (`#pysim-sub-services`) and the **Custom files**
panel (`#pysim-cf-panel`, moved from the Profiler's list tabs — `profilerListSwitch`
now toggles only Profiles/Card snapshots, and a legacy `listtab: 'custom'`
state falls back to Profiles).  Its state is `_filesSub` /
`filesSwitchSubtab` and it is recorded in the view history.

The **Status** tab (`data-tab="pysim"`) keeps the connection form, the status
block and the **Raw APDU** pill (`_pysimSub = 'apdu'`; the pySim command line
pill, input, suggestions, help and output and the `pysimExec`/`pysimCmdInput`/
`pysimCmdKeydown`/`pysimLoadCommands` helpers are gone).  The status block
renders the new `/api/status` shape only — reader, ATR, card kind/name, ICCID,
connected/card-present/initializing, the `selection` object and the state
machines (`proactive_mode`, `poll`, `test_run`, `bip`, `scp80`) — no pySim
internals (`profile`/`cla_byte`/`sel_ctrl`/`channels`/`current_selection`).
`pysimRefresh` fetches the status and renders it through `pysimStatusRender` /
`pysimStatusHtml` / `pysimReaderRender`; the 2 s poll re-renders it on every
tick, so the state machines stay live.  The new status carries no
`proactive_seq` (the legacy gate), so the visible proactive log now refreshes
with the poll; the sequence gate stays for a server that still provides it.  A **reader selector** appears in the
status row when more than one PC/SC reader is present: `POST /api/reader
{name}` after a confirm that names the reset/re-initialization, the failure
path alerts and re-reads the status.

Connection/commands: `pysimConnect` probes only `/api/version` + `/api/status`
(no `/api/command status`, no `/api/commands`) and a cardless server is a
normal state; **Equip** -> `POST /api/equip`, **Reset** -> `POST /api/reset`,
**Check status** -> `POST /api/status-poll` (then a re-read); the lifecycle
answers are run through `pysimCardStateUpdate` + `pysimRefresh`.  "Verify vs
pySim" is removed (`#sp-verify-btn`, `pysimVerifySp`, the `/api/sp-verify`
call, `#sp-verify-result` and their RU keys); the SCP80 preview stays.

i18n: the new labels ("Status", "Reader", the reader-switch warning/confirm/
failure) have RU entries and the removed keys ("Card reader", "pySim command
line", "Verify vs pySim", the verify messages) are gone.  Help EN/RU: the tab
list, the removed pySim command line section, the new Status/File manager
structure (Services §6.4, Custom files §6.5), and the removed "Verify vs
pySim" sentence.  `sw.js` CACHE is `simple-v498`; `style.css` needed no
rebuild (no new utility classes).

Tests: `tests/test_fetch_routes.py` now validates the PWA's `pysimFetch` calls
against `simple_server/server.py`'s `_dispatch_get`/`_dispatch_post` (the
cutover target) and adds a pin that `/api/command`, `/api/commands`,
`/api/help` and `/api/sp-verify` are neither served nor called, while
`/api/equip`, `/api/reset`, `/api/status-poll`, `/api/reader` (POST) and
`/api/readers` (GET) are.  `frontend/tests/html.test.js` (8 top-level tabs,
the File manager custom pill, no Profiler custom tab), `nav.test.js` (`_filesSub`,
`filesSwitchSubtab`), `profiler.test.js` (the two profiler list tabs) and
`card_state.test.js` (the status render helpers) are updated; the new
`reader.test.js` pins the status block's fields (no pySim internals), the
reader selector's one/many rule and the switch confirm/cancel/failure paths.

Review fixes folded in: the File manager pills have **one** wiring (the
delegated registration; the inline `onclick` is gone, so a click can no longer
run `pysimServicesOpen`'s card reads twice), the status block reports a BIP
session only while one exists (the session view is always present and defaults
to an inactive redirect terminal) and its labels/values are translated
(RU entries added), a stale `pysim` sub-tab replay (`files`/`services`/`cmd`)
folds onto the Raw APDU pill instead of blanking the tab, the help anchors
point at the Status block (§6.2) and the Services section (§6.4), and
`pysimCardStateUpdate` no longer overwrites the structured block with a
one-tick message (the legacy `auto_equip` gate is gone; a present card
without a session drives the spinner).  `reader.test.js` adds the success path
(a confirmed switch feeds `pysimCardStateUpdate` + `pysimRefresh`) and the BIP
gating; `card_state.test.js` pins that the block is never overwritten.

Full suites: **2525 Python + 1177 Node**, green.  The UI is unit-tested only —
not run in a browser against a live card yet.

**Phase 3d-2 is not releasable on its own.**  The cutover is PWA-side: the
`start.sh`/`start.bat` scripts still launch the legacy `pysim_simple_server`,
which does not serve `/api/equip|reset|reader|readers` or `/api/status-poll`
(the PWA's connection/commands now call exactly those), and `setup`/
`docs/api.md`/the README server section still describe the legacy API.  That
is the one transient incompatibility of this phase: the shipped launch path
cannot drive this PWA.  Phase 3d-3 flips the scripts to `simple-server`,
updates `docs/api.md` for the pysim-free API and bumps the version to **4.0.0**
(`VERSION`, `pyproject.toml`, the PWA header and `sw.js` together - this phase
bumps only the SW cache to `simple-v498`).  Do not tag a release from this
state.  Known transient doc state: the READMEs' live-tab sections name
`simple_server` while their "Server" sections still document the legacy
server, and the help's installation snippet still starts `pysim-simple-server`;
both are resolved in 3d-3.

## Phase 3d-3b outcome (the shared modules + the MCC/MNC data)

`simple_server` no longer imports the legacy package at all.  The twelve
pysim-free modules the HTTP core reused are now **copies inside the package**,
under `simple_server/shared/` (a subpackage because `simple_server` already has
its own `netsim.py` - the card adapter over the legacy scenario builders - and
`scp81.py` - the listener control over the legacy `PskTlsServer`; a flat copy
would have collided with both):

| module | used by |
|---|---|
| `presets.py` | `ota`, `server` (preset endpoints), `testrun`, `suites` |
| `testscript.py` | `server`, `testrun` |
| `events.py` | `server`, `testrun` |
| `capmem.py` | `capram` (CAP analysis, the compatibility test) |
| `catrelease.py` | (the release guard table; no importer yet) |
| `netsim.py` | `netsim` (the adapter re-exports `SCENARIO_SERVICE`), `netstate` |
| `netstate.py` | `netmon`, `server` (the network-state cache) |
| `outfile.py` | `server` (`/api/presets/import-outfile`) |
| `scp81.py` | `scp81` (the control starts `PskTlsServer`) |
| `httpota.py` | `bip`, `scp81` (the BIP terminal / dump listener) |
| `test_scripts.py` | `server`, `suites` |
| `test_suites.py` | `server`, `suites` |

The copies are verbatim (headers, docstrings and spec citations intact); only
their three intra-package imports changed (`testscript` -> `events`,
`netstate` -> `netsim`, `outfile` -> `presets`), to `simple_server.shared.*`.
The import relationships are unchanged, so there is no cycle.  The adapters
in `simple_server/` import `from simple_server.shared import X` (the `netsim`
adapter keeps the rename `as core_netsim`; its docstring's old `legacy_netsim`
name is gone).

`simple_server/netmon.py` resolves the bundled operator list from its **own**
package now (`simple_server/data/mcc-mnc-list.json` and `.LICENSE`), so the
last runtime data dependency on the legacy tree is gone.

Packaging: `pyproject.toml` gained
`[tool.setuptools.package-data] simple_server = ["data/*.json",
"data/*.LICENSE"]` (the `simple_server*` discovery already covered the
subpackage; a built wheel carries the 12 modules and both data files),
`tools/build_release.py` ships the whole `simple_server/` directory and so
picked the new files up unchanged, and `tests/test_release_package.py` now
pins the shared modules, the data files and an AST scan that **no packaged
`simple_server/**.py` imports `pysim_simple_server`/`pySim`/`osmocom`/
`construct`.

Guards: `tests/test_simple_proactive.py::PackageImportHygieneTests` now globs
the package recursively (`shared/` included) and has a second AST test for
`pysim_simple_server` imports, plus a coverage pin that the walk reaches
`shared/presets.py`.  `tests/test_simple_drift.py` remains the one module that
imports both implementations; its two module-identity assertions became
copy-vs-legacy comparisons (the netstate/netsim tables and the TLS listener
status block, the latter built once per implementation with the port
normalised), and a new `SharedCatReleaseDriftTests` compares the copied CAT
release table and its lookup rules - the one copy with no other importer.

The legacy-reuse imports in the simple suites' fixtures moved to the shared
copies (otherwise the stores/validators raised the legacy exception classes
and the endpoints answered 500 instead of 400); `tests/test_simple_drift.py`
alone keeps importing `pysim_simple_server`.

Full suites: **2551 Python + 1180 Node**, green.  Self-containment checked by
importing every `simple_server` module (and loading the operator list) with a
meta-path finder that blocks `pysim_simple_server`/`pySim`/`osmocom`/
`construct`.  `pysim_simple_server/` is untouched.

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
- **Phase 3c-1 (spec-checked, two kept deviations, no live-card run yet):**
  the CAT session engine was checked against TS 102 221 V18.4.0 §11.1.2
  (Table 11.9: P2 `0C` = "No data returned" - the STATUS poll) and §11.2
  (TP/FETCH/TR/ENVELOPE APDU shapes), TS 102 223 V18.3.0 §6.8.0/§6.8.1
  (TR structure, object order, echoed command details), §6.8.4 (the POLL
  INTERVAL Duration echo), §6.8.5/§6.8.6 (the interactive answer objects),
  §6.4.6/§6.4.14 (poll adoption) and §7.4 (timer expiration), and TS 101 220
  §7.1.2 Table 7.6 (BER lengths - the `81 nn` long form from 128 octets,
  which the ENVELOPE's concatenated TPDU needs; the old one-octet length was
  a real defect, not a legacy quirk).  **Kept deviations:** (a) the timer
  objects precede the Result in a TIMER MANAGEMENT answer (§6.8.0 puts the
  Result third - kept for the live cards' byte parity, `TODO` for the
  cutover); (b) the Item identifier is omitted for the `help` result of a
  SELECT ITEM (§6.8.6 requires it - no PWA help path exists yet).  **No live
  card has run this module**: every fixture is an existing capture replayed
  through the fake transport, so the first bench run (equip + TERMINAL
  PROFILE + a menu session + a secured packet) is a cutover gate.
- **Phase 3c-3c-1 (spec-checked, no live-card run yet):** the SCP80 operation
  logic was checked against TS 102 225 §5.1.1 (the SPI coding, CNTR/PCNTR and
  the b5b4 counter tracking), §5.1.2/A.1 (the keyset number and the dedicated
  counter per key version), §5.2 Table 3 (the response packet, its status byte
  and the CNTR copy), TS 102 226 §4 (a non-RM TAR's payload is application
  data), §5.1.2 Table 5.1 (the compact response and the command-count bound),
  §5.2.2/Table 5.12 (the Response Scripting template and the Bad format
  error types) and §8.2.1.3.2.4 (the Minimum SPI1), plus TS 101 220 V18.3.0
  Annex D Table D.1 for the ISD/RFM TAR allocations (`RM_TAR_ALLOCATIONS` is
  the curated subset this tool treats as remote management by default; a
  preset's role entries are added at call time).  No coding deviation is
  kept; the one tool-level decision is the `auto` format above (it reports
  the compact form until the RAM sender probes).  **No live card has run this
  module**: every fixture is a scripted fake transport, so the first bench
  run (a real preset-store verify, a seeded APDU send and a `cntr_low`
  verdict) is the cutover gate.
- **Phase 3c-3c-2a (spec-checked, no live-card run yet):** the CAP/RAM
  builders were checked against GP Card Specification v2.3.1 §11.5.2.1/Table
  11-41 (the INSTALL P1 roles), §11.5.2.3.1/Table 11-42 (INSTALL [for load]
  data field), §11.5.2.3.2/Table 11-43 (INSTALL [for install]: the privileges
  length is `01` or `03` with a 1- or 3-octet value, the install-parameter
  field), §11.5.2.3.3/Table 11-44 (make selectable), §11.5.2.3.7/Tables
  11-48/11-49 (the install-parameter tags: `C9` mandatory, `C7`/`C8`/`CA`
  nested in the System Specific Parameters `EF`, `EA` a sibling) and §9.7 (the
  quota coding: a 2-byte integer up to 32767, a 4-byte integer above),
  §11.6.2/Tables 11-56/11-57 (LOAD: P1.b8 = last block, P2 = the sequential
  block number) with Table 11-58 (the `C4` Load File Data Block), §11.2/Tables
  11-20-11-23 (DELETE) and §11.4/Tables 11-32-11-35 (GET STATUS); TS 102 226
  V18.5.0 §8.2.1.3.2.1 (the CA is "included in the System Specific Parameters
  (Tag 'EF')"; GP Table 11-49 nests `C7`/`C8` in that same EF, so the quotas
  and the CA share one object), §8.2.1.3.2.2 (`EA` is a separate object) and
  §8.2.1.7.2 (GET DATA FF21: `81`/`82`/`83`).  The CAP component
  order is **not** a GP rule - it is the JC VM file structure (JC VM
  Specification v2.2.2 §6.2/Table 6-3 "Reference Component Install Order") -
  and the positive APDUs are pinned to the TCA reference trace
  `samples/tca_ram_traces/test_mycard_keyset2_applet_full_load.log`
  (INSTALL [for load], the first 64-octet LOAD block, INSTALL [for install];
  the trace's caller-composed parameters carry an empty `EF 00`, which our
  composer omits when composing from the `EA` primitive - both forms are the
  same packet shape with no second EF).  Kept
  deviation: the APDUs are case 3 (no trailing `Le`) although the GP tables
  list `Le '00'` (the SCP80 scripting layer treats a trailing octet as a
  phantom second command; see the module docstring).  **No live card has run
  these endpoints**: the fixture is a synthetic CAP ZIP and a scripted fake
  transport, so the first bench run (a real `.cap`, the card's accepted RAM
  command format, a real `cntr_low`, an FF21 readout and the CA-in-one-EF
  acceptance) is the cutover gate.
- **Phase 3c-3c-2b (spec-checked, no live-card run yet):** the import-list
  question is JC VM Specification v2.2.2 §4.5.2 (the compatibility rule: equal
  major versions, the CAP's minor not above the resident package's - the spec
  fixes the rule, **not** *when* a card evaluates it; the observed live
  rejections land in the block containing the Import component's last byte),
  with §6.2/Table 6-3 (the component order), §6.6 (`import_component`, the
  `packages[]` of `package_info`) and §6.3 (`package_info`:
  `minor_version, major_version, AID_length, AID[]` - the `version_pair`
  order).  The Directory's positional size array (index = tag - 1, the value =
  the component's `size` item) is §6.4, the rule the drift fixture follows, and
  the flag cleared on a `boundary == total` case is LOAD P1.b8 (GP Card Spec
  v2.3.1 §11.6.2/Table 11-56).  The TAR probe's per-row SPI1 is the TAR's MSL
  (TS 102 226 §8.2.1.3.2.4), its counter persistence follows TS 102 225 A.1
  (one counter per keyset) and its verdict is the ENVELOPE answer plus the
  PoR (TS 102 225 §5.2 Table 3), with the response form following the packet's
  TAR (TS 102 226 §4 - a non-RM TAR's payload stays raw, v3.21.0); the delete
  plan is GP §11.2/Tables 11-20-11-23 (P2.b8 = related objects).  No coding
  deviation is kept; the refinements are the last-block flag clearing and the
  documented decisions above (the request pre-flights, the per-card lock
  re-check, the bounded shutdown).  **No live card has run these endpoints**:
  every fixture is scripted.
- **Phase 3c-3d-1 (spec-checked, no live-card run yet):** the scripted
  TERMINAL RESPONSE is `testscript.build_tr` (TS 102 223 §6.8.0 object order,
  §6.8.1 echoed command details), the terminal's own answer objects are the
  PLI dictionary entry (§6.4.15) and the POLL INTERVAL echo (§6.8.4), the
  expectation FETCH follows the `91XX` length (TS 102 221 §11.2.3), the
  pending-command drain answers `ok` and bounds the chain (§6.3), the File
  List check is §8.18, the SCP80 step's keyset/TAR/SPI1 resolution is
  TS 102 225 A.1/A.2 + TS 102 226 §8.2.1.3.2.4 and the PoR decode is
  `ota.decode_por` (TS 102 225 §5.2 Table 3 / TS 102 226 §5.1.2).  The one
  kept deviation is the legacy `84` Duration tag in the scripted POLL
  INTERVAL answer (documented above); the watchdog's 30 s bound is a
  robustness choice, not a spec value.

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

## PWA information architecture (cutover, user decision 2026-11-22)

The current **Card reader** tab mixes unrelated things: the server connection
form, the card/reader status, the file manager and three pills.  The
pysim-free PWA reorganises it:

- **A new top-level "File manager" tab** gathers the filesystem tools as
  pills: **File manager** (the live two-pane file browser, with its Read raw /
  Read decoded / Edit raw / Edit decoded actions), **Services** (the
  SST/UST/IST/EST editor) and **Custom files** (the offline custom-file
  definitions the tree injects and the scans include).  This keeps the
  **Profiler** offline-only (Profiles / Card snapshots) and gives the live
  filesystem tools their own pill pattern instead of squeezing a two-pane
  live view into the Profiler's list-switch tabs.  The file manager and
  Services are card-needed views (`data-needs`); Custom files works offline.
- **"Card reader" is renamed "Status"** and keeps only the server/card-session
  material:
  - the server connectivity form + connection status;
  - the card/reader status block (our own `/api/status` shape);
  - **Raw APDU** (the card tool that fits nowhere else, with the automatic
    `91XX` handler);
  - the state machines: the proactive session mode (idle / polling /
    FETCH-TR chain / paused on a menu command / test run / BIP session), the
    selected file/application (our `selection` object), the poll state
    (enabled/suspended, interval), the card-session state and ATR, the
    test-run and BIP session state, the SCP80 counter state, and the
    logical-channel states *if* the transport gains logical channels (the new
    transport is channel-0 only today);
  - a **reader selector** when more than one PC/SC reader is present.
    One reader is active at a time; a switch rebuilds the transport for the
    chosen reader, **resets the card** and re-initialises it (each reader has
    its own card, so the previous selection/CAT session/ICCID/caches are
    meaningless), bumps `card_session`, clears the caches and restarts the
    presence monitor.  Backed by `GET /api/readers` (`{readers, active}`) +
    `POST /api/reader {name}`; the switch is refused (409) while a test run,
    BIP session or RAM operation holds the card.
- The **pySim command line** pill and **Verify vs pySim** are removed (see the
  Phase 3c API surface).

The resulting top-level tabs: Remote APDU, SCP80, SCP81, Cards, Profiler
(Profiles / Card snapshots), **File manager** (File manager / Services /
Custom files), **Status** (server + card state, Raw APDU, reader selector),
Simulator.

The status fields come from the extended `/api/status` shape (Phase 3c-3): add
`proactive_mode`, `poll` (`{enabled, suspended, interval}`), `test_run`,
`bip`, `scp80` and `selection` to the planned `reader`/`atr`/`card`/`iccid`/
`connected`/`card_present`/`equipping`/`card_session` set.

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
- **3c-1 — the CAT session engine (done):** `simple_server/cat_session.py` — the
  `CatSession` state (proactive log, menu, pending command, timers, PLI
  dictionary), the FETCH/TERMINAL RESPONSE chain, STATUS / TERMINAL PROFILE /
  ENVELOPE exchanges (incl. the 61xx/62xx/63xx GET RESPONSE quirk), event
  downloads, menu responses/timeout and the timer-expiry ENVELOPE.  BIP is
  injectable (a `bip_handler` callable) so the next piece slots in.
- **3c-2 — BIP (done):** `simple_server/bip.py` — the BIP command handling
  (OPEN/CLOSE CHANNEL, SEND/RECEIVE DATA, GET CHANNEL STATUS) on
  `pysim_simple_server.httpota.BipTerminal`, the session control
  (sink/redirect/passthru), the Data-available event and the Channel-status
  event.
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
  the init phase.  The correction below matters for the release note: the gap
  was **not** that pySim could not handle `61xx` — its transport *does* chain
  the T=0 GET RESPONSE (`_send_apdu`: `61xx`/`9Fxx` → a GET RESPONSE TPDU,
  `62xx`/`63xx` → a dummy one, `6Cxx` → a re-send).  The gap is the **tracer
  level**: pySim's `ApduTracer` hooks `send_apdu` (the logical APDU + the
  final data/SW), so the GET RESPONSE TPDUs and the init phase never reached
  `gsmtap.py`, which therefore *reconstructed* wire TPDUs from the logical
  data (and missed the init phase entirely — the v3.40.10/3.40.11 saga).
  Our transport exposes `send_tpdu` as a distinct hook (the level of every
  `con.transmit`), so a tracer there sees every exchange the application
  makes, with no reconstruction.

  **Scope (PC/SC reader mode):** whether those bytes are the *card's* TPDUs
  or the reader's internal mapping depends on the reader.  In the common
  **APDU mode** the reader/driver performs the T=0 `61xx`/GET RESPONSE mapping
  itself, and `SCardTransmit` returns only the final data + SW — no PC/SC
  application (pySim or ours) can see those TPDUs.  Some readers in
  **TPDU/transparent mode** pass the `61xx` up; there our `_send_t0` chains
  the GET RESPONSE via `send_tpdu`, and the trace shows the real TPDUs.  So the
  claim is "the true exchange **as far as PC/SC exposes it**": the TPDUs
  (incl. the GET RESPONSE we send) on TPDU-mode readers, the logical APDUs on
  APDU-mode readers, plus the init phase (the ATR via the link, the init
  APDUs).  The reader-internal mapping (and the T=1 block framing) is out of
  reach for any application.

**This is a headline release-note item.**  The log and GSMTAP data exist for
card testing, and streaming exactly what the application exchanges — with the
init phase, no reconstruction, and the GET RESPONSE TPDUs where the reader
passes them up — is what makes that trustworthy.  pysim's logical-level
tracer + reconstruction + missed init phase was a real gap (and the init phase
matters most for a workbench).  It remains one of the reasons to phase out
pysim — but the honest wording is "pysim's tracer was at the logical level",
not "pysim could not send the trace".

Implications:

- `simple_server/transport.py` already has both hook levels: `ApduTracer`
  (`trace_command`/`trace_response` around `send_apdu`) and `send_tpdu` (one
  per `con.transmit`, i.e. per TPDU on a TPDU-mode reader).  A GSMTAP tracer
  attaches at `send_tpdu` for the closest-to-wire stream PC/SC offers.
- **Default to APDU mode; do not promise TPDU.**  No `--raw-tpdu` knob and no
  `PcscLink.control()` wrapper: a raw-PPDU escape would be per-reader
  (vendor-specific, often undocumented, needs the CCID driver's
  `ifdDriverOptions` 0x01, and can collide with PIN/firmware features), so its
  practical value is near zero for this project; `SCARD_PROTOCOL_RAW` is the
  memory-card protocol, not a SIM TPDU mode.  For a genuine wire capture use
  **simtrace2 hardware** (the GSMTAP-SIM format is built for it) - that is the
  dependable "true wire" path, independent of the reader.  Revisit the escape
  only if a concrete reader + documented escape + a real need appear.
- `pysim_simple_server/gsmtap.py` imports pySim (`ApduTracer`,
  `parse_command_apdu`); it is reimplemented on `simple_server` (Phase 3) with
  the same GSMTAP-SIM packet format but streaming the real transmits.
- The `_keep_tracer_attached` wrapper and the `_onchange_apdu_trace` monkeypatch
  disappear: the tracer is always attached, because the transport owns its own
  lifecycle.
- This is a Phase 3 concern (wired with the HTTP entry point), but it shapes the
  transport API *now*: `send_tpdu` is the hook, `send_apdu` the logical one.
- **Major version bump:** the cutover to the pysim-free server ships as a major
  release (4.0.0) — a new package/entry point, the GSMTAP streaming behaviour,
  and the dependency change are all breaking.

## Licensing & repo split plan

**Current license: MIT** (as of 4.0.0; `LICENSE` / `THIRD-PARTY-NOTICES` /
`pyproject.toml`).  The GPL history had two anchors:

1. the server **imported pysim** (GPL-2.0-or-later) — removed by this
   phase-out;
2. the **client-side EF decoders** were adapted from simtrace-analyser —
   which is **MIT** (it has been for a while), so the second anchor is
   cleared.

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
3. **simtrace-analyser is MIT** — the second anchor is cleared.  **Done.**
4. **Fork `simple-pysim`** — taken from the **last tagged release v3.40.9**
   (no history; the v1.7.0 "fresh move + archive" recipe): the
   `pysim_simple_server/` package, the pysim-era PWA, the tests, the scripts,
   `requirements-pysim.txt`, the GPL `LICENSE`, the READMEs with the frozen
   notice and `FORK.md`; the updater points at `anttro/simple-pysim`.
   **Done** (`~/projects/simple-pysim`, a local repo — the remote/push is the
   user's).
5. **`simple` is fully pysim-free** — the 12 pysim-free modules live in
   `simple_server/shared/`, the MCC/MNC data in `simple_server/data/`,
   `requirements-pysim.txt` and the pysim install steps are gone, the PWA is
   cut over, and the license is **MIT** (simtrace-analyser is MIT).  One
   server, one script set, one API, one PWA code path.  4.0.0.  **Done.**
6. Update `LICENSE`, `THIRD-PARTY-NOTICES`, `pyproject.toml`, the READMEs, the
   workspace `AGENTS.md` and the phase-out notes to match.  **Done** (the
   `AGENTS.md` carries a cutover note at the top; a full rewrite of its legacy
   chapters is a follow-up).

### GitHub releases at the fork (2026-11-22)

The already-published `simple` releases are historical pysim artifacts.  The
recommended handling:

- **Keep the last pysim release on `simple`** (the final 3.x tag) and publish
  the pysim-free line (4.0.0+) after it.  That gives a clear predecessor and a
  working "last pysim version" download on the same release page.
- **Edit that release's notes** (notes only, never the artifact) to say it is
  the **last pysim-based release** (GPL-2.0-or-later, frozen) and point at
  `simple-pysim` for the maintained legacy.
- **`simple-pysim` publishes its own release** from the frozen tree (e.g.
  `v3.40.2` in the new repo); its `update` script then points at
  `simple-pysim`, so the legacy keeps a working update path independent of
  `simple`.
- **Older pysim releases** (`simple` 3.31.1 … the one before the last): optional
  cleanup.  If they are removed, **remove the releases only, not the tags** -
  a tag is the source of truth and can still serve the source archive; deleting
  a tag also deletes its release and is hard to undo.  `CHANGELOG.md` keeps the
  history either way.
- **Updater major-version guard:** `tools/update.py --check` on a 3.x install
  will see 4.0.0 as "latest"; it must **refuse a major-version jump** (a
  cross-backend change: different package, scripts and dependencies) with a
  message pointing at the last 3.x release / `simple-pysim`, so a pysim install
  is never half-updated to the pysim-free tree.  Add this with the cutover
  (3d).
- The `CHANGELOG.md` 4.0.0 entry names the fork, the last 3.x release and
  `simple-pysim`.

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

## Follow-up tasks (recorded, not scheduled)

### PIN/PUK operations (lost with the pySim command line)

**Shipped in 4.2.0** (was the retired command line's
`verify_chv`/`change_chv`/`unblock_chv`): `POST /api/chv` runs VERIFY `20`,
CHANGE CHV `24`, UNBLOCK CHV `2C`, DISABLE `26` and ENABLE `28`, with the CHV
reference by card class (UICC PIN1 `01`/PIN2 `81`, TS 102 221 §9.5.1; a 2G
SIM's `01`/`02` with the exceptional UNBLOCK CHV1 `00`, TS 51
011 §9.2.9/§9.2.13) and the PIN's application selected first.  The Card
reader's **PIN / ADM** pill (and its PIN1/PIN2/ADM rows' Verify) drives it
since 4.3.0; every attempt is confirmed, the `63Cx` attempts left (the card's
internal retries for CHANGE/ENABLE/DISABLE, TS 102 221 Table 10.9's note),
`63C0`/`6983`/`9840` blocked and the `6984` "referenced data invalidated"
answer are reported, and a successful change/unblock offers the new value for
the preset store (the endpoint never stores a key).

**The PIN lock state** is read **without presenting a PIN** (v4.2.0):
`status.chv` + `POST /api/chv-status` combine the application DF's PS template
DO `C6` (mandatory in an MF/DF/ADF FCP, §11.1.1.3.1 Table 11.3; the PS_DO
bitmap over the `83` references, §9.5.2) with the empty-data VERIFY PIN probe
(§11.1.9.1.2: the retry counter), so a **disabled** PIN (`C6` bit clear) is
told apart from a locked one; a 2G SIM's DF.GSM SELECT bytes (§9.2.1) stand in
for the probe it cannot make (§9.2.9).  The header badge (glyphs since 4.3.0),
the Card reader's PIN1/PIN2 rows and the Verify/enable-disable gating use it.

**ENABLE CHV `28` / DISABLE CHV `26`** followed in **4.3.0** (`/api/chv`'s
`enable`/`disable` actions, the PWA's PIN / ADM pill): the card refuses a
redundant ENABLE/DISABLE (§11.1.11.1/§11.1.12.1), the PWA gates them on the
attempt-free enabled bit, and a 2G SIM's CHV1-only limitation (TS 51 011
§9.2.11/.12) is reported.  ADM remains **Verify-only** (`/api/verify-adm`):
no pinned clause covers changing or unblocking ADM1.

### pySim command audit (what else was lost)

The full pySim-shell command set was enumerated; the useful ones not already
covered:

- `verify_chv` / `change_chv` / `unblock_chv` / `enable_chv` / `disable_chv`
  — see above.
- `suspend_uicc` / `resume_uicc` — SUSPEND UICC / RESUME UICC testing.
- `open_channel` / `close_channel` / `switch_channel` — logical-channel
  management (ties into the Card reader's "logical channel states"; the new
  transport is channel-0 only today).
- `activate_file` / `deactivate_file` — file lifecycle (ACTIVATE/DEACTIVATE
  FILE).
- One-click `create_df` / `create_ef` / `resize_ef` / `delete_file` /
  `terminate_df` / `terminate_ef` on a file-manager tree node (the Remote
  APDU tab has the builders; a node action would be handier).
- `update_imsi_plmn` — the network simulation covers IMSI writes; a shortcut
  may be worth it.
- `export` / `fsdump` — snapshots cover the capture; a JSON filesystem export
  might still be useful.

Already covered without the command line: `apdu` (Raw APDU), `cardinfo`/
`status` (the Card reader tab), `dir`/`tree`/`select`/`read_*`/`update_*`/`edit_*`
(the file manager), `envelope` (Simulator), `terminal_profile` (Simulator),
the service tables (the Services editor), the eSIM ES10 calls (the eSIM
view), `authenticate` (network simulation).  Deliberately out of scope:
`aram_*` (obsolete), the eSIM download/notification-management extras.

## How to use this file with subagents

When spawning a subagent for a phase-out task, include:
1. a pointer to this file (read it first), and
2. the specific task + acceptance criteria + verification commands.

The subagent needs no other context about scope/decisions/inventory — it is all
here.
