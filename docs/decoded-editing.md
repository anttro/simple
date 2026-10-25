# Decoded editing — file-format survey and design

Status: design note for the **Edit decoded** feature.  Waves 1–3 shipped in
v3.33.0 (SPN/PLMN/ADN/PNN), v3.34.0 (service tables/scalars/PLMN lists/BCD/CB)
and v3.35.0 (EF.SMSP); decoded editing for EF.DIR/EF.ARR/EF.SMS/EF.PBR is
**postponed by decision** (their decoded view is enough).  It classifies
the EFs the workbench knows (the client-side decoder registry) by the spec's
own structure and content coding, assigns each an editing *shape*, and sets the
release waves.  The reference is the pinned specs (`projects/docs/uicc/`):
3GPP TS 31.102 v18.10 (USIM), TS 51.011 v4.15 (SIM), TS 24.008 v18.8 (network
name / PLMN / address coding), TS 102 221 (file structure / Annex A text).
pySim is a cross-check only.

The editor is client-side, like the decoders: the file is read raw, decoded
into its schema's values, edited, re-encoded and written with `/api/write`.
Only the files listed with a shape have an encoder; everything else keeps the
raw editor.

## 1. Editing shapes

Each shape is one renderer + one encoder contract.  `fields` are the flat form
values, `list` is a transparent file holding N items, `items` is a record
holding a TLV list; the record-scoped variants reuse the same field/item
renderers inside a per-record block with a "write this record" checkbox
(auto-checked when an input changes).

| Shape | File / record content | Editor |
|---|---|---|
| **S0** read-only | card- or network-managed values (location, keys, counters) | no editor; the button says why |
| **S1** scalar/flag TR | one or a few bytes / flags | one form |
| **S2** text TR | Annex A (TS 102 221) or NAI text | one form, text field |
| **S3** BCD TR | ICCID / IMSI (nibble-swapped BCD, parity/type nibble) | one form, digits |
| **S4** PLMN list TR | n × 3-byte PLMN (TS 24.008) | item rows, add/remove |
| **S5** PLMN+ACT list TR | n × (3-byte PLMN + 2-byte ACT) | item rows, add/remove, tech multi-select |
| **S6** bit/service list TR | service or identifier bit lists | item list with labels (checkbox per service/id) |
| **S7** fixed-record fields | alpha + BCD address + ids (ADN family), LAI+id (OPL), code+alpha+category (ECC), TP parameters (SMSP) | per-record form + checkbox |
| **S8** record TLV list | PNN (name TLVs), DIR (application templates), ARR (access rules), PBR (A8/A9/AA containers) | per-record item rows, add/remove/edit; unknown TLVs preserved |
| **S9** nested structures | PBR containers, DIR templates, EXT chains | later: a tree, or read-only with a structured view |
| **S10** dedicated builders | SMS (TPDU), SMSP (TPDU parameter set), key material | later: a dedicated form, not a generic encoder |

Rules that apply to every encoder (and are what the tests pin):

- encode **only the edited values**, from the original record/file bytes:
  length, padding and anything the schema does not expose are preserved;
- never change the file/record length; refuse when the new value does not fit;
- preserve the original text coding when the text is unchanged, and choose a
  spec-legal coding when it changed (Annex A: unpacked GSM 7-bit / UCS-2; PNN
  network name: packed GSM 7-bit with the spare-bit count / UCS-2);
- refuse (not silently truncate) when an edit would invalidate a cross-file
  reference (`EF.EXT1` chain, PBR/IAP, OPL→PNN record id, CMI pointer).

## 2. Survey of the known files

`Wave` is the release that adds the encoder.  Files we do not decode yet are
listed only where they matter for the plan; the spec's ~120 USIM EFs include
many specialized ones (MMS, VGCS/VBS, ePDG, IMS configuration, …) that are not
editing candidates.

### 2.1 Transparent files

| EF | FID | Spec | Size | Content | Shape | Wave |
|---|---|---|---|---|---|---|
| EF.ICCID | 2FE2 | TS 51.011 10.3.1 | 10 B | BCD (nibble-swapped), F padding; UPDATE is NEV per TS 102 221 | S3 | **3.34** |
| EF.IMSI | 6F07 | TS 31.102 4.2.2 | 9 B | length byte + parity/type nibble ('9' odd / '1' even) + BCD | S3 | **3.34** |
| EF.IST | 6F07 | TS 31.102 4.2.2 (SAIP) | 1–2 B | service bits | S6 | **3.34** |
| EF.UST | 6F38 | TS 31.102 4.2.8 | X ≥ 1 | service bits | S6 | **3.34** |
| EF.SST | 6F38 | TS 51.011 10.3.7 | X ≥ 1 | **2 bits/service** (allocated + activated) | S6 (pending) | decoder fixed; editor later |
| EF.EST | 6F56 | TS 31.102 4.2.47 | X ≥ 1 | enabled-service bits (3 services) | S6 | **3.34** |
| EF.LI | 6F05 | TS 31.102 4.2.1 | 2n | ISO 639 letters (2 B each) | S6 | 3.35 |
| EF.LP | 6F05 | TS 51.011 10.3.1 | n | **1 byte per language** (SMS 7-bit) | S6 | 3.35 |
| EF.PL | 2F05 | TS 102 221 13.3 | 2n | ISO 639 letters (2 B each, like LI) | S6 | 3.35 |
| EF.SPN | 6F46 | TS 31.102 4.2.12 | 17 B | condition byte + Annex A text | S2 | **3.33** |
| EF.PLMNsel | 6F30 | TS 51.011 10.3.4 | 3n (n ≥ 8) | PLMN list | S4 | **3.34** |
| EF.FPLMN | 6F7B | TS 31.102 4.2.16 | 3n (n ≥ 4) | PLMN list (FFFFFF holes anywhere) | S4 | **3.34** |
| EF.EHPLMN | 6FD9 | TS 31.102 4.2.84 | 3n | PLMN list | S4 | **3.34** |
| EF.PLMNwAcT | 6F60 | TS 31.102 4.2.5 | 5n (n ≥ 8) | PLMN + ACT | S5 | **3.33** |
| EF.OPLMNwAcT | 6F61 | TS 31.102 4.2.53 | 5n (n ≥ 8) | PLMN + ACT | S5 | **3.33** |
| EF.HPLMNwAcT | 6F62 | TS 31.102 4.2.54 | 5n (n ≥ 1) | PLMN + ACT | S5 | **3.33** |
| EF.SPDI | 6FCD | TS 31.102 4.2.66 | x | TLV: A3 > 80 > PLMN list | S4 | 3.35 |
| EF.GID1 / GID2 | 6F3E / 6F3F | TS 31.102 4.2.10/4.2.11 | n | identifier bytes (coding not stated in the clause) | S2 | 3.35 (verify TS 31.101) |
| EF.SUUME | 6F54 | TS 51.011 10.3.34 | X+Y | alpha (Annex A) | S2 | 3.35 |
| EF.IMPI/DOMAIN/IMPU | 6F02/6F03/6F04 | TS 31.102 (ISIM) | X | NAI text | S2 | 3.35 |
| EF.ACC | 6F78 | TS 31.102 4.2.15 | 2 B | class bits (b8…b1 = 15…8 / 7…0; ACC 10 written 0) | S6 | **3.34** |
| EF.PHASE | 6FAE | TS 51.011 10.3.19 | 1 B | phase id (00/02/03) | S1 | **3.34** |
| EF.HPPLMN | 6F31 | TS 31.102 4.2.6 | 1 B | search-period multiplier | S1 | **3.34** |
| EF.AD (SIM) | 6FAD | TS 51.011 10.3.18 | 3+X | op-mode byte + additional info (no MNC length) | S1 | **3.34** |
| EF.AD (USIM) | 6FAD | TS 31.102 4.2.18 | 4+X | op-mode byte + info + MNC length (may be 0) | S1 | **3.34** |
| EF.CBMI | 6F45 | TS 31.102 4.2.14 | 2n | CB message ids | S6 | **3.34** |
| EF.CBMID | 6F48 | TS 31.102 4.2.20 | 2n | CB message ids | S6 | **3.34** |
| EF.CBMIR | 6F50 | TS 31.102 4.2.22 | 4n | CB id ranges | S6 | **3.34** |
| EF.ACL | 6F57 | TS 31.102 4.2.48 | X | version byte + TLV list (DD APNs) | S8-TR | 3.35 |
| EF.LOCI | 6F7E | TS 31.102 4.2.17 | 11 B | TMSI/LAI/status | **S0** | — |
| EF.PSLOCI | 6F73 | TS 31.102 4.2.23 | 14 B | P-TMSI/RAI/status | **S0** | — |
| EF.EPSLOCI | 6FE3 | TS 31.102 4.2.91 | 18 B | GUTI/TAI/status | **S0** | — |
| EF.LOCIGPRS | 6F53 | TS 51.011 10.3.33 | 14 B | GPRS location | **S0** | — |
| EF.Kc / KcGPRS | 6F20 / 6F52 | TS 31.102 4.4.3.1/4.4.3.2 | 9 B | ciphering keys | **S0** | — |
| EF.Keys / KeysPS | 6F08 / 6F09 | TS 31.102 4.2.3/4.2.4 | 33 B | keys + status | **S0** | — |

### 2.2 Linear fixed / cyclic files

| EF | FID | Spec | Record | Content | Shape | Wave |
|---|---|---|---|---|---|---|
| EF.ADN | 6F3A | TS 31.102 4.4.2.3 | X+14 | alpha + len + TON/NPI + 10 B BCD + CCP + EXT1 | S7 | **3.33** |
| EF.FDN | 6F3B | TS 31.102 4.2.24 | X+14 | ADN layout (EXT2) | S7 | **3.33** |
| EF.SDN | 6F49 | TS 31.102 4.2.29 | X+14 | ADN layout (EXT3) | S7 | **3.33** |
| EF.MSISDN | 6F40 | TS 31.102 4.2.26 | X+14 | ADN layout (EXT5) | S7 | **3.33** |
| EF.LND | 6F44 | TS 51.011 10.5.8 | X+14 | ADN layout, **cyclic** | S7 | **3.33** |
| EF.BDN | 6F4D | TS 31.102 4.2.44 | **X+15** | ADN layout + **Comparison Method Pointer** | S7 | **3.33** (fix) |
| EF.PNN | 6FC5 | TS 31.102 4.2.58 | X ≥ 3 | TLVs: 43 full / 45 short / 80 PLMN info | S8 | **3.33** |
| EF.OPL | 6FC6 | TS 31.102 4.2.59 | 8 | LAI (PLMN+LAC) + PNN record id | S7 | 3.35 |
| EF.ECC (SIM) | 6FB7 | TS 51.011 10.3.27 | 3n | emergency codes (TR) | S6 | 3.35 |
| EF.ECC (USIM) | 6FB7 | TS 31.102 4.2.21 | X+4 | code(3) + alpha(X) + category(1) | S7 | 3.35 |
| EF.EXT1–EXT5 | 6F4A–6F55 | TS 31.102 4.4.2.4 / 4.2.30/31/45/37 | 13 | type-dependent: additional data / subaddress | S9 | 3.36 |
| EF.SMSP | 6F42 | TS 31.102 4.2.27 | 28+Y | alpha + parameter indicators + TP-DA/SCA/PID/DCS/VP | S10 | **3.35** |
| EF.SMS | 6F3C | TS 31.102 4.2.25 | 176 | status byte + TPDU | S10 | **postponed (read-only)** |
| EF.ARR | 2F06/6F06 | TS 31.102 4.2.55 | X | access-rule TLVs (ISO 7816-4) | S8/S9 | **postponed (read-only)** |
| EF.DIR | 2F00 | TS 51.011 10.3.3 | X | application template TLVs (61 > 4F/50/73…) | S8/S9 | **postponed (read-only)** |
| EF.PBR | 4F30/4FXX | TS 31.102 4.4.2.1 | X | BER-TLV: A8/A9/AA containers, C0–CC links | S9 | **postponed (read-only)** |
| EF.EPSNSC | 6FE4 | TS 31.102 4.2.92 | — | NAS security context | **S0** | — |
| EF.ICI/OCI (call logs) | 6F80/6F81 | TS 31.102 4.2.33/4.2.34 | X+28/X+27 | ADN-like + time/date → cyclic, ME-written | **S0** (plan) | — |

### 2.3 Coverage vs the spec

The decoder registry covers ~45 EFs.  The spec defines many more; the
remaining ones are either specialized protocols (MMS, VGCS/VBS, ePDG, IMS
configuration, GBA, 5G slice configuration) or administrative/key files that
must not be edited from a workbench without a dedicated flow (Keys, DCK,
network parameters, START-HFN).  Nothing in the survey forces a new shape
beyond S9/S10.

### 2.4 SIM ↔ USIM format divergence

Some features are represented by a different file (or the same FID with a
different structure) in the SIM (DF.GSM, TS 51.011) and USIM (ADF.USIM,
TS 31.102) applications.  Three groups:

| group | SIM | USIM | purpose |
|---|---|---|---|
| A — same FID + name, different format | EF.ECC 6FB7 transparent 3n (code+ESC) | EF.ECC 6FB7 linear X+4 (code+alpha+ESC) | emergency codes |
| A | EF.AD 6FAD 3+X (no MNC length) | EF.AD 6FAD 4+X (+ MNC length) | administrative data |
| B — same FID, different name + format | EF.SST 6F38 (2-bit) | EF.UST 6F38 (1-bit) | service table |
| B | EF.LP 6F05 (1 B/lang) | EF.LI 6F05 (2 B/lang) | language preference |
| C — different FID + name + format | EF.PLMNsel 6F30 (3n, no AcT) | EF.PLMNwAcT 6F60 (5n, +AcT) | user PLMN selector |
| C | EF.Kc/KcGPRS 6F20/6F52 (9 B) | EF.Keys/KeysPS 6F08/6F09 (33 B) | ciphering keys |
| C | EF.CCP 6F3D (14 B) | EF.CCP2 6F4F (X≥15) | bearer capability |

Group A is the only one where the *name and FID both* collide, so those two
(EF.ECC, EF.AD) are what the DF-context resolver (finding 6) is for; groups B
and C are already disambiguated by name.  EF.HPLMNwAcT (a list) vs the SIM
EF.HPPLMN (a 1-byte search period) is a related-but-different pair, and
OPLMNwAcT/OPL/PNN/EHPLMN/FPLMN/SPDI/IMG are USIM-only (no SIM counterpart).

## 3. Findings (defects and constraints the survey exposed)

1. **EF.BDN is X+15, not X+14.**  The shared ADN codec assumes the last two
   bytes are CCP1/EXT1; BDN adds a trailing **Comparison Method Pointer**
   (`X+15`, TS 31.102 4.2.44).  The read-only decoded view currently
   mis-parses BDN records, and the v3.33.0 encoder would corrupt them.  The
   codec must take the layout (ADN vs BDN) and preserve the CMI pointer.
2. **EF.ECC is two different files, not one file with two layouts.**  They
   share the FID `6FB7` **and the name `EF.ECC`**, but live in different DFs
   under different specs: DF.GSM — transparent 3n, code(2 BCD) + category
   (TS 51.011 10.3.27); ADF.USIM — linear fixed X+4, code(3 BCD) + alpha
   (Annex A) + category (TS 31.102 4.2.21).  The decoder implements the SIM
   file only; on a USIM the alpha bytes are read as more codes.  They also
   need **different editors** (a plain code list vs a record form with a text
   alpha), so they are two registry entries, resolved by the DF context —
   not a name/FID lookup, which is ambiguous here (the name *and* the FID
   collide).
3. **S8 is not one shape.**  PNN (flat tag+length+value TLVs) is the simple
   case.  DIR/ARR/PBR nest TLVs and cross-reference other files; they need
   either a tree editor or a structured read-only view first.
4. **References are part of the model.**  EXT1–5 chains (ADN/MSISDN/BDN),
   OPL→PNN record ids, PBR→(ADN/PBC/EXT/IAP) by FID/SFI.  An editor that
   ignores them can leave a card in a contradictory state: encoders must
   preserve untouched references and refuse edits that would break one.
5. **Cyclic files edit like linear ones** for our purposes (write by record
   number), but the card owns the order (LND/ICI/OCI/ACM); the editor notes
   it and the write is still a plain UPDATE.
6. **FID collisions across DFs** (e.g. 6F02 = EF.IMPI in ISIM and EF.OCST in
   USIM) make the fid-only decoder fallback unsafe; the name-based lookup is
   the primary one and encoders should not be fid-only where a collision is
   possible.  **Name collisions across DFs** also exist — EF.ECC and EF.AD
   (DF.GSM vs ADF.USIM) — where both the name *and* the FID collide.  The
   specs key files by **FID within their DF**, not by symbolic name (names are
   a derived display label), so the resolver must be **(DF, FID)-first**: the
   parent DF + FID is the authoritative key, and the name is only a label.
   The FS check in the Services editor follows the same rule — it probes a
   (DF, FID), never a name.
7. **Text coding is per file.**  Annex A (TS 102 221) for SPN/ADN/SUUME;
   the Network Name IE (TS 24.008 10.5.3.5a) for PNN, where the first byte is
   `0x80 | spare` for packed GSM 7-bit and `0x90` for UCS-2 — the real-card
   vector `43 0A 82 D4 32 BB BC 7E B7 5D E4 32 …` confirms it.
8. **EF.SST is a 2-bit table** (TS 51.011 10.3.7): 4 services per byte, first
   bit of the pair = allocated, second = activated.  The shared 1-bit
   UST/EST codec was used for it, so SIM service tables were both misread and
   miswritten; the decoder is fixed in v3.34.0, editing waits for an
   allocated/activated UI.
9. **EF.ACC class numbering** (TS 31.102 4.2.15): byte 1 b8…b1 = classes 15…8
   and byte 2 = 7…0 (class 10 is written as 0); the old decoder counted from
   class 0 at the MSB.  Fixed in v3.34.0.
10. **EF.AD's operation mode is a whole byte** (00/80/01/81/02/04), not bits
    0-1 plus a test bit; the MNC-length byte may be 0 (service n°130 available)
    or absent (3-byte SIM form).  The decoder and encoder follow this now.
11. **The IMSI parity/type nibble** (TS 24.008 10.5.1.4) is 9 for an odd digit
    count and 1 for an even one; the first encoder always wrote 9.
12. **The SIM/USIM language files differ in width.**  EF.LP (SIM, DF.GSM,
    6F05) is 1 byte per language (SMS 7-bit); EF.LI (USIM, ADF.USIM, 6F05) is
    the 2-byte ISO 639 pair; EF.PL (MF, 2F05) is 2 bytes, like LI — not 1
    byte.  The shared decoder is wrong for EF.LP only (queued with the
    language work).
13. **EF.SMSP's parameter indicators are a five-bit presence map with inverted
    meaning** (TS 31.102 §4.2.27: bit 1 DA, 2 SCA, 3 PID, 4 DCS, 5 VP; bit = 1
    means *absent*; bits 6–8 reserved = 1).  The decoder had the bits shifted
    (SCA on 5, PID on 2, DCS on 3, VP on 4), so mixed indicators were misread.
14. **The two address slots use different length conventions**: TP-DA's length
    is the digit count (TS 23.040 §9.1.2.5), TS-SCA's is the octet count of
    TON/NPI + digits with 0 = default SMSC (TS 24.011 §8.2.5.2); the decoder
    read the DA slot as octets.  Both are handled explicitly now.

## 4. Release waves

- **v3.33.0 (shipped)** — framework: encoder registry, field/list/items
  schemas, record checkboxes + auto-check, save/validation; shapes S2, S5,
  S7, S8 for SPN, PLMNwAcT/OPLMNwAcT/HPLMNwAcT, ADN/FDN/SDN/MSISDN/LND,
  PNN; the BDN layout fix; the `efPlmnAct` EC-GSM-IoT-only (`0x88`) decode fix.
- **v3.34.0 (shipped)** — the S1/S3/S4/S6 wave: UST/IST/EST service tables
  (filterable bit grid), ACC, PHASE, HPPLMN, AD, the PLMN-only lists
  (PLMNsel/FPLMN/EHPLMN), ICCID/IMSI (behind a confirmation) and
  CBMI/CBMID/CBMIR.  Decoder fixes: ACC class numbering, the AD operation
  mode / MNC-length rules, the SST 2-bit layout (read-only for now) and the
  IMSI parity nibble.
- **v3.35.0 (shipped)** — EF.SMSP as the first S10 dedicated builder: the full
  parameter form (alpha; TP-DA and TS-SCA with their own length conventions
  and TON; TP-PID; TP-DCS with TS 23.038 hints; the relative TP-VP with the
  TS 23.040 ladder), a per-parameter *absent (the ME default applies)* state,
  verbatim preservation of unchanged slots (including alphanumeric TON
  addresses) and the parameter-indicator and TP-DA-length decoder fixes.
- **v3.36.0** — LI/LP/PL (the SIM files need the 1-byte DCS language coding
  and a name-dependent decoder), GID1/GID2 (the clause states no coding —
  verify TS 31.101 first), SPDI (the A3 > 80 wrapper), OPL (PNN record
  select), ECC (**two files, two editors** — DF.GSM transparent code list vs
  ADF.USIM record form with an alpha; needs the DF-context resolver from
  finding 6), SUUME, IMPI/DOMAIN/IMPU, ACL; EXT1–EXT5 as a read-only
  structured view.
- **Postponed by decision (read-only; do not implement without confirmation)**
  — EF.DIR, EF.ARR, EF.SMS, EF.PBR: they rarely need manual changes and the
  decoded view is enough.
- **Read-only forever (S0)** unless a dedicated flow appears: LOCI/PSLOCI/
  EPSLOCI/EPSNSC/LOCIGPRS, Kc/KcGPRS, Keys/KeysPS, ACM, START-HFN/THRESHOLD,
  call logs (ME-written), DCK/NETPAR.

## 5. Testing strategy

- **Round-trip**: for every encoder, `encode(decode(original), original)`
  must reproduce the original bytes for spec-canonical records (the vectors
  come from the spec's own examples, pySim's test data and real-card dumps).
- **Edit-one-field**: changing a field changes only that field's bytes
  (padding, coding of untouched fields, ids and extension pointers stay).
- **Refusals**: over-capacity values, broken references, unsupported layouts
  (BDN with the CMI pointer, EXT1 chains) throw an actionable error.
- **No capability by accident**: a file without an encoder must never show
  the editor (gating test), and a schema/label drift guard keeps the editors
  and decoders aligned.

## 6. Cross-file "Services" editor (proposal)

A file-agnostic service editor for the Card reader tab, in addition to the
per-file decoded editors (which stay independent).  It presents the four
service tables side by side and writes them back consistently, so EF.SST's
allocated/activated layout no longer needs its own per-file grid (SST stays
read-only in the per-file editor).

- **Scope**: EF.SST (DF.GSM, 2 bits/service: allocated + activated), EF.UST
  (ADF.USIM, 1 bit), EF.EST (ADF.USIM, a 3-bit *enabled* subset of UST:
  FDN/BDN/ACL = UST 2/6/35) and EF.IST (ADF.ISIM, 1 bit, IMS services).
- **The numbering does not align across files**: SST and UST (and EST/IST)
  use different service-number -> name maps, so the editor is driven by a
  curated **feature cross-reference** (by name), not by service number.  Each
  feature row belongs to *SIM-only*, *USIM-only*, *ISIM-only* or *shared*
  (e.g. FDN = SST 3 / UST 2 / EST 1; SMS = SST 4 / UST 10).  The table is
  spec-derived and pinned by a test, like the event/TR vectors.
- **Card-aware and probed**: on open, the editor resolves the four files on
  the current card (the file-manager presence state, or a targeted probe) and
  renders a column per file that exists; an absent file (a card without ISIM
  has no EF.IST) hides that column and its feature rows entirely.  Presence is
  cached per `card_session`, like the EF.ARR copies.
- **Semantics**: "enabled" = UST bit set = SST *allocated* **and** *activated*.
  SST's intermediate "allocated but not activated" state has no UST
  equivalent, so the SIM column is a per-feature toggle between *on*
  (allocated + activated) and *off* (allocated but not activated) — the
  Services editor never de-allocates, so a card-wide change keeps the SIM
  able to re-enable the service.  Explicit *deletion* (clearing the allocation
  bit) is the per-file EF.SST editor's job: its grid is a tri-state per
  service (deleted / allocated-off / on).
- **Write, per file**: the merged form carries the intended bytes for every
  visible file, but writing is explicit and separate — one button per file
  ("Save to EF.SST", "Save to EF.UST", "Save to EF.EST", "Save to EF.IST").
  Each button writes only that file via `/api/write`; an unchanged file is
  skipped and the panel reports each write's result (there is no atomic
  cross-file write).
- **Filesystem check**: many services declare a DF or EF (ADN -> EF.ADN 6F3A,
  SPN -> EF.SPN 6F46, GSM Access -> DF.GSM, GPRS -> EF.KcGPRS, …); a second
  curated table maps a feature to the file(s) it requires.  A **Check** action
  probes the equipped card for those files (reusing the file-manager presence
  state / a targeted SELECT) and flags mismatches — a declared service whose
  file is absent, or a present file whose service is not declared.  Services
  that map to a capability rather than a file (CHV1 disable, Proactive SIM,
  …) carry no file check.  The result is cached per `card_session`.

### 6.1 Candidates for the same cross-application treatment

The cross-file pattern of the Services editor generalises to the other
SIM/USIM feature pairs (group C in §2.4), where one operator action should
update both applications consistently:

- **PLMN selectors** — EF.PLMNsel (SIM, no AcT) ↔ EF.PLMNwAcT (USIM, +AcT):
  a shared PLMN list, the SIM side dropping the AcT and the USIM side
  re-applying it (AcT is not expressible on the SIM side).
- **Language preferences** — EF.LP (SIM, 1 B/lang) ↔ EF.LI (USIM, 2 B/lang)
  ↔ EF.PL (MF, 2 B/lang): a shared ordered language list re-encoded per file.
- **Bearer capabilities** — EF.CCP (SIM) ↔ EF.CCP2 (USIM): a shared
  bearer-capability record re-encoded per file's layout.

None of these are committed; they are recorded here so the DF-context resolver
and the per-file encoders are shaped with them in mind (group A/C files must
not be fid-only).
