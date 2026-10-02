# ETSI / 3GPP CAT releases — command and parameter introduction

Reference for the release guard: which proactive command, event, data object or
qualifier appeared in which release.  The data keeps the real release (Rel-4,
Rel-5, ...); the UI collapses everything before Rel-6 to `pre-Rel-6`.  The
`baseline` rows are Rel-4 (the original specification), `?` means the change
histories do not pin the item down.

## Sources

| Document | Issuer | Change history | Source tag |
|---|---|---|---|
| 3GPP TS 31.111 (USAT) V18.12.0 | 3GPP | Annex W | `[3GPP]` |
| ETSI TS 102 223 (CAT) V18.3.0 | ETSI TC SET | Annex V | `[ETSI 223]` |
| ETSI TS 101 220 (TLVs/TARs) V18.3.0 | ETSI TC SET | Annex Q | `[ETSI 220]` |

The version major equals the release (`V7.x` = Rel-7 … `V18.x` = Rel-18).  ETSI
aligned its numbering with 3GPP, so the numbers are comparable — but the documents
are different: an `[ETSI …]` item is not guaranteed to exist in a card that only
follows 3GPP, and vice versa.  `baseline` = present in the original Rel-4
specification and not named in any change history; `?` = not resolvable from the
change histories — verify against the clause edition before relying on it.

## Events

| Code | Event | Release | Source / note |
|---|---|---|---|
| `0x00` | MT call | Rel-4 | baseline |
| `0x01` | Call connected | Rel-4 | baseline |
| `0x02` | Call disconnected | Rel-4 | baseline |
| `0x03` | Location status | Rel-4 | baseline (E-UTRAN "idle" state clarified in 8.5.0, 3GPP) |
| `0x04` | User activity | Rel-4 | baseline |
| `0x05` | Idle screen available | Rel-4 | baseline |
| `0x06` | Card reader status | Rel-4 | baseline |
| `0x07` | Language selection | Rel-4 | baseline |
| `0x08` | Browser termination | Rel-4 | baseline |
| `0x09` | Data available | Rel-4 | baseline |
| `0x0A` | Channel status | Rel-4 | baseline |
| `0x0B` | Access technology change (single) | Rel-4 | baseline; the 8.61 coding reworked in 12.0.0 (ETSI 223) |
| `0x0C` | Display parameters changed | Rel-4 | baseline |
| `0x0D` | Local connection | Rel-4 | baseline |
| `0x0E` | Network search mode change | Rel-4 | baseline |
| `0x0F` | Browsing status | Rel-4 | baseline |
| `0x10` | Frames information change | Rel-6 | ETSI 223 6.6.0 "Introduction of the frames in CAT"; ETSI 220 6.4.0 frame tags |
| `0x11` | (I-)WLAN access status | Rel-7 | 3GPP 7.2.0 BIP bearers with I-WLAN; ETSI 220 7.4.0 I-WLAN tags |
| `0x12` | Network rejection | Rel-8 | 3GPP 8.0.0 CP-070842 0190 "Addition of EVENT: Network Rejection" |
| `0x13` | HCI connectivity | Rel-9 | ? not named in the change histories (contactless/HCI work, 9.x) - verify |
| `0x14` | Access technology change (multiple) | Rel-12 | ? the multiple-technology variant (ETSI 223 12.0.0 "Adding new Access Technology To CAT") - verify |
| `0x15` | CSG cell selection | Rel-9 | 3GPP 9.0.0 "CSG cell selection event"; PLMN ID added 12.1.0 |
| `0x16` | Contactless state request | Rel-9 | ETSI 223 9.1.0 "Enabling and disabling contactless functionality of the UICC" |
| `0x17` | IMS registration | Rel-10 | 3GPP 10.2.0 CP-110238 0281 "Introduction of the IARI based IMS Registration event" |
| `0x18` | Incoming IMS data | Rel-10 | 3GPP 10.2.0 CP-110238 0279 "Introduction of the IARI based Incoming IMS Data event" |
| `0x19` | Profile container | Rel-10 | ETSI 223 10.4.0 "Encapsulated CAT commands and envelopes without security" |
| `0x1A` | Void | — | no event (value reserved) |
| `0x1B` | Secured profile container | Rel-11 | ETSI 223 11.2.0 "Security for encapsulated CAT" |
| `0x1C` | Poll interval negotiation | Rel-12 | ETSI 223 12.1.0 "Addition of POLL INTERVAL event"; 3GPP 12.3.0 "Addition of POLL INTERVAL ENVELOPE command" |
| `0x1D` | Data connection status change | Rel-14 | 3GPP 14.2.0 CP-170213 0656 "New Event Download for Data Connection Status" |
| `0x1E` | CAG cell selection | Rel-17 | 3GPP 17.5.0 "Toolkit support of CAG Cell Selection"; ETSI 223 17.2.0 "addition of CAG feature" |
| `0x1F` | Slices status change | Rel-16 | ? 3GPP 16.0.0 "PROVIDE LOCAL INFORMATION to get Slice(s) information" (the event itself is not named in the history) - verify |

## Proactive commands

| Command | Release | Source / note |
|---|---|---|
| REFRESH | Rel-4 | baseline |
| MORE TIME | Rel-4 | baseline |
| POLL INTERVAL | Rel-4 | baseline (the ENVELOPE negotiation is 12.3.0, 3GPP) |
| POLLING OFF | Rel-4 | baseline |
| SET UP EVENT LIST | Rel-4 | baseline |
| SET UP CALL | Rel-4 | baseline (multi-media calls 6.5.0, ETSI 223) |
| SEND SS | Rel-4 | baseline |
| SEND USSD | Rel-4 | baseline (3GPP2 USSD indication 12.0.0) |
| SEND SHORT MESSAGE | Rel-4 | baseline |
| SEND DTMF | Rel-4 | baseline |
| LAUNCH BROWSER | Rel-4 | baseline (APN settings 9.0.0; URI tag 10.3.0 ETSI 220; launch parameters for terminal applications 10.1.0) |
| PLAY TONE | Rel-4 | baseline (extensions 6.5.0, ETSI 223) |
| DISPLAY TEXT | Rel-4 | baseline |
| GET INKEY | Rel-4 | baseline |
| GET INPUT | Rel-4 | baseline (predictive text mode 7.8.0) |
| SELECT ITEM | Rel-4 | baseline |
| SET UP MENU | Rel-4 | baseline |
| PROVIDE LOCAL INFORMATION | Rel-4 | baseline (many qualifiers added later - see the qualifier table) |
| TIMER MANAGEMENT | Rel-4 | baseline |
| SET UP IDLE MODE TEXT | Rel-4 | baseline |
| RUN AT COMMAND | Rel-4 | baseline |
| LANGUAGE NOTIFICATION | Rel-4 | baseline |
| OPEN CHANNEL | Rel-4 | baseline (background mode 6.3.0; I-WLAN bearer 7.2.0; TCP LISTEN state 7.8.1; 5G support 15.3.0) |
| CLOSE CHANNEL | Rel-4 | baseline |
| SEND DATA | Rel-4 | baseline |
| RECEIVE DATA | Rel-4 | baseline |
| GET CHANNEL STATUS | Rel-4 | baseline |
| SERVICE SEARCH | Rel-4 | baseline |
| GET SERVICE INFORMATION | Rel-4 | baseline |
| DECLARE SERVICE | Rel-4 | baseline |
| SET FRAMES | Rel-6 | ETSI 223 frames in CAT 6.6.0 |
| GET FRAMES STATUS | Rel-6 | ETSI 223 frames in CAT 6.6.0 |
| RETRIEVE MULTIMEDIA MESSAGE | Rel-7 | ETSI 220 7.1.0/7.3.0 MMS tags; 3GPP 7.1.0 "Transfer MMS commands to SCP" |
| SUBMIT MULTIMEDIA MESSAGE | Rel-7 | ETSI 220 7.1.0/7.3.0 MMS tags |
| DISPLAY MULTIMEDIA MESSAGE | Rel-7 | ETSI 220 7.1.0/7.3.0 MMS tags |
| GEOGRAPHICAL LOCATION REQUEST | Rel-8 | 3GPP 8.3.0 CP-080658 0196 "geographical location discovery mechanism"; ETSI 223 8.2.0 |
| ACTIVATE | Rel-13 | ETSI 223 13.1.0 "CAT ACTIVATE command support" (tag reserved 8.4.0) |
| CONTACTLESS STATE CHANGED | Rel-9 | ETSI 223 9.1.0 contactless functionality |
| COMMAND CONTAINER | Rel-10 | ETSI 223 10.4.0 "Encapsulated CAT commands and envelopes without security" |
| ENCAPSULATED SESSION CONTROL | Rel-11 | ETSI 223 11.2.0 "Security for encapsulated CAT" |
| LSI COMMAND | Rel-17 | ETSI 220 17.0.0 "Tag for LSI numbers data object" |
| POWER ON CARD | ? | ? not named in the change histories - verify |
| POWER OFF CARD | ? | ? not named in the change histories - verify |
| PERFORM CARD APDU | ? | ? not named in the change histories - verify |
| GET READER STATUS | ? | ? not named in the change histories - verify |

## Data objects / TLVs (the ones the tool builds)

| Tag | Object | Release | Source / note |
|---|---|---|---|
| `06/86` | Address | Rel-4 | baseline |
| `08/88` | Subaddress | Rel-4 | baseline |
| `1C/9C` | Transaction identifier | Rel-4 | baseline |
| `1A/9A` | Cause | Rel-4 | baseline |
| `34/B4` | Browser termination cause | Rel-4 | baseline |
| `35/B5` | Bearer description | Rel-4 | baseline |
| `37/B7` | Channel data length | Rel-4 | baseline |
| `38/B8` | Channel status | Rel-4 | baseline |
| `3E/BE` | Other address (local / destination) | Rel-4 | baseline |
| `40/C0` | Display parameters | Rel-4 | baseline |
| `64/E4` | Browsing status | Rel-4 | baseline |
| `65/E5` | Network search mode | Rel-4 | baseline |
| `67/E7` | Frames Information | Rel-6 | ETSI 223 6.6.0 frames in CAT; ETSI 220 6.4.0 |
| `4B/CB` | (I-)WLAN Access Status | Rel-7 | ETSI 220 7.4.0 I-WLAN tags; 3GPP 7.2.0 |
| `55/D5` | CSG cell selection status | Rel-9 | 3GPP 9.0.0 CSG event |
| `56/D6` | CSG ID | Rel-9 | 3GPP 9.0.0 CSG event |
| `57/D7` | HNB name | Rel-9 | 3GPP 9.0.0 / 9.3.0 "HNB name corrections" |
| `31/B1` | IMS URI | Rel-10 | ETSI 220 10.3.0 "Allocation of URI tag" |
| `77/F7` | IARI | Rel-10 | 3GPP 10.2.0 IARI events |
| `77/F7` | IMPU list | Rel-11 | ETSI 220 11.0.0 "tag values for 3GPP IARI IMPU list IMS Status Code..." |
| `78/F8` | IMS status code | Rel-11 | ETSI 220 11.0.0 |
| `7E/FE` | Media Type | Rel-12 | ? 3GPP 12.4.0 "URI support in MT Call event" - verify |
| `09/89` | PLMN ID | Rel-4 | baseline (CSG PLMN ID 12.1.0) |
| `3F/BF` | Access technology | Rel-4 | baseline (coding reworked 12.0.0, ETSI 223) |
| `B4` | Supported Radio Access Technologies | Rel-13 | ETSI 220 13.0.0 tag; ETSI 223 13.1.0 |
| `9D` | Data connection status | Rel-14 | 3GPP 14.2.0 |
| `AA` | Data connection type | Rel-14 | 3GPP 14.2.0 |
| `AE` | (E/5G)SM cause | Rel-14 | 3GPP 14.2.0 |
| `C7` | Network Access Name | Rel-14 | 3GPP 14.2.0 (5GS bearer update 15.1.0) |
| `0B/8B` | PDP/PDN/PDU type | Rel-14 | 3GPP 14.2.0 |
| `26/A6` | Date-Time and Time zone | Rel-4 | baseline (TP-SCT coding) |
| `13/93` | Location Information | Rel-4 | baseline |
| `55/D5` | CAG cell selection status | Rel-17 | 3GPP 17.5.0 / ETSI 223 17.2.0 |
| `56/D6` | CAG information list | Rel-17 | 3GPP 17.5.0 |
| `57/D7` | CAG Human-readable network name list | Rel-17 | 3GPP 17.5.0 |
| `55/D5` | Slices status | Rel-16 | 3GPP 16.0.0 slice PLI |
| `56/D6` | Slices information (served) | Rel-16 | 3GPP 16.0.0 |
| `78/F8` | Allowed slices information | Rel-16 | 3GPP 16.0.0 |
| `77/F7` | Allowed slices with S-NSSAI mapping | Rel-18 | 3GPP 18.x slice alignment |
| `D7` | Rejected slices with S-NSSAI mapping | Rel-18 | 3GPP 18.4.0 "PROVIDE LOCAL INFORMATION (Rejected Slices information)" |
| `B1` | Rejected slices information | Rel-18 | 3GPP 18.4.0 |

## PROVIDE LOCAL INFORMATION qualifiers

| Qualifier | Item | Release | Source / note |
|---|---|---|---|
| `00` | Location info (MCC, MNC, LAC/TAC, Cell ID) | Rel-4 | baseline |
| `01` | IMEI | Rel-4 | baseline |
| `02` | Network measurement results | Rel-4 | baseline (multi-RAT NMR 10.x; NG-RAN 16.0.0) |
| `03` | Date, time and time zone | Rel-4 | baseline |
| `04` | Language setting | Rel-4 | baseline |
| `06` | Access technology (single) | Rel-4 | baseline |
| `07` | ESN of the terminal | Rel-4 | baseline |
| `08` | IMEISV | Rel-6 | ETSI 223 6.3.0 "Request of IMEISV in PROVIDE LOCAL INFORMATION" |
| `09` | Search mode | Rel-4 | baseline |
| `0A` | Battery charge state | Rel-4 | baseline |
| `0B` | MEID of the terminal | ? | ? not named in the change histories - verify |
| `0C` | Current WSID | ? | ? not named in the change histories - verify |
| `0D` | Broadcast network info | Rel-8 | ETSI 223 8.2.0 "Addition of Location Information for Broadcast..." |
| `0E` | Multiple access technologies | Rel-12 | ? ETSI 223 12.0.0 new access technology work - verify |
| `0F` | Location info (multi-RAT) | Rel-10 | ? 3GPP 10.x multi-RAT NMR work - verify |
| `10` | NMR (multi-RAT) | Rel-10 | ? 3GPP 10.x - verify |
| `11` | CSG ID list + HNB name | Rel-9 | 3GPP 9.0.0 CSG work |
| `12` | H(e)NB IP address | Rel-10 | 3GPP 10.7.0 "Addition of Provide local information, H(e)NB IP ad..." |
| `13` | H(e)NB surrounding macrocells | Rel-10 | 3GPP 10.7.0 |
| `14` | Current WLAN identifier | Rel-13 | 3GPP 13.0.0 "WLAN Bearer alignment of USAT" |
| `15` | Slices information | Rel-16 | 3GPP 16.0.0 |
| `16` | CAG information list | Rel-17 | 3GPP 17.5.0 |
| `17` | Rejected slices information | Rel-18 | 3GPP 18.4.0 |
| `1A` | Supported Radio Access Technologies | Rel-13 | ETSI 223 13.1.0 |

## TERMINAL PROFILE features (selected)

| Byte / bit | Feature | Release | Source / note |
|---|---|---|---|
| 5.2 byte 1 b1-b5 | Screen height | Rel-4 | baseline |
| 5.2 byte 2 b1-b5 | Screen width | Rel-4 | baseline |
| 5.2 byte 3 | Screen effects | Rel-4 | baseline |
| 5.2 byte 4 | No display / no keypad / screen sizing / variable fonts | Rel-4 | baseline |
| 5.2 byte 5 | Proactive command set (DISPLAY TEXT, GET INKEY, ...) | Rel-4 | baseline |
| 5.2 byte 6 | Proactive command set (SET UP EVENT LIST, SET UP MENU, ...) | Rel-4 | baseline |
| 5.2 byte 7 | Proactive command set (SET UP CALL, SEND SS/USSD/SMS, ...) | Rel-4 | baseline |
| 5.2 byte 8 b1-b8 | Events (MT call ... Browsing status) | Rel-4 | baseline |
| 5.2 byte 9 b1-b8 | Events (Frames information, (I-)WLAN, Network rejection, ...) | Rel-6 | ETSI 223 frames 6.6.0; (I-)WLAN 7.x; network rejection 8.0.0 |
| 5.2 byte 10 | GET INKEY / SET UP IDLE MODE TEXT / RUN AT / SETUP CALL | Rel-4 | baseline |
| 5.2 byte 11-12 | Soft keys / BIP (OPEN CHANNEL, CLOSE CHANNEL, RECEIVE/SEND DATA, GET CHANNEL STATUS) | Rel-4 | baseline |
| 5.2 byte 13 | BIP bearers (CSD, GPRS, Bluetooth, IrDA, RS232) / number of channels | Rel-4 | baseline |
| 5.2 byte 14 | Screen height/width / no display / no keypad / sizing | Rel-4 | baseline |
| 5.2 byte 17 | Frames / card reader status (corrected 6.0.0) | Rel-6 | ETSI 223 6.0.0 byte-17 correction; frames 6.6.0 |
| 5.2 byte 18 | Frames / eCAT | Rel-6 | ETSI 223 frames 6.6.0 |
| 5.2 byte 19-20 | Text attributes / BIP / I-WLAN | Rel-6 | ETSI 223 6.x; I-WLAN 7.x |
| 5.2 byte 21 | Bearer independent protocol (TCP, UDP, local) | Rel-4 | baseline |
| 5.2 byte 22-24 | 3GPP-specific (H(e)NB, CSG, ...) | Rel-9 | 3GPP CSG 9.0.0; H(e)NB 10.7.0 |
| 5.2 byte 25-27 | 3GPP-specific (geographical location, IMS, ...) | Rel-8 | 3GPP 8.3.0 geographical location; IMS 10.x |
| 5.2 byte 28-30 | 3GPP-specific (5GS, slices, CAG, ...) | Rel-16 | 3GPP 16.0.0 slices; 17.5.0 CAG |
| 5.2 byte 31-33 | 3GPP-specific (NG-RAN, satellite, ...) | Rel-15 | 3GPP 15.3.0 5G; 17.3.0 satellite |

## Feature entries by release (from the change histories)

The B (new feature) and C (functional change) entries of the three change
histories, grouped by the release that introduced them.  The subjects are
shortened and extracted from the PDFs' text layer, so a long subject can appear
as a fragment split across lines — the annexes are authoritative.

### Rel-4

- `ETSI TS 102 223` **4.1.0 B** — Terminal Profile Harmonization with ANSI/TIA/EIA-136
- `ETSI TS 101 220` **4.1.0 B** — Allocation of TAR values for the USAT

### Rel-5

- `ETSI TS 101 220` **5.0.0 B** — Interpreter.
- `ETSI TS 101 220` **5.0.0 B** — Addition of ISIM AID.

### Rel-6

- `ETSI TS 102 223` **6.3.0 B** — Request of IMEISV in PROVIDE LOCAL INFORMATION
- `ETSI TS 101 220` **6.0.0 B** — Definition of TLV Forms and TLV Tag Value Tables.
- `ETSI TS 101 220` **6.0.0 B** — Update of Statement of Scope.
- `ETSI TS 101 220` **6.0.0 B** — BER-TLV Tag Reservation for card application
- `ETSI TS 101 220` **6.1.0 B** — communication.
- `ETSI TS 101 220` **6.1.0 B** — Allocation of AID for the uicc.* packages.
- `ETSI TS 101 220` **6.4.0 B** — from ETSI TS 102 223.
- `ETSI TS 101 220` **6.4.0 B** — Allocation of new tag values for Expanded Remote Application data format.
- `ETSI TS 101 220` **6.4.0 B** — Introduction of new tags for the frames in CAT.
- `ETSI TS 101 220` **6.5.0 B** — New Tags for BER-TLV EFs.
- `ETSI TS 101 220` **6.5.0 B** — Allocation of new tag values for EAP.

### Rel-7

- `3GPP TS 31.111` **7.2.0 B** — Service CT-30
- `3GPP TS 31.111` **7.8.0 B** — Steering of Roaming Refresh Command
- `ETSI TS 102 223` **7.8.1 B** — Card request to have the TCP connection go into "TCP in LISTEN state"
- `ETSI TS 102 223` **7.9.0 C** — clause 6.6.27.6 was wrongly inserted after 6.4.27.6.
- `ETSI TS 102 223` **7.9.0 C** — Alignment with 3GPP: Steering of Roaming Refresh Command
- `ETSI TS 101 220` **7.1.0 B** — Tags for 3GPP MMS commands.
- `ETSI TS 101 220` **7.2.0 B** — Allocation of TAR values for ADF Remote File Management Applications.
- `ETSI TS 101 220` **7.3.0 B** — Tags for MMS Toolkit commands.
- `ETSI TS 101 220` **7.4.0 B** — Reservation of Comprehension-TLV tags for 3GPP related to the new I-WLAN bearer in 3GPP.
- `ETSI TS 101 220` **7.5.0 B** — Addition of specific UICC environmental
- `ETSI TS 101 220` **7.6.0 B** — conditions tag.
- `ETSI TS 101 220` **7.6.0 B** — Addition of supported system command tag.
- `ETSI TS 101 220` **7.6.0 B** — Reservation of Application code for DVB CBMS KMS.
- `ETSI TS 101 220` **7.7.0 B** — Tags for error responses for wrong TLVs.
- `ETSI TS 101 220` **7.7.0 B** — Addition of tag for the Extension of the number
- `ETSI TS 101 220` **7.8.0 B** — of logical channels.
- `ETSI TS 101 220` **7.8.0 B** — Introduction of a PIX coding for the ISIM API for Java Card™ TS 31.133.
- `ETSI TS 101 220` **7.8.0 B** — Tags for Remote Management Actions.
- `ETSI TS 101 220` **7.8.0 B** — Allocation of TAR values for the OMA SCWS
- `ETSI TS 101 220` **7.9.0 B** — and administrative agent.
- `ETSI TS 101 220` **7.9.0 B** — Modification of tags for RFM with script chaining.
- `ETSI TS 101 220` **7.10.0 B** — Tags for Launch Application feature.

### Rel-8

- `3GPP TS 31.111` **8.0.0 B** — Addition of EVENT: Network Rejection
- `3GPP TS 31.111` **8.3.0 B** — Introduction of a geographical location discovery mechanism in the
- `3GPP TS 31.111` **8.4.0 B** — I-WLAN Steering of Roaming Refresh Command
- `3GPP TS 31.111` **8.5.0 B** — Support of EPS in USAT: BIP, Provide Local Information, Call control
- `3GPP TS 31.111` **8.5.0 B** — Definition of the "idle" state in Event Download (Location Status) to
- `3GPP TS 31.111` **8.5.0 B** — Support of EPS in Network Rejection Event
- `ETSI TS 102 223` **8.0.0 B** — Alignment with 3GPP: New Network Rejection event
- `ETSI TS 102 223` **8.1.0 B** — Reserve Type of Command values for proprietary use
- `ETSI TS 102 223` **8.2.0 B** — Alignment with 3GPP for Geographical Location discovery
- `ETSI TS 102 223` **8.2.0 C** — Code reservation for Device Management and Device
- `ETSI TS 102 223` **8.2.0 B** — Synchronization applications in Registry Application Data
- `ETSI TS 102 223` **8.2.0 B** — Addition of Location Information for Broadcast technologies
- `ETSI TS 102 223` **8.3.0 B** — Reduced capability terminals in CAT
- `ETSI TS 102 223` **8.3.0 B** — Reservation of values for 3GPP related to I-WLAN Steering of
- `ETSI TS 101 220` **8.1.0 B** — Tag reservation related to addition of Network
- `ETSI TS 101 220` **8.2.0 B** — Rejection in 3GPP TS 31.111.
- `ETSI TS 101 220` **8.2.0 B** — Reserve a CAT template value for proprietary
- `ETSI TS 101 220` **8.2.0 B** — TLV reservation for Secure Channel.
- `ETSI TS 101 220` **8.3.0 B** — listing and ISIM PIX number.
- `ETSI TS 101 220` **8.3.0 C** — API for Java Card™.
- `ETSI TS 101 220` **8.3.0 B** — application.
- `ETSI TS 101 220` **8.3.0 B** — Broadcast Network Information in ETSI TS 102 223.
- `ETSI TS 101 220` **8.3.0 B** — Security Domain. ETSI Release 18 43 ETSI TS 101 220 V18.3.0 (
- `ETSI TS 101 220` **8.4.0 B** — format detection.
- `ETSI TS 101 220` **8.4.0 B** — Reservation of values for 3GPP related to
- `ETSI TS 101 220` **8.4.0 B** — I-WLAN Steering of Roaming Refresh Command.
- `ETSI TS 101 220` **8.4.0 B** — Tag reservation alignments for Secure

### Rel-9

- `3GPP TS 31.111` **9.0.0 B** — Discovery of surrounding CSG cells
- `3GPP TS 31.111` **9.0.0 B** — CSG cell selection event
- `ETSI TS 102 223` **9.0.0 C** — Addition of APN settings objects in the Launch Browser
- `ETSI TS 102 223` **9.1.0 B** — Enabling and disabling contactless functionality of the UICC
- `ETSI TS 101 220` **9.0.0 B** — GlobalPlatform.
- `ETSI TS 101 220` **9.1.0 C** — command and response structures.
- `ETSI TS 101 220` **9.1.0 C** — Generalized use of the Multiplexing
- `ETSI TS 101 220` **9.1.0 B** — Application.

### Rel-10

- `3GPP TS 31.111` **10.1.0 B** — Qualifier CP-50
- `3GPP TS 31.111` **10.1.0 B** — Communication control for IMS
- `3GPP TS 31.111` **10.2.0 B** — Rules for multiple entities providing USAT facilities
- `3GPP TS 31.111` **10.2.0 C** — Addition of Direct Communication Channel for BIP terminal server
- `3GPP TS 31.111` **10.2.0 B** — Introduction of the IARI based Incoming IMS Data event
- `3GPP TS 31.111` **10.2.0 B** — Introduction of ME behaviour during a REFRESH of EF_UICCIARI
- `3GPP TS 31.111` **10.2.0 B** — Introduction the IARI based Open Channel command
- `3GPP TS 31.111` **10.2.0 C** — Enable Re-assignment of tag values
- `3GPP TS 31.111` **10.7.0 B** — information flow between the ME and the UICC CP-56
- `3GPP TS 31.111` **10.7.0 B** — Addition of Provide local information, H(e)NB IP address
- `3GPP TS 31.111` **10.7.0 B** — Addition of Provide local information, H(e)NB surrounding macrocells
- `ETSI TS 102 223` **10.0.0 C** — Addition of objects for user confirmation
- `ETSI TS 102 223` **10.1.0 B** — Direct application channel to terminal applications
- `ETSI TS 102 223` **10.1.0 B** — Addition of launch parameters for terminal applications
- `ETSI TS 102 223` **10.1.0 B** — Extension of Broadcast Network Information for WiMAX and
- `ETSI TS 102 223` **10.2.0 B** — Reservation of terminal profile bit for 3GPP (IMS
- `ETSI TS 102 223` **10.2.0 B** — Addition of 'CAT over Modem Interface' when no routing
- `ETSI TS 102 223` **10.4.0 B** — Encapsulated CAT commands and envelopes without security
- `ETSI TS 101 220` **10.0.0 B** — Definition of TAR value for OMA BCAST smart
- `ETSI TS 101 220` **10.2.0 B** — Measurement Results (3GPP).

### Rel-11

- `3GPP TS 31.111` **11.0.0 C** — parameter CP-54
- `3GPP TS 31.111` **11.0.0 C** — Definition of procedures for Steering of Roaming
- `3GPP TS 31.111` **11.3.0 C** — Enhancements to the security of the SMS OTA download mechanisms
- `ETSI TS 102 223` **11.1.0 C** — User Confirmation Clarification
- `ETSI TS 102 223` **11.2.0 B** — CR 102 223 R11 #238r1: Security for encapsulated CAT
- `ETSI TS 101 220` **11.0.0 B** — Addition of 3G App codes for USIM-INI and
- `ETSI TS 101 220` **11.0.0 B** — reserved COMPREHENSION-TLV tag values and addition of COMPREHENSION-TLV tag values for 3GPP IARI IMPU list IMS Status Code.
- `ETSI TS 101 220` **11.1.0 B** — container and call control result.

### Rel-12

- `3GPP TS 31.111` **12.1.0 C** — PLMN ID to "CSG Cell Selection" ENVELOPE COMMAND
- `3GPP TS 31.111` **12.3.0 B** — Addition of POLL INTERVAL ENVELOPE command
- `3GPP TS 31.111` **12.4.0 B** — URI support in MT Call event
- `3GPP TS 31.111` **12.4.0 B** — Modification to TI value
- `3GPP TS 31.111` **12.5.0 C** — Reference ETSI Specifications for the POLL INTERVAL Negotiation
- `3GPP TS 31.111` **12.5.0 C** — Update of reference to ETSI TS 102 223
- `3GPP TS 31.111` **12.7.0 B** — ENVELOPE for ProSe usage information reporting
- `ETSI TS 102 223` **12.0.0 B** — Addition of new TLV to indicate the conditions to proceed with
- `ETSI TS 102 223` **12.0.0 B** — REFRESH proactive command
- `ETSI TS 102 223` **12.0.0 C** — Modification on handling default URL for LAUNCH BROWSER
- `ETSI TS 102 223` **12.1.0 C** — Clarification on reusing an existing channel with OPEN
- `ETSI TS 102 223` **12.2.0 C** — (CR renumbered to 274 instead of 270)
- `ETSI TS 101 220` **12.0.0 B** — COMPREHENSION-TLV Tag Value for 3GPP2 Emergency Call.
- `ETSI TS 101 220` **12.0.0 B** — Tags for encapsulated CAT secure channel
- `ETSI TS 101 220` **12.0.0 B** — Tag value for "MAC" is '60' as '61' was already assigned to "3GPP2 (Emergency Call Object Tag)".
- `ETSI TS 101 220` **12.0.0 B** — ID for the High Update Arrays GP Global
- `ETSI TS 101 220` **12.0.0 B** — Addition of M2MSM ETSI app code and M2M
- `ETSI TS 101 220` **12.1.0 B** — Allocation of tag values for new TLV objects
- `ETSI TS 101 220` **12.1.0 B** — Add TLV tag support for 3GPP2 USSD.
- `ETSI TS 101 220` **12.1.0 C** — Deletion of tag value for IP Address List and
- `ETSI TS 101 220` **12.1.0 B** — Surrounding macrocells TLV objects (3GPP request).
- `ETSI TS 101 220` **12.1.0 B** — Allocation of tag value for PLMN ID defined in
- `ETSI TS 101 220` **12.1.0 B** — 3GPP. ETSI Release 18 44 ETSI TS 101 220 V18.3.0 (
- `ETSI TS 101 220` **12.1.0 B** — Attribution of AID and Tag for oneM2M.
- `ETSI TS 101 220` **12.1.0 B** — Tag allocation for Platform to Platform CAT
- `ETSI TS 101 220` **12.1.0 B** — Secured APDU.
- `ETSI TS 101 220` **12.1.0 B** — Allocation of TAR value for OMA LWM2M
- `ETSI TS 101 220` **12.1.0 B** — SMS security.
- `ETSI TS 101 220` **12.1.0 B** — Allocation of 3G application code for 3GPP

### Rel-13

- `3GPP TS 31.111` **13.0.0 B** — WLAN Bearer alignment of USAT
- `3GPP TS 31.111` **13.0.0 B** — URI support by USAT SMS related features
- `3GPP TS 31.111` **13.0.0 C** — Removal of mandatory clause
- `3GPP TS 31.111` **13.0.0 B** — Enhanced IMS Call Control
- `3GPP TS 31.111` **13.1.0 B** — Alignment of Provide Local Information for Timing Advance to E-
- `3GPP TS 31.111` **13.2.0 C** — Condition to send Location status and access technology change
- `3GPP TS 31.111` **13.3.0 B** — events CP-71
- `3GPP TS 31.111` **13.3.0 B** — Handling of long URI for IMS call control
- `3GPP TS 31.111` **13.3.0 B** — Handling of long URI for MT Call event
- `3GPP TS 31.111` **13.4.0 B** — Adding of Extended EMM Cause to the EVENT DOWNLOAD –
- `ETSI TS 102 223` **13.1.0 B** — Addition of the support of Radio Access Technologies to
- `ETSI TS 102 223` **13.1.0 C** — Supported Radio Access Technologies Coding improvement
- `ETSI TS 102 223` **13.1.0 B** — Allocation of bits in the TERMINAL PROFILE for 3GPP for
- `ETSI TS 102 223` **13.1.0 B** — Terminal Server Mode
- `ETSI TS 102 223` **13.1.0 B** — REFRESH command for profile switch
- `ETSI TS 102 223` **13.1.0 C** — CAT ACTIVATE command support
- `ETSI TS 101 220` **13.0.0 B** — 1 Access Technologies.
- `ETSI TS 101 220` **13.0.0 B** — Alignment with 3GPP CT6 tag assignment.

### Rel-14

- `3GPP TS 31.111` **14.1.0 B** — URI support by USAT SMS-PP-Download
- `3GPP TS 31.111` **14.2.0 B** — 3GPP PS Data Off and BIP
- `3GPP TS 31.111` **14.2.0 B** — New Event Download for Data Connection Status
- `3GPP TS 31.111` **14.5.0 B** — Definition of values reserved by ETSI SCP for 3GPP usage
- `3GPP TS 31.111` **14.5.0 B** — Support of Non-IP Data Delivery by ME
- `ETSI TS 102 223` **14.0.0 B** — Addition of eUICC event proactive command
- `ETSI TS 102 223` **14.0.0 C** — CAT and background mode
- `ETSI TS 102 223` **14.0.0 C** — Addition of duration object in the GET INPUT proactive
- `ETSI TS 101 220` **14.0.0 B** — Addition of eUICC operation command.
- `ETSI TS 101 220` **14.0.0 B** — New Remote Management Application Data
- `ETSI TS 101 220` **14.0.0 B** — Template BER-TLV Tag.
- `ETSI TS 101 220` **14.0.0 B** — Tag for application specific refresh data.

### Rel-15

- `3GPP TS 31.111` **15.1.0 B** — Enhance Location Information object for NG-RAN Cell Identity
- `3GPP TS 31.111` **15.1.0 B** — Enhance Bearer object in Toolkit spec to accommodate 5GS
- `3GPP TS 31.111` **15.3.0 B** — related text in 31.111. CP-80
- `3GPP TS 31.111` **15.3.0 B** — Update the UICC Toolkit Data Connection Status Change Event for
- `3GPP TS 31.111` **15.3.0 B** — Update the UICC Toolkit Network Reject Event for 5GS
- `3GPP TS 31.111` **15.3.0 B** — 5G support for the OPEN CHANNEL command
- `3GPP TS 31.111` **15.3.0 B** — Call Control update for PDU sessions
- `3GPP TS 31.111` **15.4.0 B** — Network Measurement Report update for NR
- `3GPP TS 31.111` **15.4.0 B** — Update the version number of ETSI TS 102223 to latest
- `3GPP TS 31.111` **15.4.0 B** — Enhance Location Information object to accommodate 3 byte TAC for
- `3GPP TS 31.111` **15.4.0 B** — SMS-PP Data download procedure to support SoR using 5G NAS
- `3GPP TS 31.111` **15.4.0 B** — Fix implementation error for USIM Call Control procedure and allow
- `3GPP TS 31.111` **15.5.0 C** — Enable procedure for update of Routing ID data in the UICC, as
- `3GPP TS 31.111` **15.6.0 C** — data as data set.
- `3GPP TS 31.111` **15.6.0 C** — Update Routing Indicator data update procedure to indicate REFRESH
- `ETSI TS 102 223` **15.0.0 B** — Allocation of values reserved for 3GPP
- `ETSI TS 102 223` **15.0.0 B** — Addition of "NR" RAT to list of access technologies
- `ETSI TS 101 220` **15.0.0 B** — and implementation of outstanding CRs.
- `ETSI TS 101 220` **15.0.0 B** — Allocation of values reserved for 3GPP.
- `ETSI TS 101 220` **15.1.0 B** — Tag for GSMA.
- `ETSI TS 101 220` **15.2.0 B** — Introduction of the family identifier for SSP.

### Rel-16

- `3GPP TS 31.111` **16.0.0 B** — Introducing Network Measurement Results for NG-RAN
- `3GPP TS 31.111` **16.0.0 B** — radio technology
- `ETSI TS 101 220` **16.0.0 B** — ETSI TS 102 588 and ETSI TS 102 705.
- `ETSI TS 101 220` **16.0.0 B** — Addition of 3GPP USIM (non-IMSI SUPI

### Rel-17

- `3GPP TS 31.111` **17.1.0 B** — REFRESH SoR-CMCI command introduction
- `3GPP TS 31.111` **17.3.0 B** — Add EFSUPI_NAI changing procedure for REFRESH
- `3GPP TS 31.111` **17.3.0 B** — Satellite NG-RAN introduction
- `3GPP TS 31.111` **17.3.0 B** — Addition of procedure for REFRESH command triggered by update of
- `3GPP TS 31.111` **17.3.0 B** — Addition of the procedure for updating of 5G NSWO configuration in
- `3GPP TS 31.111` **17.4.0 B** — Satellite E-UTRAN in USAT
- `3GPP TS 31.111` **17.5.0 B** — Enhance Location Information object to accommodate complete TAI
- `ETSI TS 102 223` **17.0.0 B** — 3GPP Alignment : addition of Satellite NG-RAN
- `ETSI TS 102 223` **17.1.0 B** — Introduction to Multiple Logical Interfaces
- `ETSI TS 102 223` **17.2.0 B** — Alignment with 3GPP: addition of CAG feature
- `ETSI TS 101 220` **17.0.0 B** — Alignment with 3GPP CT6 tag assignment and
- `ETSI TS 101 220` **17.0.0 B** — reallocation of tag value 58 wrongly allocated, introduction of Satellite NG-RAN.
- `ETSI TS 101 220` **17.0.0 B** — Tag for LSI numbers data object.
- `ETSI TS 101 220` **17.0.0 B** — AID and TAR values unique in the scope of an

### Rel-18

- `3GPP TS 31.111` **18.0.0 B** — TERMINAL RESPONSE command length and Slice information length and CAG information list tag
- `3GPP TS 31.111` **18.0.0 B** — Events extension for slice purpose
- `3GPP TS 31.111` **18.2.0 B** — PROVIDE LOCAL INFORMATION to get Slices information with S-
- `3GPP TS 31.111` **18.4.0 B** — PROVIDE LOCAL INFORMATION (Rejected Slices information)
- `3GPP TS 31.111` **18.5.0 B** — Partially supported S-NSSAIs in a set of tracking areas of a
- `3GPP TS 31.111` **18.5.0 B** — Extended information to handle location information with
- `3GPP TS 31.111` **18.5.0 B** — extended identities in NG-RAN
- `3GPP TS 31.111` **18.5.0 B** — Required Rel 18 update to PLI - Slice Information, and Slice
- `3GPP TS 31.111` **18.6.0 B** — Handling complete list of Partial S-NSSAIs.
- `ETSI TS 102 223` **18.0.0 B** — in Terminal Profile
- `ETSI TS 102 223` **18.0.0 B** — Terminal Profile Bit assignment and Command Details for
- `ETSI TS 102 223` **18.1.0 B** — PROVIDE LOCAL INFORMATION for 3GPP related Slice(s)
- `ETSI TS 102 223` **18.1.0 B** — 3GPP Alignment on Terminal Profile for 5G ProSe, chaining of
- `ETSI TS 102 223` **18.2.0 B** — Handling Permanent and Temporary BIP Failures
- `ETSI TS 101 220` **18.0.0 B** — Addition of tag '84' to Terminal capabilities
- `ETSI TS 101 220` **18.0.0 B** — template ('A9')
- `ETSI TS 101 220` **18.0.0 B** — 3GPP SSIM Application addition
- `ETSI TS 101 220` **18.1.0 B** — Alignment with tag assignment and tag
- `ETSI TS 101 220` **18.2.0 B** — 3GPP Alignment for 5G ProSe, chaining of PLI

## Machine-readable summary

The seed for the code-side release table (`catrelease.py` / the PWA mirror):

```json
{
  "events": {
    "0x00": 4,
    "0x01": 4,
    "0x02": 4,
    "0x03": 4,
    "0x04": 4,
    "0x05": 4,
    "0x06": 4,
    "0x07": 4,
    "0x08": 4,
    "0x09": 4,
    "0x0A": 4,
    "0x0B": 4,
    "0x0C": 4,
    "0x0D": 4,
    "0x0E": 4,
    "0x0F": 4,
    "0x10": 6,
    "0x11": 7,
    "0x12": 8,
    "0x13": 9,
    "0x14": 12,
    "0x15": 9,
    "0x16": 9,
    "0x17": 10,
    "0x18": 10,
    "0x19": 10,
    "0x1B": 11,
    "0x1C": 12,
    "0x1D": 14,
    "0x1E": 17,
    "0x1F": 16
  },
  "commands": {
    "REFRESH": 4,
    "MORE TIME": 4,
    "POLL INTERVAL": 4,
    "POLLING OFF": 4,
    "SET UP EVENT LIST": 4,
    "SET UP CALL": 4,
    "SEND SS": 4,
    "SEND USSD": 4,
    "SEND SHORT MESSAGE": 4,
    "SEND DTMF": 4,
    "LAUNCH BROWSER": 4,
    "PLAY TONE": 4,
    "DISPLAY TEXT": 4,
    "GET INKEY": 4,
    "GET INPUT": 4,
    "SELECT ITEM": 4,
    "SET UP MENU": 4,
    "PROVIDE LOCAL INFORMATION": 4,
    "TIMER MANAGEMENT": 4,
    "SET UP IDLE MODE TEXT": 4,
    "RUN AT COMMAND": 4,
    "LANGUAGE NOTIFICATION": 4,
    "OPEN CHANNEL": 4,
    "CLOSE CHANNEL": 4,
    "SEND DATA": 4,
    "RECEIVE DATA": 4,
    "GET CHANNEL STATUS": 4,
    "SERVICE SEARCH": 4,
    "GET SERVICE INFORMATION": 4,
    "DECLARE SERVICE": 4,
    "SET FRAMES": 6,
    "GET FRAMES STATUS": 6,
    "RETRIEVE MULTIMEDIA MESSAGE": 7,
    "SUBMIT MULTIMEDIA MESSAGE": 7,
    "DISPLAY MULTIMEDIA MESSAGE": 7,
    "GEOGRAPHICAL LOCATION REQUEST": 8,
    "ACTIVATE": 13,
    "CONTACTLESS STATE CHANGED": 9,
    "COMMAND CONTAINER": 10,
    "ENCAPSULATED SESSION CONTROL": 11,
    "LSI COMMAND": 17
  },
  "objects": {
    "06/86 Address": 4,
    "08/88 Subaddress": 4,
    "1C/9C Transaction identifier": 4,
    "1A/9A Cause": 4,
    "34/B4 Browser termination cause": 4,
    "35/B5 Bearer description": 4,
    "37/B7 Channel data length": 4,
    "38/B8 Channel status": 4,
    "3E/BE Other address (local / destination)": 4,
    "40/C0 Display parameters": 4,
    "64/E4 Browsing status": 4,
    "65/E5 Network search mode": 4,
    "67/E7 Frames Information": 6,
    "4B/CB (I-)WLAN Access Status": 7,
    "55/D5 CSG cell selection status": 9,
    "56/D6 CSG ID": 9,
    "57/D7 HNB name": 9,
    "31/B1 IMS URI": 10,
    "77/F7 IARI": 10,
    "77/F7 IMPU list": 11,
    "78/F8 IMS status code": 11,
    "7E/FE Media Type": 12,
    "09/89 PLMN ID": 4,
    "3F/BF Access technology": 4,
    "B4 Supported Radio Access Technologies": 13,
    "9D Data connection status": 14,
    "AA Data connection type": 14,
    "AE (E/5G)SM cause": 14,
    "C7 Network Access Name": 14,
    "0B/8B PDP/PDN/PDU type": 14,
    "26/A6 Date-Time and Time zone": 4,
    "13/93 Location Information": 4,
    "55/D5 CAG cell selection status": 17,
    "56/D6 CAG information list": 17,
    "57/D7 CAG Human-readable network name list": 17,
    "55/D5 Slices status": 16,
    "56/D6 Slices information (served)": 16,
    "78/F8 Allowed slices information": 16,
    "77/F7 Allowed slices with S-NSSAI mapping": 18,
    "D7 Rejected slices with S-NSSAI mapping": 18,
    "B1 Rejected slices information": 18
  },
  "pli": {
    "00": 4,
    "01": 4,
    "02": 4,
    "03": 4,
    "04": 4,
    "06": 4,
    "07": 4,
    "08": 6,
    "09": 4,
    "0A": 4,
    "0D": 8,
    "0E": 12,
    "0F": 10,
    "10": 10,
    "11": 9,
    "12": 10,
    "13": 10,
    "14": 13,
    "15": 16,
    "16": 17,
    "17": 18,
    "1A": 13
  }
}
```
