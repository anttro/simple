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
connect and compares the major version (a 2.x server is flagged as outdated by
a 3.x PWA).

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
| `/api/presets` | GET | Card preset store: all presets + the store path |
| `/api/presets` | POST | Create a card preset (server-side store) |
| `/api/presets/update` | POST | Update a preset (`{id, fields}`; the counter may be raised) |
| `/api/presets/delete` | POST | Delete a preset (`{id}`) |
| `/api/presets/import` | POST | Import presets (`{presets: [...], mode: merge\|replace}`) |
| `/api/esim/chip` | GET | eUICC chip details (EID, EUICCInfo1/2, configured addresses) |
| `/api/esim/profiles` | GET | Installed eSIM profiles with their metadata |
| `/api/esim/notifications` | GET | Pending eSIM notifications (read-only) |
| `/api/esim/profile` | POST | Enable/disable an eSIM profile (card re-initialized after the switch) |
| `/api/help` | POST | pySim help for a given command |
| `/api/send-ota` | POST | SCP80 OTA secured packet delivery |
| `/api/ram-install` | POST | Install a Java Card `.cap` file via SCP80 (INSTALL[for load] → LOAD ×N → INSTALL[for install]) |
| `/api/ram-install-app` | POST | Single INSTALL [for install] / [for make selectable] for an already loaded package (no re-load) |
| `/api/cap-info` | POST | Validate a `.cap` archive and estimate its code/NVRAM/RAM requirements (read-only) |
| `/api/cap-compat` | POST | CAP compatibility test: LOAD only to the block completing the Import component (nothing committed) |
| `/api/test/run` | POST | Start a test script (actions + proactive expectations) |
| `/api/test/status` | GET | Test script run state and per-step results |
| `/api/test/stop` | POST | Request a running test script to stop |
| `/api/test/clear` | POST | Clear the finished run report |
| `/api/sp-verify` | POST | Verify secured packet against pySim reference |
| `/api/menu` | GET | Current STK menu (title + items + active) |
| `/api/menu-select` | POST | ENVELOPE(Menu Selection) with item_id |
| `/api/menu-respond` | POST | TERMINAL RESPONSE for paused STK command |
| `/api/stk-status` | GET | STK session state (active/pending/type) |
| `/api/events` | GET | Event list from SET UP EVENT LIST |
| `/api/event-send` | POST | Send ENVELOPE(Event Download) |
| `/api/net-sim` | POST | Run a network-condition scenario (attach, service loss, roaming, churn, 2G, SMS, CB, AUTHENTICATE) |
| `/api/net-state` | GET | Cached network-state monitor (service, location, per-file state) |
| `/api/net-state-refresh` | POST | Re-read the monitored files (optional `files` list) and return the updated monitor state |
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
| `/api/scp81/status` | GET | BIP terminal + listener state (channels, PSK identities, handshake identity, `owner`) |
| `/api/scp81/log` | GET | HTTP OTA event log (`?after=<seq>`) |
| `/api/scp81/log-clear` | POST | Clear the HTTP OTA event log |
| `/api/bip/control` | POST | Start/stop the generic BIP session (`sink` / `passthru` / `redirect`) |
| `/api/bip/status` | GET | BIP session state + owner (same shape as `/api/scp81/status`) |
| `/api/bip/log` | GET | BIP event log (`?after=<seq>`) |
| `/api/bip/log-clear` | POST | Clear the BIP event log |
| `/api/scp81/queue` | POST | Replace the SCP81 command script (optionally force-restart) |
| `/api/scp81/script` | GET | Active command script + execution state and R-APDUs |
| `/api/scp81/psk-map` | POST | Replace the PSK table of a running TLS listener |
| `/api/scp81/gen-install` | POST | Generate the RAM APDU list for a `.cap` (no queueing) |

## Endpoint details

### `GET /api/version`

Returns server version for compatibility checking.

**Example response:**
```json
{"version": "3.0.1"}
```

### `GET /api/status`

Card reader, card type, current selection, and card state. Reported fields:
`reader`, `connected`, `card_present`, `card_session` (increments on every
equip/disconnect), `proactive_seq`, `equipping`, `auto_equip`, `card`,
`profile`, `eid`, `euicc`, `iccid`, `app_ready`, `adm_verified`, `atr`,
`cla_byte`, `sel_ctrl`, `current_selection`, `channels`.

`eid` is the eUICC identifier read from the ISD-R at equip (`null` for a
non-eUICC card) and `euicc` says whether the equipped card is an eUICC
(SGP.22/32); both are used by the PWA's eSIM view.

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

### Card presets (server-side store)

Card presets (keysets, SPI, per-target TARs, PSK pair, ADM) live on the server
in `~/.pysim-simple-server/card_presets.json` (`--card-presets PATH` overrides
it; `Path.home()` resolves the same way on Linux, macOS and Windows).  The file
is written atomically and every counter change is appended to
`card_presets.json.audit.jsonl`.

**Keysets.**  A preset carries one keyset per GlobalPlatform key version — the
`b8..b5` nibble of KIc/KID (TS 102 225 §5.1.2/A.2); the version is implicit in
the bytes, there is no separate field:

```json
{"id": "…", "name": "Card A", "iccid": "…", "spi1": "16", "spi2": "01",
 "tar": "000000", "uiccTar": "B00000", "usimTar": "B00001",
 "pskIdentity": "", "pskKey": "", "adm": "",
 "keysets": [
   {"kic": "15", "kid": "15", "kicKey": "…", "kidKey": "…", "cntr": "0000000001"},
   {"kic": "29", "kid": "29", "kicKey": "…", "kidKey": "…", "cntr": "0000000005"}
 ]}
```

Rules (all refused with `400`): KIc and KID must carry the **same** key version
(A.2 — the card rejects a mismatch with "Unidentified security error"), the
version must be `01`–`0F` (`00` means "no security" and is a packet-level
choice, not a preset keyset), no two keysets may share a version, both keys and
a counter (1–10 hex digits) are required, and the counters are stored in the
fixed-width 10-hex form.  A v3.8.0 flat preset (`kic`/`kid`/`kicKey`/`kidKey`/
`cntr` at the top level) converts into a single keyset on load and on import,
so old files and exports keep working.

**Counters.**  The store is the source of truth for the SCP80 counters, one per
key version ("a dedicated counter shall be associated to each key version",
Annex A.1).  The operations below accept a `preset_id` and **persist the
counter they consumed themselves** (monotonic per keyset — a stale value never
regresses it), so a closed tab, a lost response or a second browser window can
no longer lose an increment.  A request whose KIc/KID name a **non-zero key
version the preset does not define** is refused (`400 {"error": "key version N
is not defined in preset '…' - add it in the Cards tab"}`), and so is a
KIc/KID version mismatch.  The Cards tab's export/import uses this API; the
import also accepts presets exported by the older localStorage-based builds.

- `GET /api/presets` — `{"path": "…", "count": 2, "version": 2, "presets": [{…}]}`
  (card-free: it answers while a long card operation runs).
- `POST /api/presets` — body = the preset fields (`keysets` included); returns
  `{"ok": true, "preset": {…}}`.  A duplicate ICCID (digits, spaced or raw EF
  hex are normalised to one form) is refused with
  `400 {"error": "card with this ICCID already exists: …"}`.
- `POST /api/presets/update` — `{"id": "…", "fields": {…}}` (partial update;
  a plain `{id, name, …}` body works too, and `keysets` replaces the list).
  A counter is written as given — this is the deliberate human edit; unknown
  ids answer `404`.
- `POST /api/presets/delete` — `{"id": "…"}` → `{"ok": true, "removed": true}`.
- `POST /api/presets/import` — `{"presets": […], "mode": "merge"}` (or
  `"replace"`) → `{"ok": true, "added": N, "skipped": M, "errors": […]}`;
  invalid entries and duplicates are skipped and reported, never fatal.

### eSIM / LPA (local ES10 operations)

These endpoints work only when the equipped card is an eUICC (SGP.22/32);
otherwise they answer `400` with `{"error": "The equipped card is not an
eUICC"}`.  They use pySim's ES10 static API — no lpac, no SM-DP+ contact, no
profile downloads and no notification processing.  All run under the card
lock; `GET /api/status` reports `euicc` and `eid` for the PWA.

- `GET /api/esim/chip` — `{"eid": "8908…", "info1": {…}, "info2": {…},
  "addresses": {"default_dp_address": …, "root_ds_address": …},
  "rat": [{…}], "errors": {"<part>": "<reason>"}}` (a part the card does not
  support is reported in `errors` instead of failing the whole request).
  The EUICCInfo TLVs are requested raw and decoded per SGP.22 v2.6 §5.7.8
  (pySim's classes are incomplete), `rat` comes from the ES10b GetRat command
  (§5.7.13) and undecoded TLVs are preserved in a `raw_tlvs` map:

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
- `GET /api/esim/profiles` — `{"profiles": [{"iccid": "8970…",
  "isdp_aid": "A000…", "state": "enabled"|"disabled", "nickname": …,
  "provider": …, "name": …, "class": "test"|"provisioning"|"operational",
  "owner": "250-99", "icon_type": "png"|"jpg", "icon": "89504E47…",
  "icon_size": 1234}], "error": null}`.  `icon` is the SGP.22 icon data
  (tag `0x94`) as hex and `icon_size` its byte count, so a client can show
  the profile image (`data:image/<type>;base64,…`); both are `null` when the
  card sent no icon.
- `GET /api/esim/notifications` — `{"notifications": [{"seq_number": 3,
  "operations": ["enable"], "address": "smdp.example.org",
  "iccid": "8970…"}], "error": null}`.  `operations` are the decoded
  `ProfileMgmtOperation` flags (`install`, `enable`, `disable`, `delete`, in
  that order) — one or more per notification.
- `POST /api/esim/profile` — `{"action": "enable"|"disable", "iccid"?: …,
  "isdp_aid"?: …, "refresh"?: true}` (one identifier required).  With the
  refresh flag set the ISD-R returns OK *before* the REFRESH (SGP.22 v2.6
  §5.7.16/§5.7.17 step 6) and the switch completes upon the TERMINAL RESPONSE
  or the following RESET (step 8): a `91xx` answer is that OK, so the server
  answers the REFRESH proactive command (logged in `/api/proactive-log`),
  **never retries the STORE DATA** (the mid-switch card answers `6985`) and
  re-initializes the card like an equip (physical reset, re-read
  ICCID/network state, new `card_session`).  It then re-reads the profile
  list and reports the verified result:

  ```json
  {"ok": true, "result": "ok", "refresh_seen": true, "reinitialized": true,
   "verified": true, "state_after": "disabled", "iccid": "8970…",
   "card_session": 7}
  ```

  `verified` is `false` when the target profile is not in the requested state
  after the re-init (`null` when the state could not be read).  Failures carry
  the ES10c result code (`iccidOrAidNotFound`, `profileNotInDisabledState`,
  `profileNotInEnabledState`, `disallowedByPolicy`, `wrongProfileReenabling`,
  `catBusy`, `undefinedError`) and a short `message`; an unexpected status
  word is returned as `{"ok": false, "result": "undefinedError", "sw": "6985",
  "message": "SW 6985"}`.

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

Send an OTA command (SCP80) to the card via SMS-PP-DOWNLOAD ENVELOPE
(one ENVELOPE per SMS).  With `apdu` the secured packet is built
server-side (no single-SMS limit); a packet that does not fit one SMS is
sent as a **concatenated** download per TS 31.115 §4.3: the packet is
split into SMS user-data parts (first SM 132 octets, following ones 134 —
the first one additionally carries the concatenation and CPI IEs) and the
segments are sent in order.  A packet that would need more than 5 segments
is refused (the card's concatenation buffer is the limit).  With `sp` a
pre-built packet is delivered the same way.

The card may answer with PoR status `actual_response_sms_submit` (`0x0B`):
the real response (a big GET STATUS listing, for example) then arrives as one
or more proactive SEND SHORT MESSAGE commands.  The server captures those
SMS-SUBMIT TPDUs, reassembles the concatenated segments and returns the
decoded response in `por` (as if it had arrived in the ENVELOPE), so callers
see a normal `por.response_status == "por_ok"` with the remote status word
and response data.  Responses wrapped in the TS 102 226 5.2.2 Response
Scripting template (`AB`/`AF`: executed-count TLV `80` + R-APDU TLV `23`) are
decoded the same way (`response_type: "scripting"`), with the R-APDU's own
status word and data.

With `ram_format` the server handles the two RAM command formats of
TS 102 226 §5.2.1: `"auto"` (the default when the key is present) sends a
read-only `GET STATUS [ISD]` probe first — compact, then wrapped as `AA`
(Command TLV `22`) — and sends the command in the format the card answered;
`"compact"` / `"expanded"` pin it.  The response reports the detected
`ram_format` and `final_cntr` (the counter to use next — accepted probe
packets consume counters).  Without the key the command data is sent
byte-exact: applet-directed payloads (HTTP OTA triggers, §9 push, expanded
scripts) must not be wrapped.

`final_cntr` is reported for the plain `sp` path too (a pre-built packet):
the counter that was sent, advanced by one when the card accepted the packet.
A request without a `cntr` (nothing to advance) simply reports no
`final_cntr` — it never fails the send.  With `preset_id` the server persists
the counter into the keyset of the key version the packet used (see *Card
presets*), so a lost response or a closed tab cannot lose the increment; the
request is refused when KIc/KID carry different key versions (TS 102 225 A.2)
or a non-zero key version the preset does not define.

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
  "ram_format": "auto"
}
```

**Response (delivery PoR):**
```json
{"success": true, "sw": "9000", "response_data": "027100000e0a...",
 "bytes": 36, "segments": 1,
 "ram_format": "compact", "final_cntr": "0000000002",
 "por": {"response_status": "por_ok", "tar": "B00000", "pcntr": 0,
         "decoded": {"number_of_commands": 1, "last_status_word": "6e00",
                      "last_response_data": ""}}}
```

`bytes` is the secured packet size and `segments` the number of SMS
segments sent (1 = single SMS, >1 = concatenated download; the failure
response carries them too, plus an `error`).

**Response (submit PoR):** PoR is extracted from the SMS-SUBMIT TPDU
fetched via a proactive command (FETCH). The response contains the
same `por` structure if decoding succeeds.

The SPI2 `por_in_submit` bit (0x20) selects submit-mode PoR.

### `POST /api/cap-info`

Validate a Java Card `.cap` archive and estimate its memory requirements.
Read-only: it runs the same structural parse as the install paths (so a
corrupt or wrong-format file fails here first) plus the bundled CAP analyzer
(`pysim_simple_server/capmem.py` — component parsers and a method-bytecode
allocation scan).  It never touches the card, the SCP81 listener or the
scripts; the install endpoints stay self-sufficient and the estimate is not
used for installation.

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

`imports` are the libraries the CAP is linked against (JC VM spec §6.6,
the Import component), each with the export-file version recorded in the CAP
and the number of distinct constant-pool references to it (§6.7; a linked but
unreferenced package is 0).  A card resolves an import only when the resident
package has the **same major** version and a **minor ≥** the recorded one
(§4.5.2), hence the `name >= version` display.  `flags` decodes the Header
package flags (Table 6-4: `0x01` uses `int`, `0x02` exports an API, `0x04`
applet package); `package_name` is only present in JC 2.2+ headers (CAP 2.1
files have none); `components` lists the archive entries in load-file order
(each size includes the component's tag and size header, so the sizes sum to
`load_file_bytes`).

`code.load_file` is the concatenated load file (all components) - the
package image the card stores, our proxy for the GlobalPlatform Card Spec
v2.3.1 Table 11-48 "non-volatile code" minimum memory requirement; the
tool's bytecode-only figure stays in `code.method_component`.  The suggested
`C6` follows the load file, and `nvram.requirement` = `code.load_file` +
`nvram.total` (the C6+C8-style total per §11.5.2.3.7: with no code/data
split in the card's memory the required minimum is the sum of both).  The
estimate excludes card-specific memory management, allocation rounding and
the GP registry entry.

**Errors** (HTTP 200 with `ok: false`, like `/api/scp81/gen-install`):
`{"ok": false, "error": "cap parse failed: File is not a zip file"}` for a
corrupt/wrong archive, or `cap analysis failed: …` when a component passes
the structural parse but not the analyzer.  The numbers are an estimate:
the model assumes 2-byte references, a 6-byte object header and NVM cell
rounding, and does not include applet-created runtime objects/arrays.

### `POST /api/test/run`

Runs a **test script**: an ordered list of actions and proactive-command
expectations, executed server-side on the equipped card.  While a run is
active the card is owned by the script - other card endpoints answer
`409 {"error": "test script running ..."}` and background STATUS polling is
suspended; only `/api/test/*`, `/api/status`, `/api/poll-status`,
`/api/version` and static files stay available.

```json
{"script": {"name": "STK menu browsing", "steps": [
   {"type": "action", "kind": "menu-select", "params": {"item_id": 128},
    "check": {"sw": "91??"}},
   {"type": "expect", "command": "SELECT ITEM",
    "checks": [{"kind": "item", "id": 1, "text": "test"}],
    "respond": {"result": "ok", "item_id": 1}},
   {"type": "expect", "command": "DISPLAY TEXT",
    "checks": [{"kind": "text", "value": "hello"}],
    "respond": {"result": "ok"}}
 ]},
 "preset": {"name": "lab card", "kic": "15", "kid": "15", "kicKey": "...",
            "kidKey": "...", "counter": "0000000A", "tar": "B00000",
            "spi1": "16", "spi2": "01"}}
```

**Action steps** (`type: "action"`): `kind` is `envelope` (`event`, `data`),
`menu-select` (`item_id` 1-255), `file-write` (`path`, `data`, `mode`
`auto`/`binary`/`record`, `record`), `file-read` (same, verifies `check.data`),
`apdu` (raw transport, no auto-handler), `scp80` (`apdu` or `sp`, selected by
the optional `source` field when both are present; optional `tar`/`spi1`/`spi2`
overrides - KIc/KID and the counter always come from the `preset`, which must
match the equipped card and be complete) or `status`
(`attempts`, `interval_ms` - when `attempts > 1` the default SW check is the
mask `91??`, i.e. poll until the card announces a command).

`check` is `{"sw": ..., "data": ...}` (exact or `{"mode": "mask", "value":
"91??"}`, `?` = per-nibble wildcard) plus `"por": "none"|"ok"|"any"` for
SCP80.  `on_fail` is `error` (terminates the script) or `warning` (continues).

**Expectation steps** (`type: "expect"`) require a command pending from the
previous step (`91XX`); they never poll - a `9000` response means no command
and is an error (TS 102 221 7.4.2.1 / TS 102 223 6.3; add a `status` action
if the card delivers on poll).  `command` is a name or type code; `checks`
may be `text` (contains/exact), `item` (`id`/`text` for SELECT ITEM / SET UP
MENU) or `raw` (mask); `respond` is the TERMINAL RESPONSE (`result` name or
value, `item_id`, `text`+`dcs`, raw TLVs).

The response is the initial state (`running: true`), the final counter
(`scp80_counter`) and the step list; poll `/api/test/status`.  The PWA writes
`scp80_counter` back to the card preset after the run.

### `GET /api/test/status`

The current (or last) run:

```json
{"running": true, "name": "STK menu browsing", "status": null,
 "index": 1, "total": 3, "scp80_counter": null,
 "steps": [{"index": 0, "type": "action", "label": "ENVELOPE(Menu Selection)",
            "status": "ok", "sw": "9103", "sent": "80C2000009...",
            "checks": [{"label": "SW", "ok": true, "expected": "91??",
                        "actual": "9103", "level": "error"}], "ms": 4}]}
```

`status` becomes `ok`/`warning`/`error`/`stopped` when the run finishes;
expected/actual pairs are reported per check.

### `POST /api/test/stop`

Requests a stop (`{"stop": true}` is set on the run); the runner finishes the
current step, answers any pending proactive command with a cancel TERMINAL
RESPONSE and reports the run as `stopped`.

### `POST /api/test/clear`

Clears a finished run report (409 while a run is active).

### `POST /api/ram-install`

Install a Java Card `.cap` file on the card via GlobalPlatform commands (INSTALL[for load] → LOAD ×N → INSTALL[for install (+ make selectable)]) wrapped in SCP80 secured packets. Each step is sent via ENVELOPE; the PoR verdict and the remote command's own status word are both checked and the sequence aborts on the first failure (a non-`por_ok` PoR, a remote SW outside the success set, or an undecodable PoR).  The counter advances only for a packet the card accepted (PoR `por_ok`); `final_cntr` is returned on success **and** on failure, so the caller keeps the card's consumed counter (a rejected packet leaves it unchanged).  The RAM command format is detected per operation: a read-only `GET STATUS [ISD]` probe (`FORMAT CHECK (compact)`, then `FORMAT CHECK (expanded)`) decides between the compact C-APDU and the expanded `AA`/`22` form (TS 102 226 §5.2.1), and every INSTALL/LOAD step of the chain then uses the detected format — the response carries `ram_format`.  Pass `ram_format` (`compact`/`expanded`) to pin it.  The **NV footprint** is measured around the chain: a best-effort `GET DATA FF21` (Extended Card Resources, TS 102 226 §8.2.1.7.2) read before the first step and again after the last one (also on failure) reports the free non-volatile memory and its delta — the read is silent (no step record, no failure) and a card without FF21 simply yields no fields.  The `.cap` archive (a ZIP of nested components) is parsed server-side in `_cap_parse`; no external tooling is required.

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
| `load_block_size` | no | Bytes of load-file payload per LOAD APDU, 1–240 (default 240 when omitted). SCP80 concatenation carries a secured packet larger than one SMS over up to 5 SMs, so the block size is no longer clamped to fit a single SMS. |
| `ram_format` | no | RAM command format: `auto` (default — the read-only probe decides, recorded as `FORMAT CHECK (...)` steps), `compact` or `expanded` to pin it |

**Response (success):**
```json
{"success": true, "failed_step": null,
 "steps": [{"name": "INSTALL [for load]", "por_status": "por_ok", "por_sw": "9000", "por_type": "compact", "por_cntr": "000000011B", "sw": "9000", "bytes": 58, "segments": 1},
           {"name": "LOAD (1/9)", "por_status": "por_ok", "por_sw": "9000", "por_type": "compact", "sw": "9000", "bytes": 274, "segments": 3},
           {"name": "INSTALL [for install]", "por_status": "por_ok", "por_sw": "9000", "por_type": "compact", "sw": "9000", "bytes": 66, "segments": 1}],
 "final_cntr": "0000000004",
 "load_file_aid": "A000000003000000",
 "module_aid": "A000000003000000",
 "application_aid": "A000000003000000",
 "load_block_size": 240,
 "load_block_size_requested": null,
 "load_block_size_auto": true,
 "nv_before": 50646,
 "nv_after": 23400,
 "nv_delta": 27246}
```

`load_block_size` is the effective size used for the LOAD blocks (240 by
default), `load_block_size_requested` echoes an explicit `load_block_size`
(null = the default was used) and `load_block_size_auto` marks that default.
Each step reports the secured packet size `bytes` and the number of SMS
`segments` it took.  `sw` is the transport (ENVELOPE/GET RESPONSE) status
word; the RAM results are in `por_status` (the PoR verdict: `por_ok`,
`rc_cc_ds_failed`, `cntr_low`, ... or `no_por` when the card sent none) and
`por_sw` (the remote command's own status word from the compact/expanded
response, with `por_type`/`por_cntr`/`por_data`/`por_raw` for context).

`nv_before`/`nv_after` are the free non-volatile memory (bytes) read via
`GET DATA FF21` before and after the chain, `nv_delta` their difference (what
the operation consumed; negative = memory was released, e.g. a discarded
partial load).  The three fields are omitted when the card has no FF21
readout; on a failure the values still show what the failed attempt consumed.
The two FF21 reads consume counters like any accepted packet, so
`final_cntr` already includes them.

A step fails when the PoR is not `por_ok`, when `por_sw` is outside the
success set (`9000`, `61xx` more data, `62xx`/`63xx` warnings, `CAFE` GP
"more data"), or when response data arrived but could not be decoded.  The
failing step carries `por_error` (e.g. `remote SW 6700`) and the response
sets `success: false`, `failed_step` and a detailed `error` such as
`LOAD (1/9): remote SW 6700`.

**Response (failure):**
```json
{"success": false, "failed_step": "load_1",
 "steps": [{"name": "install_for_load", "por_status": "por_ok", "sw": "9000"},
           {"name": "load_1", "por_status": "rc_error", "sw": null}],
 "error": "..."}
```

The `steps` array contains one entry per GP command. `final_cntr` is the counter value after all successful steps (use it to update the card preset). The response is not streamed — all steps run server-side before the JSON is returned.

### `POST /api/ram-install-app`

Run the single **INSTALL [for install]** (or **INSTALL [for make selectable]**) APDU for an *already loaded* package — one APDU, so the install parameters can be iterated without deleting and re-loading the `.cap`.  The APDU builders are shared with `/api/ram-install` (`_cap_install_apdu` / `_cap_make_selectable_apdu`; both case 3 — no trailing `Le`, which the card's SCP80 layer counts as a phantom command) and the step verdict/counter logic is the same (`final_cntr` is returned on success **and** on failure).

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
| `install_params` / `stk_params` | no | As in `/api/ram-install` (the PWA composes `C9`+`EF`+raw and appends the STK part) |
| `ram_format` | no | As in `/api/ram-install`: `auto` (default — a read-only probe step decides), `compact` or `expanded` |

**Response:** `{"success": bool, "steps": [...], "final_cntr": "...", "ram_format": "...", "error": "...", "failed_step": N}` — the same step records as `/api/ram-install`.

### `POST /api/cap-compat`

Run the **CAP compatibility test**: the format check, `INSTALL [for load]` and the LOAD blocks only up to the block that completes the **Import** component (the point where the JCRE verifies the import list, JC VM spec 4.5.2), then stop — no last-block flag and no `INSTALL [for install]`, so nothing is committed and nothing has to be deleted afterwards.  The verdict (`imports_ok`) covers the **LOAD/import gate only**; the link gate is what a real install checks at `INSTALL [for install]`.

**Request body:** as `/api/ram-install` (`cap_hex`, the SCP80 fields, optional `load_block_size` and `ram_format`), plus:

| Field | Req | Description |
|---|---|---|
| `probe_imports` | no | `{"all": "x.y"}` or `{aid_hex: "x.y"}`: rewrite the Import component's requested versions (diagnostic) |
| `probe_additions` | no | `[{"aid": hex, "version": "x.y"}]`: append synthetic imports (card-capability queries; an AID already present becomes a version override) |

**Response:** `{"success": bool, "imports_ok": bool, "steps": [...], "boundary_block": N, "total_blocks": M, "load_file_aid": "...", "module_aid": "...", "ram_format": "...", "probe_imports": [...], "probe_unmatched": [...], "final_cntr": "...", "load_block_size": N, "error": "...", "failed_step": N}`.

`probe_imports` lists the entries changed or appended (`{"aid": "...", "from": "1.3", "to": "0.0"}`; `from: null` = an appended synthetic entry).  `probe_unmatched` lists override AIDs that matched no Import entry — the CAP does not import them, so the card is never asked about them — as `[{"aid": "...", "version": "0.0"}]` with the version the probe requested.  The PWA renders both as a per-AID list in the result popup, together with the failing block's CAP section and the remote status word's meaning.

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

```json
{"event_type": 4, "event_data": "01A0"}
```

`event_type` is required (the SET UP EVENT LIST event byte); `event_data` is
optional hex for events that carry data. Returns the SW and any response data:

```json
{"sw": "9000", "data": "..."}
```

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
data means *accepted*. A *modified* answer sets the background poll interval,
and a *rejected* answer may carry the closest acceptable duration.

Channel status (event `0x0A`, TS 102 223 §8.56) carries the Channel status TLV
`B8 02 <status> <info>`, where the status byte is the channel id (1–7) OR-ed
with the state bits (0x00 link not established / 0x40 TCP LISTEN / 0x80 link
established) and the info byte is `00` (no further info) or `05` (link
dropped). The server also sends this event automatically when a BIP link drops
outside a proactive command and the card subscribed to `0x0A`.

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
ENVELOPE and AUTHENTICATE are sent — EF.FPLMN is appended only by
`roaming_denied` (shift-list semantics of TS 31.102 §4.2.16, duplicates
skipped) and an attach to a listed PLMN clears its entry first (successful
manual selection, TS 23.122), while the 5GS location files are never written.
The response is `{success, error, steps:[{action, file, path, data, sw, ok}],
net_state}` where `net_state` is the updated monitor state (see
`GET /api/net-state`).

### `GET /api/net-state`

Returns the cached **network-state monitor** — the panel the PWA's Phone tab
shows next to the simulation buttons. The state is created when a card is
equipped (only when EF.ICCID was readable) and is updated in place from the
bytes the network simulation wrote, so this endpoint performs no card I/O:

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
card equipped or EF.ICCID not readable). `service.state` is the simulated
service state (`normal` / `limited` / `none`, `null` until a scenario or a
Location status event sets it); `network.location.roaming` is `home` /
`equivalent` / `guest` against EF.HPLMNwAcT and EF.EHPLMN, and `rejected` is
set when the location files or EF.FPLMN show a permanent rejection.
Monitored file keys: `imsi`, `ehplmn`, `spdi`, `hplmnwact`, `loci`, `psloci`,
`epsloci`, `epsnsc`, `cbmi`, `cbmir`, `smsstatus`, `fplmn`; each entry's
`source` is `init` (equip read), `read`, `refresh` or `write` (patched from
the simulator's own writes). The PWA decodes the entries client-side.

### `POST /api/net-state-refresh`

Re-reads the monitored files from the card, merges them into the cached state
(`source: refresh`) and returns the same shape as `GET /api/net-state`. The
optional body filters the read:

```json
{"files": ["imsi", "loci", "epsnsc"]}
```

Without a body all monitored files are re-read. Answers `503` when the reader
is not initialized.

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

Background STATUS polling state. Polling is enabled by default (a CAT
terminal polls during idle, TS 102 221 §14.6.2; `--poll-interval 0` disables
it at startup) and `enabled` is the operator's switch. `card_disabled` is true
after the card sent POLLING OFF (TS 102 223 §6.4.14): proactive polling stays
suspended until a POLL INTERVAL arrives, independent of `enabled` - the PWA
shows the effective state (OFF while suspended).

```json
{"enabled": true, "interval": 30, "card_disabled": false}
```

### `POST /api/poll-toggle`

Turns background STATUS polling on or off.

```json
{"enabled": true}
```

Returns the new state (`{"enabled": ..., "interval": ..., "card_disabled": ...}`);
while the card has disabled polling, enabling adds a `warning` explaining
that the timer stays suspended until the card sends a new POLL INTERVAL.

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
 "script": ["80CAFF2100", "80F28002024F0000"], "script_kind": "Explore ISD"}
```

`script` is the APDU list served to the card (an explicit list, or `none`);
the server is agnostic to what the APDUs do. `script_kind` is an optional
label for the logs/results. Omitting `script` keeps the configured script and
its run progress.

Stop either mode with `{"action": "stop"}` (also disables the BIP terminal).

### `POST /api/bip/control`

Generic BIP terminal control for the Simulator's **BIP** pill - the terminal
side of the Bearer Independent Protocol for cards that use TCP without HTTP
OTA. The session is shared with the SCP81 listener: only one BIP session runs
at a time, starting either control stops the other, and the response reports
what was stopped as `replaced: {"owner": "scp81"|"bip", "mode": ...}` (the
PWA shows a notice).

```json
{"action": "start", "mode": "sink", "host": "127.0.0.1", "port": 0}
```

Modes:

- `sink` (default) - starts a local TCP listener that accepts the card's BIP
  channels and only logs what arrives (`conn`, `sink-rx`, `conn-close`); it
  never sends anything back. `port` may be `0`/omitted for an ephemeral port;
  the bound address is reported in the status. Use it for cards that open a
  TCP connection just to upload data.
- `passthru` - no listener and no target: each channel dials the destination
  the card requests in OPEN CHANNEL (TCP client only, no default port; an
  incomplete or non-TCP request fails with result `3A`).
- `redirect` - every channel connects to the required `host`/`port`; the
  address the card requests is only logged.

`link_events` (default `true`) controls the terminal-side Channel status
events (TS 102 223 7.5.11). An invalid request never disturbs a running
session. Stop with `{"action": "stop"}`; the response carries the same body
as the status endpoints.

### `GET /api/bip/status`

Same shape as `GET /api/scp81/status`: `{"owner": "bip"|"scp81"|null,
"bip": {...}, "listener": {...}}`. A running sink reports
`{"mode": "sink", "host": ..., "port": ..., "connections": N}`.

### `GET /api/bip/log` / `POST /api/bip/log-clear`

The shared BIP event log (`?after=<seq>`) - identical to `/api/scp81/log`.

### `GET /api/scp81/status`

```json
{"bip": {"enabled": true, "mode": "redirect", "target": "127.0.0.1:8443", "channels": [], "seq": 12},
 "listener": {"mode": "tls", "host": "127.0.0.1", "port": 8443,
              "psk_identities": ["89012345678901234567"], "psk_wildcard": false,
              "identity_seen": "89012345678901234567", "identity_matched": true,
              "version_seen": "TLSv1.2", "cipher_seen": "PSK-AES128-CBC-SHA256"}}
```

`owner` is `scp81` or `bip` (whichever control started the session), `null`
when idle; it is returned by both this endpoint and `/api/bip/status`.

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
 "done": [0, 1], "kind": "Explore ISD",
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
