# Test script authoring guide

A **test script** is a JSON document that drives a deterministic dialogue with
the card over the local server: ordered **actions** (each sends something and
checks the response) and **expectations** (each fetches the proactive command
the card announced, checks it, and sends the scripted TERMINAL RESPONSE).

Scripts live in the **server-side store**
(`~/.pysim-simple-server/test_scripts.json`, `--test-scripts`): the PWA's Test
script pill manages them (Import/Export move JSON in and out, the old
localStorage export imports as-is), and any API client can create or run them
(`POST /api/test/run` with an inline `script` or a stored `script_id` - see
`docs/api.md`).  While a run is active the card belongs to the script.

**Format**: server ≥ 3.15.0 (`kvn`, `por` and `files` checks), scripts stored
server-side since 3.16.0, `menu-select` by text and the spec-order TERMINAL
RESPONSE codings since 3.17.0, the semantic `event` action since 3.18.0, the
`envelope` action's `src` override since 3.18.1, the inline `por` object, the
`alpha`/`sms` checks, `scp80.params.format` and the case-insensitive
`menu-select` text match since 3.19.0.

## Writing a test for an applet

Collect these values before writing the JSON (from the applet's source/spec
and the card's preset):

| What | Where it goes | Notes |
|---|---|---|
| TAR to address (the RFM application or the applet's own) + MSL, or an explicit SPI1 | `scp80.params.tar` / `spi1` | the card preset must carry the TAR with its MSL, or the step passes `spi1`; RFM TARs: `B00000`/`B00001`/`B00010` compact, `B00120`/`B00140`/`B00130` expanded (see Worksheet 1) |
| Keyset number | `scp80.params.kvn` (1–15) | only when the applet's keys are not the preset's first keyset |
| Trigger C-APDUs (+ their format) | `scp80.params.apdu` / `format` | one secured packet per `scp80` step; the preset provides keys and counter, `format` wraps the APDU for an expanded-format application |
| Expected file paths + contents | `file-read.params.path` + `check.data` | a mask must have **exactly** the actual value's length |
| Expected proactive command(s) | `expect.command` + `qualifier`/`checks` | REFRESH `qualifier: "00"` = NAA init + full FCN, `files` asserts the changed EFs |
| PoR transport + expected PoR | `scp80.check.por`/`check.data` (inline) or `expect SEND SHORT MESSAGE` + `por` check | pick per the SPI2 the applet expects |
| Delivery timing | a `status` action when the card delivers on poll | the runner never polls by itself |

### Worksheet 1 - RFM update → file change → REFRESH (full FCN)

```json
{"name": "applet RFM update",
 "steps": [
  {"type": "action", "kind": "scp80",
   "params": {"apdu": "<RFM C-APDU>", "tar": "B00001", "kvn": 1, "spi2": "01"},
   "check": {"sw": {"mode": "mask", "value": "91??"}, "por": "none"},
   "label": "RFM update"},
  {"type": "expect", "command": "REFRESH", "qualifier": "00",
   "checks": [{"kind": "files", "value": "<changed FIDs, e.g. 3F007F206F07>"}],
   "respond": {"result": "ok"}},
  {"type": "action", "kind": "file-read", "params": {"path": "<EF path>"},
   "check": {"data": {"mode": "mask", "value": "<expected bytes, ? = wildcard>"}}}
 ]}
```

`tar` addresses the **RFM application that owns the file system**, not the
applet-under-test: `B00000` = UICC shared FS, `B00001` = ADF RFM, `B00010` =
SIM FS (compact), and `B00120`/`B00140`/`B00130` the expanded-format twins
(TS 101 220 Annex D; the probe list in the SCP80 tab carries the same names).
An applet that accepts secured packets under its **own** TAR - the TAR given
in its INSTALL [for install] parameters (TS 102 226 §8.2.1.3.2.7) - uses that
TAR instead.  The preset must carry a TAR entry **with its MSL** or the step
passes an explicit `spi1`; an expanded-format RFM application also expects the
wrapped command template (`"format": "expanded"`/`"expanded-ae"`, see the
actions table).

### Worksheet 2 - incoming data → particular PoR, no other actions

Inline PoR:

```json
{"name": "applet data (inline PoR)",
 "steps": [
  {"type": "action", "kind": "scp80",
   "params": {"apdu": "<data C-APDU>", "tar": "<applet TAR>", "spi2": "01"},
   "check": {"sw": "9000", "por": "ok",
             "data": {"mode": "mask", "value": "<expected PoR bytes>"}}}
 ]}
```

PoR-in-submit:

```json
{"name": "applet data (SEND SM PoR)",
 "steps": [
  {"type": "action", "kind": "scp80",
   "params": {"apdu": "<data C-APDU>", "tar": "<applet TAR>", "spi2": "21"},
   "check": {"sw": {"mode": "mask", "value": "91??"}, "por": "none"}},
  {"type": "expect", "command": "SEND SHORT MESSAGE",
   "checks": [{"kind": "por", "status": "por_ok", "sw": "<R-APDU SW>",
               "data": {"mode": "mask", "value": "<expected response data>"}}],
   "respond": {"result": "ok"}}
 ]}
```

### Validation checklist

1. **Client-side**: the PWA import (or `testScriptProblem`) rejects structural
   mistakes; the server validates again with the same engine at run start.
2. **Import** the JSON (PWA → Simulator → Test script → Import, or
   `POST /api/test/scripts`), select the matched preset, **Run**.
3. **Read the report**: every action/expectation shows expected vs actual per
   check; ⚠ = warning (continues), ✗ = error (stops).  A failed step names the
   reason (`note`).
4. **Iterate**: a mismatch on `data` usually means the mask length differs from
   the actual value, or the value changed between runs - mask the varying
   bytes (counter, date-time) with `?`.
5. **Counters**: the run starts from the preset's stored counter and advances
   it after every scp80 step the card answers (when SPI1 requests a counter
   check); a refused packet moves it forward too - see the presets section.

### Reading the run log

Every step writes a semantic line to the server log under its step number,
request before response:

```
TEST-RUN step 1: SCP80 C-APDU=80E2900000
TEST-RUN step 1: SCP80 SECURED=0100…1A2B (N B) TAR=B00001 SPI1=16 SPI2=21 CNTR=0000000A
TEST-RUN step 1: SCP80 -> SW=9000
TEST-RUN step 1: PoR[inline] status=por_ok TAR=B00001 CNTR=0000000A raw=0271…aabb
TEST-RUN step 1: R-APDU SW=9000 data=AABB
TEST-RUN step 2: STATUS 1/2 -> 9102
TEST-RUN step 3: FETCH=8012000002 -> SW=9000 D03C8103012500…
TEST-RUN step 3: CMD 0x25 SET UP MENU qual=00
TEST-RUN step 3: TR=81030125008202828183020000 -> SW=9000
TEST-RUN step 4: MENU-SELECT ENVELOPE=80c2000009d30702020181900101 item=1 ('One') -> SW=9103
```

- A PoR delivered as SEND SHORT MESSAGE is logged on the step that fetches it:
  `PoR[sms-submit] …` followed by the same `R-APDU` line.
- A SEND SHORT MESSAGE expectation also logs its parsed TPDU
  (`SMS DA=… PID=… DCS=… UDL=… UD=…`) after the `CMD` line, so an applet's own
  SMS can be read even without an `sms` check.
- A wrapped scp80 step logs both forms:
  `SCP80 C-APDU=<plain> WRAPPED=<wrapped> (<format>)`; a pre-built packet logs
  `SCP80 SP=…`.
- Other actions log their send/response pair too (`APDU TX=… -> SW=…`,
  `ENVELOPE(Event Download) … -> SW=…`, `READ BINARY <path>`,
  `READ RECORD <n> <path>`, `UPDATE BINARY <path>`, each with `-> SW=…`).
- A failing step adds `TEST-RUN step N failed: …` with the reason.
- The shared secured-packet sender logs each segment's answer
  (`OTA SEND: ENVELOPE i/N -> <SW>`).
- `--apdu-trace` adds the raw transport view
  (`APDU-TRACE(…): <cmd> → SW: … RESP: …`) for every APDU, including the
  file-manager reads and pySim's auto-handler traffic.

## Document shape

```json
{"name": "applet RFM update",
 "steps": [
   {"type": "action", "kind": "scp80", "params": {…}, "check": {…}, "label": "optional"},
   {"type": "expect", "command": "REFRESH", "qualifier": "00",
    "checks": [{"kind": "files", "value": "3F007F206F07"}],
    "respond": {"result": "ok"}}
 ]}
```

Every step may carry `on_fail`: `"error"` (default - stops the run) or
`"warning"` (continues, the report shows ⚠).  Each entry of `checks` may
override it with its own `on_fail`.

## Presets: where the keys, TARs and counters come from

An SCP80 action never carries key material.  The PWA sends the **stored
preset's id** with the run; the server resolves it and takes:

- the keyset - the step's `kvn` (1–15) or the preset's first keyset,
- the keys and the counter of that keyset,
- the TAR table with each TAR's **MSL** (Minimum SPI1).

So before a run the matched preset (ICCID) must contain the keyset and, for
every TAR the script addresses, either a TAR entry with its MSL or an explicit
`spi1` in the step.  A step that omits them gets the preset's ISD role TAR as
`tar`, that TAR's MSL as `spi1` and `01` as `spi2`; `kvn` defaults to the
first keyset.

Counter handling: the run starts from the keyset's stored counter and advances
(and persists) its next value after **every** scp80 step the card answers, when
the packet requests a counter check (SPI1 b5b4 ≠ 00, TS 102 225 §5.1.1) - a
refused packet moves the local value forward too.  The local counter therefore
only ever moves up, so a later packet is never below what the card has seen;
consecutive runs continue correctly.

## Actions

```json
{"type": "action", "kind": "…", "params": {…}, "check": {…}}
```

| kind | params | notes |
|---|---|---|
| `envelope` | `event` 0–255, `data` hex (optional), optional `src` | ENVELOPE(Event Download) with raw data; `src` overrides the device-identities source (`82` terminal / `83` network) |
| `event` | `event` (name or hex), `fields` (object), optional `src` | semantic ENVELOPE(Event Download) for 0x03/0x0B/0x12/0x1D (see below) |
| `menu-select` | `item_id` 1–255 **or** `text` + `mode` `exact`/`contains` (+ `case_sensitive: true` for an exact-case match — the default is case-insensitive; the step editor's **Match case** choice writes it), not both | ENVELOPE(Menu Selection); the text is resolved against the card's cached menu - refreshed whenever the card sends SET UP MENU, so it follows the applet's install parameters |
| `file-write` | `path`, `data`, `mode` `auto`/`binary`/`record`, `record` 1–255 | UPDATE BINARY/RECORD |
| `file-read` | `path`, `mode`, `record` | READ BINARY/RECORD; verify with `check.data` |
| `apdu` | `apdu` hex | raw transport, no auto-handler |
| `scp80` | `apdu` **xor** `sp` (optional `source`), `kvn`, `tar`, `spi1`, `spi2`, `format` | secured packet to the TAR; `format`: `compact` (default - the C-APDU verbatim), `expanded` (the `AA`-prefixed Command Scripting template) or `expanded-ae` (the `AE 80 … 00 00` form), TS 102 226 §5.2.1 |
| `status` | `attempts` 1–1000, `interval_ms` 0–10000 | with `attempts > 1` the default SW check is `91??` (poll until a command) |

`path` is `/`-separated: `MF` (or `3F00`) or an ADF name/AID first, then FIDs
or file names - e.g. `MF/7F20/6F07`, `ADF.USIM/EF.TEST`.

`file-write` sends a direct terminal-side UPDATE BINARY/RECORD - it does **not**
fire the card's EVENT_REMOTE_FILE_UPDATE (that event reports an update done by
a remote entity through OTA/RFM, TS 102 223 §7.5.9): assert the change with
`file-read` or the REFRESH `files` check.

### Semantic events (`kind: "event"`)

The server builds the event objects for the events a test needs most; an
event without a builder goes through the raw `envelope` action, whose data hex
the Phone tab's event form shows (**Event data (hex)**, copyable).  Both
actions take the same optional `src` device-identities override.

```json
{"type": "action", "kind": "event",
 "params": {"event": "location_status",
            "fields": {"status": 0, "mcc": "250", "mnc": "01",
                       "lac": "00FF", "cell": "0001"}}}
```

| `event` | fields |
|---|---|
| `location_status` / `0x03` | `status` 0 normal / 1 limited / 2 no service; for normal service `mcc`, `mnc`, `lac`, `cell` |
| `access_tech` / `0x0B` | `tech` (TS 102 223 8.61 coding: 0 GSM, 3 UTRAN, 8 E-UTRAN, 10 NG-RAN, …) |
| `network_rejection` / `0x12` | `reg_type` 0–17, `access_tech`, `cause`; the location object follows the registration group - LU: `lac`, GPRS: `lac`+`rac`, EPS/5GS: `tac` (4–6 hex) + optional `ext_cause`; `mcc`/`mnc`; optional `ext_info_type` (1 CAG ID / 2 NID / 3 RedCap) + `ext_info` |
| `data_connection` / `0x1D` | `status` 0–2, `type` 0–2, optional `cause`, `ti` (hex byte), `datetime: "now"` (host clock, 8.39), `mcc`/`mnc`/`lac`/`cell`, `tech`, `loc_status`, `apn`, `pdp_type` |

`src` overrides the device-identities source (`82` terminal, `83` network);
the default is the terminal, matching the Phone tab's forms.

### `check` on actions

```json
"check": {"sw": {"mode": "mask", "value": "91??"},
          "data": {"mode": "exact", "value": "0271…"},
          "por": "ok"}
```

- `sw` - default `{"mode": "exact", "value": "9000"}` (a `status` action with
  `attempts > 1` defaults to the `91??` mask).
- `data` - the response data (for SCP80: the inline PoR packet).
- `por` (SCP80 actions only) - `"any"` (default, no check), `"ok"`
  (`response_status == por_ok`), `"none"` (no PoR expected), or an **object**
  `{"status"?, "sw"?, "data"?}` asserting the decoded PoR of this very
  exchange (at least one field; the same fields as the expectation's `por`
  check, `status` accepting `ok` for `por_ok`) - it works for both the inline
  and the SEND SHORT MESSAGE transport.
  Example: `"por": {"status": "por_ok", "sw": "9000"}`.
- A plain hex string is exact; a value containing `?` is a mask.  **A mask
  must have exactly the same length as the actual value** (`?` is a per-nibble
  wildcard) - e.g. `91??` matches `9102`, `027100????` matches a 4-byte
  prefix.

## Expectations

```json
{"type": "expect", "command": "SEND SHORT MESSAGE",
 "qualifier": "00",
 "checks": [{"kind": "por", "status": "por_ok", "sw": "9000"}],
 "respond": {"result": "ok"}}
```

The card must have announced a command (`91XX`) in the previous step - the
runner never polls by itself.  `command` is a name (as shown in the proactive
log) or a hex type; `ANY`/`*` skips the type check.  `qualifier` is the
command-details byte 3 (one byte, exact/mask).

### Content checks

| kind | fields | checks |
|---|---|---|
| `text` | `mode` contains/exact, `value`, `case_sensitive` | the **Text string** (`8D`, DCS coded) of DISPLAY TEXT / GET INKEY / GET INPUT |
| `alpha` | `mode` contains/exact, `value`, `case_sensitive` | the command's **Alpha identifier** (tag `05`/`85`, TS 102 223 §8.2, Annex A coded) - distinct from `text` when a command carries both |
| `item` | `id`, `text`, `mode`, `case_sensitive` | items of SELECT ITEM / SET UP MENU |
| `raw` | `mode` exact/mask, `value` | the whole fetched command (hex, exact length needed) |
| `por` | `status`, `sw`, `data` | the PoR carried by a **SEND SHORT MESSAGE** (see below) |
| `sms` | `da`, `pid`, `dcs`, `udl`, `ud` | the TPDU of a SEND SHORT MESSAGE (see below) |
| `files` | `files` (list of path hex strings) or a comma/space separated string | the **File List** of a REFRESH, TS 102 223 §8.18; matched order-insensitively |

`por` needs an SCP80 step **before** the expectation: the TPDU of the SEND
SHORT MESSAGE is decoded with that step's keyset, SPI and counter.  `status`
is the decoded response status name (`por_ok`, `cntr_low`,
`rc_cc_ds_failed`, …; `ok` is accepted for `por_ok`); `sw` is the R-APDU
status word and `data` the R-APDU response data (exact/mask) - both come from
the decoded response (scripting `AB`/`AF`/compact forms).  A PoR split over
several SMS parts is accumulated across SEND SHORT MESSAGE expectations: put
the `por` check on the step that completes the sequence (intermediate steps
should use `raw` or no PoR check).

`sms` parses the fetched SEND SHORT MESSAGE's TPDU (SMS-SUBMIT only - any other
MTI fails the check): `da` the destination digits as the TPDU carries them
(exact; whitespace and a leading `+` in the script are ignored - the
international flag lives in the TPDU's type-of-number),
`pid`/`dcs` single bytes and `ud` the raw user data (a UDH included) as hex
exact/mask, `udl` the user-data length in decimal (octets, or septets for the
7-bit alphabet - whatever the TPDU carries).  Use it for an applet's own SMS;
the OTA PoR of the preceding scp80 step is what `por` decodes.  At least one
field is required.

### `respond`

Optional - an omitted (or empty) object answers `ok` (`0x00`).  The TERMINAL
RESPONSE follows the TS 102 223 §6.8.0 object order (Command details, Device
identities, **Result**, Duration, Text string, Item identifier, …):

- `result` - name or hex (`ok`, `partial`, `missing`, `refused`,
  `not_understood`, `modified`, `cancel`, `back`, `timeout`, `no_response`).
- `text` + `dcs` - the Text string for a GET INKEY / GET INPUT answer.  `dcs`
  uses the spec's recommended codings: `"00"` GSM default alphabet 7 bits
  **packed**, `"04"` GSM default alphabet 8 bits (one octet per character),
  `"08"` **UCS2** (UTF-16-BE).  An empty `text` sends the null text string
  (Length `00`).
- `item_id` - the Item identifier for a successful SELECT ITEM answer.
- `raw` - extra COMPREHENSION-TLVs appended after the standard objects.

UCS2 answer to a GET INPUT:

```json
{"type": "expect", "command": "GET INPUT",
 "respond": {"result": "ok", "text": "Пароль", "dcs": "08"}}
```

## The two PoR transports (SPI2)

1. **Inline** - the card answers the ENVELOPE `9000` with the PoR in the
   response data.  Verify on the action: `"por": "ok"` and/or
   `"data": {"mode": "mask", "value": "…"}`, or assert the decoded PoR with
   `"por": {"status": "por_ok", "sw": "9000"}`.
2. **PoR-in-submit** (SPI2 bit `20`, e.g. `spi2: "21"`) - the card announces
   a **SEND SHORT MESSAGE** (`91XX`); the PoR is inside its SMS TPDU:

```json
{"type": "action", "kind": "scp80",
 "params": {"apdu": "…", "tar": "B00001", "spi2": "21"},
 "check": {"sw": {"mode": "mask", "value": "91??"}, "por": "none"}},
{"type": "expect", "command": "SEND SHORT MESSAGE",
 "checks": [{"kind": "por", "status": "por_ok", "sw": "9000"}],
 "respond": {"result": "ok"}}
```

If the card announces the SEND SHORT MESSAGE only on a later STATUS poll,
insert a `status` action (`attempts` > 1) before the expectation.

## "No actions" after a trigger

An exact SW check is the assertion: `"check": {"sw": "9000"}` fails if the
card answers `91XX` (a command was announced); the runner also errors on a
pending command the next step does not expect.

## Worked examples

### Event download before an applet action

```json
{"name": "applet event",
 "steps": [
  {"type": "action", "kind": "event",
   "params": {"event": "location_status",
              "fields": {"status": 0, "mcc": "250", "mnc": "01",
                         "lac": "00FF", "cell": "0001"}},
   "check": {"sw": {"mode": "mask", "value": "91??"}}},
  {"type": "expect", "command": "ANY", "respond": {"result": "ok"}}
 ]}
```

The event's default SW check is an exact `9000`; a mask (`91??`) is needed
when the applet answers with a proactive command, which the next expectation
then fetches.

### Menu selection by text (the id varies with the install parameters)
```json
{"name": "applet menu",
 "steps": [
  {"type": "action", "kind": "menu-select",
   "params": {"text": "Settings", "mode": "exact"}},
  {"type": "expect", "command": "ANY", "respond": {"result": "ok"}}
 ]}
```

The text is matched against the card's cached menu, **case-insensitively by
default** (`case_sensitive: true` forces an exact-case match); the cache is
refreshed whenever the card sends SET UP MENU (e.g. after an install +
REFRESH).  If the
script must wait for the menu, add a `status` action and an
`expect SET UP MENU` before the selection.

### RFM update → file change → REFRESH (NAA init + full FCN)

```json
{"name": "applet RFM update",
 "steps": [
  {"type": "action", "kind": "scp80",
   "params": {"apdu": "80E2900000", "tar": "B00001", "spi2": "01"},
   "check": {"sw": {"mode": "mask", "value": "91??"}, "por": "none"},
   "label": "RFM update"},
  {"type": "expect", "command": "REFRESH", "qualifier": "00",
   "checks": [{"kind": "files", "value": "3F007F206F07"}],
   "respond": {"result": "ok"}},
  {"type": "action", "kind": "file-read", "params": {"path": "MF/7F20/6F07"},
   "check": {"data": {"mode": "mask", "value": "AA??CC"}}}
 ]}
```

`qualifier` `00` is "NAA Initialization and Full File Change Notification";
the `files` check asserts the exact set of changed files the applet reports.
For this mode the terminal must **not** reset the card
(TS 102 223 §6.4.7), so the run continues - the following `file-read`
verifies the updated content.

### Incoming data → particular PoR, no other actions

Inline PoR:

```json
{"name": "applet data (inline PoR)",
 "steps": [
  {"type": "action", "kind": "scp80",
   "params": {"apdu": "80E2900000", "tar": "B00001", "spi2": "01"},
   "check": {"sw": "9000", "por": "ok",
             "data": {"mode": "mask", "value": "027100????0102"}}}
 ]}
```

PoR-in-submit (with the transport asserted by the expectation):

```json
{"name": "applet data (SEND SM PoR)",
 "steps": [
  {"type": "action", "kind": "scp80",
   "params": {"apdu": "80E2900000", "tar": "B00001", "spi2": "21"},
   "check": {"sw": {"mode": "mask", "value": "91??"}, "por": "none"}},
  {"type": "expect", "command": "SEND SHORT MESSAGE",
   "checks": [{"kind": "por", "status": "por_ok"}],
   "respond": {"result": "ok"}}
 ]}
```

## Run errors worth knowing

- `SCP80 preset is incomplete: …` - the matched preset has no complete
  keyset; `step N: keyset K is not defined in the preset` - the step's `kvn`
  is outside the preset's keysets.
- `no MSL for TAR … - set it in the Cards tab or pass spi1` - the step's TAR
  has no entry in the preset and the step carries no `spi1`.
- `no proactive command pending …` - an expectation ran without a `91XX` from
  the previous step (add a `status` action when the card delivers on poll).
- `menu-select: no menu item matches …` - the error lists the cached menu; a
  miss on an explicit `case_sensitive: true` names the case-insensitive
  candidates.
- `card verdict: no PoR (the card sent none for SPI2 …)` - an SCP80 operation
  expected a PoR that never arrived (check SPI2 and the transport).

## Known limitations

- The runner executes one command at a time and never polls by itself; a
  proactive command that arrives only on a STATUS poll needs an explicit
  `status` action.
- A `por` check only decodes SEND SHORT MESSAGE commands; the inline transport
  is checked on the action (`por`/`data`).
- An expanded-format command (`"format": "expanded"`/`"expanded-ae"`) has no
  in-packet GET RESPONSE (TS 102 226 §5.2.1.1): if the card answers `61xx`,
  send the GET RESPONSE as a follow-up `scp80` step (its own secured packet,
  same format).
- `files` matches the File List as a set (order-insensitive); duplicate paths
  are kept.
- Semantic `event` actions cover four events (0x03/0x0B/0x12/0x1D); every
  other event uses the raw `envelope` action with the data hex from the
  Phone tab's event form.
