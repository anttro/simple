# simple-server — HTTP API reference

The pysim-free server (`simple_server`, the console script `simple-server`,
version **4.0.0**) behind the SIMple PWA.  It talks to the card through its
own PC/SC transport, file model and command layer; **there is no pySim**
anywhere in the request path, so pySim's interpreter and packet cross-check
are not part of the API (see *Deviations from the 3.x server* at the end).
The PWA decodes files client-side from the raw bytes these endpoints return.

The server exposes a JSON HTTP API under `/api/*`.  All API responses carry
`Access-Control-Allow-Origin: *` (plus `Access-Control-Allow-Private-Network:
true` on the preflight), so the API is reachable from a separately-hosted PWA.
The PWA files themselves are served from `--web-dir` (default `<repo>/frontend`)
on the same origin: `GET /` is `index.html`, an unknown non-`/api/` path is a
`404` (there is no SPA rewrite), and a static file answers `304` through
`Last-Modified`.

## Version compatibility

| Server | PWA (SIMple) | Status |
|--------|--------------|--------|
| same major | any | ✅ Compatible |
| older major | any | ❌ Outdated — update server |
| newer major | any | ⚠️ Server newer — update PWA |

`GET /api/version` serves the server's own version.  The PWA reads it on
connect and compares the major version.  4.0.0 is a backend cutover: a 3.x
install cannot be updated in place (`tools/update.py` refuses the jump), so
the 3.x/4.x pair is a fresh install, not a version mismatch to patch around.

## Conventions

**Request bodies.**  A `POST` body must be a JSON **object**; a non-object is
`400 {"error": "the request body must be a JSON object"}` and unparseable JSON
is `400 {"error": "invalid JSON body: …"}`.  Every field documented below is a
top-level key of that object.

**The card lock.**  Every `/api/` request is serialized by one re-entrant card
lock, because one card and one CAT session exist per server.  Two allowlists
answer without it, so a long card operation cannot starve the UI:

- the **card-free GETs** (pure cached state): `/api/version`, `/api/status`,
  `/api/readers`, `/api/arr`, `/api/pbr`, `/api/presets`,
  `/api/ram-batch/status`, `/api/test/status`, `/api/test/scripts`,
  `/api/test/suites`, `/api/menu`, `/api/stk-status`, `/api/events`,
  `/api/poll-status`, `/api/proactive-log`, `/api/terminal-profile`,
  `/api/pli-qualifiers`, `/api/pli-dict`, `/api/net-state`, `/api/mcc-mnc`,
  `/api/scp81/status`, `/api/bip/status`, `/api/scp81/script`,
  `/api/scp81/log` and `/api/bip/log`;
- the **card-free POSTs** (flag/store writes that touch no card):
  `/api/cap-info`, `/api/ram-batch/stop`, `/api/test/stop`, `/api/test/clear`,
  the test-script and test-suite store mutations (`/api/test/scripts*`,
  `/api/test/suites*`) and the two log clears (`/api/scp81/log-clear`,
  `/api/bip/log-clear`).

Everything else waits for the lock.  The test scripts and test suites
therefore stay editable while a RAM install or a test run holds the card; a
preset write queues behind it (it needs no card, but it is not on the POST
allowlist).

**Test-run guard.**  While a test run owns the card, the card-exchange endpoints
answer `409 {"error": "test script running - card commands are blocked until it
finishes"}` before they are dispatched: `/api/tree`, `/api/select`, `/api/read`,
`/api/write`, `/api/apdu`, `/api/verify-adm`, `/api/send-ota`, the RAM/CAP
operations (`/api/ram-install`, `/api/ram-install-app`, `/api/cap-compat`,
`/api/tar-probe`, `/api/counter-probe`), the menu/event/profile/poll writes
(`/api/menu-select`, `/api/menu-respond`, `/api/event-send`, `POST
/api/terminal-profile`, `/api/rescue`, `/api/status-poll`, `/api/poll-toggle`),
the eSIM endpoints, `/api/net-sim`, `/api/net-state-refresh` and the SCP81/BIP
control (`/api/scp81/bip`, `/api/scp81/queue`, `/api/scp81/psk-map`,
`/api/scp81/gen-install`, `/api/bip/control`) and log clears.  The card-free
GETs above stay available (that is how the run's own progress is polled), as do
the card-free store POSTs.  `/api/equip`, `/api/reset` and `/api/reader` are not
part of the guard: they queue behind the card lock, and a reader switch while a
run is active is refused by the switch itself (`409`).

**Status codes.**

| Situation | Status | Body |
|---|---|---|
| success | `200` | the endpoint's own shape |
| a request value the endpoint refuses (`ValueError`) | `400` | `{"error": "…"}` |
| a refused preset/script/suite store mutation | `400` | `{"error": "…"}` (the store's message) |
| a refused cross-store move/copy | `400`/`404` | `{"error": "…"}` |
| no card session (equip a card first) | `503` | the error plus the `/api/status` body |
| a transport failure (the link is torn down and the auto-equip re-armed) | `503` | the error plus the `/api/status` body |
| the file does not exist / does not resolve | `404` | `{"success": false, "exists": false, "error": …}` |
| a run/batch/reader switch is busy | `409` | `{"error": "…"}` |
| an unknown `/api/` path | `404` | `{"error": "Not found"}` |
| an unexpected server error | `500` | `{"error": …, "success": false, "ok": false}` |

A refused **card** endpoint keeps the running session: only a transport-level
failure (a pyscard error, a dead link) tears it down and re-arms the
auto-equip, never a decoder/protocol error.

## Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/version` | GET | Server version string |
| `/api/status` | GET | Reader, card, session, poll and operation state |
| `/api/readers` | GET | PC/SC reader names + the active one |
| `/api/reader` | POST | Switch the active reader (rebuilds the card session) |
| `/api/equip` | POST | Initialize (or re-initialize) the current card |
| `/api/reset` | POST | Reset and fully re-initialize the card |
| `/api/tree` | POST | File tree browser for a path/FID/name (the DF holding EF_PBR gains the PBR-referenced files) |
| `/api/select` | POST | Select a file and report its FCP metadata |
| `/api/read` | POST | Read a file's bytes or records (`mode: "raw"` only) |
| `/api/write` | POST | Write raw hex to a file (`UPDATE BINARY`/`UPDATE RECORD`) |
| `/api/arr` | GET | Cached EF.ARR copies (the FCP `'8B'` references) |
| `/api/pbr` | GET | Cached EF.PBR phonebook reference records |
| `/api/apdu` | POST | Raw APDU send (automatic `91XX` chain) |
| `/api/verify-adm` | POST | Verify the card's ADM PIN |
| `/api/presets` | GET | Card preset store: all presets + the store path |
| `/api/presets` | POST | Create a card preset |
| `/api/presets/update` | POST | Update a preset (`{id, fields}`; the counter is written as given) |
| `/api/presets/delete` | POST | Delete a preset (`{id}`) |
| `/api/presets/import` | POST | Import presets (`{presets: [...], mode: merge\|replace}`) |
| `/api/presets/import-outfile` | POST | Import a card vendor's **outfile** (`{text, name?, algorithm16?, tars?, tar_keyset?, start_counter?, mode?}`; without `mode` a dry-run preview) |
| `/api/counter-probe` | POST | Bounded counter sync: probe increasing counter values until the card accepts one, then persist `accepted+1` |
| `/api/tar-probe` | POST | Probe the preset's TAR list (one secured packet per TAR, TS 101 220 Annex D) |
| `/api/send-ota` | POST | SCP80 secured-packet delivery (`sp` pre-built or `apdu` built server-side) |
| `/api/cap-info` | POST | Validate a `.cap` and estimate its code/NVRAM/RAM requirements (read-only) |
| `/api/ram-install` | POST | Install a Java Card `.cap` over SCP80 (`INSTALL [for load]` → `LOAD` ×N → `INSTALL [for install]`), optional `delete_package` pre-step |
| `/api/ram-install-app` | POST | `INSTALL [for install]` (deletes the instance first) / `INSTALL [for make selectable]` for an already loaded package |
| `/api/cap-compat` | POST | CAP compatibility test: LOAD only to the block completing the Import component (nothing committed) |
| `/api/ram-batch/start` | POST | Start a batch operation (`install-cap` / `reinstall-app` / `delete`) across equipped cards |
| `/api/ram-batch/status` | GET | Batch state: phase, current card, counts and attempts (card-free) |
| `/api/ram-batch/stop` | POST | Stop the batch (a running install finishes first; card-free) |
| `/api/ram-batch/clear` | POST | Drop the finished batch report (`409` while active) |
| `/api/test/scripts` | GET | Test script store: all scripts + the store path |
| `/api/test/scripts` | POST | Create a test script (`{name, steps, suite_id, require_adm?}` or `{script}`) |
| `/api/test/scripts/update` | POST | Update a script (`{id, script}`; a changed `suite_id` is a move) |
| `/api/test/scripts/delete` | POST | Delete a script (`{id}`; detaches it from its suite) |
| `/api/test/scripts/move` | POST | Move a script to another suite (`{id, suite_id}`) |
| `/api/test/scripts/copy` | POST | Copy a script into another suite (`{id, suite_id, name?}`) |
| `/api/test/scripts/import` | POST | Import scripts (`{scripts: [...], mode, suite_id?}`) |
| `/api/test/suites` | GET | Test suite store: all suites + the store path |
| `/api/test/suites` | POST | Create a suite (`{name, require_adm?, start_reset?, scripts?}` or `{suite}`) |
| `/api/test/suites/update` | POST | Update a suite (`{id, suite}`; the member set changes through the script endpoints) |
| `/api/test/suites/delete` | POST | Delete a suite (`{id}`; refused while it holds scripts) |
| `/api/test/suites/import` | POST | Import a suite bundle (`{suites, scripts, mode}`; script references remapped) |
| `/api/test/run` | POST | Start a script (`script`/`script_id`) or a suite (`suite_id`) |
| `/api/test/status` | GET | Run state, per-step results and the suite report |
| `/api/test/stop` | POST | Request a stop (the current step finishes first) |
| `/api/test/clear` | POST | Clear the finished run report (`409` while running) |
| `/api/menu` | GET | Cached STK menu (title + items + active) |
| `/api/menu-select` | POST | ENVELOPE(Menu Selection) by item id or text |
| `/api/menu-respond` | POST | TERMINAL RESPONSE for the paused STK command |
| `/api/stk-status` | GET | STK dialogue state + the paused command |
| `/api/events` | GET | Event list from SET UP EVENT LIST |
| `/api/event-send` | POST | ENVELOPE(Event Download) with raw or semantic data |
| `/api/terminal-profile` | GET | Current TERMINAL PROFILE + the startup default |
| `/api/terminal-profile` | POST | Set and re-send the TERMINAL PROFILE (in memory) |
| `/api/rescue` | POST | Re-send the current profile to recover a stuck CAT session |
| `/api/status-poll` | POST | Manual STATUS poll + the chain on `91XX` |
| `/api/poll-status` | GET | Background STATUS poll state |
| `/api/poll-toggle` | POST | Enable/disable background polling |
| `/api/proactive-log` | GET | Last 50 proactive commands (newest first) |
| `/api/pli-qualifiers` | GET | PROVIDE LOCAL INFORMATION qualifier codes + names |
| `/api/pli-dict` | GET | Effective PLI data dictionary |
| `/api/pli-dict` | POST | Update the PLI dictionary overrides |
| `/api/esim/chip` | GET | eUICC chip details (EID, EUICCInfo1/2, addresses, RAT) |
| `/api/esim/profiles` | GET | Installed eSIM profiles with their metadata |
| `/api/esim/notifications` | GET | Pending eSIM notifications (read-only) |
| `/api/esim/profile` | POST | Enable/disable an eSIM profile (the card is re-initialized) |
| `/api/net-sim` | POST | Run a network-condition scenario (attach, service loss, roaming, churn, 2G, SMS, CB, AUTHENTICATE) |
| `/api/net-state` | GET | Cached network-state monitor |
| `/api/net-state-refresh` | POST | Re-read the monitored files and return the monitor state |
| `/api/mcc-mnc` | GET | Search the bundled MCC/MNC operator list (`?q=`; `?random=1&exclude=`) |
| `/api/scp81/bip` | POST | Start/stop the SCP81 listener (`dump`/`tls`/`redirect`/`passthru`) |
| `/api/bip/control` | POST | Start/stop the generic BIP session (`sink`/`redirect`/`passthru`) |
| `/api/scp81/status` | GET | BIP session + listener state (shared with `/api/bip/status`) |
| `/api/bip/status` | GET | Same shape as `/api/scp81/status` |
| `/api/scp81/log` | GET | BIP/TLS event log (`?after=<seq>`) |
| `/api/bip/log` | GET | Same log as `/api/scp81/log` |
| `/api/scp81/log-clear` | POST | Clear the BIP/TLS event log |
| `/api/bip/log-clear` | POST | Same as `/api/scp81/log-clear` |
| `/api/scp81/script` | GET | Configured command script + execution state and R-APDUs |
| `/api/scp81/queue` | POST | Replace the command script (optionally force-restart) |
| `/api/scp81/psk-map` | POST | Replace the PSK table of a running TLS listener |
| `/api/scp81/gen-install` | POST | Generate the RAM APDU list for a `.cap` (nothing queued) |

## Endpoint details

### `GET /api/version`

Returns the server version for compatibility checking.

```json
{"version": "4.0.0"}
```

### `GET /api/status`

One card-free body with the whole server/card state (it never enters the card
exchange, so a long operation's live progress stays visible).

```json
{"version": "4.0.0",
 "app_ready": true,
 "connected": true,
 "card_present": true,
 "equipping": false,
 "card_session": 3,
 "card_ready_session": 3,
 "reader": "ACS ACR39U ICC Reader 0",
 "readers": ["ACS ACR39U ICC Reader 0"],
 "atr": "3B9F...",
 "card": {"kind": "uicc", "name": "UICC", "cla": "80", "aids": ["…"]},
 "iccid": "8970119000004600098",
 "euicc": false,
 "eid": null,
 "selection": {"path": "ADF.USIM/6F07", "name": "EF.IMSI", "fid": "6F07",
               "kind": "ef", "size": 9, "record_len": null,
               "num_records": null},
 "adm_verified": false,
 "proactive_mode": "polling",
 "poll": {"enabled": true, "suspended": false, "interval": 30},
 "test_run": {"running": false},
 "bip": {"owner": null, "bip": {...}, "listener": null},
 "scp80": null,
 "ram_progress": {"active": false, "kind": "", "step": 0, "total": 0,
                  "name": "", "elapsed": 0},
 "ram_batch": null,
 "terminal_profile": "FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003"}
```

| Field | Meaning |
|---|---|
| `version` | the server version (`4.0.0`) |
| `app_ready` | the API is serving (`true`; a startup failure stops the process instead) |
| `connected` | a card session is live (a card has been initialized) |
| `card_present` | the reader reports a card (independent of initialization) |
| `equipping` | an equip/reset/reader switch is in flight |
| `card_session` | bumps on **every** session change (equip success *and* failure, disconnect, reader switch) |
| `card_ready_session` | the session of the last *completed* post-equip sequence; equal to `card_session` once the init finished, smaller mid-equip and after a `--no-card-init` start |
| `reader` | the active reader name (`null` when none was chosen) |
| `readers` | the current PC/SC reader list (cached for 2 s) |
| `atr` | the card's ATR as hex, `null` before a connect |
| `card` | `null` or `{kind, name, cla, aids}`: our own card class (`uicc`/`sim`/`euicc`/`unknown`), its human name, its CAT CLA (`80` UICC / `a0` 2G SIM) and the AIDs the detection found |
| `iccid` | the E.118 digit string from EF.ICCID (MF/2FE2), `null` when not connected or unreadable |
| `euicc` / `eid` | whether the card is an eUICC and its EID (best-effort ES10 read at equip) |
| `selection` | `null` or the last file a file endpoint addressed: `{path, name, fid, kind, size, record_len, num_records}` - a publication, not a live mirror of the card's selected file |
| `adm_verified` | a VERIFY succeeded in the current card session (the latch the test runner's ADM gate reads); an equip/disconnect clears it |
| `proactive_mode` | `test` (a run owns the card) > `paused` (a FETCHed command awaits its TR) > `chain` (a FETCH/TR chain runs) > `bip` (a BIP session is active) > `polling` (idle, poll on) > `idle` |
| `poll` | `{enabled, suspended, interval}` - the operator's switch, the card's POLLING OFF state and the adopted interval (seconds) |
| `test_run` | `{running}` - the full run report is `GET /api/test/status` |
| `bip` | `null` or the BIP session block (`{owner, bip, listener}` - see `/api/scp81/status`) |
| `scp80` | **reserved**: `null` in 4.0.0 (the per-preset counter state lives in the preset store and is reported by the operations) |
| `ram_progress` | the RAM operation in flight: `{active, kind, step, total, name, elapsed}` (the install chains hold the card lock, so this poll is how the steps stay visible) |
| `ram_batch` | `null` or the batch headline (the detailed report is `GET /api/ram-batch/status`) |
| `terminal_profile` | the profile currently in effect (hex) |

The `iccid` is read **before** the TERMINAL PROFILE, so no CAT session is
active during the read; a removal clears it.  The PWA uses it to auto-select
the card preset with the same ICCID.

### `GET /api/readers`

```json
{"readers": ["ACS ACR39U ICC Reader 0", "Virtual reader"], "active": "ACS ACR39U ICC Reader 0"}
```

`readers` is `[]` when pyscard or pcscd is unavailable (the server still
starts and answers).  Card-free.

### `POST /api/reader`

Switch the active reader.  Each reader has its own card, so the switch
rebuilds the transport for the named reader, resets and re-initializes its
card, bumps `card_session` and clears the previous selection/CAT
session/caches.

```json
{"name": "Virtual reader"}
```

The response is the new `/api/status` body.  Errors:

- `400` when `name` is missing or not in `/api/readers` (the message lists the
  available readers) - the active reader is unchanged;
- `409` while a test run or a BIP/SCP81 session owns the terminal (the guard
  keys on the persistent BIP session owner, so a cardless listener counts);
- `503` when the switch itself fails (no card in the new reader, a PC/SC
  error): the reader **stays selected** for the next equip and the body is the
  error plus the status (the `card_session` may already have advanced).

### `POST /api/equip`

Initialize (or re-initialize) the card in the active reader: connect/reset,
detect the card, read EF.ICCID, the EF.ARR/EF.PBR caches and the EID, then
send the TERMINAL PROFILE and drain its proactive answers.  Body: `{}`.

The response is the resulting `/api/status` body (`connected: true`,
`card_ready_session == card_session`).  A failure answers `503` with the error
and the status body, leaves no half-initialized session (`card_session` has
advanced) and marks the PC/SC link stale, so the next attempt rebuilds it.

### `POST /api/reset`

A power cycle plus a full re-initialization.  The card detection already
resets/connects, so this is exactly `/api/equip` with an explicit reset
semantic: one power cycle per request, one `card_session` bump.  Body: `{}`;
the response and the errors are those of `/api/equip`.

### File endpoints: `POST /api/tree`, `/api/select`, `/api/read`, `/api/write`

All four address a file the same two ways:

- `path` - the whole chain from MF, e.g. `"MF/2FE2"` or
  `"ADF.USIM/6F07"` (`3F00` is normalised to `MF`);
- `name`/`fid` plus the parent chain `parent_path` (`["MF", "7F10"]`, or the
  legacy single-segment `parent_sel`, which is unambiguous only for ADFs and
  a model-unique DF FID - a FID that exists in several DFs answers `404`
  "ambiguous" rather than guessing).  The PWA sends `fid` and
  `parent_path`; when both `fid` and `name` are present the FID is
  authoritative.

Resolution is strictly parent-scoped through the server's file model: a
same-FID file under another DF is never picked, and the model is never
mutated.  A model-unknown 4-hex FID under a model-known DF (the PWA's custom
files, and the EF.PBR phonebook files) is **always** selectable - there is no
`allow_probe` flag any more (the legacy flag existed to protect pySim's model
from the probe; this model has no probe side effect, so the flag is ignored).

A selector that resolves to nothing is a `404` with
`{"success": false, "exists": false, "error": …}`; a request-value error is a
`400`; no card session is a `503` with the status body.  A transport error
tears the link down and re-arms the auto-equip (`503`); a decoder/protocol
error is a `500` that **keeps** the session.

#### `POST /api/tree`

Lists a node's children from the model (the model lists what the standard
file list describes, not what the card holds - an absent file is listed and
answers `exists: false` when selected).

```json
{"name": "MF", "fid": "3F00"}
```

```json
{"exists": true, "name": "MF", "fid": "3F00", "file_type": "df",
 "children": [{"name": "EF.ICCID", "fid": "2FE2", "isDir": false},
              {"name": "DF.TELECOM", "fid": "7F10", "isDir": true},
              {"name": "ADF.USIM", "fid": null, "isDir": true,
               "aid": "A0000000871002"}]}
```

A child is `{name, fid, isDir}` plus, when it applies, `aid` (an ADF; an ADF
is a model root but appears as an MF child carrying its AID so the Profiler's
walk can root it) and `custom: true` (a user-defined custom file).  A
model-unknown FID is probed (an EF has no children) and reported with its own
FID as `name` and `file_type` from its FCP.

Use `parent_path` to list a subdirectory, e.g.
`{"name": "DF.GSM-ACCESS", "fid": "5F3B", "parent_path": ["MF", "ADF.USIM"]}`.

**PBR-derived children.**  When the requested node is the DF that holds the
card's **EF_PBR** (`4F30`, TS 31.102 §4.4.2.1 - `MF/7F10/5F3A`,
`ADF.USIM/5F3A` or `ADF.USIM-NON-IMSI/5F3A`), the server parses the cached PBR
records (see `GET /api/pbr`) and appends the phonebook files the PBR
references, which the model does not list:

```json
{"name": "EF.ADN", "fid": "4F3A", "isDir": false, "pbr": true, "sfi": "02"}
```

`pbr: true` marks a PBR-derived node and `sfi` its short file identifier
(`null` when the PBR declares none).  A file the model already lists is not
added twice, and the match is by the requested node's full normalised path
(upper-case, `3F00` → `MF`); a bare DF FID that cannot be told apart (the
three PBR copies all sit in a DF with FID `5F3A`) injects nothing.

#### `POST /api/select`

Selects the node and reports its FCP metadata; the node also becomes the
`/api/status` `selection`.

```json
{"name": "EF.ICCID", "fid": "2FE2", "parent_path": ["MF"]}
```

```json
{"name": "EF.ICCID", "fid": "2FE2", "file_type": "transparent",
 "file_size": 10, "record_len": null, "num_of_rec": null,
 "fci_hex": "621082024021...", "fci_form": "ber",
 "apdu_times": [{"type": "select", "ms": 12}], "exists": true}
```

`file_type` is `df` for a DF/ADF/MF, else the EF structure
(`transparent`, `linear_fixed`, `cyclic`, or `no_info_given` when the
descriptor's structure bits are RFU - the string the stored profiles and
snapshots pin).  `fci_hex` is the raw SELECT response and `fci_form` says
what it is: `ber` for a TS 102 221 FCP/FCI, **`gsm`** for a 2G SIM's
TS 51 011 §9.2.1 file information, or `null` when the card returned no data
at all.  The GSM answer is parsed into the same fields - bytes 3-4 are the
file size, byte 14 the structure (`00` transparent, `01` linear fixed, `03`
cyclic) and byte 15 the record length, with the record count computed as
size / record length - so a 2G SIM reports its own real
`file_size`/`record_len`/`num_of_rec` (the live bench card: EF.ICCID size
10, EF.ARR 31 records of 76 bytes).  `apdu_times` reports the request's own
commands (classified `select`/`read_binary`/`read_record`, command-sent to
response-received) for the snapshot timing statistics.

#### `POST /api/read`

Reads the addressed file.  A transparent EF answers the whole file as
upper-case hex under `data`; a linear-fixed/cyclic EF answers
`records: [{num, data}]` (1-based).

```json
{"path": "ADF.USIM/6F07"}
```

```json
{"success": true, "sw": "9000", "file_type": "transparent",
 "data": "0829051032547698",
 "apdu_times": [{"type": "select", "ms": 11}, {"type": "read_binary", "ms": 8}]}
```

```json
{"success": true, "sw": "9000", "file_type": "linear_fixed",
 "records": [{"num": 1, "data": "..."}, {"num": 2, "data": "..."}],
 "apdu_times": [{"type": "select", "ms": 12},
                {"type": "read_record", "ms": 11}, {"type": "read_record", "ms": 13}]}
```

```json
{"success": true, "sw": "9000", "file_type": "transparent",
 "data": "980711090000640090F8", "size_inferred": true,
 "apdu_times": [{"type": "select", "ms": 11},
                {"type": "read_binary", "ms": 8}]}
```

`mode` must be `"raw"` (the default).  **There is no `mode: "decoded"`**: the
legacy mode ran pySim's server-side EF decoders; the PWA decodes the same raw
bytes client-side, so any other value is a `400` naming the rule instead of
being silently ignored.

`length` / `record_len` carry a caller-supplied size for a file whose SELECT
reported none (a card that returned nothing, a custom file): `length` is the
transparent read size (1-65535), `record_len` the record length (1-256).  A
value the FCP already reports wins (the card's layout is authoritative); a
`length` on a record file or a `record_len` on a transparent one is a `400`.
Without them a transparent file is read with **`READ BINARY Le = 256`** until
the card signals the end - a short read, the `6282` end-of-file warning (its
bytes are kept), or the 2G SIM's `9402` out-of-range address - and a record
file with `Le = 256` from record 1 until a refusal; both are marked
**`size_inferred: true`** (the PWA uses it for the profile/snapshot
metadata).  An immediate `6B00` is a failed read, not an empty file (it is
"wrong P1-P2", not an end-of-file code).

A **2G SIM's** SELECT carries the TS 51 011 §9.2.1 file information, which
`/api/select` parses into the ordinary fields, so its files are read with the
size-driven path; the read-until fallback is the residual "the card returned
nothing" case.

A **card-level** read failure (a refused READ) is a `200` with
`success: false` and the card's status word plus an `error` line - the PWA
shows the SW and offers its ADM action for `6982`/`9804`.  A record file whose
FCP announces a count is read to that count; a refusal mid-file is
`success: false` with the refusing `sw` **and the partial `records`** (a
truncated list must never look like a complete one).  Only a failed SELECT is
a `404` (the file is not there).

#### `POST /api/write`

`UPDATE BINARY` on a transparent EF or `UPDATE RECORD` on a record EF.

```json
{"path": "ADF.USIM/6F7E", "data": "4FE69DE7..."}
```

```json
{"name": "EF.ADN", "fid": "6F3A", "data": "A0A1...", "record_nr": 1,
 "parent_path": ["MF", "7F10"]}
```

`data` is non-empty even-length hex (1-255 bytes - a short APDU's data
field); `record_nr` (1-255) is for record files and defaults to record 1.
Both are validated **before** the selection, and a `record_nr` on a
transparent file is a `400` before any write.  A successful write answers
`{"success": true, "sw": "9000"}`; a card refusal is a `200` with
`success: false`, the `sw` and an `error` line.  A write to EF.ARR refreshes
the session cache (the `8B` references may have changed).

### `GET /api/arr`

The card's cached **EF.ARR copies** - the access-rule files the FCP security
attribute DO `'8B'` references (ISO 7816-4 Table 25).  Read once per card
session at equip (`MF/2F06`, `MF/7F10/6F06`, `MF/7F20/6F06`, `ADF.USIM/6F06`,
best effort, at most 64 records each) and served from memory (no card I/O).
A disconnect clears the cache; an EF.ARR write through `/api/write` refreshes
it.

```json
{"ok": true, "session": 12, "arrs": [
   {"fid": "2F06", "path": "MF/2F06", "record_len": 10, "num_records": 23,
    "records": ["FFFF...", "..."]}]}
```

`ok` is `false` (with an empty `arrs`) when the card has no readable ARR.
`path` is the legacy **FID** label (`MF/7F10/6F06`): the PWA's file paths are
FID-based, so a symbolic DF name would never resolve.  The PWA's file manager
resolves the selected file's `8B` reference against this cache and shows the
record's access rules; snapshots resolve the same references from their own
captured EF.ARR copy.

### `GET /api/pbr`

The card's cached **EF.PBR records** (`4F30`, TS 31.102 §4.4.2.1) - the
BER-TLV structure describing the phonebook files.  Read once per card session
at equip (`MF/7F10/5F3A/4F30`, `ADF.USIM/5F3A/4F30`,
`ADF.USIM-NON-IMSI/5F3A/4F30`, best effort, at most 16 records each) and
served from memory; a disconnect clears the cache.

```json
{"ok": true, "session": 12, "pbrs": [
   {"fid": "4F30", "path": "MF/7F10/5F3A/4F30", "record_len": 32,
    "num_records": 1, "records": ["A80AC0034F3A02C5034F0904AA0A..."]}]}
```

`ok` is `false` (with an empty `pbrs`) when the card has no readable PBR.  The
records are served **raw**: the PWA owns the decoder for the decoded file
views (`efDecPbr`).  The server keeps a small parser of the same structure
only to populate the file tree (`/api/tree` appends the referenced phonebook
files and they stay selectable/readable - see the file endpoints).  A
container that does not parse as primitive tags is kept raw by both sides and
its files are neither resolved nor injected.  The two parsers share
`frontend/tests/pbr_vectors.json` (drift-guarded from both suites).

### `POST /api/apdu`

Sends one raw APDU through the transport (the T=0 `61xx`/`6Cxx` chaining is
the transport's business).

```json
{"apdu": "00A40004023F00"}
```

```json
{"response": "", "sw": "9000"}
```

The APDU is sent exactly as given and **any** status word is returned with
`200` (the legacy `send_apdu_checksw` path raised on a non-`9000` answer and
answered `500`).  A `91XX` answer is **not** passed through: the card
announced a proactive command, so the terminal FETCHes and answers it with the
full chain (PLI dictionary/host clock, BIP, timers, display/menu auto-answers)
and the response reports `9000` - a card that queues a command as a side
effect is never left with an unanswered FETCH.  The auto-handler is off only
when something else owns the card: a test run answers `409` before dispatch,
and a running BIP session returns its own `91XX` as-is (its terminal answers
its own commands).  A malformed APDU is a `400`; a transport failure a `503`;
no card session a `503`.

### `POST /api/verify-adm`

Verify the card's ADM PIN (TS 102 221 VERIFY, the card's own CLA).  The key is
`adm` or, when that is empty, the stored `adm` of the preset named by
`preset_id`; it is never stored by the verify itself and is redacted from the
request log.  A key shorter than 8 bytes is right-padded with `f` to the 8 CHV
bytes (like pySim's `verify_adm`).

```json
{"adm": "0011223344556677", "chv": "0A", "preset_id": "7ee0367f…"}
```

`chv` is the CHV number as a one-byte hex string (`"0A"`) or as an integer
byte value (`10` is 0x0A, not a hex-encoded `10`); it defaults to `0x0A`.
`adm` must be 4-16 hex digits in byte pairs (`(?:[0-9A-F]{2}){2,8}`), else
`400`.

Returns on success (and sets the `adm_verified` latch for the card session):

```json
{"ok": true, "sw": "9000"}
```

On a wrong key (`63Cx`, x attempts left):

```json
{"ok": false, "sw": "63C2", "attempts_left": 2}
```

A blocked ADM (`6983`/`9804`) reports `{"ok": false, "sw": "9804",
"blocked": true}` and cannot be recovered here (the card's unblock key is
needed).  Any other refusal is `{"ok": false, "sw": …, "error": …}`.  Every
wrong key consumes an attempt, so the UI confirms before a retry.

### Card presets (server-side store)

Card presets (keysets, a TAR table with per-TAR MSLs, the optional card codes,
the SCP81 PSK pair, the CAT release and the ADM key) live on the server in
`card_presets.json` inside the server's data directory - `~/.pysim-simple-server/`,
a name kept from 3.x so an existing install's stores carry over unchanged
(`--card-presets PATH` overrides the file; `Path.home()` resolves the same way
on Linux, macOS and Windows).  The file is written atomically and every
counter change is appended to `card_presets.json.audit.jsonl` with its keyset
number.

```json
{"id": "…", "name": "Card A", "iccid": "…", "release": "",
 "pin1": "", "puk1": "", "pin2": "", "puk2": "",
 "pskIdentity": "", "pskKey": "", "adm": "",
 "tars": [
   {"role": "isd",     "tar": "000000", "msl": "16", "desc": "", "keyset": 1},
   {"role": "uiccRfm", "tar": "B00000", "msl": "16", "desc": "", "keyset": 1},
   {"role": "usimRfm", "tar": "B00001", "msl": "16", "desc": "", "keyset": 1},
   {"role": "",        "tar": "AF4D01", "msl": "1A", "desc": "my applet", "keyset": 2}
 ],
 "keysets": [
   {"kic": "15", "kid": "15", "kicKey": "…", "kidKey": "…", "cntr": "0000000001"},
   {"kic": "29", "kid": "29", "kicKey": "…", "kidKey": "…", "cntr": "0000000005"}
 ]}
```

**Keysets.**  A keyset's number is the `b8..b5` nibble of its KIc/KID
(TS 102 225 §5.1.2/A.2); the number is implicit in the bytes, there is no
separate field.  KIc and KID must carry the **same** number (a card rejects a
mismatch with "Unidentified security error"), the number must be `01`–`0F`
(`00` means "no security" and is a packet-level choice), no two keysets may
share a number, and both keys and a counter (1-10 hex digits) are required.
The counters are stored in the fixed-width 10-hex form.  A v3.8.0 flat preset
(`kic`/`kid`/`kicKey`/`kidKey`/`cntr` at the top level) converts into a single
keyset on load and import, so old files and exports keep working.

**TAR table.**  Each Remote Management application is addressed by its TAR and
each entry carries its own **MSL** (Minimum SPI1, TS 102 226 §8.2.1.3.2.4 -
the card checks it before security processing and answers response status
`0A` "Insufficient security level" below it) and its **default keyset**
(`keyset`, 1-15).  The three role entries (`isd`, `uiccRfm`, `usimRfm` - GP
ISD `000000`, TS 31.116 UICC RFM `B00000`, TS 31.115 ADF RFM `B00001`) are
always present - the store fills a missing one with its defaults - and further
TARs can be added with an optional `desc`.  A TAR is three hex bytes, an MSL
exactly one hex byte (it may not be empty), and a TAR may appear once (a
duplicate is refused).  The keyset is **mandatory for a non-zero MSL** (the
counters are keyset-linked, Annex A.1) and optional for MSL `00` - where
setting one still passes its KIc/KID in the packet, which some cards require
instead of the no-security `00/00` form.  A preset with exactly one keyset
fills the entries that name none with it automatically.  A v3.9.1 preset's
card-wide `spi1`/`tar`/`uiccTar`/`usimTar` convert into the three role
entries, the `spi1` as their MSL.

Operations **derive a packet's SPI1 from the MSL of its TAR** and its keyset
from that TAR's `keyset`; a request naming a TAR outside the preset must pass
`spi1` itself, otherwise it is refused with `400` and a hint.

**Card codes.**  The optional `pin1`/`puk1`/`pin2`/`puk2`/`adm` fields are
stored for later use (nothing consumes the PIN/PUK codes yet).  PIN/PUK must be
**4-8 decimal digits** when set (empty is fine; anything else is refused with
`400` naming the field).  The ADM key is stored as upper-case hex; verification
requires 4-16 hex digits in **byte pairs** - the rule `/api/verify-adm` and the
outfile import enforce - so an odd-length ADM can never verify.

**CAT (USAT) release.**  The optional `release` field (4-18) names the release
the card implements; the release guard hides CAT items introduced after it
(the Simulator's event parameters).  Empty means unspecified and resolves to
Rel-12 - an unset release never filters anything away.  A value outside 4-18 is
refused.

A preset needs **at least one keyset**; the SCP81 PSK pair is all-or-nothing
(an identity and a 32-hex key, or neither).

**Counters.**  The store is the source of truth for the SCP80 counters, one
per keyset ("a dedicated counter shall be associated to each key version",
Annex A.1).  Every operation below accepts a `preset_id` and persists the
counter it consumed itself (monotonic per keyset - a stale value never
regresses it), so a closed tab, a lost response or a second browser window
cannot lose an increment.  A request whose KIc/KID name a non-zero keyset the
preset does not define is refused (`400`), and so is a KIc/KID number
mismatch.

- `GET /api/presets` - `{"path": "…", "count": 2, "version": 6, "presets":
  [{…}]}`.  Card-free: it answers while a long card operation runs.
  `version` is the store schema (currently 6).
- `POST /api/presets` - body = the preset fields (`keysets` and `tars`
  included); returns `{"ok": true, "preset": {…}}`.  A duplicate ICCID
  (digits, spaced digits, digits with the odd-nibble F pad, or raw EF hex -
  all normalised to one form) is refused with
  `400 {"error": "card with this ICCID already exists: …"}`.
- `POST /api/presets/update` - `{"id": "…", "fields": {…}}` (partial update; a
  plain `{id, name, …}` body works too, `keysets` replaces the list and `tars`
  the TAR table - a non-list `tars` is refused).  A counter is written as
  given: this is the deliberate human edit.  Unknown ids answer `404`.
- `POST /api/presets/delete` - `{"id": "…"}` →
  `{"ok": true, "removed": true}`.
- `POST /api/presets/import` - `{"presets": […], "mode": "merge"}` (or
  `"replace"`) → `{"ok": true, "added": N, "skipped": M, "errors": […]}`;
  invalid entries and duplicates are skipped and reported, never fatal.  An
  omitted or unrecognised `mode` falls back to `merge` for this endpoint (the
  outfile and test-script imports refuse a misspelled mode instead).  The
  import also accepts presets exported by the older localStorage-based
  builds.

All four mutations answer `400` with the store's message when the store refuses
them (a shape rule above).  They need no card, but - unlike the
test-script/-suite store mutations - they are **not** in the card-free
allowlist, so a preset write queues behind a running card operation.

### `POST /api/presets/import-outfile`

Import many cards at once from a card vendor's **outfile**.  An outfile is the
vendor personalization tool's plain-text dump: a banner and a configuration
header (customer, batch, profile, …) followed by the actual data.  The data
starts at a **column-name line** - whitespace-separated tokens that include
`ICCID` (the first token may carry a `var_out:`-style prefix) - and continues
with one whitespace-separated row per card; everything before the column line
and every column the presets do not use is ignored.

Without a `mode` the call is a **dry run**: the parsed presets and the report
come back for the preview (nothing is stored).  With `mode: "merge"` /
`"replace"` the same parse runs and the presets are imported through the store
(`replace` wipes the store first, like `/api/presets/import`); any other
`mode` value is refused with `400` - a typo must not silently turn an import
into a preview.

```json
{"text": "…", "name": "Batch 0002", "algorithm16": "3des2",
 "tars": [{"role": "isd", "tar": "000000", "msl": "16", "desc": ""}, …],
 "tar_keyset": 1, "start_counter": "1", "mode": "merge"}
```

- `text` (required) - the outfile contents.  A file without a column line
  answers `{"ok": true, "preview": true, "presets": [], "report": {"error":
  "no column-name line found - …"}}` (a preview, not a `400`: the panel shows
  the message).  The same report error covers a structurally invalid file: a
  data row whose token count differs from the header's column count (the
  format is positional, so an empty value drops its token - including in the
  middle, which would shift the columns), or a `KICn`/`KIDn` pair declared in
  the header without a usable hex value in some row.  An import (`mode`) of
  such a file answers `400` with that error - it is never partially imported.
- `name` - one common name applied to every imported card; empty falls back to
  the card's ICCID.
- `algorithm16` - the algorithm nibble for a **16-byte** key: `"3des2"`
  (default, nibble `5`) or `"aes"` (nibble `2`); any other value is refused
  with `400` (the nibble decides whether the card accepts the packet).  The
  key size alone decides the others: 8 bytes → DES (`1`), 24 bytes → 3DES3
  (`9`); any other size skips that keyset with a report line.
- `tars` - the TAR table every imported card gets (defaults to the three role
  entries with their spec TARs and MSL 16).  A malformed table (a non-3-byte
  TAR, a non-1-byte MSL, a duplicate) is refused with `400`.
- `tars` entries may carry their own `keyset` (1-15): it wins; the entries
  without one fall back to `tar_keyset`, and without that to the card's first
  keyset (a multi-keyset card would otherwise fail the non-zero-MSL rule).  A
  keyset outside 1-15 is refused with `400`.
- `tar_keyset` - an optional **global default keyset** (1-15) for the TAR
  entries that do not name one (the API's shorthand; the PWA sends per-row
  keysets instead).
- `start_counter` - the counter **every created keyset starts from** (1-10 hex
  digits; whitespace stripped, uppercased).  Without it the store's default
  (`1`) applies.  A bad value is a `400`; the import stores the protocol's
  10-digit form.

Recognised columns (case-insensitive): `ICCID` (digits + odd-nibble F pad,
plain digits, spaced digits or the raw nibble-swapped EF hex all normalise to
one value), `PIN1`/`PIN2`, `PUK1`/`PUK2` (4-8 digits; an invalid value is
dropped and reported, the card is kept), `ADM` (the key's **hex value**, 4-16
hex digits in byte pairs - the rule `/api/verify-adm` applies), `PSK` (the
SCP81 key, exactly 32 hex chars) and `KICn`/`KIDn` - one keyset per numbered
pair, up to 15.  The **KIc/KID bytes are never in the file**: the keyset
number is the column number (`KIC2` → keyset 2) and the algorithm nibble
follows the key size, both bytes carrying the same number (TS 102 225 §A.2).
A `PSKID`/`PSK_ID` column names the PSK identity; without one the card's ICCID
is used.  `IMSI*`, `EKI` and `KIKn` are ignored.

The response carries the parsed presets (a dry run) or the import result plus
the `report`:

```json
{"ok": true, "preview": false, "added": 5, "skipped": 0, "errors": [],
 "report": {"columns": ["IMSI", "ICCID", "KIC1", …], "header_lines": 11,
            "rows": 5, "cards": 5, "keysets": ["KIC1/KID1", …], "tars": 3,
            "skipped": ["line 4: keyset 2 has an unsupported key size (10 bytes)"],
            "skipped_more": 0}}
```

### `POST /api/counter-probe`

Sync a preset's SCP80 counter with the card.  The card accepts only a counter
*above* its own (TS 102 225 §5.1.1, SPI1 b5b4 = `10`) and updates it only when
the packet is accepted, so rejected attempts change nothing; the probe walks a
doubling ladder (`start+1`, `+2`, `+4`, …) with a fixed read-only command
until one is accepted, then persists `accepted+1` (monotonic) into the keyset.
The search stops at a ceiling and after a bounded number of packets, so it can
never run towards the 40-bit maximum, where the card blocks the counter.  The
card's own counter is never reported by the card (the PoR's CNTR is a copy of
the packet's, TS 102 225 §5.2 Table 3), which is why this probe exists.

```json
{"preset_id": "…", "kvn": 2, "tar": "000000", "spi1": "16", "spi2": "21",
 "cntr": "0001000000", "ceiling": "FFFFFFFF", "max_attempts": 40}
```

`preset_id` is required.  `kvn` picks a keyset (the TAR's default, else the
first), `tar` defaults to the preset's ISD entry and `spi1` to that TAR's MSL,
`spi2` to the RAM listing transport `21`, `cntr` to the keyset's counter,
`ceiling` to `FFFFFFFF` (must stay below `FFFFFFFFFF`) and `max_attempts` to
40 (clamped to 1-64).  The probe needs the PoR verdict, so bit b1 of `spi2` is
always set (a caller's `00` becomes `01`; the transport bits are kept) -
without a PoR the probe is blind.  A SPI1 without a counter check is refused
(`400`).

The ladder only advances on a `cntr_low` verdict (the card's "your counter is
not above mine"); **any other verdict stops the search**.  A stop on a
non-`por_ok` verdict (`rc_cc_ds_failed`, `tar_unknown`, …) *after* at least one
`cntr_low` keeps the counter value anyway - the earlier rejection proves the
value is above the card's counter - but the response reports `success: false`,
`counter_saved: true`, the `verdict` and the saved counter, stating that
something is likely wrong with the security settings.  Without a prior
`cntr_low` nothing is saved, and a missing PoR is an error too.

```json
{"success": true, "stopped": "accepted", "verdict": "por_ok",
 "counter_saved": true, "accepted_cntr": "0001000000",
 "stored_cntr": "0001000001", "packets": 3,
 "preset_id": "…", "preset_name": "Card A", "kvn": 2,
 "start": "0000FFFFFF", "ceiling": "FFFFFFFF", "max_attempts": 40,
 "tar": "000000", "spi1": "16", "spi2": "21", "apdu": "00A40000023F00",
 "attempts": [{"cntr": "…", "por_status": "cntr_low", "sw": "9000",
               "bytes": 36, "segments": 1}]}
```

`stopped` is `accepted`, `ceiling`, `attempts` or `error`.  An attempt carries
`cntr`, `por_status`, the transport `sw`, `bytes`/`segments` and, when
available, `remote_sw` and `bad_format`.

### `POST /api/tar-probe`

Probe which OTA applications the card has registered: one **secured packet per
TAR of the named preset** (the preset's TAR list only; a `tars` list may
select a subset, and a TAR outside the preset is refused with `400`), each
carrying a harmless C-APDU (`SELECT MF` by default).  Each row uses **its own
MSL** for SPI1 (the request's `spi1` is only a fallback) and **its own default
keyset**, so the request carries no KIc/KID/keys.  A TAR whose MSL requires
security (non-zero) without a default keyset, or one naming an undefined
keyset, is refused with a hint **before any packet is sent**; so is a TAR whose
resolved SPI1 has no counter check (b5b4 = 00) - the probe needs the consumed
counter to advance.  Every accepted packet consumes a counter, persisted into
that row's keyset (each keyset has its own counter, TS 102 225 A.1); an
explicit `cntr` seeds the first row only.  The delivered SMS carries the OTA
CPI IE by default (`includeCpi`, as in `/api/send-ota`).

```json
{"preset_id": "…", "spi1": "16", "spi2": "01", "apdu": "00A40000023F00",
 "tars": ["000000", "B00000"], "includeCpi": true}
```

```json
{"success": true, "registered": 1, "total": 2, "final_cntr": "…", "kvn": 2,
 "spi1": "16", "spi2": "01", "apdu": "00A40000023F00",
 "results": [{"tar": "000000", "label": "ISD", "spi1": "16", "kvn": 2,
              "verdict": "registered", "sw": "9000", "por_status": "por_ok",
              "por_sw": "6D00", "por_data": "", "bytes": 36, "segments": 1}],
 "steps": [...]}
```

Verdicts: `registered` (ENVELOPE `9000` + a PoR carrying the application's own
status word/data), `no_answer` (accepted, PoR without an R-APDU), `no_por`
(accepted, no PoR at all - the packet was silently dropped), `refused` (a
non-`9000` ENVELOPE, typically `6200` for a TAR without a registered
application).  The top-level `kvn`/`spi1` are the legacy fields: the first
row's keyset and the request's `spi1` fallback - **not** a row's effective
SPI1, which is the row's `spi1` in `results`.

### `POST /api/send-ota`

Send an OTA command (SCP80) to the card via SMS-PP-DOWNLOAD ENVELOPE (one
ENVELOPE per SMS).  With `apdu` the secured packet is built server-side (no
single-SMS limit); a packet that does not fit one SMS is sent as a
**concatenated** download per TS 31.115 §4.3: the packet is split into SMS
user-data parts (first SM 132 octets, following ones 134 - the first one
additionally carries the concatenation and CPI IEs) and the segments are sent
in order.  A packet that would need more than 5 segments is refused (the
card's concatenation buffer is the limit).  With `sp` a pre-built packet is
delivered the same way.

**SMS framing - `includeCpi`.**  By default (`true`) the delivered SMS TPDUs
carry the OTA **CPI information element** (IEI `70`, null IED - TS 31.115
§4.2/§4.3), the marker of a command packet; it shrinks the SMS user data - a
single SM takes up to **137** packet octets instead of 140, and the first
concatenated segment **132** instead of 134.  The RAM/ISD operations send
with `includeCpi: true`.  With `false` no CPI IE is added (a single SM then
carries no UDH at all and the full 140-octet budget is available).

**The keyset.**  An explicit `kic`/`kid` wins; otherwise the preset's default
keyset for the packet's TAR (`tar`, else the ISD role entry for an `apdu`
send) supplies both bytes and the keys.  An `apdu` send without a usable
keyset is refused (`400` - "the packet needs KIc/KID").  The spec's
no-security values `00/00` are accepted when the SPI declares no ciphering /
no RC-CC-DS (A.2), so `{apdu, spi1: "00", kic: "00", kid: "00"}` builds a
keyless packet.

**SPI1 and the TAR's MSL.**  With a `preset_id` the packet's SPI1 is derived
from the MSL of its `tar` (TS 102 226 §8.2.1.3.2.4) unless the request passes
`spi1` - an explicit value wins (a hand send may deliberately ask for more
security than the minimum), and a value below the TAR's MSL is reported as
`"warning"` in the response (numeric compare).  A TAR the preset does not
carry, without an explicit `spi1`, is refused with `400` and a hint.

**The counter.**  `cntr` is optional - the preset store is the source of
truth:

- with `apdu` a missing `cntr` is seeded from the named preset's keyset (the
  one the packet's KIc/KID resolve to); the effective value is echoed back as
  `cntr`, so a caller that omits it learns what was used.  The seed uses the
  packet's keyset whatever `SPI1.b5b4` says - with no counter check the CNTR
  field is spec-ignored, nothing is advanced or persisted, and the
  `OTA SEND: counter not tracked` log line names it;
- an explicit `cntr` always wins - the packet uses it verbatim;
- the store only moves **forward**: an explicit value (or its next counter)
  below the stored one is used for the packet but never lowers the store; a
  higher accepted value is persisted;
- the `sp` (pre-built packet) path never seeds - the packet carries its own
  counter - so pass `cntr` with it to get `final_cntr`/persistence.  A request
  with neither simply reports no `final_cntr`; it never fails the send.

With `preset_id` the server persists the counter into the keyset the packet
used (see *Card presets*), so a lost response or a closed tab cannot lose the
increment; the request is refused when KIc/KID carry different keyset numbers
or a non-zero keyset the preset does not define.

**Counter tracking follows SPI1.b5b4** (TS 102 225 §5.1.1): with `00` the
counter field is "present, ignored, never updated", so a packet whose SPI1 has
no counter check reports **no** `final_cntr` and persists nothing.  When a PoR
was requested and the card did not answer, the response carries
`por_missing: true`.

**RAM command format.**  `ram_format` (`compact` / `expanded` / `expanded-ae`
/ `auto`) wraps the `apdu` in the TS 102 226 §5.2.1 Command Scripting template
when an expanded coding is asked (`expanded` = the definite `AA` template,
`expanded-ae` = the indefinite `AE 80` one) and is echoed as `ram_format` in
the response.  The plain send has no RAM step to probe with, so **`auto` here
sends and reports the compact form** - the per-operation probe belongs to the
RAM operations.  An unrecognised value is treated as `auto`.  Without the key
the command data is sent byte-exact: applet-directed payloads (HTTP OTA
triggers, §9 push, expanded scripts) must not be wrapped.

**The PoR.**  The card may answer with PoR status `actual_response_sms_submit`
(`0x0B`): the real response then arrives as one or more proactive SEND SHORT
MESSAGE commands.  The server captures those SMS-SUBMIT TPDUs, reassembles
the concatenated segments and returns the decoded response in `por` (as if it
had arrived in the ENVELOPE), so callers see a normal
`por.response_status == "por_ok"` with the remote status word and response
data.  Responses wrapped in the TS 102 226 §5.2.2 Response Scripting template
(`AB`/`AF`: executed-count TLV `80` + R-APDU TLV `23`) are decoded the same
way (`response_type: "scripting"`), with the R-APDU's own status word and
data.  The SPI2 `por_in_submit` bit (`0x20`) selects submit-mode PoR.

The response form follows the command's TAR: a remote-management TAR (the
preset's role entries and the standard ISD/RFM allocations of TS 101 220
Annex D) is decoded as an RM response; any other TAR's secured data is the
receiving application's own (TS 102 226 §4) and is reported with
`por.response_type` `raw` - the whole secured data in
`decoded.last_response_data` and no fabricated status word (`secured_data`
carries the response bytes in every case).  The same `raw` classification
covers a secured data that cannot be the compact structure (its command count
exceeds the command script that was sent, or it is too short for Table 5.1),
while a genuinely malformed packet or a failed RC/CC/DS check yields no `por`.

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
  "kidKey": "1B07E7E0...",
  "preset_id": "7ee0367f3a444b28988fea5fbc50de9f",
  "ram_format": "compact",
  "includeCpi": true
}
```

Use `sp` **or** `apdu` (plus the SPI/keys/TAR fields for a server-built
packet).

**Response (success):**

```json
{"success": true, "sw": "9000", "response_data": "027100000e0a...",
 "bytes": 36, "segments": 1, "cntr": "0000000001",
 "ram_format": "compact", "final_cntr": "0000000002",
 "por": {"response_status": "por_ok", "tar": "B00000", "pcntr": 0,
         "decoded": {"number_of_commands": 1, "last_status_word": "6e00",
                     "last_response_data": ""}}}
```

`bytes` is the secured packet size and `segments` the number of SMS segments
sent (1 = single SMS, >1 = concatenated download).  `final_cntr` is the
counter to use next (reported on success **and** on failure for an accepted
packet).  A low-counter verdict is flagged as `cntr_low: true` (the card's own
counter value is never reported - the PoR's CNTR is a copy of the packet's and
a rejection leaves it zeroed): retrying cannot fix it, the caller raises the
preset counter (the counter probe can do that in bounded steps), and the
response carries no other counter fields.

**Response (failure):** the same transport fields with `success: false` and
an `error` (`ENVELOPE failed at segment N`, the packet-splitting refusal, or
an invalid/empty packet), plus `por` when the warning ENVELOPE still carried
a PoR:

```json
{"success": false, "sw": "6200", "bytes": 36, "segments": 1,
 "error": "ENVELOPE failed at segment 1",
 "por": {"response_status": "cntr_low", "...": "..."},
 "cntr_low": true}
```

The PoR is reported with `por_data`, `por_sw`, `por_type`/`por_cntr`/`por_raw`
for context; the raw status word of the *remote* command is `por_sw`, the
transport's ENVELOPE status word is `sw`.

### `POST /api/cap-info`

Validate a Java Card `.cap` archive and estimate its memory requirements.
Read-only: it runs the same structural parse as the install paths (so a
corrupt or wrong-format file fails here first) plus the bundled CAP analyzer
(component parsers and a method-bytecode allocation scan).  It never touches
the card, the listener or the scripts; the estimate is not used for
installation.  Card-free.

```json
{"cap_hex": "504B0304..."}
```

**Response (ok):**

```json
{
  "ok": true,
  "load_file_aid": "AA1902BC226001", "module_aid": "AA1902BC226001",
  "load_file_bytes": 1234,
  "memory": {
    "package_aid": "AA1902BC226001",
    "applet_count": 1, "applets": ["AA1902BC226001"],
    "imports": [{"aid": "A0000000620101", "minor": 0, "major": 1, "refs": 6},
                {"aid": "A0000000090005FFFFFFFF8912000000", "minor": 11, "major": 1, "refs": 7}],
    "flags": {"raw": 4, "int": false, "export": false, "applet": true},
    "package_name": null,
    "components": [{"name": "Header", "size": 20}, {"name": "Method", "size": 433}],
    "class_count": 3, "method_count": 12,
    "code": {"method_component": 850, "load_file": 1234},
    "nvram": {"static_image": 12, "array_init": 4, "install_objects": 100,
              "header_overhead": 24, "ref_storage": 8, "total": 148, "runtime": 0,
              "requirement": 1382},
    "ram": {"transient_arrays": 16, "runtime_transient": 0, "peak_frame": 8, "total": 24},
    "suggested": {"c6": 850, "c7": 272, "c8": 148},
    "warnings": []
  }
}
```

`imports` are the libraries the CAP is linked against (JC VM spec §6.6, the
Import component), each with the export-file version recorded in the CAP and
the number of distinct constant-pool references to it (§6.7; a linked but
unreferenced package is 0).  A card resolves an import only when the resident
package has the **same major** version and a **minor ≥** the recorded one
(§4.5.2).  `flags` decodes the Header package flags (Table 6-4: `0x01` uses
`int`, `0x02` exports an API, `0x04` applet package); `package_name` is only
present in JC 2.2+ headers; `components` lists the archive entries in
load-file order (each size includes the component's tag and size header, so
the sizes sum to `load_file_bytes`).

`code.load_file` is the concatenated load file (all components) - the package
image the card stores, our proxy for the GP Card Spec v2.3.1 Table 11-48
"non-volatile code" minimum memory requirement; the bytecode-only figure stays
in `code.method_component`.  The suggested `C6` follows the load file, and
`nvram.requirement` = `code.load_file` + `nvram.total` (§11.5.2.3.7: with no
code/data split in the card's memory the required minimum is the sum of both).
The estimate excludes card-specific memory management, allocation rounding and
the GP registry entry.

**Errors:** a bad archive or an invalid request value answers
`400 {"ok": false, "error": "cap parse failed: …"}`.

### `POST /api/ram-install`

Install a Java Card `.cap` on the card via GlobalPlatform commands
(`INSTALL [for load]` → `LOAD` ×N → `INSTALL [for install (+ make selectable)]`)
wrapped in SCP80 secured packets.  Each step is sent via ENVELOPE; the PoR
verdict and the remote command's own status word are both checked and the
sequence aborts on the first failure (a non-`por_ok` PoR, a remote SW outside
the success set, or an undecodable PoR).  The counter advances only for a
packet the card accepted; `final_cntr` is returned on success **and** on
failure, so the caller keeps the card's consumed counter.  A missing `cntr` is
seeded from the preset's keyset; an explicit value wins but never lowers the
stored counter.

The RAM command format is detected per operation: a read-only
`GET STATUS [ISD]` probe (`FORMAT CHECK`) decides between the compact C-APDU
and the two expanded codings (`AA` definite / `AE 80` indefinite), and every
following step uses the detected format.  Pass `ram_format` (`compact` /
`expanded` / `expanded-ae`) to pin it.  The **NV footprint** is measured
around the chain: a best-effort `GET DATA FF21` (Extended Card Resources,
TS 102 226 §8.2.1.7.2) read before the first step and again after the last one
(also on failure) reports the free non-volatile memory and its delta; the read
is silent (no step record, no failure) and a card without FF21 yields no
fields.  `delete_package: true` prepends a **cascade GP DELETE** (`P2=0x80`)
on the package AID - a leftover package blocks `INSTALL [for load]`, so a
re-install can clear it first; the delete step tolerates 'not found'
(`6A88`/`6A82`) and any other delete failure stops the chain with its reason.

**Request body:**

```json
{
  "cap_hex": "DECAFFED...",
  "sd_aid": "A000000003000000",
  "install_params": "C900",
  "stk_params": "",
  "nv_quota": 0,
  "volatile_quota": 0,
  "make_selectable": true,
  "load_block_size": 240,
  "ram_format": "auto",
  "includeCpi": true,
  "delete_package": false,
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
| `sd_aid` | no | Security Domain AID for `INSTALL [for load]`; empty → default ISD `A000000003000000` |
| `install_params` | no | Hex of the install-parameter field - the mandatory `C9` TLV (application-specific parameters; `C900` when empty), the System Specific Parameters `EF`, extra raw TLVs.  When empty the server **composes** it from the quotas and `stk_params` (see below) |
| `stk_params` | no | The STK part appended to the install parameters: the **EF-wrapped** CA (`EF <len> CA <len> …`, TS 102 226 §8.2.1.3.2.1 - the SIM access domain leads the CA value as `01 <ad>` / `00`) or the UICC template (`EA <len> 80 … [81 …]`).  A bare `CA` is refused (it belongs inside the EF) |
| `nv_quota` / `volatile_quota` | no | Integer memory quotas (bytes) coded as `C8` / `C7` in the install EF; only composed when `install_params` is empty.  `0` is a valid quota (the vendor reference form carried `C7 02 0000 C8 02 0000`); omit the field to leave the TLV out |
| `make_selectable` | no | If true (default), the final INSTALL uses P1=`0C` (install + make selectable) |
| `load_block_size` | no | Bytes of load-file payload per LOAD APDU, 1-240 (default 240 when omitted).  SCP80 concatenation carries a larger secured packet over up to 5 SMs, so the block size is not clamped to one SMS |
| `ram_format` | no | RAM command format: `auto` (default - the read-only probe decides, recorded as a `FORMAT CHECK` step), `compact`, `expanded` or `expanded-ae` to pin it |
| `includeCpi` | no | As in `/api/send-ota`: `true` (default) marks the delivered SMS with the OTA CPI IE |
| `delete_package` | no | If true, prepend a cascade DELETE on the package AID (`P2=0x80` - the load file with its modules and installed applications).  The delete step tolerates 'not found'; any other delete failure stops the chain |
| `preset_id`, `tar`, `spi1`, `spi2`, `kic`/`kid`, `cntr`, `kicKey`/`kidKey` | no | As in `/api/send-ota`; the ISD role TAR supplies the keyset when not named |

**Install-parameter composition (TS 102 226 §8.2.1.3.2.1; GPD_SPE_013 v1.1
Table 6-5).**  The memory quotas (`C7`/`C8`) and an EF-form STK part (the CA)
must share **one** System Specific Parameters `EF`; an EA-form STK part is a
sibling of `EF` (the reference TCA loader sends `C9 00 EF 00 EA …`).  A second
`EF` makes the card read the quotas and ignore the CA, so the server refuses
the combination with a `400` and names the fix:

- a caller-composed `install_params` that already carries an `EF` **plus** an
  EF-form `stk_params` → refused;
- `nv_quota`/`volatile_quota` next to a non-empty `install_params` → refused;
- with an empty `install_params` the server composes
  `C900 EF{C7?,C8?,CA}` (or `C900 EF{quotas} EA…` for the EA form).

The PWA composes the same single EF itself, so its RAM installs never hit the
refusal.

**Response (success):**

```json
{"success": true,
 "steps": [{"name": "INSTALL [for load]", "por_status": "por_ok", "por_sw": "9000",
            "por_type": "compact", "por_cntr": "000000011B", "sw": "9000",
            "bytes": 58, "segments": 1},
           {"name": "LOAD (1/9)", "por_status": "por_ok", "por_sw": "9000",
            "por_type": "compact", "sw": "9000", "bytes": 274, "segments": 3},
           {"name": "INSTALL [for install]", "por_status": "por_ok",
            "por_sw": "9000", "por_type": "compact", "sw": "9000",
            "bytes": 66, "segments": 1}],
 "final_cntr": "0000000004", "ram_format": "compact",
 "load_file_aid": "A000000003000000",
 "module_aid": "A000000003000000",
 "load_block_size": 240,
 "load_block_size_requested": null,
 "load_block_size_auto": true,
 "nv_before": 50646,
 "nv_after": 23400,
 "nv_delta": 27246}
```

`load_block_size` is the effective size used for the LOAD blocks (240 by
default), `load_block_size_requested` echoes an explicit `load_block_size`
(`null` = the default was used) and `load_block_size_auto` marks that default.
Each step reports the secured packet size `bytes` and the number of SMS
`segments` it took.  `sw` is the transport (ENVELOPE/GET RESPONSE) status
word; the RAM results are in `por_status` (the PoR verdict: `por_ok`,
`rc_cc_ds_failed`, `cntr_low`, … or `no_por` when the card sent none) and
`por_sw` (the remote command's own status word), with `por_type`/`por_cntr`/
`por_data`/`por_raw` for context.

`nv_before`/`nv_after` are the free non-volatile memory (bytes) read via
`GET DATA FF21` before and after the chain, `nv_delta` their difference (what
the operation consumed; negative = memory was released, e.g. a discarded
partial load).  The three fields are omitted when the card has no FF21
readout; on a failure the values still show what the failed attempt consumed.
The two FF21 reads consume counters like any accepted packet, so `final_cntr`
already includes them.

A step fails when the PoR is not `por_ok`, when `por_sw` is outside the
success set (`9000`, `61xx` more data, `62xx`/`63xx` warnings, `CAFE` GP
"more data"), or when response data arrived but could not be decoded.  The
failing step carries `por_error` (e.g. `remote SW 6700`), and the response
sets `success: false`, `failed_step` (the 1-based step index) and an `error`
such as `LOAD (1/9): remote SW 6700`.

**Response (failure):**

```json
{"success": false, "failed_step": 2,
 "steps": [{"name": "INSTALL [for load]", "por_status": "por_ok", "sw": "9000", "bytes": 58, "segments": 1},
           {"name": "LOAD (1/9)", "por_status": "rc_error", "sw": "9000", "por_sw": "6700",
            "por_error": "remote SW 6700", "bytes": 274, "segments": 3}],
 "final_cntr": "0000000002", "ram_format": "compact",
 "error": "LOAD (1/9): remote SW 6700"}
```

The `steps` array has one entry per GP command (plus the `FORMAT CHECK` probe
when `ram_format` was `auto`).  The response is not streamed - all steps run
server-side while the request holds the card lock; the live progress rides
`/api/status`'s `ram_progress`.

### `POST /api/ram-batch/start`, `GET /api/ram-batch/status`, `POST /api/ram-batch/stop`, `POST /api/ram-batch/clear`

The **batch operations**: one operation applied to card after card, for bulk
provisioning or repair.  The loop runs **in the server** (a worker thread), so
a page reload or a closed tab does not interrupt it; the state lives in memory
and a server restart ends the run.

- `POST /api/ram-batch/start` - `kind` selects the operation; the plan is card
  independent and is built once, so a bad `.cap`, AID or parameter combination
  is a `400` at Start, not a per-card failure.  The body carries the kind's
  fields **without** the per-card ones (`preset_id`, `tar`, `spi1`, `cntr`,
  `kic`/`kid`/`kicKey`/`kidKey`):
  - `kind: "install-cap"` (default) - the `/api/ram-install` payload (`cap_hex`,
    `sd_aid`, `install_params`, …) plus `delete_package` (a cascade DELETE on
    the package AID before the chain);
  - `kind: "reinstall-app"` - the `/api/ram-install-app` payload (`loadfile_aid`,
    `module_aid`, `instance_aid`, `privileges`, `install_params`,
    `stk_params`, `make_selectable`, `nv_quota`/`volatile_quota`): DELETE the
    applet instance (tolerating 'not found'), then `INSTALL [for install]`
    from the loaded package - no `.cap`, no LOAD;
  - `kind: "delete"` - `aid` (5-16 bytes of hex) and `target`: `"app"` deletes
    the object only (an applet instance, `P2=00`), `"package"` the object and
    its related objects (the load file with its modules and installed
    applications, `P2=80`).

  **No card is needed** - the worker waits for one.  `400` for a bad request
  or an unknown `kind`, `409` when another batch or a test run is active.  The
  response is the status payload below plus `ok: true`.
- `GET /api/ram-batch/status` - **card-free** (the panel polls it while an
  install holds the card lock):

```json
{"active": true, "phase": "waiting_card", "kind": "reinstall-app",
 "attempts": [{"ts": 1760000000.0, "iccid": "8970119000004600098",
               "preset": "Batch 0002", "result": "installed",
               "duration_ms": 4120, "final_cntr": "0000000005",
               "steps": [{"name": "DELETE (application)", "ok": true, "sw": "6101", "not_found": false},
                          {"name": "INSTALL [for install]", "ok": true, "sw": "6101", "not_found": false}],
               "steps_total": 2}],
 "attempts_total": 3,
 "current": null, "notice": "Card 8970119000004600098 reinstalled.",
 "counts": {"installed": 2, "already_absent": 0, "failed": 1, "skipped": 0},
 "started": 1760000000.0, "stopped": null, "progress": null}
```

  `phase` is `idle` (no run), `waiting_card`, `installing`, `stopping`,
  `stopped` or `finished`; `kind` names the operation (the panel's labels
  follow it: installed / deleted / reinstalled).  A failed attempt carries
  `failed_step` (e.g. `LOAD (3/7)`), `por_sw`, `por_status`, `por_error` and
  `sw` - or, when the chain died before a verdict (a lost card, a transport
  error), just `error`; a skipped one carries `reason` (a request/eligibility
  error: the operation never started).  An attempt also carries the last
  three steps (`steps`: `[{name, ok, sw, not_found}]` plus `steps_total`), so
  the panel shows the per-step verdicts for the short delete/reinstall chains;
  a delete step that answered 'not found' has `not_found: true` and `ok: true`.
  `current` names the card being processed (`{iccid, preset, started}`) and
  `progress` mirrors the RAM progress payload while a chain runs.  The report
  keeps the last 500 attempts (`attempts_total` counts them all).  The counts
  are `installed`, `already_absent` (a batch delete whose DELETE answered 'not
  found': the goal is met, reported separately from OK), `failed` and
  `skipped`.
- `POST /api/ram-batch/stop` - sets the stop flag: a running install finishes
  first (an interrupted LOAD chain can leave a half-loaded package), then the
  run ends.  It touches no card state, so it answers while the card lock is
  held; a repeated stop is idempotent, `409` when no run exists.  The response
  is the status payload plus `ok: true`.
- `POST /api/ram-batch/clear` - drops the finished run's report (`409` while a
  batch is active) → `{"ok": true}`.

**Per-card resolution.**  Every equipped card's **preset is matched by
ICCID**; the preset's ISD role TAR supplies the keyset, the keys and the
counter seed - the same rules as `/api/ram-install`.  A card with no readable
ICCID, no matching preset or no usable ISD keyset is recorded as `skipped`
with its reason and the batch keeps waiting.  A card is attempted **once per
run** (any outcome): re-equipping it is recorded as
`already processed in this batch (<outcome>)`, never installed again -
restart the batch to retry it.  Failures never stop the run and are never
retried automatically.  A delete step (the reinstall's instance delete, the
delete kind, the `delete_package` pre-step) tolerates 'not found'
(`6A88`/`6A82`); any other delete failure stops that card's chain and the
attempt reports the delete's reason.

`/api/status` carries a small summary in `ram_batch` - the detailed state
**minus the attempts list** (`{active, phase, kind, current, notice,
attempts_total, counts, started, stopped}`) so the panel's headline rides the
2 s status poll; the attempts report is fetched only when `attempts_total` (or
the active flag) changed.

### `POST /api/ram-install-app`

Run `INSTALL [for install]` (or `INSTALL [for make selectable]`) for an
**already loaded** package - no `.cap` and no LOAD, so the install parameters
can be iterated without re-loading.  `mode: "install"` **deletes the applet
instance first** (the operation does not delete the package, so the instance
is removed for a clean re-install), tolerating 'not found'; any other delete
failure stops the chain with its reason.  `mode: "make_selectable"` stays a
single APDU.  The APDU builders are shared with `/api/ram-install` (both
case 3 - no trailing `Le`, which the card's SCP80 layer counts as a phantom
command) and the step verdict/counter logic is the same (`final_cntr` is
returned on success **and** on failure).

**Request body:**

```json
{
  "mode": "install",
  "loadfile_aid": "F0414C46416101",
  "module_aid": "F0414C4641610101",
  "instance_aid": "",
  "privileges": "00",
  "install_params": "C900",
  "stk_params": "",
  "make_selectable": true,
  "ram_format": "auto",
  "includeCpi": true,
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
| `mode` | no | `install` (default) or `make_selectable` (GP Table 11-44; needs `instance_aid`) |
| `loadfile_aid` | cond | Package AID (required for `mode: "install"`) |
| `module_aid` | cond | Executable module / applet class AID (required for `mode: "install"`) |
| `instance_aid` | no | Application AID; empty → the module AID |
| `privileges` | no | Hex privileges value (1 or 3 bytes), default `00` |
| `install_params` / `stk_params` | no | As in `/api/ram-install`: the `C9` field (with any extra TLVs) and the STK part; an EF-form STK part must not follow a caller-composed `EF` (the quotas and the CA share one `EF`) |
| `nv_quota` / `volatile_quota` | no | As in `/api/ram-install`: composed into the install `EF` only when `install_params` is empty |
| `ram_format` | no | As in `/api/ram-install`: `auto` (default - a read-only probe step decides), `compact`, `expanded` or `expanded-ae` |
| `includeCpi` | no | As in `/api/ram-install`: `true` (default) marks the delivered SMS with the OTA CPI IE |

**Response:** `{"success": bool, "steps": [...], "final_cntr": "...",
"ram_format": "...", "error": "...", "failed_step": N}` plus, for `mode:
"install"`, `load_file_aid`, `module_aid` and `instance_aid` - the same step
records as `/api/ram-install`.  A missing `cntr` is seeded from the preset's
keyset; an explicit value wins but never lowers the stored counter.

### `POST /api/cap-compat`

Run the **CAP compatibility test**: the format check, `INSTALL [for load]` and
the LOAD blocks only up to the block that completes the **Import** component
(the point where the JCRE verifies the import list, JC VM spec §4.5.2), then
stop - no last-block flag and no `INSTALL [for install]`, so nothing is
committed and nothing has to be deleted afterwards.  The verdict
(`imports_ok`) covers the **LOAD/import gate only**; the link gate is what a
real install checks at `INSTALL [for install]`.

**Request body:** as `/api/ram-install` (`cap_hex`, the SCP80 fields, optional
`load_block_size`, `ram_format` and `includeCpi`) - a missing `cntr` is seeded
from the preset's keyset - plus:

| Field | Req | Description |
|---|---|---|
| `probe_imports` | no | `{"all": "x.y"}` or `{aid_hex: "x.y"}`: rewrite the Import component's requested versions (diagnostic) |
| `probe_additions` | no | `[{"aid": hex, "version": "x.y"}]`: append synthetic imports (card-capability queries; an AID already present becomes a version override) |

**Response:** `{"success": bool, "imports_ok": bool, "steps": [...],
"boundary_block": N, "total_blocks": M, "load_file_aid": "...",
"module_aid": "...", "ram_format": "...", "probe_imports": [...],
"probe_unmatched": [...], "final_cntr": "...", "load_block_size": N,
"load_block_size_requested": …, "load_block_size_auto": bool, "error": "...",
"failed_step": N}`.  On a rejection `error` reads
`the card rejected the import list` when the refused ENVELOPE carries no
step-level detail (the verdict is still `imports_ok: false`, `success:
false`).

`probe_imports` lists the entries changed or appended (`{"aid": "...", "from":
"1.3", "to": "0.0"}`; `from: null` = an appended synthetic entry).
`probe_unmatched` lists override AIDs that matched no Import entry - the CAP
does not import them, so the card is never asked - as `[{"aid": "...",
"version": "0.0"}]` with the version the probe requested.

### `GET /api/test/scripts` and the script store

Test scripts live in a server-side store (`test_scripts.json`, in the server's
data directory; `--test-scripts PATH`) - the runner is server-side, so the
store is the source of truth and every browser manages the same set.  The
store validates with the same engine the runner uses, so a stored script never
fails at run start for a validation reason.

```json
{"path": "…/test_scripts.json", "count": 1, "version": 2,
 "scripts": [{"id": "…", "name": "applet RFM update", "suite_id": "…",
              "require_adm": false, "steps": [...],
              "created": 1690000000.0, "updated": 1690000000.0}]}
```

**Schema v2: every script belongs to exactly one test suite** (`suite_id`; the
suite is the root object) and may declare `require_adm` (the run verifies the
ADM before starting).  A v1 entry loads with `suite_id: null` and is attached
to an auto-created "Imported scripts" suite at startup.

A script mutation is card-free (the editor stays usable while a run owns the
card).  Errors: `400` with the store's message for a validation refusal, `404`
for an unknown id.

- `POST /api/test/scripts` - `{name, steps, suite_id, require_adm?}` or
  `{script: {...}}`.  A `suite_id` naming an existing suite is required (the
  script joins that suite's member list as a `member` with `on_fail: stop`);
  returns `{"ok": true, "script": {…}}`.
- `POST /api/test/scripts/update` - `{id, script}` replaces
  name/steps/require_adm; a `suite_id` that differs from the current one is a
  **move** (both stores are updated in the one request).  Returns
  `{"ok": true, "script": {…}}`, plus `"moved": true` for a move; an unknown id
  is `404`.
- `POST /api/test/scripts/delete` - `{id}` → `{"ok": true, "removed": true}`.
  The script is detached from its suite first, so no dangling id is left.
- `POST /api/test/scripts/move` - `{id, suite_id}` moves a script to another
  suite (the target appends it as a member, keeping the `on_fail` policy);
  returns `{"ok": true, "script": {…}}`.
- `POST /api/test/scripts/copy` - `{id, suite_id, name?}` clones a script into
  the target (default name `<name> (copy)`); returns
  `{"ok": true, "script": {…}}`.
- `POST /api/test/scripts/import` - `{scripts: [...], mode: "merge"|"replace",
  suite_id?}` (the old localStorage export shape too): invalid entries are
  reported in `errors` instead of failing the whole import, `suite_id`
  attaches the imported scripts to that suite, and a misspelled `mode` is a
  `400`.  A `replace` is **refused while any suite still lists scripts** (the
  wipe would leave dangling references; delete those scripts first, or import
  with `merge`).  Returns the store's result (`added`, `skipped`, `errors`)
  plus `ok: true`.

### `GET /api/test/suites` and the suite store

Test suites live next to the scripts (`test_suites.json`, in the same data
directory; `--test-suites PATH`): a suite is the ordered list of its scripts -
the optional `setup` first, the `member` scripts, the optional `teardown`
last - plus the per-member `on_fail` policy, the suite-level `require_adm` and
the optional **`start_reset`** (reset + fully re-initialize the card before the
run).

```json
{"path": "…/test_suites.json", "count": 1, "version": 2,
 "suites": [{"id": "…", "name": "alfa regression", "require_adm": true,
             "start_reset": false,
             "scripts": [{"script_id": "…", "role": "setup", "on_fail": "stop"},
                         {"script_id": "…", "role": "member", "on_fail": "stop"},
                         {"script_id": "…", "role": "teardown", "on_fail": "stop"}],
             "created": 1690000000.0, "updated": 1690000000.0}]}
```

The store normalises the order (setup first, teardown last, at most one of
each; a script appears once per suite) and validates `on_fail` as `stop`
(default) or `continue` - "which failures cascade" is a visible per-suite
decision.  Every member reference must name a stored script and must not
already belong to another suite (one owner per script; ownership changes
through the move/copy endpoints).

- `POST /api/test/suites` - `{name, require_adm?, start_reset?, scripts?}` or
  `{suite: {...}}` → `{"ok": true, "suite": {…}}`.
- `POST /api/test/suites/update` - `{id, suite}` replaces
  name/require_adm/start_reset/scripts.  The **member set** changes through
  the script endpoints, not here; the one repair exception is **pruning a
  dangling reference** (a member whose script is already gone).  Returns
  `{"ok": true, "suite": {…}}`; unknown ids are `404`.
- `POST /api/test/suites/delete` - `{id}` → `{"ok": true, "removed": true}`;
  refused (`400`) while the suite still holds scripts.
- `POST /api/test/suites/import` - `{suites: [...], scripts: [...], mode:
  "merge"|"replace"}`: the scripts are imported first **always with `merge`**
  (a script replace would wipe the other suites' scripts; the mode applies to
  the suite store), ids preserved, a collision gets a fresh uuid and the
  suites' references are remapped; then the suites are imported (`replace`
  wipes the suite store first); an unresolvable reference is reported in
  `errors` and the suite is skipped.  A `replace` with nothing importable is
  refused instead of wiping the store.  After the import the ownership
  invariant is reconciled: a script the imported suites list adopts its
  listing suite, a script listed by several suites keeps its explicit owner
  and the other listings are pruned, and a script no suite lists any more is
  adopted into "Imported scripts" instead of becoming invisible.

The import always **merges**: an id already in the store gets a fresh
uuid/ suite id, so importing a bundle **into the store it came from duplicates
the suite and its scripts** - the round-trip is not idempotent.  Restore a
bundle into a fresh store, or wipe the target first (`mode: "replace"` for the
suites).

### `POST /api/test/run`

Runs a **test script** - an ordered list of actions and proactive-command
expectations, executed server-side on the equipped card - or a **suite**
(ordered scripts, one card session).  While a run is active the card is owned
by it: other card endpoints answer `409`, background STATUS polling is
suspended and `/api/apdu`'s automatic chain is off (the script drives the
card).

```json
{"script": {"name": "STK menu browsing", "suite_id": "…", "require_adm": false,
            "steps": [
   {"type": "action", "kind": "menu-select", "params": {"item_id": 128},
    "check": {"sw": "91??"}},
   {"type": "expect", "command": "SELECT ITEM",
    "checks": [{"kind": "item", "id": 1, "text": "test"}],
    "respond": {"result": "ok", "item_id": 1}},
   {"type": "expect", "command": "DISPLAY TEXT",
    "checks": [{"kind": "text", "value": "hello"}],
    "respond": {"result": "ok"}}
 ]},
 "script_id": "…", "preset_id": "…", "iterations": 1}
```

The PWA sends the current script inline (a run always uses what the editor
shows) plus `script_id` when the script is stored; `script_id` alone resolves
the script from the store.  The PWA sends `preset_id` (the stored preset's id)
instead of an inline `preset`; the server resolves it from the preset store,
so a run always uses the stored keysets, TARs and counters.  The inline
`preset` form stays for external callers.

The body is one of:

- `{script, script_id?, preset_id?|preset, iterations?}` - one script (the
  server validates and runs it); `require_adm` rides **inside** the script
  object;
- `{suite_id, preset_id?, iterations?}` - a suite: its scripts in order, one
  card session, no resume.

**Action steps** (`type: "action"`): `kind` is
- `envelope` (`event`, `data`, optional `src` - the device-identities source
  override);
- `event` (`event` name or hex + `fields` for the four server-built events
  0x03/0x0B/0x12/0x1D, or the PWA form's built `data` hex for any other event
  - validated as hex and against the one-ENVELOPE budget at load; optional
  `src`);
- `menu-select` (`item_id` 1-255, or `text` + `mode` `exact`/`contains`
  (case-insensitive by default; `case_sensitive: true` forces an exact-case
  match) - resolved against the card's cached SET UP MENU, which is refreshed
  whenever the card sends the menu);
- `file-write` (`path`, `data`, `mode` `auto`/`binary`/`record`, `record`);
- `file-read` (same, verifies `check.data`);
- `apdu` (raw transport, no auto-handler);
- `scp80` (`apdu` or `sp`, selected by the optional `source` field when both
  are present; optional `kvn` (keyset number 1-15), `tar`/`spi1`/`spi2`
  overrides and `format` (`compact` default / `expanded` / `expanded-ae` - the
  TS 102 226 §5.2.1 Command Scripting template around the C-APDU).  KIc/KID
  and the counter always come from the resolved preset, which must match the
  equipped card and be complete);
- `status` (`attempts`, `interval_ms` - when `attempts > 1` the default SW
  check is the mask `91??`, i.e. poll until the card announces a command);
- `proactive-drain` (poll STATUS and consume every announced command:
  `respond`/`first` TERMINAL RESPONSE results, `attempts`/`interval_ms` bound,
  optional `require` `{command, qualifier?}` asserting at least one drained
  command; the final SW is the step's `check.sw`, so a still-pending card
  fails and hands the command to the next step - an empty drain passes unless
  `require` was given).  It is exempt from the runner's
  unexpected-pending-command guard, so it also consumes a command a previous
  step left pending (`91XX`).

`check` is `{"sw": ..., "data": ...}` (exact or `{"mode": "mask", "value":
"91??"}`, `?` = per-nibble wildcard) plus `"por"` for SCP80 -
`"none"`/`"ok"`/`"any"` or an object `{"status"?, "sw"?, "data"?}`
asserting the decoded PoR of this exchange (inline or SEND SHORT MESSAGE; at
least one field).  `on_fail` is `error` (terminates the script) or `warning`
(continues) and applies to every step failure - including an expectation that
finds no pending command (the tolerant consume-if-pending form) and a
card/transport error raised during the step (reported as a failed `Error`
check at the step's level).

**Expectation steps** (`type: "expect"`) require a command pending from the
previous step (`91XX`); they never poll - a `9000` response means no command
and fails the step at its `on_fail` level (TS 102 221 §7.4.2.1 / TS 102 223
§6.3; a `status` action with `attempts > 1` polls when the card delivers on
poll).  `command` is a name or type code; `checks` may be `text`
(contains/exact), `alpha` (the command's Alpha identifier `05`/`85`,
contains/exact - distinct from the `8D` text string), `item` (`id`/`text` for
SELECT ITEM / SET UP MENU), `raw` (mask), `por` (a SEND SHORT MESSAGE carrying
the PoR of the preceding `scp80` step - PoR-in-submit, SPI2 bit `20`:
`status` (the response status name, `ok` = `por_ok`), `sw` (the R-APDU status
word, exact/mask) and `data` (the R-APDU response data, exact/mask)), `sms`
(the SEND SHORT MESSAGE TPDU: `da` digits with whitespace skipped and a
leading `+` ignored, `pid`/`dcs`/`ud` hex exact/mask, `udl` decimal; SMS-SUBMIT
only) or `files` (the File List of a REFRESH, TS 102 223 §8.18; `files` is a
list of path hex strings, matched order-insensitively); `respond` is the
TERMINAL RESPONSE in the TS 102 223 §6.8.0 object order (`result` - a script
alias or a two-digit hex value from the full §8.12.0 list; `additional_info` -
the general result's additional-information hex byte, sent as
`83 02 <result> <additional_info>`, default `00`; `text`+`dcs` for GET
INKEY/GET INPUT, `dcs` `00` packed / `04` 8-bit / `08` UCS2; `item_id`; extra
`raw` TLVs appended after the standard objects).  The aliases (`ok`=00,
`partial`=01, `missing`=02, `modified`=07, `cancel`=10, `back`=11,
`timeout`=12, `refused`=22, `not_understood`=32, plus the compatibility-only
`no_response`=12) follow the spec.  The terminal's own objects ride along
unless `raw` overrides them: a successful PROVIDE LOCAL INFORMATION answer
carries the PLI dictionary entry (or the host clock for the date/time
qualifier) and a POLL INTERVAL answer the current-interval echo.

**Suites.**  With `start_reset` the card is **physically reset and fully
re-initialized** first (ICCID, network state, TERMINAL PROFILE, the equip
drain); the run's preset must still match the card's ICCID afterwards (a
mismatch refuses the start) and a required ADM is verified against the **new**
card session.  Then the ADM prerequisite (suite- or member-level
`require_adm`: the matched preset's key is verified **once**; a failure
refuses the start, is not retried automatically and requires a manual verify),
setup, members (per-member `on_fail`), teardown.  The initialization consumes
the card's initial proactive commands (they stay in the proactive log), so a
script cannot catch the first command after the reset.  **The teardown runs on
every stop** as long as the card is in the reader; a card reset (session
change) stops the suite, skips the remaining members and is flagged in the
report.

`iterations` (optional, 1-1,000,000,000, default 1) repeats the script or the
whole suite: successful iterations are discarded (only the counter and the
timing are kept; the per-step log is not retained), the first iteration that
ends in an **error** stops the loop and keeps that iteration's report, a
manual stop ends it after the current iteration, and a card session change
between iterations is an error.

**Response and errors.**  The response is the initial run snapshot
(`running: true`, the step list, `load`).  Poll `/api/test/status`.

| Situation | Status |
|---|---|
| a run or a batch is already active | `409 {"error": …}` |
| an unknown `script_id` | `404 {"error": …}` |
| a script/suite validation refusal, or a bad `iterations` | `400 {"error": …}` |
| the ADM gate fails | `400 {"error": …, "adm": {required, verified, …}}` (the `adm` block also carries `attempts_left`/`blocked`/`result` when the verify ran) |
| the worker cannot start | `500` |

The **server persists** every counter an SCP80 step consumed into the preset
store as the run proceeds; the snapshot's `scp80_counter`/`scp80_counters`
carry the latest values (the PWA only syncs its local copy - nothing is
written back).

A run that ends (an error, a stop, an unexpected pending command) with a
command still announced **drains it with `ok` (0x00)** - fetching while the
TERMINAL RESPONSE answers `91XX` (bounded); a cancel is never sent
automatically (it tells the card the user aborted the session and can push
assertive applications onto an error path).  The cleanup is reported as a
`cleanup` step; a script that wants the refusal path answers explicitly (an
expectation's `respond`, or the `proactive-drain` action's `first`).

### `GET /api/test/status`

The current (or last) run, a JSON-safe copy of the whole state.  Card-free.

```json
{"running": true, "kind": "script", "name": "STK menu browsing",
 "status": null, "session": 3, "index": 1, "total": 3,
 "preset": "lab card", "script_id": null,
 "scp80_counter": null, "scp80_counters": {},
 "started": 1760000000.0, "finished": null, "error": null, "stop": false,
 "stuck": false, "heartbeat": 1760000000.0, "suite": null,
 "log": [], "log_prefix": "", "adm": {"required": false, "verified": false},
 "load": {"enabled": false, "suite": false, "iterations": null, "iteration": 0,
          "done": 0, "failed_iteration": null, "started": null,
          "finished": null, "elapsed_ms": null, "avg_ms": null},
 "steps": [{"index": 0, "type": "action", "label": "ENVELOPE(Menu Selection)",
            "status": "ok", "sw": "9103", "sent": "80C2000009...",
            "checks": [{"label": "SW", "ok": true, "expected": "91??",
                        "actual": "9103", "level": "error"}], "ms": 4}]}
```

| Field | Meaning |
|---|---|
| `kind` | `script` (a single-script run) or `suite` (the suite runner) |
| `status` | `ok`/`warning`/`error`/`stopped` once the run finishes (`null` while running) |
| `index` / `total` | the current step and the step count |
| `steps` | one record per step: `index`, `type`, `kind`, `label`, `status`, `sw`, `checks` (each `label`/`ok`/`expected`/`actual`/`level`, plus `detail` when the check explains itself), `ms`, and - when it applies - `sent`, `data`, `note`, `por`, `sms`, `counter`, `command`, `drained` |
| `scp80_counter` / `scp80_counters` | the latest consumed counter / the per-keyset map |
| `preset` | the run's preset name (or its ICCID), `null` without one |
| `script_id` | the stored script's id when the run resolved one |
| `suite` | `null` for a single script; for a suite the member records (`status`, `steps`, `note`, `log` - the semantic run-log excerpt) plus `summary` (ok/warning/error/skipped counts, `wall_ms`, `stopped`, `session_changed`, the SCP80 `counters_before`/`counters_after` per keyset), the `start_reset` flag, the `reset` result (ICCID + ms) and the run's `adm` state |
| `log`/`log_prefix` | the run log tail and the current member's prefix |
| `adm` | the ADM gate state (`{required, verified, …}`) |
| `load` | the load-test block: `enabled`, `suite`, `iterations`, `iteration`, `done`, `failed_iteration`, `started`, `finished`, `elapsed_ms`, `avg_ms` (a normal run has `enabled: false` and `iterations: null`) |
| `stuck`/`heartbeat` | the stuck-card watchdog: `heartbeat` is the last runner progress mark |

A card that stops answering is not waited on silently: after 30 s without
runner progress the watchdog sets `stuck: true`, `stop` and the run's error
(`the card stopped responding (step N, no answer for N s)`) and tries to
cancel the blocked transmit by disconnecting the PC/SC link on a background
thread (pcsc-lite serializes the disconnect behind the pending transmit, so
the call returns only when the driver's card timeout fires).  Once the step
returns, the run finalises as `error` - a stuck card is a failure, not a user
stop - and the next equip rebuilds the link (the transport is marked stale).

A successful load test leaves `suite` `null` (no iteration is tracked); a
failing one keeps the failing iteration's suite state.

### `POST /api/test/stop`

Requests a stop (`{"stop": true}` is set on the run); the runner finishes the
current step, drains any pending proactive command with an **ok** (0x00)
TERMINAL RESPONSE (the bounded cleanup above - a cancel is never sent
automatically) and reports the run as `stopped`.  Card-free (it only sets a
flag), so it answers while the run holds the card.  The response is the run
snapshot.

### `POST /api/test/clear`

Clears a finished run report and returns the fresh empty snapshot; `409
{"error": "test script is running"}` while a run is active.  Card-free.  The
run state lives in the server until this call - there is no history.

### `GET /api/menu`

The STK SET UP MENU captured from the card's TERMINAL PROFILE answers (or the
latest one the card sent), with the menu state.  Empty `{"items": []}` if the
card sent no menu.  Card-free.

```json
{"command_number": 1, "title": "Alfa Mobile", "active": false,
 "items": [{"id": 128, "text": "Настройки/Settings"}]}
```

The Alpha identifier (the `title`) is mandatory per TS 102 223 §6.6.7, but a
card that omits it must still render: the key is then absent (the PWA supplies
its translated label) - the server never fabricates an English title.

### `POST /api/menu-select`

Sends an `ENVELOPE(Menu Selection)` for the chosen item, then consumes the
card's answer (DISPLAY TEXT, SELECT ITEM, SET UP MENU, GET INKEY/GET INPUT
pause the chain; the rest is answered generically).  A paused command from an
earlier FETCH is answered first.

By id:

```json
{"item_id": 128}
```

By text (the item ids vary with the applet's install parameters):

```json
{"item_name": "coffe", "mode": "contains", "case_sensitive": false}
```

`text` is accepted as an alias of `item_name`.  `mode` is `exact` (default) or
`contains` and is **validated** (anything else is a `400`); the match is
case-insensitive unless `case_sensitive` is true, and the flag must be a
boolean (the literal `"true"`/`"false"`/`"0"`/`"1"` strings are accepted).  A
miss or an ambiguous match is a `400` naming the menu's items; an `item_id`
outside one octet is a `400` too.

```json
{"type": "display_text", "text": "Hello", "sw": "9122"}
```

or

```json
{"type": "select_item", "items": [{"id": 1, "text": "Sub-menu"}], "sw": "9122"}
```

`type` is `done` when nothing was paused (the response carries only `sw`).  A
paused user-interactive command is also the `/api/stk-status` `pending_data`;
if it is not answered within `--menu-timeout` seconds (default 60, `0`
disables) the server's watchdog sends the `timeout` result itself.

### `POST /api/menu-respond`

Sends the TERMINAL RESPONSE for the paused command.  The chain continues if
the card answers `91XX`.

```json
{"result": "ok", "item_id": 1}
```

`item_id` is one octet (`0`-`255`, like `/api/menu-select`); a larger or
negative value is a `400` rather than a silently truncated item number.  For a
GET INKEY / GET INPUT command `text` carries the answer (validated against the
command's Response Length and charset, then coded per §6.8.5).

| `result` | TERMINAL RESPONSE code | Meaning |
|---|---|---|
| `ok` | `0x00` | Command performed successfully |
| `cancel` | `0x10` | Proactive session terminated by the user |
| `back` | `0x11` | Backward move in the proactive session requested by the user |
| `timeout` | `0x12` | No response from the user |
| `help` | `0x13` | Help information required by the user |

An unknown `result` name falls back to `ok` (the legacy `MENU_RESULTS` default).
The response is `{"sw": "…", "type": …}` with the newly paused command's
`text`/`items` (or `type: "done"` when the card went idle).  No pending
command is a `400`.

### `GET /api/stk-status`

The STK dialogue state and the paused command.  Card-free.

```json
{"active": true, "pending": true, "pending_type": "select_item",
 "pending_data": {"type": "select_item", "cmd_num": 3, "cmd_type": 36,
                  "items": [{"id": 1, "text": "Sub-menu"}], "raw": "D0…"}}
```

`active` is the menu dialogue state; `pending_data` is the paused command's
JSON-safe dict (the PWA renders the prompt/items) and is absent when nothing
is pending.

### `GET /api/events`

The event bytes of the card's SET UP EVENT LIST (TS 102 223 §6.4.16), as a
JSON array of integers, or `[]` when none was received.  Card-free.

### `POST /api/event-send`

Sends an `ENVELOPE(Event Download)`.

```json
{"event_type": 4, "event_data": "01A0"}
```

`event_type` is required (the SET UP EVENT LIST event byte).  The accepted
forms are a JSON number (the documented form), a string of decimal digits
(`"12"` is event `0x0C`), the script hex form (`"0x1D"` / `"1D"`) and the
server's event name keywords (`"location_status"`, `"access_tech"`,
`"network_rejection"`, `"data_connection"`).  `event_data` is optional hex for
events that carry data.

The four events the server models (`0x03` Location status, `0x0B` Access
technology change, `0x12` Network rejection, `0x1D` Data connection status
change) may instead carry their objects as `fields` (validated server-side):

```json
{"event_type": 3, "fields": {"status": 0}}
```

When both `fields` and `event_data` are present, **`fields` wins** (the raw hex
is ignored).  `event_src` overrides the device-identities source (`82`
terminal default / `83` network, TS 102 223 §8.7):

```json
{"event_type": 4, "event_data": "01A0", "event_src": "83"}
```

Response:

```json
{"sw": "9000", "data": "…", "net_state": {…}}
```

`data` is present only when the card answered with data.  When the session has
a network-state monitor (the card's EF.ICCID was readable at equip), a
**Location status event only** patches it: the event sets the simulated
service state, EF.IMSI is re-read (a multi-IMSI applet may switch it) and the
updated monitor rides the response as `net_state` - the same block
`GET /api/net-state` serves.  Any other event leaves the monitor (and EF.IMSI)
alone: the response still carries the cached `net_state`, no card read is
issued.

For **Poll Interval Negotiation** (event `0x1C`, TS 102 223 §7.5.22) the
proposal rides in `event_data` as the Duration TLV `04 02 <unit> <interval>`
(unit `00` minutes / `01` seconds / `02` tenths of seconds) and the UICC's
response is decoded into `negotiation`:

```json
{"sw": "9000", "data": "1101020402011E",
 "negotiation": {"result": 2, "result_name": "modified",
                 "unit": 1, "interval": 30, "seconds": 30}}
```

`result` is `00` accepted / `01` rejected / `02` modified (§8.97); no response
data means *accepted*.  A *modified* answer sets the background poll interval
and clears the card's POLLING OFF; a *rejected* answer may carry the closest
acceptable duration.  A `91XX` answer is drained by the generic chain.

Channel status (event `0x0A`, TS 102 223 §8.56) carries the Channel status TLV
`B8 02 <status> <info>`, where the status byte is the channel id (1-7) OR-ed
with the state bits (`0x00` link not established / `0x40` TCP LISTEN / `0x80`
link established) and the info byte is `00` (no further info) or `05` (link
dropped).  The server also sends this event automatically when a BIP link
drops outside a proactive command and the card subscribed to `0x0A`.

### `GET /api/terminal-profile`

The TERMINAL PROFILE currently in effect and the startup default (runtime
changes are in-memory only).  Card-free.

```json
{"profile": "FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003",
 "bytes": 33,
 "cli_default": "FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003"}
```

### `POST /api/terminal-profile`

Sets the TERMINAL PROFILE at runtime (in memory) and re-sends it to the card,
starting a new CAT session: a pending command is answered first, the menu and
event-list state are cleared, the proactive log keeps its history with a
boundary marker, and the profile's answers repopulate the caches.  With a new
profile, or `{}` to re-send the current one:

```json
{"profile": "FFFFFFFF7F1F007FFF00001F230811060700"}
```

The hex must be even-length, non-empty and at most 255 bytes (one APDU's Lc);
a bad value is a `400`, no card a `503`.  The response:

```json
{"ok": true, "profile": "FFFF…", "menu": true, "events": [4, 5]}
```

`menu` tells whether a menu was captured again, `events` lists the new event
bytes.

### `POST /api/rescue`

Recovers a stuck CAT session by re-sending the current profile - exactly
`POST /api/terminal-profile` with `{}`.  The response is its shape
(`{ok, profile, menu, events}`).

### `POST /api/status-poll`

Manually sends `STATUS` (F2) and, if the card answers `91XX`, runs the
proactive chain (FETCH → TERMINAL RESPONSE) until it settles.  The manual poll
is bounded (10 answers) like the background one, runs whatever the operator's
poll switch says, and is unaffected by the card's POLLING OFF wish (the button
is an explicit action).

```json
{"sw": "9000", "proactive": true}
```

`proactive` is present only when the card announced a command.  Body: `{}`.

### `GET /api/poll-status`

The background STATUS polling state.  Polling is enabled by default (a CAT
terminal polls during idle, TS 102 221 §14.6.2; `--poll-interval 0` disables
it at startup) and `enabled` is the operator's switch.  `card_disabled` is
true after the card sent POLLING OFF (TS 102 223 §6.4.14): proactive polling
stays suspended until a POLL INTERVAL arrives, independent of `enabled`.
`interval` is the adopted interval (the card's own POLL INTERVAL may have
replaced the startup default).  Card-free.

```json
{"enabled": true, "interval": 30, "card_disabled": false}
```

### `POST /api/poll-toggle`

Turns background STATUS polling on or off.

```json
{"enabled": true}
```

Returns the new state (`{"enabled": …, "interval": …, "card_disabled": …}`);
enabling is refused (stays off) when no interval is configured
(`--poll-interval 0` is the operator's "no polling" setup and the toggle must
not override it), and while the card has disabled polling the response adds a
`warning` explaining that the timer stays suspended until the card sends a new
POLL INTERVAL.

### `GET /api/proactive-log`

The last 50 proactive commands fetched during CAT sessions, newest first.
Card-free.  Each entry is our log record:

```json
[{"id": 7, "type_hex": "25", "type_name": "SET UP MENU", "elapsed": 3.2,
  "bytes": 97, "raw": "D0…", "cmd_decoded": {…}, "tr_hex": "…", "tr_sw": "9000"}]
```

`cmd_decoded` is the decoded command (the `_decode_cmd` equivalent: display
text/select item/menu/BIP/timer/PLI qualifiers/SEND SHORT MESSAGE), and the
`tr_*` fields describe the TERMINAL RESPONSE sent.  A TERMINAL PROFILE re-send
appends a boundary-marker entry (the PWA renders it as a divider).

### `GET /api/pli-qualifiers`

The PROVIDE LOCAL INFORMATION qualifier codes with their names.  Card-free.

```json
[{"code": "00", "name": "Location Information"}, {"code": "0A", "name": "Battery Charge Level"}]
```

### `GET /api/pli-dict` and `POST /api/pli-dict`

`GET` returns the effective PLI data dictionary as a qualifier-code map: the
operator's override, else the built-in default (the qualifiers whose objects
the pinned ETSI specs define - location as the test network 001/01 with test
LAC 0001 and Cell ID 0001 (the 7-byte PLI form per TS 31.111 §8.19.1; the
5-byte form without the Cell ID belongs to the Network Rejection event
download), the test IMEI/IMEISV, language, access technology, search mode,
battery).  The date/time qualifier stays empty - the host clock answers it
while no override is set.  One entry per known qualifier; card-free.

```json
{"00": "130700F11000010001", "0A": "630104", "03": ""}
```

`POST` updates the entries: the body is a map of qualifier code to hex value.
A non-empty value becomes an override; an **empty value clears the override**,
so the built-in default (if any) applies again.  The response is the updated
effective dictionary plus, when any field was not saved, the lists that name
them: `invalid` (a non-string value, a malformed key, a non-hex or odd-length
value) and `unknown` (a well-formed code outside the qualifier list); both
keys are omitted when everything was applied.

```json
{"00": "130500F1100000", "0A": "630102", "invalid": ["0G"], "unknown": ["18"]}
```

### eSIM / LPA (local ES10 operations)

These endpoints work only when the equipped card is an eUICC (SGP.22/32);
otherwise they answer `400 {"error": "The equipped card is not an eUICC"}`.
They are our own ES10 implementation (`simple_server/esim.py`: the SGP.22 data
objects and the card-level flow) - no lpac, no SM-DP+ contact, no profile
downloads and no notification processing.  All run under the card lock (they
select the ISD-R and transceive); `GET /api/status` reports `euicc` and `eid`
for the PWA.

- `GET /api/esim/chip` - `{"eid": "8908…", "info1": {…}, "info2": {…},
  "addresses": {"default_dp_address": …, "root_ds_address": …},
  "rat": [{…}], "errors": {"<part>": "<reason>"}}` (a part the card does not
  support is reported in `errors` instead of failing the whole request).
  The EUICCInfo TLVs are requested raw and decoded per SGP.22 v2.6 §5.7.8,
  `rat` comes from the ES10b GetRat command (§5.7.22) and undecoded TLVs are
  preserved in a `raw_tlvs` map:

  ```json
  {"eid": "89086030202200000026000024920451",
   "info1": {"svn": "2.2.2",
             "euicc_ci_pki_list_for_verification": ["8137…FB"],
             "euicc_ci_pki_list_for_signing": ["8137…FB"]},
   "info2": {"profile_version": "2.3.1", "svn": "2.2.2",
             "euicc_firmware_ver": "4.2.0",
             "ext_card_resource": {"installed_application": 0,
                                   "free_non_volatile_memory": 439084,
                                   "free_volatile_memory": 9798},
             "uicc_capability": ["usimSupport", "isimSupport", "…"],
             "ts102241_version": "9.2.0",
             "globalplatform_version": "2.3.0",
             "rsp_capability": ["additionalProfile", "testProfileSupport"],
             "euicc_category": "other",
             "forbidden_profile_policy_rules": ["ppr1"],
             "pp_version": "1.0.0",
             "ss_acreditation_number": "ED-ZI-UP-0826"},
   "addresses": {"default_dp_address": null,
                 "root_ds_address": "testrootsmds.gsma.com"},
   "rat": [{"ppr_ids": ["ppr1", "ppr2"],
            "allowed_operators": [{"plmn": "EEEEEE", "gid1": null, "gid2": null}],
            "ppr_flags": ["consentRequired"]}],
   "errors": {}}
  ```

  `uicc_capability`, `rsp_capability`, `forbidden_profile_policy_rules` and
  `ppr_flags` are ASN.1 BIT STRINGs decoded to the names of the set bits (the
  first content octet is the unused-bit count, bits are MSB-first);
  `euicc_category` accepts both the implicit (`0x8B`) and explicit (`0xAB`)
  tag encodings; `pp_version` (`0x04`) and `ss_acreditation_number` (`0x0C`)
  are the bare, untagged SGP.22 types; `tre_properties`,
  `tre_product_reference`, `additional_euicc_profile_package_versions` and
  `certification_data_object` are decoded when a card sends them.
- `GET /api/esim/profiles` - `{"profiles": [{"iccid": "8970…",
  "isdp_aid": "A000…", "state": "enabled"|"disabled", "nickname": …,
  "provider": …, "name": …, "class": "test"|"provisioning"|"operational",
  "owner": "250-99", "icon_type": "png"|"jpg", "icon": "89504E47…",
  "icon_size": 1234}], "error": null}`.  `icon` is the SGP.22 icon data
  (tag `0x94`) as hex and `icon_size` its byte count, so a client can show
  the profile image (`data:image/<type>;base64,…`); both are `null` when the
  card sent no icon.  A `profileInfoListError` answer is a result, not an
  HTTP failure: the list stays empty and `error` carries its short text.
- `GET /api/esim/notifications` - `{"notifications": [{"seq_number": 3,
  "operations": ["enable"], "address": "smdp.example.org",
  "iccid": "8970…"}], "error": null}`.  `operations` are the decoded
  `ProfileMgmtOperation` flags (`install`, `enable`, `disable`, `delete`, in
  that order) - one or more per notification.
- `POST /api/esim/profile` - `{"action": "enable"|"disable", "iccid"?: …,
  "isdp_aid"?: …, "refresh"?: true}` (one identifier required; the AID wins
  when both are given).  The request is validated before the card is touched
  (`400` for a bad action, an unknown profile identifier shape or a
  non-boolean `refresh` - the literal `"true"`/`"false"`/`"1"`/`"0"` strings
  are accepted).  With the refresh flag set the ISD-R returns OK *before* the
  REFRESH (SGP.22 v2.6 §3.2.1 step 6 for Enable, §3.2.2 step 5 for Disable)
  and the switch completes upon the TERMINAL RESPONSE or the following RESET
  (§3.2.1 step 8, §3.2.2 step 7): a `91xx` answer is that OK, so the server
  answers the REFRESH proactive command (logged in `/api/proactive-log`),
  **never retries the STORE DATA** (the mid-switch card answers `6985`) and
  re-initializes the card like an equip (physical reset, re-read
  ICCID/network state, new `card_session`).  It then re-reads the profile
  list and reports the verified result:

  ```json
  {"ok": true, "result": "ok", "message": "ok", "refresh_seen": true,
   "reinitialized": true, "verified": true, "state_after": "disabled",
   "iccid": "8970…", "card_session": 7}
  ```

  `verified` is `false` when the target profile is not in the requested state
  after the re-init (`null` when the state could not be read - a failed
  post-switch verification, e.g. the ISD-R refusing the selection, is
  `null`/`null` and never a `400`: the switch itself already happened).
  **`refresh` defaults to `true`** and is read as a JSON boolean.  The flag
  only asks whether the card should also send its REFRESH (this endpoint
  resets the card itself), and card behaviour varies: the Phase 3a bench
  probe refused `refreshFlag` TRUE (an undefined `enableResult` 7) and
  accepted FALSE, while the 4.0.0 live run accepted TRUE in both directions
  (the card returned its result inline and sent no REFRESH -
  `refresh_seen: false`).  A client may pass `refresh: false` when it resets
  the card itself or when a card refuses TRUE.  Failures carry the ES10c
  result code
  (`iccidOrAidNotFound`, `profileNotInDisabledState`,
  `profileNotInEnabledState`, `disallowedByPolicy`, `wrongProfileReenabling`,
  `catBusy`, `undefinedError`) and a short `message`; an unexpected status
  word is returned as `{"ok": false, "result": "undefinedError", "sw": "6985",
  "message": "SW 6985"}`.  No card session is a `503`.

### `POST /api/net-sim`

Runs one network-condition scenario against the equipped card and returns the
step log:

```json
{"scenario": "service_lost", "mcc": "262", "mnc": "01", "lac": "6CD7",
 "send_event": true, "dummy_locations": true, "keep_kasme": true}
```

Scenarios: `cold_boot`, `attach_eps`, `attach_2g`, `service_lost`,
`limited_service`, `roaming_denied`, `churn`, `sms_received`, `cb_reconfig`,
`authenticate`.  Optional parameters: `mcc`/`mnc` (or `plmn`), `lac`,
`cell_id`, `tac`, `rac`, `tmsi`, `ptmsi`, `ptmsi_sig`, `guti`, `ksi`,
`kasme`, `ul`, `dl`, `algo`, `kc`, `rand`, `autn`, `churn_count`,
`churn_delay_ms`, and the toggles `send_event`, `dummy_locations`,
`invalidate_epsnsc`, `keep_kasme`, `write_kc`, `sms_location`, `cb_clear`
(empty identity values are randomized).  The event step is skipped when the
card did not subscribe to Location status; only UPDATE BINARY/RECORD,
ENVELOPE and AUTHENTICATE are sent - EF.FPLMN is appended only by
`roaming_denied` (shift-list semantics of TS 31.102 §4.2.16, duplicates
skipped) and an attach to a listed PLMN clears its entry first (successful
manual selection, TS 23.122), while the 5GS location files are never written.

The response is `{scenario, success, error, steps: [{action, file, path, data,
sw, ok}], net_state}` where `net_state` is the updated monitor state (see
`GET /api/net-state`).  An unknown scenario is a `400`.

A 2G SIM's SELECT carries no FCP (TS 51 011 §9.2.1), so a file whose FCP
cannot be read is reported as a failed read (`sw: "6985"`) and the scenario
step is skipped - the server never guesses a length.

### `GET /api/net-state`

The cached **network-state monitor** - the panel the PWA's Simulator shows
next to the simulation buttons.  The state is created when a card is equipped
(only when EF.ICCID was readable) and is updated in place from the bytes the
network simulation wrote, so this endpoint performs no card I/O.  Card-free.

```json
{"available": true, "state": {
   "files": {"imsi": {"name": "EF.IMSI", "fid": "6F07",
                      "path": "ADF.USIM/6F07", "present": true,
                      "source": "init", "updated": 1758396000.0,
                      "kind": "transparent", "data": "0829051032547698"},
             "epsnsc": {"kind": "record",
                        "records": [{"num": 1, "data": "..."}]}},
   "service": {"state": "normal", "source": "service_lost",
               "time": 1758396123.0},
   "network": {"service": {"state": "normal"},
               "location": {"plmn": "26201", "mcc": "262", "mnc": "01",
                            "area": "6CD7", "lac": "6CD7",
                            "country": "Germany", "operator": "Telekom",
                            "roaming": "home", "rejected": false,
                            "source": "write"},
               "home": "26201"},
   "read_at": 1758396000.0}}
```

`available: false` with `state: null` means no monitor state exists yet (no
card equipped or EF.ICCID not readable).  `service.state` is the simulated
service state (`normal` / `limited` / `none`, `null` until a scenario or a
Location status event sets it); `network.location.roaming` is `home` /
`equivalent` / `guest` against EF.HPLMNwAcT and EF.EHPLMN, and `rejected` is
set when the location files or EF.FPLMN show a permanent rejection.
Monitored file keys: `imsi`, `ehplmn`, `spdi`, `hplmnwact`, `loci`, `psloci`,
`epsloci`, `epsnsc`, `cbmi`, `cbmir`, `smsstatus`, `fplmn`; each entry's
`source` is `init` (equip read), `read`, `refresh` or `write` (patched from
the simulator's own writes).  The PWA decodes the entries client-side.

### `POST /api/net-state-refresh`

Re-reads the monitored files from the card, merges them into the cached state
(`source: refresh`) and returns the same shape as `GET /api/net-state`.  The
optional body filters the read:

```json
{"files": ["imsi", "loci", "epsnsc"]}
```

Without a body all monitored files are re-read.  A monitor state is created
when the session has none yet; no card session is a `503`.

### `GET /api/mcc-mnc`

Searches the bundled worldwide operator list
(`simple_server/data/mcc-mnc-list.json`, MIT, from pbakondy/mcc-mnc-list;
`--mcc-mnc-list PATH` overrides it).  `?q=<text>` matches country, country
code, MCC/MNC, brand and operator (compact rows, max 50); `?random=1[&exclude
=MCCMNC]` returns one random entry (for roaming tests).  MVNO entries are
hidden from both (they do not operate their own network).  When the list is
not configured or missing the response is `{"available": false}`.  Card-free.

```json
{"available": true, "count": 2400,
 "results": [{"countryName": "Germany", "countryCode": "DE", "mcc": "262",
              "mnc": "01", "brand": "Telekom", "operator": "T-Mobile",
              "status": "Operational"}]}
```

A `?random=1` call answers `{"available": true, "count": N, "result": {…}}`
(one row or `null`).

### `POST /api/scp81/bip`

Starts or stops the local target the card's BIP channel is redirected to.
`action` is `start` (default) or `stop`.

Dump mode captures whatever the card sends (e.g. its TLS ClientHello) without
answering:

```json
{"action": "start", "mode": "dump", "host": "127.0.0.1", "port": 8443}
```

Redirect mode (`mode: "redirect"`) starts **no local listener**: every BIP
channel the card opens is connected to the configured target (`host`/`port`
are required - no defaults), which terminates TLS and runs the administration
dialog; the address the card requests is only logged.  The status API reports
`mode: "redirect"` with the target while it runs.

```json
{"action": "start", "mode": "redirect", "host": "203.0.113.10", "port": 10174}
```

Pass-through mode (`mode: "passthru"`) starts **no listener and has no
target**: every BIP channel dials the destination the card requests in OPEN
CHANNEL - the `Other address` (`3E`/`BE`) plus the `Transport level`
(`3C`/`BC`) port, TCP client remote (`02`) only.  The specs define no default
port, so an incomplete or non-TCP request fails the channel with result `3A`
and an `open-fail` log reason; `host`/`port` in the request are ignored.  The
per-channel targets appear in `bip.channels`.

```json
{"action": "start", "mode": "passthru"}
```

TLS mode runs the Phase B PSK TLS server (GPC v2.2 Amendment B): the PSK
table is applied to the TLS handshake, and the GP HTTP administration dialog
(`X-Admin-*` headers, 200 with a command string or 204 No Content) is served.
`psk_map` is the lookup table for the identity the card presents in the TLS
handshake - the PWA sends it from the card presets (`{identity, psk_hex}`
objects or an `{identity: psk_hex}` map); a handshake whose identity is not
listed fails with the log entry `tls-psk-unknown`.  The legacy single-key
form `psk_hex` (with optional `psk_identity`, empty = accept any identity) is
still accepted; when both are omitted the table of the previous start is
reused.  Keys are never stored or logged.

```json
{"action": "start", "mode": "tls", "host": "127.0.0.1", "port": 8443,
 "psk_map": [{"identity": "89012345678901234567",
              "psk_hex": "00112233445566778899aabbccddeeff"}],
 "script": ["80CAFF2100", "80F28002024F0000"], "script_kind": "Explore ISD"}
```

`script` is the APDU list served to the card (an explicit list, or `none`);
the server is agnostic to what the APDUs do.  `script_kind` is an optional
label for the logs/results.  Omitting `script` keeps the configured script and
its run progress.  `script_template` (`indefinite` default / `definite`),
`cr_tag`, `next_uri` (`%d` = the command id; empty omits the header,
`X-Admin-Next-URI`), `targeted_app` (`X-Admin-Targeted-Application`),
`chunked` (default `true`), `chunk_size`, `compact_headers`, `conn_header`
(`none` / `keep-alive` / `close`), `tls_version` (`auto` / `1.0` / `1.1` /
`1.2`), `cipher`, `keylog`, `answer_delay`, `link_events` (default `true`,
all modes) and (dump mode) `port` follow the listener contract described in
`GET /api/scp81/script` below.  `next_uri` and `targeted_app` must be free of
control characters (they become response headers; a CRLF would split the
response) - a value that carries one is a `400`.

The response is the status body of the started session plus `ok` and
`replaced` (the session that was stopped, `{"owner": …, "mode": …}`, or
`null`): `owner`, `bip`, `listener` and the control's `mode`/`target`.  A TLS
start additionally echoes the legacy fields `script` (**the APDU list**, not
the status endpoints' state dict), `script_kind`, `script_template`,
`cr_tag`, `link_events`, `targeted_app` and `chunked`.

**Refusals are `400` with `{"ok": false, "error": "…"}`** (a bad mode, a
bad/missing PSK, an invalid knob, a taken port, TLS-PSK unavailable on
Python < 3.13).  Every field is validated *before* the running session is
touched, so a refused request keeps it.  The previous session is stopped
**before** the new listener binds (the legacy order), so a Start on the port
the old listener holds rebinds it - the PWA's Start is the normal way to
apply the Options.  The documented trade-off: a bind failure at a *foreign*
port leaves no session behind (the previous one is already stopped); only a
validation refusal preserves it.  The script reset/rewrite also happens only
after a successful bind, so a refused Start never swaps the running script's
progress.

Stop either mode with `{"action": "stop"}` (also disables the BIP terminal);
the response is the status body plus `ok`/`replaced`.

### `POST /api/bip/control`

Generic BIP terminal control for the Simulator's **BIP** pill - the terminal
side of the Bearer Independent Protocol for cards that use TCP without HTTP
OTA.  The session is shared with the SCP81 listener: only one BIP session runs
at a time, starting either control stops the other, and the response reports
what was stopped as `replaced: {"owner": "scp81"|"bip", "mode": …}`.

```json
{"action": "start", "mode": "sink", "host": "127.0.0.1", "port": 0}
```

Modes:

- `sink` (default) - starts a local TCP listener that accepts the card's BIP
  channels and only logs what arrives (`conn`, `sink-rx`, `conn-close`); it
  never sends anything back.  `port` may be `0`/omitted for an ephemeral port;
  the bound address is reported in the status.  Use it for cards that open a
  TCP connection just to upload data.
- `passthru` - no listener and no target: each channel dials the destination
  the card requests in OPEN CHANNEL (TCP client only, no default port; an
  incomplete or non-TCP request fails with result `3A`).
- `redirect` - every channel connects to the required `host`/`port`; the
  address the card requests is only logged.

`link_events` (default `true`) controls the terminal-side Channel status
events (TS 102 223 §7.5.11).  An invalid request never disturbs a running
session; its refusal is a `400` with `{"ok": false, "error": "…"}`.  Stop with
`{"action": "stop"}`.  The response is `{owner, bip, listener, ok, replaced}`
(the session block of the status body - the additive `mode`/`target`/`script`
fields belong to the status endpoints).

### `GET /api/scp81/status` and `GET /api/bip/status`

Both serve one shared shape (card-free):

```json
{"owner": "scp81",
 "bip": {"enabled": true, "mode": "redirect", "target": "127.0.0.1:8443",
         "channels": [], "seq": 12},
 "listener": {"mode": "tls", "host": "127.0.0.1", "port": 8443,
              "psk_identities": ["89012345678901234567"], "psk_wildcard": false,
              "identity_seen": "89012345678901234567", "identity_matched": true,
              "version_seen": "TLSv1.2", "cipher_seen": "PSK-AES128-CBC-SHA256",
              "chunked": true, "chunk_size": 0, "compact_headers": false,
              "tls_version": "auto", "cipher": null},
 "mode": "tls", "target": "127.0.0.1:8443",
 "script": {"script": ["80CAFF2100"], "next": 1, "total": 1, "done": [0],
            "pending": null, "pages": 0, "pages_queued": 0, "complete": true,
            "kind": "Explore ISD", "template": "indefinite", "cr_tag": false,
            "results": [{"index": 1, "pos": 0, "page": false,
                         "apdu": "80CAFF2100", "rapdu": "FF21...", "sw": "9000"}]}}
```

`owner` is `scp81` or `bip` (whichever control started the session), `null`
when idle.  Three additive fields drive the panel from one poll: `mode` (the
listener's mode - `tls`/`dump`/`sink`/`redirect`/`passthru`, `null` when
idle), `target` (the `host:port` the channels go to, `null` for
`passthru`/idle) and `script` (the `GET /api/scp81/script` state, so the
listener status and the script progress cannot disagree between two polls).
Note the **type of `script`**: a state **dict** here and on `/api/bip/status`,
but the start responses keep the legacy **APDU list** (`POST /api/scp81/bip`
in `tls` mode).

Listener modes: `tls` (local PSK TLS server), `dump` (capture-only TCP
listener), `redirect` (no local listener; the BIP channels go straight to the
configured `host:port` - reported as `{"mode": "redirect", "host": …,
"port": …, "target": "host:port"}`) and `passthru` (no listener and no
target; each channel dials the destination the card requests - reported as
`{"mode": "passthru"}`, with the actual peer in `bip.channels[].target`).

`psk_identities` lists the identities the listener accepts (keys are never
exposed); `psk_wildcard` marks the legacy single-key mode.  `identity_seen` /
`identity_matched` reflect the last handshake: an unknown identity is logged
as `tls-psk-unknown` and the handshake fails.  A running sink reports
`{"mode": "sink", "host": …, "port": …, "connections": N}`.

### `GET /api/scp81/log` and `GET /api/bip/log`

The shared BIP/TLS event log (open/close, SEND/RECEIVE DATA hex, TLS handshake
and HTTP request/response records).  `?after=<seq>` returns only newer
entries; `seq` echoes the latest sequence number, and the response is capped
to the last 200 entries.

```json
{"seq": 12, "entries": [{"seq": 11, "kind": "tls-handshake", "…": "…"}]}
```

### `POST /api/scp81/log-clear` and `POST /api/bip/log-clear`

Clear the shared log → `{"ok": true, "seq": <new>}`.  Card-free (a listener
may run cardless), but the test-run guard still applies.

### `GET /api/scp81/script`

The configured command script and its execution state.  Card-free.

```json
{"script": ["80CAFF2100", "80F28002024F0000"], "next": 2, "total": 2,
 "done": [0, 1], "kind": "Explore ISD",
 "pending": {"index": 17, "pos": null, "page": true, "apdu": "80F28003024F0000"},
 "pages": 11, "pages_queued": 0, "complete": false,
 "results": [{"index": 1, "pos": 0, "page": false, "sw": "9000",
              "apdu": "80CAFF2100", "rapdu": "FF210C810102..."}]}
```

`next` is the index of the next script APDU to send; `done` lists the script
indices the card reported.  `pending` describes the C-APDU awaiting the card's
`X-Admin-Script-Status` report as `{index, pos, page, apdu}` (`pos` = script
index, `null` for an auto continuation page) or `null`; `pages` counts the
continuation pages queued so far and `pages_queued` those not yet sent.
`complete` is true when every configured APDU was reported and nothing is in
flight - a script can therefore be complete while a listing page is still
being fetched (`pending.page` = true), which is tracked separately.
`results` entries carry the send order (`index`), the script position (`pos`,
`null` for continuation pages) and the `page` flag.

Execution tracking and resume: an APDU counts as executed only when the card
reports it in the next POST's Response Scripting template.  A POST with
`X-Admin-Resume` continues with the unexecuted tail (the pending APDU is
resent if its report never arrived), a POST without it is a fresh dialog where
the script runs from the start, and a completed script closes the session with
204.

Each APDU is delivered in an `AE 80 22 <len> <apdu> 00 00` Command Scripting
template (TS 102 226 §5.2.1) with `X-Admin-Next-URI`; the card returns its
R-APDUs in the next POST's Response Scripting template, which is parsed and
logged (`script-rapdu`, `script-memory`).  Long GET STATUS listings that
answer `63 10` / `CA FE` ("more data available") are auto-continued with the
same command carrying P2.b1=1.

TLS framing knobs (applied at Start and echoed by `GET /api/scp81/status`):
`chunked` (**default `true`** - the reference server's chunked framing; the
card rejects a chunked response that also carries a Content-Length) and
`chunk_size` (default `0` - the whole response in one TLS record, as in the
decrypted reference session; a positive value writes the head and each body
piece as its own record).  The connection stays open between POSTs for the
whole dialog; only the 204 ends the session, and the server then shuts the TLS
session down cleanly (`close_notify` while the response is still buffered,
then FIN).  `compact_headers` (default `false`) omits the optional space after
each header colon; `conn_header` (default `'none'` = omit the header;
`'keep-alive'` adds it) declares the connection fate; `tls_version` (default
`'auto'` - accept TLS 1.0-1.2 and let OpenSSL pick the highest the card
offers; `'1.0'`/`'1.1'`/`'1.2'` pin a version) selects the protocol window;
`cipher` pins one suite; `next_uri` overrides the per-command
`X-Admin-Next-URI` (`%d` = command id; empty omits the header); `link_events`
(default `true`) controls the automatic Channel status events; `answer_delay`
waits before answering a request; `keylog` writes the TLS traffic secrets to
the given file (SSLKEYLOGFILE format) - it contains key material, use a
temporary path.

The response headers are minimal: `X-Admin-Protocol`, the optional
`X-Admin-Next-URI`/`X-Admin-Targeted-Application`, `Content-Type` on 200s and
`Transfer-Encoding`/`Content-Length` per `chunked`.  `GET /api/scp81/status`
echoes the negotiated handshake as `version_seen`/`cipher_seen` (plus
`identity_seen`); a handshake that fails for TLS/cipher reasons (no shared
cipher, unsupported protocol version, card alert) is logged as
`tls-handshake-failed` with the OpenSSL reason, separately from post-handshake
record errors (`tls-error`).

### `POST /api/scp81/queue`

Replace the SCP81 command script (used by the Remote APDU tab's RAM chain
"Queue in SCP81" and the PWA's "Restart script").

```json
{"apdus": ["80E60C002E...", "..."], "kind": "custom", "force": false}
```

The documented `script` alias carries the same list, and a single `apdu` is
accepted; the first non-empty form wins.  Every entry must be a hex string of
at least 4 bytes; a malformed list is a `400 {ok: false, error}` (an APDU the
card would refuse never reaches the listener).  Entries that already are
Command Scripting templates (`AA...`/`AE80...`, the expanded format) are sent
verbatim instead of being wrapped again.  Refused while a script is mid-run
unless `force` (`400`, `reason: "script in progress"`), and when no listener
is running (`400`); queuing resets the execution progress.  The response is
the engine's queue result plus `ok` and a `note`.

### `POST /api/scp81/psk-map`

Replaces the PSK table of the running TLS listener (the PWA pushes card-preset
edits without a listener restart):

```json
{"psk_map": [{"identity": "89012345678901234567",
              "psk_hex": "00112233445566778899aabbccddeeff"}]}
```

Returns `{"ok": true, "identities": [...], "listener": {...}}`; entries
without an identity or a valid key are skipped, and an empty table is a `400`.

### `POST /api/scp81/gen-install`

Generate the RAM (GP) APDU sequence for a `.cap` without touching the listener
or the running script; the PWA's "Install from .cap" script template stores
the returned list.  The `.cap` is parsed server-side (the same parser as
`/api/ram-install`) and expanded to `INSTALL [for load]` → LOAD blocks
(240-byte payloads) → `INSTALL [for install]`; the file itself is never stored.

```json
{"cap_hex": "504B0304...", "sd_aid": "A000000003000000", "privileges": "00",
 "install_params": "", "stk_params": "", "nv_quota": 0, "volatile_quota": 0,
 "make_selectable": true}
```

`sd_aid` empty = the ISD.  The archive travels as `cap_hex` (or the `cap`
shorthand).  `install_params`, `stk_params` and the `nv_quota`/
`volatile_quota` primitives follow the composition rule of `/api/ram-install`
(the quotas and an EF-form STK part share one System Specific Parameters
`EF`).  A refusal answers **HTTP `200`** with `{"ok": false, "error": "…"}`
(the legacy contract - its refusals are file/parameter problems, not a
request shape).  Success answers `{"ok": true, "apdus": [...],
"load_file_aid": ..., "module_aid": ...}`.

## Deviations from the 3.x server

The 4.0.0 cutover keeps the request/response shapes the PWA and external
callers depend on, with these deliberate differences:

- **The pySim surface is gone.**  `/api/command`, `/api/commands`,
  `/api/cardinfo` and `/api/help` no longer exist (`404 {"error": "Not
  found"}`).  The three actions the pySim command line served have dedicated
  endpoints: `POST /api/equip`, `POST /api/reset` and `GET /api/status`.
- **`/api/sp-verify` is gone and is not replaced.**  It cross-checked the PWA's
  JS packet builder against pySim's `OtaDialectSms.encode_cmd`; with no pySim
  there is no external reference.  The JS and Python builders are the same
  logic, and a shared vector
  (`00201506091515B00000C08F58C38860ACB3A362FFFE670AD13759A2A6B4C1A91116`)
  is pinned in both suites, so a builder that drifts breaks its own tests.
- **`/api/status` has our own shape.**  The pySim internals (`profile`,
  `cla_byte`, `sel_ctrl`, `current_selection`, `channels`) are gone; the
  replacement fields are documented above.  `card_ready_session`, `selection`,
  `proactive_mode`, `poll`, `test_run`, `bip`, `ram_progress` and `ram_batch`
  are additive.
- **`/api/read` has no `mode: "decoded"`** (see its section): the PWA decodes
  client-side from the same raw bytes, so the legacy mode is refused with
  `400` instead of being silently ignored.
- **`/api/apdu` returns `200` with any status word.**  The legacy
  `send_apdu_checksw` path raised on a non-`9000` answer and answered `500`; a
  raw-APDU workbench must see every status word.  The automatic `91XX` chain
  stays on (off during a test run or while a BIP session owns the card).
- **The file endpoints always probe a model-unknown FID** - the legacy
  `allow_probe` flag/detach dance protected pySim's model from the probe; this
  model is never mutated by a select, so the flag is ignored.
- **The card-free POST allowlist covers the stores and the flags**: the
  test-script/-suite store mutations, `/api/cap-info`, `/api/ram-batch/stop`,
  `/api/test/stop`, `/api/test/clear` and the two log clears answer while a
  long card operation holds the card lock (the legacy queued them behind it).
  The preset mutations need no card either but are not on that list, so they
  queue behind a running card operation.
- **The `scp80` field of `/api/status` is a placeholder:** it is `null` in
  4.0.0.  The counter and keyset state lives in the preset store and is
  reported by the operations that consume it (`final_cntr` and friends).
- **`ram_format: "auto"` on `/api/send-ota`** sends (and reports) the compact
  form: the plain send has no RAM step to probe with.  The RAM operations keep
  the per-operation probe.
- **Equip is a direct connect/reset + detect** (no pySim-shell), so every
  equip is a full re-initialization; a failed equip is a `503` with the status
  body and leaves no half-initialized session.
- **PC/SC only.**  The serial `-d/--device` transport and the `-p` reader-slot
  form are gone; `-r/--reader NAME` selects a reader and `POST /api/reader`
  switches it at runtime.
- **`--no-card-init`** starts the server without the detection/MF
  select/TERMINAL PROFILE phase: the link and ATR are read, `connected` is
  true and the poll/presence monitor run, but `card_ready_session` stays 0
  until a `POST /api/equip` initializes a card.  This mode also disables
  auto-equip (the operator initializes explicitly).

## Diagnostics

The card exchange can be traced to stderr (`--apdu-trace`) and streamed as
GSMTAP-SIM UDP packets (`--gsmtap [HOST[:PORT]]`, default `127.0.0.1:4729`)
for Wireshark / SIMtrace Analyser.  Both are **command-line only** - there is
no API or UI for them - and the tracer sits on the transport's `send_tpdu`,
so it records what `SCardTransmit` carries: on a TPDU-mode reader the wire
TPDUs (including the T=0 GET RESPONSE rounds), on an APDU-mode reader the
logical APDU with the final data and status word (the reader hides its own
`61xx`/GET RESPONSE framing).  The ATR is streamed as its own packet.  A
capture is therefore only as detailed as the transport it observes; for a
guaranteed wire capture use simtrace2 hardware.
