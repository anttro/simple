# pysim-simple-server — HTTP API reference

The server exposes a JSON HTTP API under `/api/*`. All responses carry
`Access-Control-Allow-Origin: *` (plus `Access-Control-Allow-Private-Network: true`
on the preflight), so the API is reachable from a separately-hosted PWA.

## Version compatibility

| Server | PWA (SIMple) | Status |
|--------|-------------|--------|
| same major | any | ✅ Compatible |
| older major | any | ❌ Outdated — update server |
| newer major | any | ⚠️ Server newer — update PWA |

The server reports its version via `GET /api/version`. The PWA checks this on
connect and compares the major version (a 1.x server is flagged as outdated by
a 2.x PWA).

## Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/version` | GET | Server version string |
| `/api/status` | GET | Card reader + card info + current selection |
| `/api/command` | POST | pySim command (equip, status, tree, etc.) |
| `/api/commands` | GET | List available pySim commands |
| `/api/cardinfo` | GET | pySim `cardinfo` output |
| `/api/tree` | POST | File tree browser for given FID/name |
| `/api/select` | POST | Select a file by name or FID |
| `/api/read` | POST | Read file content |
| `/api/write` | POST | Write raw hex data to a file |
| `/api/apdu` | POST | Raw APDU send |
| `/api/verify-adm` | POST | Verify the card's ADM PIN (from the matched card preset) |
| `/api/esim/chip` | GET | eUICC chip details (EID, EUICCInfo1/2, configured addresses) |
| `/api/esim/profiles` | GET | Installed eSIM profiles with their metadata |
| `/api/esim/notifications` | GET | Pending eSIM notifications (read-only) |
| `/api/esim/profile` | POST | Enable/disable an eSIM profile (card re-initialized after the switch) |
| `/api/help` | POST | pySim help for a given command |
| `/api/send-ota` | POST | SCP80 OTA secured packet delivery |
| `/api/ram-install` | POST | Install a Java Card `.cap` file via SCP80 (INSTALL[for load] → LOAD ×N → INSTALL[for install]) |
| `/api/sp-verify` | POST | Verify secured packet against pySim reference |
| `/api/menu` | GET | Current STK menu (title + items + active) |
| `/api/menu-select` | POST | ENVELOPE(Menu Selection) with item_id |
| `/api/menu-respond` | POST | TERMINAL RESPONSE for paused STK command |
| `/api/stk-status` | GET | STK session state (active/pending/type) |
| `/api/events` | GET | Event list from SET UP EVENT LIST |
| `/api/event-send` | POST | Send ENVELOPE(Event Download) |
| `/api/net-sim` | POST | Run a network-condition scenario (attach, service loss, roaming, churn, 2G, SMS, CB, AUTHENTICATE) |
| `/api/mcc-mnc` | GET | Search the optional MCC/MNC operator list (`?q=`; `?random=1&exclude=`) |
| `/api/proactive-log` | GET | Last 50 proactive commands |
| `/api/status-poll` | POST | Manual STATUS poll + FETCH if 91XX |
| `/api/rescue` | POST | Re-send TERMINAL PROFILE to recover CAT session |
| `/api/terminal-profile` | GET | Current TERMINAL PROFILE (hex) + CLI default |
| `/api/terminal-profile` | POST | Set and re-send the TERMINAL PROFILE at runtime (in-memory) |
| `/api/poll-status` | GET | Background STATUS polling state |
| `/api/poll-toggle` | POST | Enable/disable background polling |
| `/api/pli-qualifiers` | GET | List of qualifier codes with descriptions |
| `/api/pli-dict` | GET | Current dictionary (hex values per qualifier) |
| `/api/pli-dict` | POST | Update dictionary entries |
| `/api/scp81/bip` | POST | Start/stop the HTTP OTA listener (dump capture or PSK TLS server) |
| `/api/scp81/status` | GET | BIP terminal + listener state (channels, PSK identities, handshake identity) |
| `/api/scp81/log` | GET | HTTP OTA event log (`?after=<seq>`) |
| `/api/scp81/log-clear` | POST | Clear the HTTP OTA event log |
| `/api/scp81/queue` | POST | Replace the SCP81 command script (optionally force-restart) |
| `/api/scp81/script` | GET | Active command script + execution state and R-APDUs |
| `/api/scp81/psk-map` | POST | Replace the PSK table of a running TLS listener |
| `/api/scp81/gen-install` | POST | Generate the RAM APDU list for a `.cap` (no queueing) |

## Endpoint details

### `GET /api/version`

Returns server version for compatibility checking.

**Example response:**
```json
{"version": "2.1.2"}
```

### `GET /api/status`

Card reader, card type, current selection, and card state. Reported fields:
`reader`, `connected`, `card_present`, `card_session` (increments on every
equip/disconnect), `proactive_seq`, `equipping`, `auto_equip`, `card`,
`profile`, `iccid`, `app_ready`, `adm_verified`, `atr`, `cla_byte`,
`sel_ctrl`, `current_selection`, `channels`.

`iccid` is the E.118 digit string read from EF.ICCID (MF/2FE2) when the card is
equipped, or `null` when it is not connected / the card does not let the
terminal read it. The equip path reads it **before** the TERMINAL PROFILE, so
no CAT session is active yet; the read is best-effort and never fails an equip
(and a card removal clears it). The PWA uses it to auto-select the card preset
with the same ICCID in both SCP80 views (Secured Packet and RAM).

### `GET /api/commands`

List all available shell commands for the current card profile.

### `GET /api/cardinfo`

Runs pySim's `cardinfo` command and returns its output as `{"output": "..."}`
(card type, ATR, ICCID and other information pySim reports for the equipped
card). A shortcut for `POST /api/command` with `{"cmd": "cardinfo"}`.

### `GET /api/mcc-mnc`

Searches the optional worldwide operator list loaded from
`--mcc-mnc-list` (default `<workspace>/samples/mcc-mnc-list.json`).
`?q=<text>` matches country, country code, MCC/MNC, brand and operator
(compact results, max 50); `?random=1[&exclude=MCCMNC]` returns one random
entry (for roaming tests). When the list is not configured or missing the
response is `{"available": false}`.

### `POST /api/command`

Execute any pysim-shell command.

```json
{"cmd": "select MF"}
```

Returns:

```json
{"output": "..."}
```

### `POST /api/apdu`

Send a raw APDU to the card.

```json
{"apdu": "00A4040000..."}
```

Returns:

```json
{"response": "...", "sw": "9000"}
```

### `POST /api/verify-adm`

Verify the card's ADM PIN (TS 102 221 VERIFY, CHV number from the card
model). The key comes from the PWA's matched card preset and is never stored;
it is redacted from the request log. Short keys (4-16 hex digits) are padded
to the 8 CHV bytes with `f`, like pySim's `verify_adm`. The response is
structured so the UI can warn about the remaining attempts before retrying.

```json
{"adm": "0011223344556677"}
```

Returns on success:

```json
{"ok": true, "sw": "9000"}
```

On a wrong key (`63Cx`, x attempts left):

```json
{"ok": false, "sw": "63C2", "attempts_left": 2}
```

A blocked ADM (`6983`/`9804`) reports `{"ok": false, "sw": "9804",
"blocked": true}` and cannot be recovered without the card's unblock key.

### eSIM / LPA (local ES10 operations)

These endpoints work only when the equipped card is an eUICC (SGP.22/32);
otherwise they answer `400` with `{"error": "The equipped card is not an
eUICC"}`.  They use pySim's ES10 static API — no lpac, no SM-DP+ contact, no
profile downloads and no notification processing.  All run under the card
lock; `GET /api/status` reports `euicc` and `eid` for the PWA.

- `GET /api/esim/chip` — `{"eid": "8904…", "info1": {…}, "info2": {…},
  "addresses": {"default_dp_address": …, "root_ds_address": …},
  "errors": {"<part>": "<reason>"}}` (a part the card does not support is
  reported in `errors` instead of failing the whole request).
- `GET /api/esim/profiles` — `{"profiles": [{"iccid": "8970…",
  "isdp_aid": "A000…", "state": "enabled"|"disabled", "nickname": …,
  "provider": …, "name": …, "class": "test"|"provisioning"|"operational",
  "owner": "250-99", "icon_type": "png"|"jpg"}], "error": null}`.
- `GET /api/esim/notifications` — `{"notifications": [{"seq_number": 3,
  "operations": ["enable"], "address": "smdp.example.org",
  "iccid": "8970…"}], "error": null}`.
- `POST /api/esim/profile` — `{"action": "enable"|"disable", "iccid"?: …,
  "isdp_aid"?: …, "refresh"?: true}` (one identifier required).  The card
  usually answers with a REFRESH proactive command first (logged in
  `/api/proactive-log`); after a successful switch — or whenever a REFRESH
  was seen — the server re-initializes the card like an equip (reset,
  re-read ICCID/network state, new `card_session`) and returns
  `{"ok": true, "result": "ok", "refresh_seen": true, "reinitialized": true,
  "iccid": …, "card_session": N}`.  Failures carry the ES10c result code
  (`iccidOrAidNotFound`, `profileNotInDisabledState`,
  `profileNotInEnabledState`, `disallowedByPolicy`, `wrongProfileReenabling`,
  `catBusy`, `undefinedError`) and a short `message`.

### `POST /api/help`

Get structured help for a shell command.

```json
{"cmd": "apdu"}
```

Returns:
```json
{"usage": "apdu [-h] [--expect-sw EXPECT_SW] [--raw] APDU", "description": "...", "args": [{"name": "APDU", "type": "positional", "help": "..."}]}
```

### `POST /api/send-ota`

Send an OTA command (SCP80) to the card via SMS-PP-DOWNLOAD ENVELOPE.
The secured packet is delivered in an SMS-DELIVER TPDU wrapped in an ENVELOPE command.

**Request body:**
```json
{
  "sp": "00201516011515b00000...",
  "spi1": "16",
  "spi2": "01",
  "kic": "15",
  "kid": "15",
  "tar": "b00000",
  "cntr": "0000000001",
  "kicKey": "D6FCC023...",
  "kidKey": "1B07E7E0..."
}
```

**Response (delivery PoR):**
```json
{"success": true, "sw": "9000", "response_data": "027100000e0a...",
 "por": {"response_status": "por_ok", "tar": "B00000", "pcntr": 0,
         "decoded": {"number_of_commands": 1, "last_status_word": "6e00",
                      "last_response_data": ""}}}
```

**Response (submit PoR):** PoR is extracted from the SMS-SUBMIT TPDU
fetched via a proactive command (FETCH). The response contains the
same `por` structure if decoding succeeds.

The SPI2 `por_in_submit` bit (0x20) selects submit-mode PoR.

### `POST /api/ram-install`

Install a Java Card `.cap` file on the card via GlobalPlatform commands (INSTALL[for load] → LOAD ×N → INSTALL[for install (+ make selectable)]) wrapped in SCP80 secured packets. Each step is sent via ENVELOPE and its PoR is checked; the sequence aborts on the first PoR error. The `.cap` archive (a ZIP of nested components) is parsed server-side in `_cap_parse`; no external tooling is required.

**Request body:**
```json
{
  "cap_hex": "DECAFFED...",
  "sd_aid": "A000000003000000",
  "install_params": "C90000",
  "stk_params": "",
  "nv_quota": 0,
  "volatile_quota": 0,
  "make_selectable": true,
  "spi1": "0E", "spi2": "01",
  "kic": "15", "kid": "15",
  "tar": "000000",
  "cntr": "0000000001",
  "kicKey": "D6FCC023...",
  "kidKey": "1B07E7E0..."
}
```

| Field | Req | Description |
|---|---|---|
| `cap_hex` | yes | Even-length hex of the `.cap` file (zipped Java Card CAP), max 48 kB (98304 hex chars) |
| `sd_aid` | no | Security Domain AID for INSTALL[for load]; empty → default ISD `A000000003000000` |
| `install_params` | no | Hex C9 TLV install parameters; if empty, `gen_install_parameters()` is used with the quota/stk params |
| `stk_params` | no | Hex CA TLV (TS 102 226 §8.2.1.3.2.1) for SIM toolkit app-specific params |
| `nv_quota` / `volatile_quota` | no | Integer memory quotas (bytes) for `gen_install_parameters()` |
| `make_selectable` | no | If true (default), final INSTALL uses P1=`0C` (install + make selectable) |
| `load_block_size` | no | Bytes of load-file payload per LOAD APDU, 1–240. When empty/omitted the server auto-fits: the largest size whose SCP80 secured packet still encodes into one SMS (140 octets; e.g. 107 for the 3DES `spi1=16/spi2=01` configuration). An explicit value larger than the fitting size is clamped; over SCP80 the default 240 does **not** fit and used to fail with pySim's "Cannot encode command in a single SMS". |

**Response (success):**
```json
{"success": true, "failed_step": null,
 "steps": [{"name": "install_for_load", "apdu": "80E60200...", "por_status": "por_ok", "sw": "9000"},
           {"name": "load_0", "apdu": "80E80000...", "por_status": "por_ok", "sw": "9000"},
           {"name": "install_for_install", "apdu": "80E60C00...", "por_status": "por_ok", "sw": "9000"}],
 "final_cntr": "0000000004",
 "load_file_aid": "A000000003000000",
 "module_aid": "A000000003000000",
 "application_aid": "A000000003000000",
 "load_block_size": 107,
 "load_block_size_requested": null,
 "load_block_size_clamped": false}
```

`load_block_size` is the effective size used for the LOAD blocks,
`load_block_size_requested` echoes an explicit `load_block_size` (null =
auto-fit) and `load_block_size_clamped` is true when the requested size was
reduced to fit one SMS.

**Response (failure):**
```json
{"success": false, "failed_step": "load_1",
 "steps": [{"name": "install_for_load", "por_status": "por_ok", "sw": "9000"},
           {"name": "load_1", "por_status": "rc_error", "sw": null}],
 "error": "..."}
```

The `steps` array contains one entry per GP command. `final_cntr` is the counter value after all successful steps (use it to update the card preset). The response is not streamed — all steps run server-side before the JSON is returned.

### `POST /api/sp-verify`

Cross-check a secured packet against pySim's `OtaDialectSms.encode_cmd`
reference. Returns the JS-generated packet, pySim reference, a match flag,
and the decoded SPI fields.

```json
{"spi1": "16", "spi2": "01", "kic": "15", "kid": "15", "tar": "b00000",
 "cntr": "0000000001", "apdu": "00a40000023f00",
 "kicKey": "D6FCC023...", "kidKey": "1B07E7E0..."}
```

**Response:**
```json
{"js_sp": "...", "py_sp": "...", "match": true,
 "diffs": [], "spi": {"counter": "counter_must_be_higher", ...}}
```

### `GET /api/menu`

Returns the SIM Toolkit SETUP MENU captured from the card's TERMINAL PROFILE
response at startup. Empty `{"items": []}` if the card didn't send a menu.

**Response:**
```json
{"command_number": 1, "items": [{"id": 128, "text": "Настройки/Settings"}],
 "title": "Alfa Mobile", "active": false}
```

### `POST /api/menu-select`

Sends an `ENVELOPE(MENU SELECTION)` with the selected item ID, then handles
the card's proactive response (DISPLAY TEXT or SELECT ITEM).

```json
{"item_id": 128}
```

**Response:**
```json
{"type": "display_text", "text": "Hello", "sw": "9122"}
```
or
```json
{"type": "select_item", "items": [{"id": 1, "text": "Sub-menu"}], "sw": "9122"}
```

### `POST /api/menu-respond`

Sends `TERMINAL RESPONSE` to the current proactive command with the given result
code. Continues the proactive chain if the card responds with `91XX`. If no
response arrives within `--menu-timeout` seconds (default 60, `0` disables), the
server watchdog sends the `timeout` result itself.

```json
{"result": "ok", "item_id": 1}
```

| `result` | TERMINAL RESPONSE code | Meaning |
|---|---|---|
| `ok` | `0x00` | Command performed successfully |
| `cancel` | `0x10` | Proactive session terminated by the user |
| `back` | `0x11` | Backward move in the proactive session requested by the user |
| `timeout` | `0x12` | No response from the user |

### `GET /api/stk-status`

Returns the current STK session state.
```json
{"active": true, "pending": true, "pending_type": "select_item"}
```

### `POST /api/read`

Read file content. Auto-detects transparent vs record files.

```json
{"name": "EF.ICCID", "fid": "2FE2", "parent_path": ["MF"], "mode": "raw"}
```

Returns transparent data:
```json
{"success": true, "sw": "9000", "file_type": "transparent", "data": "...",
 "apdu_times": [{"type": "select", "ms": 12}, {"type": "read_binary", "ms": 9}]}
```

Returns records:
```json
{"success": true, "sw": "9000", "file_type": "linear_fixed",
 "records": [{"num": 1, "data": "..."}, {"num": 2, "data": "..."}],
 "apdu_times": [{"type": "select", "ms": 12},
                {"type": "read_record", "ms": 11}, {"type": "read_record", "ms": 13}]}
```

`apdu_times` reports each command's duration (command sent to response
received) classified as `select`, `read_binary` or `read_record`; the PWA uses
it for snapshot timing statistics. Other commands are not reported.

### `POST /api/write`

Write raw hex data to a file.

```json
{"name": "EF.ICCID", "fid": "2FE2", "data": "A0A1A2...", "parent_path": ["MF"]}
```

A full path may be used instead of name/fid/parent, like `/api/select` and
`/api/read`:

```json
{"path": "ADF.USIM/6F7E", "data": "4FE69DE7..."}
```

For record files:
```json
{"name": "EF.ADN", "fid": "6F3A", "data": "A0A1...", "record_nr": 1, "parent_path": ["MF", "7F10"]}
```

Returns:
```json
{"success": true, "sw": "9000"}
```

### `POST /api/select`

Select a file by name or FID, with optional parent selection.

```json
{"name": "EF.ICCID", "fid": "2FE2", "parent_path": ["MF"]}
```

`parent_path` lists the path segments from MF to the parent (ADF names or
FIDs); the legacy single-segment `parent_sel` is still accepted but is only
unambiguous for ADFs. Resolution is strictly parent-scoped: model-known files
are selected through the requested parent only (pySim `select_file()`), never
via pySim's global selectables or its `probe_file()` model injection, so a
same-FID file under another parent is never picked and the filesystem model
is not modified. `allow_probe: true` (PWA custom files) additionally allows a
model-unknown 4-hex FID to be selected directly; any temporary model object
created for it is detached again before the response is sent.

Returns:
```json
{"name": "EF.ICCID", "fid": "2FE2", "file_type": "transparent",
 "file_size": 10, "record_len": null, "num_of_rec": null,
 "fci_hex": "621082024021...",
 "apdu_times": [{"type": "select", "ms": 12}], "exists": true}
```

`fci_hex` is the raw FCP template (`'62'`) from the SELECT response, used by
the PWA's Exact FCI checks; `file_size` / `record_len` / `num_of_rec` drive
the profiler's size and record checks. When the file does not exist the
endpoint responds `404` with `{"error": "...", "exists": false}`.

### `POST /api/tree`

Get directory listing with typed children.

```json
{"name": "MF", "fid": "3F00"}
```

Use `parent_path` (or the legacy `parent_sel`) to list a subdirectory, e.g.
`{"name": "DF.GSM-ACCESS", "fid": "5F3B", "parent_path": ["MF", "ADF.USIM"]}`.

Returns:
```json
{"exists": true, "name": "MF", "fid": "3F00", "file_type": "df", "children": [{"name": "EF.ICCID", "fid": "2fe2", "isDir": false}]}
```

### `GET /api/events`

Returns the event list captured from the card's SET UP EVENT LIST (an array of
event byte values, or `[]` when none was received).

### `POST /api/event-send`

Sends an `ENVELOPE(Event Download)` for a subscribed event.

### `POST /api/net-sim`

Runs one network-condition scenario from `projects/UICC_NAA.md` section 13
against the equipped card and returns the step log:

```json
{"scenario": "service_lost", "mcc": "262", "mnc": "01", "lac": "6CD7",
 "send_event": true, "dummy_locations": true, "keep_kasme": true}
```

Scenarios: `cold_boot`, `attach_eps`, `attach_2g`, `service_lost`,
`limited_service`, `roaming_denied`, `churn`, `sms_received`, `cb_reconfig`,
`authenticate`. Optional parameters: `mcc`/`mnc` (or `plmn`), `lac`,
`cell_id`, `tac`, `rac`, `tmsi`, `ptmsi`, `ptmsi_sig`, `guti`, `ksi`,
`kasme`, `ul`, `dl`, `algo`, `kc`, `rand`, `autn`, `churn_count`,
`churn_delay_ms`, and the toggles `send_event`, `dummy_locations`,
`invalidate_epsnsc`, `keep_kasme`, `write_kc`, `sms_location`, `cb_clear`
(empty identity values are randomized). The event step is skipped when the
card did not subscribe to Location status; only UPDATE BINARY/RECORD,
ENVELOPE and AUTHENTICATE are sent (never FPLMN/5GS location files). The
response is `{success, error, steps:[{action, file, path, data, sw, ok}]}`.

```json
{"event_type": 4, "event_data": "01A0"}
```

`event_type` is required (the SET UP EVENT LIST event byte); `event_data` is
optional hex for events that carry data. Returns the SW and any response data:

```json
{"sw": "9000", "data": "..."}
```

Channel status (event `0x0A`, TS 102 223 §8.56) carries the Channel status TLV
`B8 02 <status> <info>`, where the status byte is the channel id (1–7) OR-ed
with the state bits (0x00 link not established / 0x40 TCP LISTEN / 0x80 link
established) and the info byte is `00` (no further info) or `05` (link
dropped). The server also sends this event automatically when a BIP link drops
outside a proactive command and the card subscribed to `0x0A`.

### `GET /api/proactive-log`

Returns the last 50 proactive commands fetched during CAT sessions, newest
first:

```json
[{"type_hex": "25", "type_name": "SET UP MENU", "elapsed": 3.2, "bytes": 97}]
```

### `POST /api/status-poll`

Manually sends `STATUS` (F2) and, if the card answers `91XX`, runs the
proactive chain (FETCH → TERMINAL RESPONSE) until it settles. Returns:

```json
{"sw": "9000", "proactive": true}
```

### `POST /api/rescue`

Recovers a stuck CAT session by clearing the pending state and re-sending the
TERMINAL PROFILE. Returns whether a menu and event list were captured again
(plus the profile used):

```json
{"ok": true, "profile": "FFFF...", "menu": true, "events": [4, 5]}
```

### `GET /api/terminal-profile`

The TERMINAL PROFILE currently in effect and the CLI default (for reference;
runtime changes are in-memory only):

```json
{"profile": "FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003",
 "bytes": 33,
 "cli_default": "FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003"}
```

### `POST /api/terminal-profile`

Sets the TERMINAL PROFILE at runtime (in-memory) and re-sends it to the card,
resetting the STK session state exactly like `/api/rescue`. Body with a new
profile, or `{}` to re-send the current one:

```json
{"profile": "FFFFFFFF7F1F007FFF00001F230811060700"}
```

Hex, even number of digits, 1–255 bytes. Response is the same shape as
`/api/rescue` (with `ok: true`); invalid hex is a 400, no reader a 503.

### `GET /api/poll-status`

Background STATUS polling state.

```json
{"enabled": true, "interval": 300}
```

### `POST /api/poll-toggle`

Turns background STATUS polling on or off.

```json
{"enabled": true}
```

Returns the new state (`{"enabled": ..., "interval": ...}`).

### `GET /api/pli-qualifiers`

Lists the PROVIDE LOCAL INFORMATION qualifier codes with their names.

```json
[{"code": "00", "name": "Location Information"}, {"code": "0A", "name": "Battery Charge Level"}]
```

### `GET /api/pli-dict`

Returns the current PLI data dictionary as a qualifier-code map.

```json
{"00": "0291...", "0A": "64"}
```

### `POST /api/pli-dict`

Updates dictionary entries. Body is a map of qualifier code to hex value; keys
must be known qualifiers and values valid hex, otherwise they are ignored.
Returns the updated dictionary.

### `POST /api/scp81/bip`

Starts or stops the local target the card's BIP channel is redirected to.

Dump mode captures whatever the card sends (e.g. its TLS ClientHello)
without answering:

```json
{"action": "start", "mode": "dump", "host": "127.0.0.1", "port": 8443}
```

Redirect mode (`mode: "redirect"`) starts **no local listener**: every BIP
channel the card opens is connected to the configured target (`host`/`port`
are required — no defaults), which terminates TLS and runs the administration
dialog; the address the card requests is only logged. The status API reports
`mode: "redirect"` with the target while it runs. (This is the behavior that
was called `passthru` before 2.2.13 — the name is now taken by the mode
below.)

```json
{"action": "start", "mode": "redirect", "host": "203.0.113.10", "port": 10174}
```

Pass-through mode (`mode: "passthru"`) starts **no listener and has no
target**: every BIP channel dials the destination the card requests in OPEN
CHANNEL — the `Other address` (`3E`/`BE`) plus the `Transport level`
(`3C`/`BC`) port, TCP client remote (`02`) only. The specs define no default
port, so an incomplete or non-TCP request fails the channel with result `3A`
and an `open-fail` log reason; `host`/`port` in the request are ignored. The
status API reports `{"mode": "passthru"}` and the per-channel targets appear
in `bip.channels`.

```json
{"action": "start", "mode": "passthru"}
```

TLS mode runs the Phase B PSK TLS server (GPC v2.2 Amendment B): the PSK
table is applied to the TLS handshake, and the GP HTTP administration dialog
(`X-Admin-*` headers, 200 with a command string or 204 No Content) is served.
`psk_map` is the lookup table for the identity the card presents in the TLS
handshake — the PWA sends it from the card presets (`{identity, psk_hex}`
objects or an `{identity: psk_hex}` map); a handshake whose identity is not
listed fails with the log entry `tls-psk-unknown`. The legacy single-key form
`psk_hex` (with optional `psk_identity`, empty = accept any identity) is still
accepted; when both are omitted the table of the previous start is reused.
Keys are never stored or logged.

```json
{"action": "start", "mode": "tls", "host": "127.0.0.1", "port": 8443,
 "psk_map": [{"identity": "89012345678901234567",
              "psk_hex": "00112233445566778899aabbccddeeff"}],
 "script": ["80CAFF2100", "80F28002024F0000"], "script_kind": "Explore"}
```

`script` is the APDU list served to the card (an explicit list, or `none`);
the server is agnostic to what the APDUs do. `script_kind` is an optional
label for the logs/results. Omitting `script` keeps the configured script and
its run progress.

Stop either mode with `{"action": "stop"}` (also disables the BIP terminal).

### `GET /api/scp81/status`

```json
{"bip": {"enabled": true, "mode": "redirect", "target": "127.0.0.1:8443", "channels": [], "seq": 12},
 "listener": {"mode": "tls", "host": "127.0.0.1", "port": 8443,
              "psk_identities": ["89012345678901234567"], "psk_wildcard": false,
              "identity_seen": "89012345678901234567", "identity_matched": true,
              "version_seen": "TLSv1.2", "cipher_seen": "PSK-AES128-CBC-SHA256"}}
```

Listener modes: `tls` (local PSK TLS server), `dump` (capture-only TCP
listener), `redirect` (no local listener; the BIP channels go straight to the
configured `host:port`, e.g. an external HTTP OTA platform — reported as
`{"mode": "redirect", "host": ..., "port": ..., "target": "host:port"}`) and
`passthru` (no listener and no target; each channel dials the destination the
card requests in OPEN CHANNEL — reported as `{"mode": "passthru"}`, with the
actual peer in `bip.channels[].target`).

`psk_identities` lists the identities the listener accepts (keys are never
exposed); `psk_wildcard` marks the legacy single-key mode. `identity_seen` /
`identity_matched` reflect the last handshake: an unknown identity is logged
as `tls-psk-unknown` and the handshake fails.

### `POST /api/scp81/psk-map`

Replaces the PSK table of the running TLS listener (the PWA pushes card-preset
edits without a listener restart):

```json
{"psk_map": [{"identity": "89012345678901234567",
              "psk_hex": "00112233445566778899aabbccddeeff"}]}
```

Returns `{"ok": true, "identities": [...], "listener": {...}}`; entries
without an identity or a valid key are skipped, and an empty table is
rejected.

### `GET /api/scp81/log`

Returns the BIP/TLS event log (open/close, SEND/RECEIVE DATA hex, TLS
handshake and HTTP request/response records). `?after=<seq>` returns only
newer entries; `seq` echoes the latest sequence number.

### `POST /api/scp81/queue`

Replace the SCP81 command script (used by the Remote APDU tab's RAM chain
"Queue in SCP81" and the PWA's "Restart script"). Body
`{"apdus": ["80E60C002E...", ...]}` (or a single `apdu`), optional `kind` and
`force`. Entries that already are Command Scripting templates
(`AA...`/`AE80...`, the expanded format) are sent verbatim instead of being
wrapped again. Refused while a script is mid-run unless forced; queuing resets
the execution progress.

### `POST /api/scp81/gen-install`

Generate the RAM (GP) APDU sequence for a `.cap` without touching the listener
or the running script; the PWA's "Install from .cap" script template stores
the returned list. The `.cap` is parsed server-side (same parser as
`/api/ram-install`) and expanded to INSTALL [for load] -> LOAD blocks
(240-byte payloads) -> INSTALL [for install]; the file itself is never stored.

```json
{"cap_hex": "504B0304...", "sd_aid": "A000000003000000", "privileges": "00",
 "install_params": "", "stk_params": "", "make_selectable": true}
```

`sd_aid` empty = the ISD. Responds with `{"ok": true, "apdus": [...],
"load_file_aid": ..., "module_aid": ...}`.

### `GET /api/scp81/script`

Returns the configured command script and the execution state:

```json
{"script": ["80CAFF2100", "80F28002024F0000"], "next": 2, "total": 2,
 "done": [0, 1], "kind": "Explore",
 "pending": {"index": 17, "pos": null, "page": true, "apdu": "80F28003024F0000"},
 "pages": 11, "pages_queued": 0, "complete": false,
 "results": [{"index": 1, "pos": 0, "page": false, "sw": "9000",
              "apdu": "80CAFF2100", "rapdu": "FF210C810102..."}]}
```

`next` is the index of the next script APDU to send; `done` lists the script
indices the card reported. `pending` describes the C-APDU awaiting the card's
`X-Admin-Script-Status` report as `{index, pos, page, apdu}` (`pos` = script
index, `null` for an auto continuation page) or `null`; `pages` counts the
continuation pages queued so far and `pages_queued` those not yet sent.
`complete` is true when every configured APDU was reported and nothing is in
flight — a script can therefore be complete while a listing page is still
being fetched (`pending.page` = true), which is tracked separately from the
script's own progress. `results` entries carry the send order (`index`), the
script position (`pos`, `null` for continuation pages) and the `page` flag.

Execution tracking and resume: an APDU counts as executed only when the card
reports it in the next POST's Response Scripting template. A POST with
`X-Admin-Resume` continues with the unexecuted tail (the pending APDU is
resent if its report never arrived), a POST without it is a fresh dialog where
the script runs from the start, and a completed script closes the session with
204.

Each APDU is
delivered in an `AE 80 22 <len> <apdu> 00 00` Command Scripting template
(TS 102 226 §5.2.1) with `X-Admin-Next-URI`; the card returns its R-APDUs in
the next POST's Response Scripting template, which is parsed and logged
(`script-rapdu`, `script-memory`). Long GET STATUS listings that answer
`63 10` / `CA FE` ("more data available") are auto-continued with the same
command carrying P2.b1=1.

TLS mode also accepts `chunked` (**default `true`** — the reference server's
chunked framing; the card rejects a chunked response that also carries a
Content-Length) and `chunk_size` (default `0` — the whole response in one TLS
record, as in the decrypted reference session; a positive value writes the
head and each body piece as its own record). Both are echoed by
`GET /api/scp81/status`.

The connection stays open between POSTs for the whole dialog (the card is the
HTTP client and may reuse it or dial a new one at will — GP Am. B 4.3.1 leaves
connection management to the SD); only the 204 ends the session, and the server
then shuts the TLS session down cleanly (`close_notify` while the response is
still buffered, then FIN). There is no option to close after every response.

`compact_headers` (default `false`) omits the optional space after each header
colon — legal per RFC 7230 3.2 (OWS), though a single SP is preferred, and it
saves one byte per header (useful to keep a response within one card-sized TLS
record); `conn_header` (default `'none'` = omit the header; `'keep-alive'` adds
it) declares the connection fate, `tls_version` (default `'auto'` — accept TLS
1.0-1.2 and let OpenSSL pick the highest the card offers; `'1.0'`/`'1.1'`/`'1.2'`
pin a version for debugging) selects the protocol window, `cipher` pins one
suite, `next_uri`
overrides the per-command `X-Admin-Next-URI` (`%d` = command id; empty string
omits the header), `link_events` (default `true`, all modes) controls the
automatic Channel status events, `answer_delay` waits before answering a
request. `keylog` writes the TLS traffic secrets to
the given file (SSLKEYLOGFILE format) for debugging captures — it contains key
material, use a temporary path.

The response headers are minimal: `X-Admin-Protocol`, the optional
`X-Admin-Next-URI`/`X-Admin-Targeted-Application`, `Content-Type` on 200s and
`Transfer-Encoding`/`Content-Length` per `chunked`. The Date/Server/X-Powered-By
mimicry (`apache_headers`) was removed in 2.2.14 — the reference server's extra
headers were an unnecessary copy (the real blocker was the BIP TERMINAL
RESPONSE BER length) and the card accepts the minimal set.

`GET /api/scp81/status` echoes the negotiated handshake as `version_seen` /
`cipher_seen` (plus `identity_seen`); a handshake that fails for TLS/cipher
reasons (no shared cipher, unsupported protocol version, card alert) is logged
as `tls-handshake-failed` with the OpenSSL reason, separately from
post-handshake record errors (`tls-error`).

The PWA's Listener **Options** block (collapsed by default; a `custom` marker
appears when anything differs from the reference defaults) exposes `chunked`,
`chunk_size`, `conn_header`, `compact_headers`, `next_uri` (checkbox + template),
`script_template`, `cr_tag`, `targeted_app` (checkbox + `//aid/...` field) and
`link_events` (applied at Start, persisted in `localStorage`); `tls_version`,
`cipher`, `keylog` and `answer_delay` stay API-only.
