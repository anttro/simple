# SCP81 / HTTP OTA live-card findings

Living debug log for the HTTP OTA (RAM over HTTP) work against the live UICC.
Purpose: record **every attempted configuration and its outcome**, so the same
variations are not repeated. Add rows as tests are run; keep the confirmed
rules section current.

Setup: the server (`simple_server`) with a PC/SC reader, the PWA SCP81 tab (or
`POST /api/scp81/bip`), the card triggered by its SMS-PP push / the Location
status event. Server log at `GET /api/scp81/log`, script state at
`GET /api/scp81/script`, proactive history at `GET /api/proactive-log`.

## RESOLVED 2026-09-16: the card never received the response - BIP TLV bug

**Root cause:** our RECEIVE DATA TERMINAL RESPONSE encoded the channel-data
TLV length as a raw byte (`36 ED ...` for a 237-byte chunk). BER requires the
long form for lengths >127: **`36 81 ED ...`** (the reference terminal traces
use exactly that, e.g. `push_3311_success_req2.pcapng`). The card's BIP layer
silently mis-parsed the malformed TLV, so the TLS record bytes never reached
its TLS stack: no alert, no script processing, and the SD kept resuming its
dialog ("no complete script received") forever. Every delivery <=127 bytes
(handshake records, 204 responses) always worked - which is why the handshake
succeeded and only the large script responses "vanished".

**Fix:** `_handle_bip_command` (cmd 0x42) BER-encodes the channel data length
(`36 81 <len>` above 127); regression test
`test_receive_data_tlv_long_form_length`.

**Result with the live card** (one push, `explore` script, 5/5 commands):

```
#1 80CAFF2100        SW 9000  FF210B 81010D 8202C5D6 83020962   (13 applets,
                              free NV 50646 B, free volatile 2402 B)
#2 80F28002024F0000  SW 9000  ISD A000000003000000 + D276000005AAFFCAFE00
#3 80CA008500        SW 9000  stored HTTP OTA parameters
#4 80F24002024F0000  SW CAFE  127-byte ELF registry page (more available)
#5 80F21002024F0000  SW CAFE  127-byte applications page (more available)
```

Every command returned `X-Admin-Script-Status: ok` on the card's own POST to
the incremented `X-Admin-Next-URI`, on the same keep-alive connection, and the
session ended with 204 + mutual close_notify - exactly the reference flow.
`SW CAFE` marks a truncated 127-byte page: the remaining entries need a
continuation GET STATUS (P2=02 with the last AID as search criterion).

## Live card facts (verified via the reader, 2026-09-16)

- `80CAFF2100` (GET DATA extended card resources) **works**:
  `FF21 0B 81 01 0D 82 02 C5 D6 83 02 09 62` -> 13 applets installed,
  free NV memory `0xC5D6` = 50646 B, free volatile `0x0962` = 2402 B.
- `80CA008500` (GET DATA HTTP administration parameters) **works** and returns
  the SD's stored OTA configuration: `8A 09 "localhost"`, `8B 14 <agent id>`,
  `8C 01 "/"` (stored URI), `85 14 <PSK identity>`, `86 07 00 01 25 03 00 10 00`
  (retry counter 1, timer **1 minute** — the `25 03` h:m:s fields are TP-SCTS
  semi-octets, TS 23.040 §9.1.2.3: low nibble = most significant digit, so
  `10` = `01` min), `02 40 01` (KVN/KID), APN-ish
  `C7 04 03 47 50 42`, destination `BE 05 21 5B D5 05 02` = 91.213.5.2.
- `80F28002/80F24002/80F21002 ...4F0000` return `6985` through the reader when
  the ISD is not the current DF; the reference platform sends
  `80F28002024F0000` over HTTP, where the SD executes inside the ISD.
- `SELECT` of the ISD (`00A4040008A000000003000000`) returns `6112`;
  a subsequent GET RESPONSE (`00C0000012`) returns `6D00`.
- BIP device identities: OPEN CHANNEL uses destination `0x82`; SEND/RECEIVE
  DATA carry channel `0x21..0x27` (e.g. `82 02 81 22` = channel 2).
- Subscribed events (`99 03`): `03` location status, `09` data available,
  `0A` channel status.
- A Location status event re-triggers the OTA session only while the last
  session is incomplete; after a clean session end the card waits for a push.
- The SD stores a 1-minute retry timer (`25 03 00 10 00`; timer fields are
  TP-SCTS semi-octets per TS 102 223 §8.38 / TS 23.040 §9.1.2.3).

## Confirmed rules (with evidence)

1. **The card needs a clean TLS close, with the close_notify actually
   fetched.** Keep-alive (no close) -> fatal `unexpected_message` after it
   fetched the response. `close_notify` sent *after* the buffer drained is
   never fetched (the card ends the dialog on its own first). Correct order:
   send it while the response still waits, then wait for the drain, then
   close.
2. **The card's abort alert is `fatal unexpected_message`** - decrypted with
   the listener's `keylog` option (see `tools/scp81_decrypt.py`).
3. **A dropped link must be signalled (TS 102 223 7.5.11), and only after the
   buffered data was fetched.** Signalling the drop while bytes are still in
   the BIP buffer makes the card abort the fetch mid-record and end the
   session. Omitting the signal entirely hangs the SD: after a listener
   restart dropped the channel silently, the card ignored pushes and location
   events for minutes; a manual `ENVELOPE (Channel status, B8 02 02 05)`
   immediately made it start a fresh session.
4. **The Next-URI shape matters.** A path-only or absolute Next-URI (`/`,
   `/1`, `http://127.0.0.1:8443/api/scp81`) draws the fatal
   `unexpected_message`; the reference-style relative path **with a query**
   (`/adminserver?PHPSESSID=...&apdu_id=101`) does not.
5. **The reference administration server** (`samples/HTTP_OTA/
   httpota_adminserver_php_v2`) uses: command script
   `AE 80 22 <len> <apdu> 00 00`; response `200` with
   `X-Admin-Protocol`, `X-Admin-Next-URI: /adminserver?PHPSESSID=<id>&apdu_id=<n>`,
   `Content-Type: ...;version=1.0`, **chunked** body (100-byte chunks);
   the card returns the R-APDU as the body of its next POST with
   `X-Admin-Script-Status: ok`; the server ends with `204`.
   Its log proves the card followed the Next-URI three times within 1-2 s per
   step (`Got next request ... Script status is 'ok' - storing R-APDU data`).
6. **`chunked=false` (Content-Length) has never produced an R-APDU.** All
   sessions that ended silently (clean close, no alert, no POST) used
   `Content-Length`. Hypothesis: the card only treats a chunked body as a
   command script; with Content-Length it sees an empty script, executes
   nothing and ends the session gracefully.

## The one fully successful session trace (ground truth)

`traces/HTTPOTA_session_3311_success1.pcap` (2019, **plain HTTP on port 80**,
one TCP connection for the whole session, card `3311` - *not* our UICC):

```
POST /server/adminagent?cmd=1            <- card (trigger URI, with query!)
200 OK + Date/Server + X-Admin-Protocol
     + X-Admin-Next-URI: /Download?req=1 + Content-Length: 11
     + Content-Type: .../card-content-mgt;version=1.0
     body: ae 80 22 05 80 ca 00 85 00 00 00     (script: GET DATA 0085)
POST /Download?req=1                     <- card, SAME connection
  X-Admin-Script-Status: ok
  Content-Type: .../card-content-mgt-response;version=1.0
  Transfer-Encoding: chunked
  body: "8
" af 80 23 02 6a 88 00 00 "0

"   (R-APDU SW 6A88)
200 OK + X-Admin-Next-URI: /Download?req=2 + Content-Length: 14
     body: ae 80 22 08 80 f2 80 02 02 4f 00 00 00 00    (GET STATUS P1=80)
POST /Download?req=2  ->  X-Admin-Script-Status: ok, chunked
     body: "1F
" af 80 23 19 <25-byte R-APDU ... 90 00> 00 00 "0

"
200 OK + /Download?req=3 + 11-byte script
POST /Download?req=3  ->  status ok, R-APDU 23 02 6d 00 (SW 6D00)
204 No Content                          <- session ends
```

Confirmed from it: the card echoes the `X-Admin-Next-URI` (path *and* query)
verbatim; its response POST goes on the **same TCP connection**; its response
is the `AF 80 23 <len> <R-APDU> 00 00` indefinite Response Scripting template
(in a chunked body, with `X-Admin-Script-Status`); the server's script
`AE 80 22 <len> <APDU> 00 00` matches ours byte for byte; the server uses
`Content-Length` (not chunked), no `Connection` header (implicit keep-alive),
and ends with 204.

## Attempt matrix

| # | transport | framing | Next-URI | close | link events | outcome |
|---|-----------|---------|----------|-------|-------------|---------|
| 1 | dump mode only | - | - | - | off | OPEN CHANNEL + ClientHello captured (Phase A) |
| 2 | TLS, 204 only | - | - | yes | off | session completes cleanly, no alert (Phase B, live) |
| 3 | TLS + script | chunked 100 | `/N` | early (raced fetch) | on | fetch truncated (237/399); card re-opened and repeated its POST with `X-Admin-Resume: true` -> breakdown-resume works |
| 4 | TLS + script | chunked 100 / single | `/1`, `/`, absolute | keep-alive | off | full fetch, then fatal `unexpected_message` (Next-URI shape) |
| 5 | TLS + script | single | none (`""`) | keep-alive | off | no alert, no POST, session left open (spec: no Next-URI -> no response) |
| 6 | TLS + script | chunked 100 | reference | close_notify after drain | off | full fetch, alert (notify never fetched) |
| 7 | TLS + script | chunked 100 | reference | close_notify before drain | off | full fetch, alert (head split into its own record) |
| 8 | TLS + script | **single record** | reference | drain + close_notify | off | **no alert**, card CLOSE CHANNELs, no R-APDU (`chunked=false` -> suspected empty script) |
| 9 | TLS + script | single record | reference | keep-alive (no close) | off | fatal `unexpected_message` (close required) |
| 10 | TLS + script | chunked 100 | reference | drain + close_notify | off | full fetch, then alert; later the SD hung until a manual link-dropped event |
| 11 | TLS + script | single record | reference | keep-alive | off | fatal `unexpected_message` after the full fetch (no close) |
| 12 | TLS + script | single record | reference | drain + close_notify | off | **no alert**, card CLOSE CHANNELs, no R-APDU (`Content-Length`) |
| 13 | TLS + script | chunked100 + single | reference | drain + close_notify | off | no alert, no R-APDU |
| 14 | TLS + script | single record | reference | keep-alive | off | alert again |
| 15 | TLS + script | chunked 100 | reference | keep-alive | on | alert (small records, ruled out record size) |
| 16 | TLS + script | single record | reference | keep-alive, no `Connection` header | on | alert |
| 17 | TLS + script (RFM! `00D6` write-probe) | chunked, single | reference | drain + close_notify | on | no alert, no R-APDU; EF.SPN unchanged - **RFM result is void**: the ISD only accepts RAM commands |

All script attempts used the `explore` list, except #8-#17 which used only
`80CAFF2100` (or the RFM probe). #3-#17 ran with the card's PSK identity
`89390…903` (push trigger) or `89701…` (event trigger).

**Status after #17 (superseded by the 2026-09-16 resolution above):** the
failures were caused by the BIP TLV length bug, not by the HTTP/TLS details;
resume mode was a symptom (the working session even started as a resume). The
key working recipe (also now the server default): one keep-alive connection,
`Transfer-Encoding: chunked` body with the script in
one TLS record, no Connection header, `X-Admin-Next-URI` with a query whose
command id increments. (2.2.14: the reference server's Date/Server/X-Powered-By
headers were dropped — they were mimicry, the blocker was the BIP length bug.)

**Also confirmed:** a TLS half-close (close_notify then keep reading for the
card's POST which RFC 5246 leaves open in practice) cannot be done with
CPython's `ssl`: `SSLSocket.unwrap()` with a short timeout raises and poisons
the session (tested), so the `half_close` option is a documented no-op.

## RESOLVED 2026-09-16b: SW CAFE continuation pages

**Implemented:** the script responder auto-follows a truncated listing page
(`SW CAFE`, 127 bytes) by inserting a continuation GET STATUS
(`80F2 <P1> 02 <Lc> 4F <len> <last-complete-AID> 00`, next-occurrence mode)
as the next command. The last AID comes from the last complete `E3` entry in
the page (truncated tails and the live `FC`-prefixed junk are skipped).
Logged as `script-page`; a repeated page logs `script-page-stalled` and
stops; max 24 pages; inserted continuations are dropped at session start.

**Live-verified (2026-09-16):** ELF registry: page 1 `SW CAFE` ->
continuation with `D276000005AA060200000000B00000` -> page 2 `SW 9000`
(complete, 2 entries). Applications: page 1 `SW CAFE` -> continuation with
`D276000005AAFFCAFE0010` -> page 2 `SW 9000` (complete, incl.
`D276000005AAFFCAFE0001/0010`, `A0000001515350`, `A000000151535041`).
Full session: 7/7 commands, all `X-Admin-Script-Status: ok`.

## RESOLVED 2026-09-16c: RAM install over SCP81 - BER length in the script template

**Root cause:** `_scp81_command_body` wrote the C-APDU TLV length as a raw
byte (`AE 80 22 F5 <245 bytes> 00 00` for a 245-byte LOAD). BER reads a byte
above 0x7F as a long-form marker, so the card mis-parsed every LOAD >128
bytes; the small INSTALL commands (<128 bytes) executed normally, which made
the install look alive. Symptoms: the card accepted the LOAD responses with
`X-Admin-Script-Status: ok` but sent a degenerate `AF 80` body, no LOAD
R-APDU appeared in the results, and the final INSTALL [for install] answered
`6A88` (module not found) because the package was never loaded.

**Fix:** both the indefinite ("22" TLV) and definite ("AA" outer) template
lengths are BER-encoded (`_ber_len_bytes`); the same rule as the BIP channel
data TLV fix earlier the same day. Tests cover the 245-byte LOAD body, the
short-form case and the definite variant.

## RESOLVED 2026-09-16d: RAM install - LOAD blocks were overlapping copies

**Root cause:** the LOAD block slicer indexed the load file TLV with the
*block number* (`loadfile_tlv[i * 2:(i + 240) * 2] for i in range(blocks)`)
instead of a *character offset*, so every block after the first was a
1-byte-shifted copy of its predecessor. On the wire the cap header repeated
every 239 bytes. The card accepted the first three blocks and failed block 4
with `SW 6400` (execution error), then refused the rest (`6985`) and the
final INSTALL answered `6A88`. The same slicing lived in the SCP80
/api/ram-install path (the helper was extracted from it), so multi-block caps
could never install there either.

**Fix:** consecutive chunks at char offsets
(`range(0, len(tlv), 240 * 2)`), with a reassembly test that pins the joined
blocks to the C4 TLV byte for byte.

## RESOLVED 2026-09-16e: RAM install over SCP81 - complete and verified

**Live-verified end-to-end**: .cap parse -> INSTALL [for load] (SW 9000) ->
LOAD x6 (all SW 9000, after the block-slicing fix) -> INSTALL [for install]
with the full parameter set -> SW 9000. The applet's INSTALL [for install]
needed the real install parameters (`C900` + the STK parameters
`EA 0C 80 0A ...`), which the compact SCP81 form could not express - the
Remote APDU -> RAM -> INSTALL [for install] builder has all the fields and
its "Queue in SCP81" button feeds the commands straight into the HTTP OTA
script (the "To expanded" button shows them in the TS 102 226 command
scripting (AA/AE80) form). The card's answer for the parameter-less attempt
was SW 6A80 (incorrect parameters in data field).

Working INSTALL [for install] example (compact):
`80E60C002E07AA1902BC22580108AA1902BC2258010108AA1902BC22580101010010C900EA0C800A00000F010000000000000000`

## RESOLVED 2026-09-16f: SW CAFE pagination used the wrong P2 (02 instead of 03)

**Root cause:** the continuation GET STATUS used `P2=02`, which Table 11-34
(GP Card Spec 2.3.1) defines as "**Get first or all occurrence(s)**" - the
card returned the first listing again (with the search criterion's single
match), so every listing appeared to end after one extra page and newly
installed/registered entries were invisible (the installed package
`AA1902BC225801` was missing from the ELF registry). The correct value is
`P2=03` = "**Get next occurrence(s)**".

**Fix:** the continuation repeats the *same* GET STATUS command with P2.b1
set (`80F2 <P1> 03 <same data> 00`) - the pagination state lives in the card.
A changed `4F` criterion is a match filter, not a position: `P2=03` combined
with the last AID as criterion is rejected with SW 6A80, and `P2=02` with it
returns that single match (the duplicate seen earlier). The card's
truncation warning is its proprietary `CA FE`; GP defines `63 10` (Table
11-38) and both trigger the continuation.

**Also fixed (same week):** the explore script's P1 values - per Table 11-33
`P1=40` is *applications and supplementary security domains*, `P1=20` the
*ELF registry* and `P1=10` *ELF+modules*; the script never queried the
ELF-only registry, which is why the installed package `AA1902BC225801` was
invisible. Labels/decoder updated; the remote APDU script builder's P1 map
(0x02 load / 0x0C install / 0x08 make-selectable / 0x40 reg-update / 0x10
extradition) was already correct.

## RESOLVED 2026-09-16g: response R-APDU TLV length also needs BER long form

**Root cause:** `_scp81_parse_response` read the `23` (R-APDU) TLV length as a
raw byte. A listing page above 127 bytes arrives as `AF 80 23 81 FC <252
bytes> 00 00`; the parser took `0x81` as the length, so every page was
silently cut to 127 bytes with a bogus status word (the data's last two
bytes, e.g. `CAFE`/`0001`/`9F70` instead of the real `63 10`). The bogus SW
also stopped the pagination, so later registry entries - including the
installed package `AA1902BC225801` - never appeared.

**Fix:** the response template TLVs use `httpota.ber_len_read` (BER length,
same class of bug as the channel data TLV and the command script template
earlier the same day). Regression tests cover a 250-byte page with
`23 81 FC` and the short-form case.

## VERIFIED 2026-09-16h: the installed applet in all registries

After the response-TLV BER fix, `explore` ran 18 commands / 10 auto
continuation pages (all real statuses: `63 10` -> next, `9000` = complete)
and the installed applet shows up everywhere:

```
80F240 (applications+SDs): AA1902BC22580101  life=07 (SELECTABLE)  priv=00  elf=AA1902BC225801
80F220 (ELF registry):     AA1902BC225801    life=01 (loaded)
80F210 (ELF+modules):      AA1902BC225801    life=01  module=AA1902BC22580101
```

Full RAM-over-HTTP install cycle: .cap -> INSTALL [for load] -> LOAD x6 ->
INSTALL [for install] -> registries.

## Next tests / work

1. **UI:** group the per-page R-APDUs under their logical command in the
   SCP81 tab (page merging/decoding for ELF and application listings).
2. **Load/store over SCP81:** implemented - `POST /api/scp81/gen-install`
   takes a `.cap`, expands it with the shared `_cap_apdu_sequence` helper
   (INSTALL [for load] -> 240-byte LOAD blocks -> INSTALL [for install]) and
   returns the APDU list, which the PWA stores as an "Install from .cap"
   script (the `.cap` itself is never stored). Live install verified
   2026-09-16.

## Tooling

- `tools/scp81_decrypt.py <log.json> <keys.log>` - decrypts the dialog from
  `GET /api/scp81/log` plus the listener's `keylog` file (SSLKEYLOGFILE
  format; PSK-AES128-CBC-SHA256, TLS 1.2 PRF + OpenSSL CLI). Shows each
  record's plaintext and any alert level/description.
- Start the listener with `"keylog": "/tmp/.../scp81.keys"` to collect the
  secrets (contains key material - use a temp path, never commit).
