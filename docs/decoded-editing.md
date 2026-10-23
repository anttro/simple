# Decoded editing — file-format survey and design

Status: design note for the **Edit decoded** feature (v3.33.0).  It classifies
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
| EF.ICCID | 2FE2 | TS 51.011 10.3.1 | 10 B | BCD (nibble-swapped), F padding | S3 | 3.34 |
| EF.IMSI | 6F07 | TS 31.102 4.2.2 | 9 B | length byte + '9'-prefixed BCD | S3 | 3.34 |
| EF.IST | 6F07 | TS 31.102 4.2.2 (SAIP) | 1–2 B | service bits | S6 | 3.34 |
| EF.UST | 6F38 | TS 31.102 4.2.8 | X ≥ 1 | service bits | S6 | 3.34 |
| EF.SST | 6F38 | TS 51.011 10.3.7 | X ≥ 1 | service bits | S6 | 3.34 |
| EF.EST | 6F56 | TS 31.102 4.2.47 | X ≥ 1 | enabled-service bits | S6 | 3.34 |
| EF.LI | 6F05 | TS 31.102 4.2.1 | 2n | ISO 639 language codes | S6 | 3.34 |
| EF.LP / EF.PL | 6F05 / 2F05 | TS 51.011 10.3.1/10.3.2 | 1–n | language codes | S6 | 3.34 |
| EF.SPN | 6F46 | TS 31.102 4.2.12 | 17 B | condition byte + Annex A text | S2 | **3.33** |
| EF.PLMNsel | 6F30 | TS 51.011 10.3.4 | 3n (n ≥ 8) | PLMN list | S4 | 3.34 |
| EF.FPLMN | 6F7B | TS 31.102 4.2.16 | 3n (n ≥ 4) | PLMN list | S4 | 3.34 |
| EF.EHPLMN | 6FD9 | TS 31.102 4.2.84 | 3n | PLMN list | S4 | 3.34 |
| EF.PLMNwAcT | 6F60 | TS 31.102 4.2.5 | 5n (n ≥ 8) | PLMN + ACT | S5 | **3.33** |
| EF.OPLMNwAcT | 6F61 | TS 31.102 4.2.53 | 5n (n ≥ 8) | PLMN + ACT | S5 | **3.33** |
| EF.HPLMNwAcT | 6F62 | TS 31.102 4.2.54 | 5n (n ≥ 1) | PLMN + ACT | S5 | **3.33** |
| EF.SPDI | 6FCD | TS 31.102 4.2.66 | x | TLV: A3 > 80 > PLMN list | S4 | 3.34 |
| EF.GID1 / GID2 | 6F3E / 6F3F | TS 31.102 4.2.10/4.2.11 | n | text (Annex A) | S2 | 3.34 |
| EF.SUUME | 6F54 | TS 51.011 10.3.34 | X+Y | alpha (Annex A) | S2 | 3.35 |
| EF.IMPI/DOMAIN/IMPU | 6F02/6F03/6F04 | TS 31.102 (ISIM) | X | NAI text | S2 | 3.35 |
| EF.ACC | 6F78 | TS 31.102 4.2.15 | 2 B | class bits | S1/S6 | 3.34 |
| EF.PHASE | 6FAE | TS 51.011 10.3.19 | 1 B | phase id | S1 | 3.34 |
| EF.HPPLMN | 6F31 | TS 31.102 4.2.6 | 1 B | search period | S1 | 3.34 |
| EF.AD | 6FAD | TS 31.102 4.2.18 | 4+X | op mode, info, MNC length | S1 | 3.34 |
| EF.CBMI | 6F45 | TS 31.102 4.2.14 | 2n | CB message ids | S6 | 3.34 |
| EF.CBMID | 6F48 | TS 31.102 4.2.20 | 2n | CB message ids | S6 | 3.34 |
| EF.CBMIR | 6F50 | TS 31.102 4.2.22 | 4n | CB id ranges | S6 | 3.34 |
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
| EF.ECC (SIM) | 6FB7 | TS 51.011 10.3.27 | 3n | emergency codes (TR) | S6 | 3.34 |
| EF.ECC (USIM) | 6FB7 | TS 31.102 4.2.21 | X+4 | code(3) + alpha(X) + category(1) | S7 | 3.35 |
| EF.EXT1–EXT5 | 6F4A–6F55 | TS 31.102 4.4.2.4 / 4.2.30/31/45/37 | 13 | type-dependent: additional data / subaddress | S9 | 3.36 |
| EF.SMSP | 6F42 | TS 31.102 4.2.27 | 28+Y | alpha + parameter indicators + TP-DA/SCA/PID/DCS/VP | S10 | 3.36 |
| EF.SMS | 6F3C | TS 31.102 4.2.25 | 176 | status byte + TPDU | S10 | 3.36 |
| EF.ARR | 2F06/6F06 | TS 31.102 4.2.55 | X | access-rule TLVs (ISO 7816-4) | S8/S9 | 3.36 |
| EF.DIR | 2F00 | TS 51.011 10.3.3 | X | application template TLVs (61 > 4F/50/73…) | S8/S9 | 3.36 |
| EF.PBR | 4F30/4FXX | TS 31.102 4.4.2.1 | X | BER-TLV: A8/A9/AA containers, C0–CC links | S9 | later |
| EF.EPSNSC | 6FE4 | TS 31.102 4.2.92 | — | NAS security context | **S0** | — |
| EF.ICI/OCI (call logs) | 6F80/6F81 | TS 31.102 4.2.33/4.2.34 | X+28/X+27 | ADN-like + time/date → cyclic, ME-written | **S0** (plan) | — |

### 2.3 Coverage vs the spec

The decoder registry covers ~45 EFs.  The spec defines many more; the
remaining ones are either specialized protocols (MMS, VGCS/VBS, ePDG, IMS
configuration, GBA, 5G slice configuration) or administrative/key files that
must not be edited from a workbench without a dedicated flow (Keys, DCK,
network parameters, START-HFN).  Nothing in the survey forces a new shape
beyond S9/S10.

## 3. Findings (defects and constraints the survey exposed)

1. **EF.BDN is X+15, not X+14.**  The shared ADN codec assumes the last two
   bytes are CCP1/EXT1; BDN adds a trailing **Comparison Method Pointer**
   (`X+15`, TS 31.102 4.2.44).  The read-only decoded view currently
   mis-parses BDN records, and the v3.33.0 encoder would corrupt them.  The
   codec must take the layout (ADN vs BDN) and preserve the CMI pointer.
2. **EF.ECC has two layouts.**  SIM: transparent 3n (TS 51.011 10.3.27);
   USIM: linear fixed X+4 (TS 31.102 4.2.21).  The decoder implements the SIM
   layout only; on a USIM the alpha bytes are read as more codes.  A
   structure-aware decode is needed before an ECC editor.
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
   possible.
7. **Text coding is per file.**  Annex A (TS 102 221) for SPN/ADN/SUUME;
   the Network Name IE (TS 24.008 10.5.3.5a) for PNN, where the first byte is
   `0x80 | spare` for packed GSM 7-bit and `0x90` for UCS-2 — the real-card
   vector `43 0A 82 D4 32 BB BC 7E B7 5D E4 32 …` confirms it.

## 4. Release waves

- **v3.33.0 (current)** — framework: encoder registry, field/list/items
  schemas, record checkboxes + auto-check, save/validation; shapes S2, S5,
  S7, S8 for SPN, PLMNwAcT/OPLMNwAcT/HPLMNwAcT, ADN/FDN/SDN/MSISDN/LND,
  PNN; the BDN layout fix; the `efPlmnAct` EC-GSM-IoT-only (`0x88`) decode fix.
- **v3.34.0** — S1/S3/S6 wave: UST/SST/IST/EST service toggles, LI/LP/PL,
  ACC, PHASE, HPPLMN, AD, CBMI/CBMID/CBMIR, ECC (SIM), ICCID, IMSI,
  PLMNsel/FPLMN/EHPLMN, SPDI, GID1/GID2.
- **v3.35.0** — remaining S7 records: OPL (PNN record select), ECC (USIM),
  SUUME, IMPI/DOMAIN/IMPU; EXT1–5 as a read-only structured view.
- **v3.36.0+** — S10/complex: SMSP (parameter form), SMS (TPDU editor,
  reusing the existing PDU builders), ARR (rule editor — the decoder already
  renders rule lines), DIR (application templates), PBR (nested tree).
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
