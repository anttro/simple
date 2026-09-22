# SIMple — a UICC workbench: APDU · OTA · STK · profiling

> Formerly known as OTAMan — renamed to SIMple on 2026-09-19.

SIMple is a workbench for UICC/SIM card specialists: an offline HTML/JS PWA plus a local card server ([`pysim-simple-server`](pysim_simple_server/), a pySim/PC-SC bridge that also serves the PWA). It covers the full lab cycle:

- **APDU workbench** — SIM/USIM RFM and Expanded Script builders (TS 102 226), GlobalPlatform RAM commands, a C-APDU/R-APDU parser and a response decoder (ISO 7816 / TS 102 221 / GP);
- **OTA lab** — SCP80 secured packets (TS 102 225) and Java Card RAM installation, plus the terminal side of HTTP OTA (SCP81): BIP channel emulation, a PSK TLS administration server and APDU script execution;
- **Phone/CAT simulator** — SIM Toolkit menu browser, proactive command / event handling, TERMINAL PROFILE editing and the C-AT dialogue log;
- **Card profiler** — filesystem capture (profiles and immutable snapshots with timings), exact snapshot comparison and FCP/FCI decoding.

**Demo:** [simple.atroshin.ru](https://simple.atroshin.ru) — the PWA alone, for experimenting. Install the server (below) for card-reader functions.

## Quick start

**PWA only (client-side tools):** open `frontend/index.html` in any browser, or serve `frontend/` with any static server. No Python required.

**Full (PWA + card server):**

```sh
git clone https://github.com/anttro/simple.git
cd simple
./setup.sh     # or setup.bat on Windows — creates .venv, installs pysim + server
./start.sh     # or start.bat — starts the server (it serves the PWA too)
```

Then open http://127.0.0.1:8080 — the UI and API share one origin, so no CORS or browser-permission setup is needed. The server starts fine with an empty reader (no card is a normal state): insert a card and it is initialized automatically (auto-equip), or press **Equip card** in the UI.

## Build

Tailwind CSS is used for styling. After cloning, rebuild the CSS:

```sh
cd frontend
npm install
npm run build
```

## Interface

Seven top-level tabs: **Remote APDU**, **SCP80**, **SCP81**, **Cards**, **Profiler**, **Card reader**, and **Phone simulator**. **Remote APDU** and **SCP80** use pill sub-tabs; the **SCP81** tab has **Listener** and **Scripts** pills; the Card reader tab has three sub-tabs: **File manager**, **pySim command line**, and **Raw APDU**; the Profiler tab lists **Profiles**, **Card snapshots**, and **Custom files**.

---

## Remote APDU tab

Builds command APDUs (C-APDUs). Seven sub-tabs cover different card generations, command sets and decoding tools: **SIM RFM**, **USIM RFM**, **Expanded Script**, **RAM/GP**, **HTTP OTA**, **C-APDU Parser**, and **Response parser**.

### SIM RFM

CLA = `A0` (GSM 11.11 / ISO 7816-4).

#### Commands

| Command | INS | Description |
|---|---|---|
| SELECT | A4 | Select EF/DF by FID, path, dfname, or chain |
| UPDATE RECORD | DC | Update a record in a record-oriented EF |
| UPDATE BINARY | D6 | Update binary content at an offset |
| READ RECORD | B2 | Read a record |
| READ BINARY | B0 | Read binary content |
| ERASE BINARY | 0E | Erase binary at an offset |
| ACTIVATE FILE | 44 | Activate a file |
| DEACTIVATE FILE | 04 | Deactivate a file |
| VERIFY PIN | 20 | Verify PIN1 or PIN2 |
| CHANGE PIN | 24 | Change PIN1 or PIN2 |

#### SELECT methods

| Method | P1 | P2 | Input |
|---|---|---|---|
| By FID | 00 | 00 | 2-byte FID (4 hex) |
| By full path from MF | 08 | 00 | Full path hex from MF |
| By DF name / AID | 04 | 00 | AID (application ID) |
| ADF RFM chain | 00 | 00 | Comma-separated FIDs, each selected in turn |

#### Options

- **Start with SELECT** — checkbox to prepend a SELECT command before the operation. When unchecked, the operation is sent standalone with CLA.
- **Selection mode (P2)** — for record commands: Absolute (04), Next (06), Previous (02).
- **Record size** — pad/truncate data to the specified byte count.
- **Allow P1/P2 editing** — checkbox to enable manual override of P1/P2 bytes.

#### References

- ISO/IEC 7816-4: Organization, security and commands for interchange
- ETSI TS 102 226: Remote APDU structure for UICC based applications
- GSM 11.11: SIM-ME Interface

### USIM RFM

CLA = `00` (ETSI TS 102 221). Same commands as SIM, but SELECT uses P1=09, P2=0C (by FID from current directory).

#### References

- ETSI TS 102 221: UICC-Terminal Interface; Physical and Logical Characteristics
- ETSI TS 102 226: Remote APDU structure for UICC based applications

### Expanded Script

Builds Expanded Remote Application data format per ETSI TS 102 226 §5.2.1.

#### Format

Two encoding variants:
- **Definite (AA)**: `AA` + length + Command TLVs
- **Indefinite (AE)**: `AE` + `80` + Command TLVs + `00 00`

#### Command TLVs

| Type | Tag | Description |
|---|---|---|
| C-APDU | 22 | Raw APDU hex |
| Immediate Action | 81 | Proactive command or action indicator |
| Error Action | 82 | Proactive command on error |
| Script Chaining | 83 | Chaining data for multi-packet scripts |

#### Immediate Action builder

When the type is set to Immediate Action, the tool provides a structured builder for:

- **Action indicator**: `81` (Proactive session indication) / `82` (Early response)
- **Proactive command**: REFRESH, DISPLAY TEXT, or PLAY TONE — with auto-generated COMPREHENSION-TLV data objects (command details, device identities, text string, tone, etc.)
- **Custom hex**: freeform input for manual TLV construction

Error Action supports the same builder (DISPLAY TEXT, PLAY TONE).

#### References

- ETSI TS 102 226 V13.0.0 §5.2.1: Expanded Remote Application data format
- ETSI TS 102 223: Card Application Toolkit (CAT) — proactive command structure
- ETSI TS 101 220: BER-TLV tag assignments

### RAM/GP

CLA = `80` (GlobalPlatform Card Specification v2.3.1). Remote Application Management commands for card content management.

#### GP Commands Reference

| Command | INS | P1 | Description |
|---|---|---|---|
| INSTALL [for load] | E6 | 02 | Register a load file for loading |
| INSTALL [for install] | E6 | 0C | Install an application or SD |
| INSTALL [for make selectable] | E6 | 10 | Make an application selectable |
| INSTALL [for registry update] | E6 | 01 | Update registry entries |
| INSTALL [for extradition] | E6 | 04 | Extradition (move between SDs) |
| LOAD | E8 | 00 | Load executable code blocks |
| DELETE | E4 | 00/80 | Delete application or SD |
| GET STATUS | F2 | 80/40/20/10 | Get card status |
| GET DATA | CA | tag | Read card data objects |
| STORE DATA | E2 | 00/40/80/C0 | Store data (key, certificate, etc.) |
| SET STATUS | F0 | 80/40/60 | Lifecycle state management |
| EXTERNAL AUTHENTICATE | 82 | 00 | SCP host authentication |
| INTERNAL AUTHENTICATE | 88 | 00 | Card challenge-response |

#### INSTALL [for install] — Privilege Builder

Tag `C7` in the INSTALL data field. Built from 3 privilege bytes (GP spec Tables 11-7, 11-8, 11-9):

**Byte 1** (bits):
| Bit | Privilege |
|---|---|
| b8 | Security Domain |
| b7 | DAP Verification |
| b6 | Delegated Management |
| b5 | Card Lock |
| b4 | Card Terminate |
| b3 | Card Reset |
| b2 | CVM Management |

**Byte 2** (bits):
| Bit | Privilege |
|---|---|
| b8 | Trusted Path |
| b7 | Authorized Management |
| b6 | Token Verification |
| b5 | Global Delete |
| b4 | Global Lock |
| b3 | Global Registry |
| b2 | Final Application |

**Byte 3** (bits):
| Bit | Privilege |
|---|---|
| b8 | Receipt Generation |

#### INSTALL [for install] — SIM/UICC Toolkit Parameters

Optional TLV objects appended to the INSTALL data field:

- **Tag `CA`** (SIM Toolkit): Priority, Timers, Text Length, Menu Entries, Menu Positions, Channels, MSL, TAR, Access Domain
- **Tag `80`** (UICC Toolkit, inside `EA`): Same fields minus Access Domain

**MSL (Minimum Security Level)** — SPI1 byte per TS 102 225:
| Value | Meaning |
|---|---|
| 00 | No check |
| 11 | RC/CC/DS |
| 12 | RC/DS/CC |
| 15 | RC/DS/CC + MAC |
| 16 | RC/DS/CC + MAC + Cipher |
| 19 | RC/DS/CC + MAC + Cipher + DS |

#### GET STATUS P1 values

| Value | Meaning |
|---|---|
| 80 | Issuer Security Domain (ISD) |
| 40 | Applications and Supplementary Security Domains |
| 20 | Executable Load Files |
| 10 | ELF and their Executable Modules |

#### GET STATUS P2 values

| Value | Meaning |
|---|---|
| 40 | First/all occurrences, GP TLV format (default) |
| 42 | Next occurrence, GP TLV format |
| 00 | First/all, old format (deprecated) |
| 02 | Next, old format (deprecated) |

#### GET DATA tag values

| Tag | Data Object |
|---|---|
| 42 | Issuer Identification Number (IIN) |
| 45 | Card Image Number (CIN) |
| 66 | Card Data / SD Management Data |
| 67 | Card Capability Information |
| E0 | Key Information Template |
| D3 | Current Security Level |
| 2F00 | List of Applications (ISO 7816-4) |
| FF21 | Extended Card Resources Info |
| 5F50 | SD Manager URL |
| C1 | Sequence Counter (SCP02/03) |
| C2 | Confirmation Counter |
| 7F21 | Certificate (SD public key) |
| 5031 | Certificate info (EF.OD) |

#### DELETE P1 values

| Value | Meaning |
|---|---|
| 00 | By AID |
| 80 | Delete associated objects |

#### STORE DATA P1 values

| Value | Meaning |
|---|---|
| 00 | Last block, no encryption |
| 40 | More blocks, no encryption |
| 80 | Last block, encrypted |
| C0 | More blocks, encrypted |

#### SET STATUS parameters

**P1 (Status Type)**:
| Value | Target |
|---|---|
| 80 | Issuer Security Domain |
| 40 | Application or Supplementary Security Domain |
| 60 | Security Domain and its associated Applications |

**P2 (State)**:
| Value | Action |
|---|---|
| 00 | Unlock (return to previous state) |
| 80 | Lock (LOCKED state) |

#### References

- GlobalPlatform Card Specification v2.3.1 (GPC_Spec_v2.3.1): Commands, Privileges, TLV structures
- ETSI TS 102 226 V13.0.0 §8.2.1.3.2: SIM/UICC Toolkit parameters, MSL, TAR, Access Domain

### Conversion (SIM/USIM sidebars)

Value encoding conversions embedded in the SIM RFM and USIM RFM tabs.

#### IMSI → EF.IMSI

Per TS 31.102 §4.2.3. Encodes a 15-digit IMSI into the 9-byte EF.IMSI format:
- Byte 0: number of subsequent bytes (8)
- Odd/even indicator nibble in the last byte
- BCD digits, swapped nibble pairs per identity

Input: 15 decimal digits. Output: 18 hex characters.

#### MSISDN → BCD

Strips leading `+`, pads odd length with `f`, swaps nibble pairs.

#### ICCID → hex

Swaps nibble pairs of the ICCID string.

#### Provider Name → SPN

Per 3GPP TS 31.102 §4.2.5 (EF_SPN). Three encoding paths:

1. **GSM 7-bit packed** (all chars in GSM 7-bit default alphabet): prefix `01`, DCS byte (spare bits), packed septets, 0xFF padding to 16 bytes.
2. **UCS2 non-BMP** (emoji / chars > U+FFFF): prefix `00`, DCS `80`, UTF-16BE, 0xFF padding to 16 bytes.
3. **UCS2 BMP non-GSM7** (Cyrillic, etc.): prefix `00`, DCS `81`, base byte, per-char offsets, 0xFF padding to 16 bytes.

GSM 7-bit alphabet per 3GPP TS 23.038. Full extension table supported.

#### PLMN → EF_PLMNsel / PLMNwAcT

Per TS 31.102 §4.2.3. 3-byte BCD encoding for PLMN, plus optional 2-byte Access Technology selector.

#### Nibble swap

Swaps nibble pairs of an even-length hex string.

#### References

- 3GPP TS 31.102: Characteristics of the USIM Application
- 3GPP TS 23.038: Alphabets and language information
- ETSI TS 102 225: Secured packet structure for (U)SIM toolkit
- pySim: enc_imsi() implementation

### C-APDU Parser

Pastes raw APDU hex and renders a collapsible tree. It auto-detects the container: an **Expanded Script** (leading `AA` or `AE80`, decoded per ETSI TS 102 226 §5.2.1) or a **Compact C-APDU chain** (a sequence of ISO 7816 C-APDUs). Each node shows its label, hex, and a short description; parent nodes expand to reveal their sub-elements.

### HTTP OTA

Builds the Remote Application Management over HTTP payloads defined in GlobalPlatform **GPC v2.2 Amendment B v1.1** (§4.7). Two modes:

- **Trigger (Push SMS)** — administration session triggering parameters (`81 > 83 > 84/[85]/[86]/89`, Table 4-3). This is the message that asks the card's Security Domain to dial out and start an HTTP session.
- **Store (SD admin params)** — writes the same parameters as card (Security Domain) data via **STORE DATA in TLV mode** (`80 E2 90 00`, P1=90 = last block + BER-TLV per GP v2.2 Amendment B v1.1.3), wrapped in tag `85` (or `A5`) per Table 4-4.

| Section | Tag | Contents |
|---|---|---|
| Connection parameters | `84` | COMPREHENSION-TLVs needed to open the TCP connection (OPEN CHANNEL per TS 102 223): Device Identities `02`, Alpha `80`, Bearer `01`, vendor TLVs. Row editor + presets, editable hex. |
| Security parameters | `85` | Table 4-6: LV PSK Identity (text), LV Key version/KID. Identifies the PSK TLS key (RFC 4279). |
| Retry policy | `86` | Table 4-7: retry counter (2 bytes, e.g. `B000`), retry waiting delay as the TS 102 223 timer TLV (`25 03 HH MM SS`), optional vendor-specific report-failure TLV. |
| HTTP POST | `89` | Tables 4-8/9/10: Host header (`8A`), X-Admin-From agent ID (`8B`), URI (`8C`) — text converted to octets. |

The **Command Scripting template** checkbox wraps the whole `81` triggering command in the definite-length Expanded Remote Application data format (`AA`, ETSI TS 102 226 §5.2.1) for TARs that process the expanded format. **Pack into Secured packet** sends the built payload to the SCP80 tab for SPI/counter filling — insert the TAR the SD listens on (typically the OTASD TAR) there.

---


### Response parser

Decodes a raw command response: pick the command that was sent, enter the SW (e.g. `9000`) and the response data hex, then press **Decode**.

- **Command** — SIM/USIM group (SELECT, STATUS, READ/UPDATE, PIN ops, CAT commands like TERMINAL PROFILE/ENVELOPE/FETCH/TERMINAL RESPONSE, MANAGE CHANNEL, ...) or RAM/GP group (INSTALL, LOAD, DELETE, GET/STORE DATA, auth, SCP commands).
- **SW decode** — status words resolved against generic, UICC (TS 102 221), and GlobalPlatform maps, with context auto-detected.
- **Privilege decode** — GET DATA / INSTALL response payloads decode the privilege bytes into human-readable flags.
- **Response data** — raw hex rendered and interpreted per command (e.g. SELECT FCP templates).

---


## SCP80 tab

The **SCP80** top-level tab groups SCP80-related views, switched by two pills: **Secured Packet** and **RAM**. Assembles secured packets per ETSI TS 102 225. When a card is equipped the server reads its EF.ICCID (2FE2); if a preset carries the same number (digits, or the raw EF hex), it is selected automatically in both views.

### Secured Packet

Builds SCP80 secured packets per ETSI TS 102 225.

#### Packet structure

| Field | Size | Description |
|---|---|---|
| CPI | 1 | Command Packet Identifier (`02`) |
| CPL | 1 | Command Packet Length |
| CHI | 1 | Command Header Identifier (`01`) |
| CHL | 1 | Command Header Length |
| SPI | 2 | Security Parameter Indicator |
| KIc | 1 | Key Identifier for ciphering |
| KID | 1 | Key Identifier for MAC |
| TAR | 3 | Toolkit Application Reference |
| CNTR | 5 | Replay counter |
| PCNTR | 1 | Padding counter |
| RC/CC/DS | 8 | Cryptographic Checksum / MAC |
| Secured Data | variable | Padded APDU (encrypted if required) |

#### SPI1 (Security Level)

SPI1 bit layout (TS 102 225 §5.1.1): `b8–b6` padding, `b5–b4` counter, `b3` ciphering, `b2–b1` RC/CC/DS.

| Value | Security | Ciphering | Counter (b5 b4) |
|---|---|---|---|
| 00 | None | No | 00 none |
| 01 | RC | No | 00 none |
| 02 | CC/MAC | No | 00 none |
| 06 | CC/MAC | Yes | 00 none |
| 0A | CC/MAC | No | 01 available |
| 0E | CC/MAC | Yes | 01 available |
| 12 | CC/MAC | No | 10 higher |
| 16 | CC/MAC | Yes | 10 higher |
| 1A | CC/MAC | No | 11 +1 |
| 1E | CC/MAC | Yes | 11 +1 |

> **AES requires `b5 b4 = 10` (higher) or `11` (+1)** per TS 102 225 §5.1.2 and §5.1.3.1.
> The 3DES values `00/01/02/06` (no counter) remain valid for 3DES only.

#### SPI2 (PoR settings)

| Value | Mode | Security | Cipher |
|---|---|---|---|
| 00 | No PoR | — | No |
| 01 | PoR required | None | No |
| 05 | PoR required | RC | No |
| 09 | PoR required | CC | No |
| 0D | PoR required | DS | No |
| 11 | PoR required | None | Yes |
| 02 | PoR on error | None | No |
| 06 | PoR on error | RC | No |

#### Crypto

- **3DES-CBC** encryption (zero ICV), supporting 8, 16, and 24 byte keys — deprecated since Rel-18, still supported for backwards compatibility
- **AES-CBC** encryption (zero ICV, zero-padded to 16), supporting 16, 24, and 32 byte keys (TS 102 225 §5.1.2, KIc `x2`)
- **Retail MAC** (ISO 9797-1 MAC algorithm 3) for the DES/3DES cryptographic checksum
- **AES-CMAC** (NIST SP 800-38B, truncated to 8 octets) for the AES cryptographic checksum (TS 102 225 §5.1.3.1, KID `x2`)
- Padding byte configurable (`00` per TS 102 225 default, or `FF`)

#### PoR (Proof of Reception)

PoR confirms the card received and executed the secured packet. Two modes:

| SPI2 (bit 5) | Mode | Description |
|---|---|---|
| `0x00` | Delivery PoR | PoR is returned in the ENVELOPE response SW+data |
| `0x20` | Submit PoR | PoR is sent back as an SMS-SUBMIT via a proactive FETCH command |

Delivery PoR (SPI2 `01`) is simpler — the card returns the PoR directly in the ENVELOPE response. Submit PoR (SPI2 `21`) is used when the card cannot respond inline (e.g. during ELF operations where the ENVELOPE response space is limited).

#### References

- ETSI TS 102 225 V18.1.0: Secured packet structure for UICC based applications
- ETSI TS 102 226: Remote APDU structure for UICC based applications
- ISO 9797-1: MAC algorithms
- NIST SP 800-38B: CMAC

### RAM

All RAM operations are delivered as SCP80 secured packets (ETSI TS 102 225) via SMS-PP-DOWNLOAD ENVELOPE. The card must support SCP03 (AES or 3DES) for secure transport.

Select a saved card configuration from the **Card preset** dropdown. If no preset is selected, the RAM tab warns and refuses to execute.

The RAM subtab offers two operations selected from the **Operation** dropdown:

| Operation | Description |
|---|---|
| **Explore Card (all GP data)** | Queries GET STATUS for ISD, Applications, ELFs, and ELF Modules, plus GET DATA FF21 for memory info. Results appear in an explorer view with per-item **Delete** buttons. |
| **Install Package (.cap file)** | Sends a `.cap` file to the card via the server: INSTALL\[for load\] → LOAD ×N → INSTALL\[for install (+make selectable)\]. |

#### Explorer View

After "Explore Card" runs, the explorer view displays:

- **ISD** — AID, lifecycle, privileges (no delete; the ISD cannot be removed)
- **Applications** — AID, lifecycle, privileges, associated ELF/SD. Each has a **Delete** button (GP `DELETE` by AID).
- **Executable Load Files** — AID, lifecycle, version, module AIDs. Each has **Delete** (ELF only) and **Delete All** (cascade: ELF + modules + installed Applications, P2=0x80) buttons.

Delete confirms via a browser prompt before sending the GP `DELETE` command via SCP80. The explorer auto-refreshes after a successful deletion.

---

## Cards

Stores saved card configurations (presets) in `localStorage`. A preset holds the cryptographic keys, SPI settings, TARs and replay counter for SCP80 operations, the optional **ADM** key, plus the **PSK identity / PSK key** pair used by the SCP81 HTTP OTA listener. Cards is a **top-level tab**. The form groups the fields into two bordered blocks — **SCP80 (GSM 03.48, ETSI TS 102 225)** and **SCP81 (HTTP OTA)** — with the optional **ADM** field in the top row. When the card is equipped its EF.ICCID is read and the preset with the same ICCID is selected automatically in both SCP80 views. The header shows gray **SCP80** / **SCP81** markers and a key glyph on the **ADM** badge when the preset matching the equipped card's ICCID has those settings filled in.

| Field | Description |
|---|---|
| Name | Human-readable label (required) |
| ICCID | Optional card identifier |
| ADM | Optional administrator PIN (hex, or up to 8 ASCII digits), stored for the file manager; not used by the SCP80/SCP81 views |
| SPI1 / SPI2 | Security level and PoR settings |
| KIc / KID index | Key version number (required together with the keys) |
| KIc / KID key | Encryption and MAC key hex |
| ISD TAR | Issuer Security Domain TAR (TS 101 220 Annex D), used for RAM/GP operations; spec default `000000` |
| UICC RFM TAR | UICC Shared File System RFM TAR (TS 102 226 §7.2), used by the SIM RFM view; spec default `B00000` |
| ADF RFM TAR | ADF RFM TAR (TS 102 226 §7.3), linked to the ADF AID (ADF.USIM in the USIM RFM view); spec default `B00001` |
| Counter (CNTR) | 10-digit hex replay counter, auto-incremented after each successful SCP80 send |
| PSK identity | SCP81 HTTP OTA: the identity the card sends in the TLS handshake |
| PSK key | SCP81 HTTP OTA: 32 hex chars (16 bytes); the listener picks it by the identity the card presents |

The **SCP81** column shows whether the preset supplies a usable PSK pair: **✓** (identity and key), **⚠** (only one of the two — the listener ignores such a preset), **—** (no PSK). Identity and key must be set together.

**Add a card:** fill in the name, ICCID (optional — **From card** fills it from the equipped card's EF.ICCID), the optional **ADM**, SPI1/SPI2, KIc/KID keys and indices, the three TARs, the SCP81 PSK pair (optional) and click **Add**. A duplicate ICCID (compared ignoring spaces and the raw-hex form) is refused, naming the conflicting preset. The card appears in the list and becomes available in the RAM tab's **Card preset** dropdown.

**Edit / remove:** **Edit** loads a preset into the form (the Add button becomes **Save**; **Cancel** clears the form); **Remove** deletes the row from `localStorage`. A successful SCP80 send advances and stores the replay counter, and edits are pushed into a running SCP81 listener automatically.

---

## Card Reader (pySim integration)

Connects to the bundled [`pysim-simple-server`](pysim_simple_server/) for live card operations.

> **Browser restriction:** when the PWA is served from a public HTTPS host, reaching the local server (`http://127.0.0.1:8080`) requires two things: the server must send `Access-Control-Allow-Private-Network: true` (pysim-simple-server ≥ 1.6.1 does this automatically), and the browser must be allowed to access the local network — in Chrome/Edge/Vivaldi: Site settings → Local network access → allow the site (or accept the permission prompt). Without the browser permission, the request to `127.0.0.1` is blocked before any preflight is sent.

### File Browser

Browse the UICC filesystem in a tree view. Files are shown with names, FIDs, and AIDs (for ADFs). Selecting a file shows its detail pane: FID, file type, size / record layout and the decoded FCI (in a bordered block labelled with the file's symbolic name) above the content pane.

- Entries are grouped with DFs above EFs and sorted by **FID** or symbolic **Name** (pills pinned above the tree together with **Probe all files**, remembered in `localStorage`)
- **Read raw** / **Read decoded** — read the selected file as a hex dump or as a decoded field table (client-side decoders for IMSI, ICCID, SPN, PLMN lists, LOCI/EPSLOCI, ADN/MSISDN, service tables, SUME, …); the highlighted pill is the current view and clicking either pill (re-)reads the file. The server-side pySim JSON of the same read stays behind a collapsed *pySim JSON (server)* disclosure; **Read decoded** is disabled when the client-side decoders do not cover the file (resolved by name/FID before reading)
- **Edit raw** — edit the raw hex data and **Save** to write back (or **Cancel**); transparent files use a textarea, record files one input per record with selection checkboxes. The decoded view is read-only — **Edit raw** switches to the hex view first and reads the file if needed
- **Probe all files** — walks the whole tree (incl. custom files), marks every entry present/absent with *N / total* progress, stoppable, and ends with a summary; browsing itself stays lazy. Missing files are shown in red (✗); a present but empty DF shows `(empty)`
- **ADM** — files that need the administrator PIN fail with `6982`/`9804`; when the matching card preset (same ICCID) carries an ADM key, a **Verify ADM** button appears next to the error and the header badge (`ADM ✓/✗ ⚿`) becomes clickable. Every wrong key consumes an attempt (the remaining attempts are shown and a retry asks for confirmation); a blocked ADM needs the card's unblock key

### Command Hints

Type a command name in the **pySim command line** input. Usage hints appear as a tooltip after 300ms. Command autocomplete suggestions appear above the input.

---

## Profiler

Verifies that a card matches a named **profile** — an ordered set of rules describing the expected file system and, optionally, file contents. Profiles are stored in `localStorage`.

- **New profile** creates an empty ruleset; **Profile from card** scans the equipped card and generates one rule per existing file; **Profile from snapshot** generates the same ruleset from a saved snapshot (same ignore/mask/FCP-FCI options, no card reader, name prefilled from the snapshot); **Import profile** loads a ruleset from JSON (the name is stored inside the file).
- Each profile row has **Check card ▶** (run against the equipped card), **Check card snapshot** (run offline against a saved snapshot), **Edit**, **Clone** (copies the profile as *Copy of &lt;name&gt;* and opens the copy in the editor), **Export** (download JSON), and **Delete**.

A filesystem rule is defined by:

- **Path** — `MF`-rooted (e.g. `MF/7F10/6F3A`) or ADF AID-rooted (e.g. `A0000000871002/6F07`).
- **FCP/FCI check** — **Filetype only (FCP)**, **Filetype + size (FCP)** (adds file size, or record length/count for record files), or **Exact FCI** (byte-for-byte comparison of the raw SELECT FCP template `'62'`, catching FID/AID, life-cycle status, security-attribute, and proprietary-parameter changes).
- **File attributes** — file type, size, record length and record count, taken from the FCP template (any may be left unset).
- **Check contents** (optional) — **Exact** hex equality, or **Mask** where `?` is a per-nibble wildcard (a mask with no `?` is a prefix match, e.g. `0891` for the IMSI MCC/MNC). Record files store a per-record list.

The check report marks each verified aspect (e.g. *filetype ✓, size ✗, contents ✓*), lists mismatches as read-only monospace expected/actual fields aligned in one column, shows content mismatches of files with a decoder as a decoded per-field comparison (only the differing fields, expected and actual side by side; the raw pair is kept when the decoded values match), and shows a decoded per-parameter FCI comparison for FCI mismatches. Corrupt FCI data shows whatever decoded before the faulty part plus an explicit decode-failure note; record mismatches list the *matching records*. In the report the mismatch fields and FCI comparison columns are labelled `expected (profile name)` and `actual (card ICCID)` for a live check, or `actual (snapshot name)` for a snapshot check; the results header reads `Profile verification results for: <profile> → <card ICCID>` (or `… → <snapshot name>`; snapshot comparison: `Snapshot comparison results: <master> → <checked>`). **Only mismatches** in the results header hides all passing files and keeps failures and errors only.

#### “Profile from card” scan options

The scan dialog asks for a profile name and offers the FCP/FCI mode described above, an **Ignore contents of files** checklist of frequently-overwritten files (all checked by default except `EF.ARR`; the header checkbox toggles the whole list) — `EF.LOCI`, `EF.PSLOCI`, `EF.EPSLOCI`, `EF.5GS3GPPLOCI`, `EF.Keys`, `EF.KeysPS`, `EF.SMS`, `EF.Kc`, `EF.KcGPRS`, `EF.LOCIGPRS`, `EF.CBMID`, `EF.SMSS`, `EF.ACC`, `EF.EPSNSC`, `EF.START-HFN`, `EF.ARR` — and two checked-by-default mask options that capture only the first 4 bytes of `EF.IMSI` and `EF.ICCID` (uncheck for exact matching). A progress line shows *N / total files* with the current path; the options are locked while scanning. Rules are created only for files that actually exist (a FCP template is returned); custom files from the **Custom files** sub-tab are included under the same existence check.

#### Card snapshots

The list view has three tabs — **Profiles**, **Card snapshots** and **Custom files**. A snapshot is an immutable capture of the card filesystem: for every existing file it stores the path, symbolic name, file type, size (or record length/count), the raw FCI from the SELECT response, and the contents whenever the file is readable (no ignore list, no masking). The ICCID is decoded from EF.ICCID and shown next to the snapshot name. Captured contents are shown with their decoded form where a decoder exists — a field table for transparent files and a one-line summary per record for record files. The scan also measures every card command (SELECT / READ BINARY / READ RECORD) from command to response and stores min/avg/max per type plus the total scan time; the snapshot view shows these in the summary and the select/read time per file (read time per record). Timings are display-only and ignored by checks/comparisons.

- **New snapshot** scans the card; **Import snapshot** loads JSON.
- Each snapshot row has **Open** (all captured data read-only, raw FCI with decoded FCI and contents; only the name is editable), **Export**, and **Delete**.
- **Check card snapshot** on a profile row runs the profile rules against a snapshot picked from the list, without a card reader. Files whose contents were not captured are reported as unverifiable errors.
- **Compare snapshots** compares two snapshots offline exactly like a profile check: pick the *master* snapshot and the *snapshot to check* and get the same report — snapshot comparison is always exact (no content masking). Files present only in the checked snapshot are reported as extra files. In the comparison report the mismatch fields and FCI comparison columns are labeled with the master/checked snapshot names instead of expected/actual.

---

---


#### Custom files

Files not in pysim's model can be added manually from the **Custom files** tab in the Profiler list:

1. Pick the **Root (MF / ADF)** and type the **Parent DF** path — the root itself, a standard DF known from the file-manager tree, or a custom DF (any depth; suggestions while you type). A parent that has not been seen in the tree yet stays valid and is marked `⚠`
2. Enter the 4-hex **FID** and an alias (`EF.…`/`DF.…`; the prefix decides whether the entry is an EF or a DF)
3. Click **Add** — the file appears in the File manager tree; use **Edit** on a row to reload it into the form (the button becomes **Save**, **Cancel** aborts) or **Delete** to remove it (deleting a DF also deletes its child entries after a confirmation)

The canonical path removes the old ambiguity where the same file could be described both relatively and absolutely. Custom files persist in `localStorage` across sessions and are included in card scans under the same existence check. Export/import as JSON for sharing; legacy relative paths (e.g. `a153/4954`) are resolved on load, unresolvable ones are dropped and reported in the list.

## Phone simulator

The **Phone simulator** tab provides real-time CAT session interaction. It has three pills: **Phone** (STK menu, STATUS and polling, subscribed events, proactive command log), **TR Config** (response data injected into TERMINAL RESPONSEs for proactive commands) and **eSIM** (local eUICC operations).

**Subscribed Events** — the card's SET UP EVENT LIST is displayed with per-event **Send** buttons. Clicking opens a form specific to the event type:

- **No-data events** (User Activity, Idle Screen, etc.) — single-click confirmation
- **Location Status** — dropdown for Normal / Limited / No service
- **Access Technology Change** — dropdown for all 13 RAT types
- **Card Reader Status, Language, UICC Access** — appropriate inputs
- **Channel Status** — channel selector, link state (not established / TCP
  LISTEN / established) and info (no further info / link dropped), per TS 102 223 8.56
- **Network Rejection** — full adaptive form with registration type dropdown
  (LU / GPRS / EPS / 5GS), location fields (MCC, MNC, LAC, RAC, TAC), access
  technology selection, and 53-cause unified rejection cause code dropdown
  covering EMM, GMM, 5GMM, and LU causes

**Proactive Command Log** — chronological list of proactive commands encountered (elapsed time, type code, name and a decoded qualifier; expanding a row shows the decoded command and the TERMINAL RESPONSE). Covers SET UP MENU, SET UP EVENT LIST, POLL INTERVAL, DISPLAY TEXT, SELECT ITEM, PROVIDE LOCAL INFORMATION, TIMER MANAGEMENT, REFRESH and the BIP commands (OPEN/CLOSE CHANNEL, SEND/RECEIVE DATA, GET CHANNEL STATUS); BIP commands are decoded with both plain and comprehension-required TLV tags.

**Timer management** — the server acts as the terminal for TIMER MANAGEMENT (TS 102 223 §6.6.21/§7.4): started timers are tracked per card session, deactivate/get TERMINAL RESPONSEs carry the remaining value, and on expiry the card receives ENVELOPE (TIMER EXPIRATION). The live card uses this to retry the OTA session after a failed OPEN CHANNEL.

**TR Config: PLI data dictionary** — editable per-qualifier hex values for all 22 PROVIDE LOCAL INFORMATION qualifiers (TS 102 223 + TS 131 111). 10 qualifiers have inline decode/encode forms (toggle):

| Code | Decoded fields |
|------|--------------|
| 00 | MCC, MNC, LAC/TAC |
| 01 | IMEI (15 digits) |
| 03 | Date, Time, TZ offset |
| 04 | Language (2-char code) |
| 05 | ME Status, Timing Advance |
| 06 | Access Technology (dropdown) |
| 08 | IMEISV (16 digits) |
| 09 | Search Mode (Auto/Manual) |
| 0A | Battery charge (%) |
| 0E | Multiple Access Technologies (comma-list) |

Values persist on the server until restart. Apply → hex updates; Save → POSTs to server. The server will use these values to populate TERMINAL RESPONSE data for future PLI proactive commands.

**Network simulation** — replays the card-facing write patterns of a real phone on network-condition changes (trace study: `projects/UICC_NAA.md`): **Cold boot**, **EPS attach**, **2G attach**, **Service lost**, **Limited service**, **Roaming denied**, **Churn**, **SMS received**, **CB reconfig** and **AUTHENTICATE**. Each scenario sends the Location status event (only when the card subscribed to it), updates the EPS NAS context, location files, Kc and CB/SMS files exactly as observed, and logs every step with its SW. Parameters (collapsed) cover the operator (searchable worldwide MCC/MNC list served by the server, plus a random roaming picker and a **Home network** button that fills the card's HPLMN from EF.HPLMNwAcT's first record, falling back to the IMSI), LAC/Cell ID/TAC/RAC, optional identity values (empty = random: TMSI, GUTI, KSI, KASME, Kc, NAS counts, algorithm, RAND/AUTN), scenario toggles and the churn count/delay. Only UPDATE BINARY/RECORD, ENVELOPE and AUTHENTICATE are sent; EF.FPLMN is appended only by **Roaming denied** (TS 31.102 §4.2.16, duplicates skipped) and an attach to a listed PLMN clears its entry first (successful manual selection, TS 23.122), while the 5GS location files are never written. The operator list is bundled (`pysim_simple_server/data/mcc-mnc-list.json`, MIT; MVNO entries are hidden — the picker lists real networks), and `--mcc-mnc-list` overrides it.

**Network state monitor** — a compact **Network state** panel next to the simulation buttons shows what the card currently holds and what was last simulated. Its header carries the **simulated service state** (*Undefined* until a scenario or a Location status event sets it, then *Normal service* / *Limited service* / *No service*) with a *PLMN not allowed* marker when the location files or EF.FPLMN show a rejection, plus the current location: PLMN, country and operator, the LAI/RAI/TAI, and the **roaming class** (*Home* when the PLMN equals the HPLMN, *Home equivalent* when it is in EF.EHPLMN, otherwise *Guest*). Below it, one compact line per monitored file (IMSI, EHPLMN, SPDI, HPLMNwAcT, LOCI, PSLOCI, EPSLOCI, EPSNSC, CBMI, CBMIR, SMSstatus, FPLMN) with its decoded summary and how it was last updated (`init`, `write`, `read`, `refresh`); hover for the full decoded fields — long PLMN lists are abbreviated (EF.HPLMNwAcT shows only the first network plus a `… +N` counter). The panel reads the files once at equip (only when the ICCID was readable), updates them in place from the bytes the simulator wrote, re-reads EF.IMSI after every scenario and Location-status event, and never polls the card — **Refresh** re-reads all files on demand.

**eSIM** — for an eUICC (SGP.22/SGP.32) the **eSIM** pill reads the chip and manages the installed profiles through the local ES10 interface (via pySim, no SM-DP+ contact): **Chip** (EID, EUICCInfo1/2 with decoded capability bit lists, card resources, CI PKI identifiers, category, forbidden PPRs, certification data; configured default SM-DP+ / root DS addresses; the rules authorisation table from ES10b GetRat), **Profiles** (state, nickname, provider, ICCID, ISD-P AID, class, owner, and the icon image at its natural size with its type/byte size; **Enable**/**Disable** switches a profile — the card usually sends REFRESH first and the card session is then re-initialized like an equip, so the ICCID, network state and every cached card view are re-read, and the requested state is verified against the re-read profile list) and **Notifications** (read-only pending list). No profile downloads, no notification handling and no SM-DP+ interaction — only the local ES10a/b/c functions are used; a non-eUICC card is reported as such.

## SCP81

The **SCP81** tab drives HTTP OTA (GP RAM over HTTP, GPC v2.2 Amendment B) and has two pills: **Listener** and **Scripts**.

The **Listener** starts/stops the target the card's BIP channel is redirected to, in one of four modes:

- **PSK TLS server** (default) — a PSK TLS listener on **Host:Port** that answers with the TLS 1.2 PSK cipher suites of the spec and speaks the GP HTTP administration dialog (`X-Admin-*` headers, `200` with a command string or `204 No Content`). PSK keys come from the card presets (**Cards** tab): the key is picked by the identity the card sends in the handshake, and Start is refused when no preset has both parts. Keys are never stored or logged; an unrecognised identity is logged as `tls-psk-unknown`.
- **Redirect to external server** — no local listener: every BIP channel is connected to the configured target (Host and Port required), which terminates TLS and runs the administration dialog; the address the card requests is only logged.
- **Pass-through (card destination)** — no listener and no target: the terminal connects each BIP channel to the destination the card requests in OPEN CHANNEL (`Other address` + transport port, TCP client remote only; no spec default port, so an incomplete request fails the channel). TLS is terminated by that platform, so the server's network is used (lab only).
- **Capture (dump)** — accepts the card's TCP channel and logs whatever it sends (e.g. the TLS ClientHello) without answering.

The **Options** block (collapsed by default; a `custom` marker appears when anything differs from the reference defaults; applied at **Start**, remembered in the browser) exposes the HTTP framing (Chunked body / chunk size, 0 = one TLS record / Connection header / compact headers, legal per RFC 7230 §3.2 / Next-URI checkbox + template, unchecked = one-shot), the script framing (indefinite `AE 80 …` or definite `AA` Command Scripting template, comprehension-required tags, optional `X-Admin-Targeted-Application` with its own checkbox) and **Show link events** (all modes, TS 102 223 §7.5.11). The connection stays open between POSTs and closes cleanly at the 204. TLS has no settings: the listener offers TLS 1.0–1.2 and all PSK suites and lets the card negotiate; the log and state line report the version/cipher actually used, and a TLS/cipher handshake failure is logged as `tls-handshake-failed`.

**Script** selects the command list served over the session: **None** (leave the server's configured script) or one of the scripts created in the **Scripts** pill; **Restart script** re-queues the selected script with `force`, starting over from the first APDU.

The state line shows the listener, the negotiated identity and live channels (bytes in/out); **Script results (R-APDUs)** lists each served C-APDU with its R-APDU and SW (`done/total` progress); the **HTTP OTA log** records OPEN/CLOSE CHANNEL, SEND/RECEIVE DATA and every TLS/HTTP/script step (`tls-handshake`, `tls-request`, `script-send`, `script-rapdu`, `script-page`, `script-done`, `data-available`, `peer-close`). The same controls are available through `POST /api/scp81/bip` and `GET /api/scp81/script` (see `docs/api.md`).

### Scripts

The **Scripts** pill manages named APDU lists stored in `localStorage` (`simple_scripts`) and sent to the server when a listener starts. Each list is a sequence of hex C-APDUs, one per line (`#`/`;` comments allowed). **New** creates one from a template:

| Template | What it builds |
|---|---|
| **Empty** | an empty list |
| **Explore** | the reference administration sequence (`GET DATA FF21`, GET STATUS ISD/ELF/application listings, `GET DATA 0085`); long listings auto-continue through `SW 6310/CAFE` pages |
| **Install from .cap** | INSTALL [for load] → LOAD ×N → INSTALL [for install] from a `.cap` (optional SD AID, install/STK parameters, make selectable) via `POST /api/scp81/gen-install`; the file is only used to generate the APDUs — it is not stored, not even its name |
| **Delete** | DELETE APDUs from an AID list (one per line) with P2 = object only / object and related objects |

The table lists each script with kind, APDU count and creation time; **Edit** opens the name + APDU editor and **Delete** removes it. Over a session the server serves one C-APDU per card POST and tracks execution: the card reports status in its next POST (`X-Admin-Script-Status`), a session that dies resends only the unexecuted APDUs (`X-Admin-Resume` continues, a fresh dialog restarts), and a completed script is closed with `204 No Content`. The Remote APDU → **RAM/GP** builder can feed a command chain straight into the run with **Queue in SCP81**.

## PWA

SIMple is a Progressive Web App and can be installed for offline use. Use the **INSTALL PWA** button in the header, or use the browser's install prompt.

- Service worker pre-caches all assets on first visit
- App icons at 192×192 and 512×512

## Theme

A dark theme is included. It follows the system preference and can be toggled manually with the header button (🌙/☀️); the choice is stored in `localStorage`.

## Localization

The interface is in English with Russian support. The language is detected from `navigator.language`; the header toggle (EN/RU) stores the choice in `localStorage`. Switching the language also re-renders visible dynamic views (profile lists, check reports, snapshots, cards, proactive views).

## Server (pysim-simple-server)

The bundled Python server wraps [pySim](https://osmocom.org/projects/pysim/wiki) and serves both the SIMple PWA (from `frontend/`) and a JSON API under `/api/*`.

### Prerequisites

- **Python 3.8+** with `pip`, and **Git**
- **Smart card reader** (PC/SC or serial/FTDI) — PC/SC is preferable (`pcsc-lite` + `ccid` on Linux)
- **Windows** — use Python 3.10–3.13 (3.13 recommended): `pyscard` ships precompiled wheels for these versions. On 3.9 / 3.14 it builds from source (needs MSVC C++ Build Tools). The SMPP bridge (`smpp.twisted3`) is intentionally skipped on Windows.

### Scripts

| Script | What it does |
|--------|-------------|
| `setup.sh` / `setup.bat` | Creates `.venv/`, installs pysim and the server. Run once after cloning. |
| `start.sh` / `start.bat` | Starts the server from the venv (serves the PWA + API on `:8080`). |

`start.sh` auto-detects the reader (PC/SC if `pcscd` is running, else `/dev/ttyUSB0`); `start.bat` always uses `-p 0` (PC/SC is built into Windows). Extra arguments are passed through to the server, e.g. `./start.sh --gsmtap` or `start.bat --gsmtap 10.0.0.5:4729`. If no reader is found the server still starts ("Reader: none") — initialize the card later via the **Equip** button.

### Manual installation

```sh
python3 -m venv .venv
source .venv/bin/activate          # Linux/macOS   (Windows: .venv\Scripts\activate)
pip install git+https://github.com/osmocom/pysim.git
pip install -e .                   # editable — serves frontend/ from the source tree
pysim-simple-server --http-port 8080
```

### CLI options

| Option | Description |
|--------|-------------|
| `--http-host` | Bind address (default: `127.0.0.1`) |
| `--http-port` | TCP port (default: `8080`) |
| `--web-dir` | Directory with PWA static files (default: `<repo>/frontend`) |
| `-p` / `--pcsc-device` | PC/SC reader slot number |
| `-d` / `--device` | Serial device path |
| `--no-card-init` | Skip card init to preserve the CAT session (no file manager) |
| `--apdu-trace` | Log APDU-level traces to stderr |
| `--gsmtap [HOST[:PORT]]` | Stream the card APDUs as GSMTAP-SIM UDP packets for Wireshark / SIMtrace Analyser (`--capture gsmtap`); default target `127.0.0.1:4729`. APDUs only (no ATR), emitted in wire shape (case-4 Le stripped, data as a GET RESPONSE TPDU, case-2/3 responses merged into the command packet). Combines with `--apdu-trace` |
| `--log-requests` | Log request/response payloads to stderr |
| `--sms-oa` / `--sms-sm-sc` | SMS-DELIVER originating address / SM-SC for PoR-in-submit |
| `--terminal-profile` | TERMINAL PROFILE payload hex (default: 33-byte real-handset profile that advertises BIP events/commands; the live card ignores HTTP OTA without it) |
| `--poll-interval` | Idle interval before automatic STATUS polling (default 30s; `0` disables polling) |
| `--mcc-mnc-list` | Override the bundled worldwide MCC/MNC operator list (`pysim_simple_server/data/mcc-mnc-list.json`) |
| `--full-pysim-init` | Use pysim's stock init/equip (redundant card resets). The default init/equip is reset-free — only explicit equip/reset reconnect the card |
| `--no-auto-equip` | Do not initialize a card automatically right after it is inserted (default: auto-equip on) |
| `--menu-timeout` | Auto-answer a paused STK command with a timeout TERMINAL RESPONSE (default 60s; `0` disables) |
| `--timing` | Log phase durations, card resets and APDU counters with elapsed timestamps |

### Troubleshooting

- **"Failed to establish context: Access denied"** — `pcscd` isn't running or the user lacks permission: `sudo systemctl enable --now pcscd && sudo usermod -a -G pcscd $USER`.
- **"device file /dev/ttyUSB0 does not exist"** — no serial reader; connect a USB reader or pass `-d` explicitly. The server still starts without a reader.

### API reference

See [docs/api.md](docs/api.md) for the full endpoint reference.

## Version compatibility

| PWA (SIMple) | Server | Status |
|-------------|--------|--------|
| any | same major | ✅ Compatible |
| any | older major | ❌ Outdated — update server |
| any | newer major | ⚠️ Server newer — update PWA |

The PWA checks the server version on connect via `GET /api/version` and compares the major version (e.g. a 3.x PWA with a 3.x server; a 2.x server is flagged as outdated).
