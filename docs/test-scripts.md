# Test script authoring guide

A **test script** is a JSON document that drives a deterministic dialogue with
the card over the local server: ordered **actions** (each sends something and
checks the response) and **expectations** (each fetches the proactive command
the card announced, checks it, and sends the scripted TERMINAL RESPONSE).

Scripts are imported into the PWA (Simulator → Test script → Import) and run
with **Run** against the equipped card, or POSTed directly to
`/api/test/run` (see `docs/api.md`).  While a run is active the card belongs to
the script.

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
`spi1` in the step.  Counter handling: the run starts from the keyset's stored
counter and persists each accepted packet's next value, so consecutive runs
continue correctly.

## Actions

```json
{"type": "action", "kind": "…", "params": {…}, "check": {…}}
```

| kind | params | notes |
|---|---|---|
| `envelope` | `event` 0–255, `data` hex (optional) | ENVELOPE(Event Download) |
| `menu-select` | `item_id` 1–255 | ENVELOPE(Menu Selection) |
| `file-write` | `path`, `data`, `mode` `auto`/`binary`/`record`, `record` 1–255 | UPDATE BINARY/RECORD |
| `file-read` | `path`, `mode`, `record` | READ BINARY/RECORD; verify with `check.data` |
| `apdu` | `apdu` hex | raw transport, no auto-handler |
| `scp80` | `apdu` **xor** `sp` (optional `source`), `kvn`, `tar`, `spi1`, `spi2` | secured packet to the TAR |
| `status` | `attempts` 1–1000, `interval_ms` 0–10000 | with `attempts > 1` the default SW check is `91??` (poll until a command) |

`path` is `/`-separated: `MF` (or `3F00`) or an ADF name/AID first, then FIDs
or file names - e.g. `MF/7F20/6F07`, `ADF.USIM/EF.TEST`.

### `check` on actions

```json
"check": {"sw": {"mode": "mask", "value": "91??"},
          "data": {"mode": "exact", "value": "0271…"},
          "por": "ok"}
```

- `sw` - default `{"mode": "exact", "value": "9000"}` (a `status` action with
  `attempts > 1` defaults to the `91??` mask).
- `data` - the response data (for SCP80: the inline PoR packet).
- `por` - `"any"` (default, no check), `"ok"` (`response_status == por_ok`),
  `"none"` (no PoR expected).
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
| `text` | `mode` contains/exact, `value`, `case_sensitive` | alpha identifier / text |
| `item` | `id`, `text`, `mode`, `case_sensitive` | items of SELECT ITEM / SET UP MENU |
| `raw` | `mode` exact/mask, `value` | the whole fetched command (hex, exact length needed) |
| `por` | `status`, `sw`, `data` | the PoR carried by a **SEND SHORT MESSAGE** (see below) |
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

### `respond`

The TERMINAL RESPONSE for the fetched command: `result` (name or hex -
`ok`, `partial`, `missing`, `refused`, `not_understood`, `modified`, `cancel`,
`back`, `timeout`, `no_response`), `item_id`, `text`+`dcs`, `raw` (extra
COMPREHENSION-TLVs before the result).

## The two PoR transports (SPI2)

1. **Inline** - the card answers the ENVELOPE `9000` with the PoR in the
   response data.  Verify on the action: `"por": "ok"` and/or
   `"data": {"mode": "mask", "value": "…"}`.
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
- `card verdict: no PoR (the card sent none for SPI2 …)` - an SCP80 operation
  expected a PoR that never arrived (check SPI2 and the transport).

## Known limitations

- The runner executes one command at a time and never polls by itself; a
  proactive command that arrives only on a STATUS poll needs an explicit
  `status` action.
- A `por` check only decodes SEND SHORT MESSAGE commands; the inline transport
  is checked on the action (`por`/`data`).
- `files` matches the File List as a set (order-insensitive); duplicate paths
  are kept.
