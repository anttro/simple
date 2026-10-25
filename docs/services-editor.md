# Services editor — data tables (draft)

> **Status: DRAFT for human review — data only.** No code or UI has been written
> against these tables yet. They are a semantic merge of the four service lists
> from `frontend/index.html` (`EF_SST_SERVICES`, `EF_UST_SERVICES`,
> `EF_IST_SERVICES`, `EF_EST_SERVICES`) cross-referenced with the filesystem
> registry `docs/uicc/UICC_FILES.md` and the standard file tree
> `frontend/uicc_files.json`.

**Counts:** SST **59** / UST **146** / IST **22** / EST **3** services →
**160 feature rows** (10 SIM-only, 81 USIM-only, 5 ISIM-only, 61 shared,
3 RFU/reserved). Reconciliation is exact; see Notes §4.

---

## A. Feature cross-reference

Columns: **SST #** (SIM, TS 51.011 §10.3.7, 2 bits/service: allocated + activated),
**UST #** (USIM, TS 31.102 §4.2.8, 1 bit), **IST #** (ISIM, TS 31.103 §4.2.7, 1 bit),
**EST #** (USIM Enabled Services Table, TS 31.102 §4.2.47). Class ∈
`SIM-only · USIM-only · ISIM-only · shared · RFU`. The **Filesystem** column is
deliverable B merged in (see §B for the legend and the full `capability`/`?` list).

Names are canonicalised; wording aliases are listed in Notes §1.

### A.1 Features present in the SIM list (SST), and their USIM/ISIM counterparts

| # | Feature | SST # | UST # | IST # | EST # | Class | Filesystem |
|---|---|---|---|---|---|---|---|
| 1 | CHV1 disable function | 1 | | | | SIM-only | capability |
| 2 | Local Phone Book (ADN) | 2 | 1 | | | shared | EF.ADN 6F3A (USIM: DF.PHONEBOOK 5F3A → EF.PBR 4F30) |
| 3 | Fixed Dialling Numbers (FDN) | 3 | 2 | | 1 | shared | EF.FDN 6F3B (+ EF.EXT2 6F4B) |
| 4 | Short Message Storage (SMS) | 4 | 10 | 6 | | shared | EF.SMS 6F3C |
| 5 | Advice of Charge (AoC) | 5 | 13 | | | shared | EF.ACM 6F39 · EF.ACMmax 6F37 · EF.PUCT 6F41 |
| 6 | Capability Configuration Parameters (CCP) | 6 | | | | SIM-only | EF.CCP 6F3D |
| 7 | Capability Configuration Parameters 2 (CCP2) | | 14 | | | USIM-only | EF.CCP2 6F4F |
| 8 | PLMN selector | 7 | | | | SIM-only | EF.PLMNsel 6F30 |
| 9 | MSISDN | 9 | 21 | | | shared | EF.MSISDN 6F40 |
| 10 | Extension 1 | 10 | | | | SIM-only | EF.EXT1 6F4A |
| 11 | Extension 2 | 11 | 3 | | | shared | EF.EXT2 6F4B |
| 12 | SMS Parameters (SMSP) | 12 | 12 | | | shared | EF.SMSP 6F42 |
| 13 | Last Number Dialled (LND) | 13 | | | | SIM-only | EF.LND 6F44 |
| 14 | Cell Broadcast Message Identifier (CBMI) | 14 | 15 | | | shared | EF.CBMI 6F45 |
| 15 | Group Identifier Level 1 (GID1) | 15 | 17 | | | shared | EF.GID1 6F3E |
| 16 | Group Identifier Level 2 (GID2) | 16 | 18 | | | shared | EF.GID2 6F3F |
| 17 | Service Provider Name (SPN) | 17 | 19 | | | shared | EF.SPN 6F46 |
| 18 | Service Dialling Numbers (SDN) | 18 | 4 | | | shared | EF.SDN 6F49 (+ EF.EXT3 6F4C) |
| 19 | Extension 3 | 19 | 5 | | | shared | EF.EXT3 6F4C |
| 20 | VGCS Group Identifier List | 21 | 57 | | | shared | EF.VGCS 6FB1 · EF.VGCSS 6FB2 |
| 21 | VBS Group Identifier List | 22 | 58 | | | shared | EF.VBS 6FB3 · EF.VBSS 6FB4 |
| 22 | enhanced Multi-Level Precedence and Pre-emption (eMLPP) | 23 | 24 | | | shared | EF.eMLPP 6FB5 |
| 23 | Automatic Answer for eMLPP (AAeM) | 24 | 25 | | | shared | EF.AAeM 6FB6 |
| 24 | Data download via SMS-CB | 25 | 29 | | | shared | capability (gate: EF.CBMID 6F48) |
| 25 | Data download via SMS-PP | 26 | 28 | | | shared | capability |
| 26 | Menu selection | 27 | | | | SIM-only | capability (menu title: EF.SUME 6F54) |
| 27 | Call control (by SIM/USIM) | 28 | 30 | | | shared | capability |
| 28 | Proactive SIM | 29 | | | | SIM-only | capability |
| 29 | Cell Broadcast Message Identifier Ranges (CBMIR) | 30 | 16 | | | shared | EF.CBMIR 6F50 |
| 30 | Barred Dialling Numbers (BDN) | 31 | 6 | | 2 | shared | EF.BDN 6F4D (+ EF.EXT4 6F4E/6F55) |
| 31 | Extension 4 | 32 | 7 | | | shared | EF.EXT4 6F4E (SIM) / 6F55 (USIM) |
| 32 | De-personalisation Control Keys (DCK) | 33 | 36 | | | shared | EF.DCK 6F2C |
| 33 | Co-operative Network List (CNL) | 34 | 37 | | | shared | EF.CNL 6F32 |
| 34 | Short Message Status Reports (SMSR) | 35 | 11 | 7 | | shared | EF.SMSR 6F47 |
| 35 | Network's indication of alerting in the MS (NIA) | 36 | 56 | | | shared | EF.NIA 6F51 (SIM) / 6FD3 (USIM) |
| 36 | MO-SMS control (by SIM/USIM) | 37 | 31 | | | shared | capability |
| 37 | GPRS | 38 | | | | SIM-only | EF.KcGPRS 6F52 (+ DF.GSM 7F20) |
| 38 | Image (IMG) | 39 | 22 | | | shared | DF.GRAPHICS 5F50 → EF.IMG 4F20 |
| 39 | SoLSA | 40 | 23 | | | shared | DF.SoLSA 5F70 → EF.SAI 4F30 · EF.SLL 4F31 |
| 40 | USSD string data object supported in Call Control | 41 | | | | SIM-only | capability |
| 41 | RUN AT COMMAND command | 42 | 32 | | | shared | capability |
| 42 | User controlled PLMN selector with Access Technology | 43 | 20 | | | shared | EF.PLMNwAcT 6F60 |
| 43 | Operator controlled PLMN selector with Access Technology | 44 | 42 | | | shared | EF.OPLMNwAcT 6F61 |
| 44 | HPLMN selector with Access Technology | 45 | 43 | | | shared | EF.HPLMNwAcT 6F62 |
| 45 | CPBCCH Information | 46 | 39 | | | shared | EF.CPBCCH 6F63 (SIM) / 4F63 (USIM) |
| 46 | Investigation Scan | 47 | 40 | | | shared | EF.InvScan 6F64 (SIM) / 4F64 (USIM) |
| 47 | Extended Capability Configuration Parameters (ECCP) | 48 | | | | SIM-only | EF.ECCP 6F4F |
| 48 | MExE | 49 | 41 | | | shared | DF.MExE 5F3C → EF.MExE-ST 4F40 |
| 49 | PLMN Network Name (PNN) | 51 | 45 | | | shared | EF.PNN 6FC5 |
| 50 | Operator PLMN List (OPL) | 52 | 46 | | | shared | EF.OPL 6FC6 |
| 51 | Mailbox Dialling Numbers (MBDN) | 53 | 47 | | | shared | EF.MBDN 6FC7 (+ EF.EXT6 6FC8) |
| 52 | Message Waiting Indication Status (MWIS) | 54 | 48 | | | shared | EF.MWIS 6FCA (+ EF.MBI 6FC9) |
| 53 | Call Forwarding Indication Status (CFIS) | 55 | 49 | | | shared | EF.CFIS 6FCB (+ EF.EXT7 6FCC) |
| 54 | Service Provider Display Information (SPDI) | 56 | 51 | | | shared | EF.SPDI 6FCD |
| 55 | Multimedia Messaging Service (MMS) | 57 | 52 | | | shared | EF.MMSN 6FCE (+ MMS set: EXT8/MMSICP/MMSUP/MMSUCP) |
| 56 | Extension 8 | 58 | 53 | | | shared | EF.EXT8 6FCF |
| 57 | MMS User Connectivity Parameters | 59 | 55 | | | shared | EF.MMSICP 6FD0 · EF.MMSUCP 6FD2 · EF.MMSUP 6FD1 |

### A.2 USIM-only, and USIM+ISIM shared features (no SIM entry)

| # | Feature | SST # | UST # | IST # | EST # | Class | Filesystem |
|---|---|---|---|---|---|---|---|
| 58 | Outgoing Call Information (OCI and OCT) | | 8 | | | USIM-only | EF.OCI 6F81 · EF.OCT 6F83 |
| 59 | Incoming Call Information (ICI and ICT) | | 9 | | | USIM-only | EF.ICI 6F80 · EF.ICT 6F82 |
| 60 | GSM Access | | 27 | | | USIM-only | DF.GSM-ACCESS 5F3B |
| 61 | Enabled Services Table | | 34 | | | USIM-only | EF.EST 6F56 |
| 62 | APN Control List (ACL) | | 35 | | 3 | USIM-only | EF.ACL 6F57 |
| 63 | GSM security context | | 38 | | | USIM-only | EF.Kc 4F20 · EF.KcGPRS 4F52 (DF.GSM-ACCESS 5F3B) |
| 64 | Extension 5 | | 44 | | | USIM-only | EF.EXT5 6F4E (USIM) |
| 65 | Call control on GPRS by USIM | | 54 | | | USIM-only | capability |
| 66 | Pseudonym | | 59 | | | USIM-only | EF.Pseudo 4F41 (DF.WLAN 5F40) |
| 67 | User Controlled PLMN selector for I-WLAN access | | 60 | | | USIM-only | EF.UPLMNWLAN 4F42 |
| 68 | Operator Controlled PLMN selector for I-WLAN access | | 61 | | | USIM-only | EF.OPLMNWLAN 4F43 |
| 69 | User controlled WSID list | | 62 | | | USIM-only | EF.UWSIDL 4F44 |
| 70 | Operator controlled WSID list | | 63 | | | USIM-only | EF.OWSIDL 4F45 |
| 71 | VGCS security | | 64 | | | USIM-only | EF.VGCSCA 6FD4 |
| 72 | VBS security | | 65 | | | USIM-only | EF.VBSCA 6FD5 |
| 73 | WLAN Reauthentication Identity | | 66 | | | USIM-only | EF.WRI 4F46 |
| 74 | Multimedia Messages Storage | | 67 | | | USIM-only | EF.MML 4F47 · EF.MMDF 4F48 (DF.MULTIMEDIA 5F3B) |
| 75 | Generic Bootstrapping Architecture (GBA) | | 68 | 2 | | shared | EF.GBABP 6FD6 (USIM) / 6FD5 (ISIM) · EF.GBANL 6FDA/6FD7 · EF.NAFKCA 6FDD |
| 76 | MBMS security | | 69 | | | USIM-only | EF.MSK 6FD7 · EF.MUK 6FD8 |
| 77 | Data download via USSD and USSD application mode | | 70 | | | USIM-only | capability |
| 78 | Equivalent HPLMN | | 71 | | | USIM-only | EF.EHPLMN 6FD9 |
| 79 | Additional TERMINAL PROFILE after UICC activation | | 72 | | | USIM-only | capability |
| 80 | Equivalent HPLMN Presentation Indication | | 73 | | | USIM-only | EF.EHPLMNPI 6FDB |
| 81 | Last RPLMN Selection Indication | | 74 | | | USIM-only | EF.LRPLMNSI 6FDC |
| 82 | OMA BCAST Smart Card Profile | | 75 | | | USIM-only | DF.BCAST 5F80 |
| 83 | GBA-based Local Key Establishment Mechanism | | 76 | 4 | | shared | capability |
| 84 | Terminal Applications | | 77 | | | USIM-only | capability |
| 85 | Service Provider Name Icon | | 78 | | | USIM-only | EF.SPNI 6FDE |
| 86 | PLMN Network Name Icon | | 79 | | | USIM-only | EF.PNNI 6FDF |
| 87 | Connectivity Parameters for USIM IP connections | | 80 | | | USIM-only | EF.NCP-IP 6FE2 |
| 88 | Home I-WLAN Specific Identifier List | | 81 | | | USIM-only | EF.HWSIDL 4F47 |
| 89 | I-WLAN Equivalent HPLMN Presentation Indication | | 82 | | | USIM-only | EF.WEHPLMNPI 4F48 |
| 90 | I-WLAN HPLMN Priority Indication | | 83 | | | USIM-only | EF.WHPI 4F49 |
| 91 | I-WLAN Last Registered PLMN | | 84 | | | USIM-only | EF.WLRPLMN 4F4A |
| 92 | EPS Mobility Management Information | | 85 | | | USIM-only | EF.EPSLOCI 6FE3 · EF.EPSNSC 6FE4 |
| 93 | Allowed CSG Lists and corresponding indications | | 86 | | | USIM-only | EF.ACSGL 4F81 · EF.CSGT 4F82 · EF.HNBN 4F83 |
| 94 | Call control on EPS PDN connection by USIM | | 87 | | | USIM-only | capability |
| 95 | HPLMN Direct Access | | 88 | | | USIM-only | EF.HPLMNDAI 4F4B |
| 96 | eCall Data | | 89 | | | USIM-only | ? |
| 97 | Operator CSG Lists and corresponding indications | | 90 | | | USIM-only | EF.OCSGL 4F84 · EF.OCSGT 4F85 · EF.OHNBN 4F86 |
| 98 | Support for SM-over-IP | | 91 | 8 | | shared | capability (ISIM-related: EF.PSISMSC 6FE5) |
| 99 | Support of CSG Display Control | | 92 | | | USIM-only | capability |
| 100 | Communication Control for IMS | | 93 | 9 | | shared | capability |
| 101 | Extended Terminal Applications | | 94 | | | USIM-only | capability |
| 102 | Support of UICC access to IMS | | 95 | 10 | | shared | capability |
| 103 | Non-Access Stratum configuration by USIM | | 96 | | | USIM-only | EF.NASCONFIG 6FE8 |
| 104 | PWS configuration by USIM | | 97 | | | USIM-only | EF.PWS 6FEC |
| 105 | URI support by UICC | | 99 | 11 | | shared | capability (URI files: EF.FDNURI 6FED · EF.BDNURI 6FEE · EF.SDNURI 6FEF) |
| 106 | Extended EARFCN support | | 100 | | | USIM-only | EF.EARFCNList 6FFD |
| 107 | ProSe | | 101 | | | USIM-only | DF.ProSe 5F90 → EF.PST 4F10 |
| 108 | USAT Application Pairing | | 102 | | | USIM-only | EF.IPS 6FF1 (+ EF.IAL 6FF0 · EF.IPD 6FF2) |
| 109 | Media Type support | | 103 | 12 | | shared | capability |
| 110 | IMS call disconnection cause | | 104 | 13 | | shared | capability |
| 111 | URI support for MO SHORT MESSAGE CONTROL | | 105 | 14 | | shared | capability |
| 112 | ePDG configuration Information support | | 106 | | | USIM-only | EF.ePDGSelection 6FF4 |
| 113 | ePDG configuration Information configured | | 107 | | | USIM-only | EF.ePDGId 6FF3 |
| 114 | ACDC support | | 108 | | | USIM-only | DF.ACDC 5FA0 → EF.ACDC_LIST 4F01 |
| 115 | MCPTT | | 109 | 15 | | shared | DF.MCS 5F3D → EF.MST 4F01 (USIM) |
| 116 | ePDG configuration Information for Emergency Service support | | 110 | | | USIM-only | EF.ePDGSelectionEm 6FF6 |
| 117 | ePDG configuration Information for Emergency Service configured | | 111 | | | USIM-only | EF.ePDGIdEm 6FF5 |
| 118 | eCall Data over IMS | | 112 | | | USIM-only | ? |
| 119 | URI support for SMS-PP DOWNLOAD | | 113 | 16 | | shared | capability |
| 120 | From Preferred | | 114 | 17 | | shared | EF.FromPreferred 6FF7 |
| 121 | IMS configuration data | | 115 | 18 | | shared | EF.IMSConfigData 6FF8 |
| 122 | TV configuration | | 116 | | | USIM-only | EF.TVCONFIG 6FFB (+ DF.TV 5FB0 → EF.TVUSD) |
| 123 | 3GPP PS Data Off | | 117 | | | USIM-only | EF.3GPPPSDATAOFF 6FF9 |
| 124 | 3GPP PS Data Off Service List | | 118 | | | USIM-only | EF.3GPPPSDATAOFFservicelist 6FFA |
| 125 | V2X | | 119 | | | USIM-only | DF.V2X 5F3E → EF.VST 4F01 · EF.V2X_CONFIG 4F02 |
| 126 | XCAP Configuration Data | | 120 | 19 | | shared | EF.XCAPConfigData 6FFC |
| 127 | EARFCN list for MTC/NB-IOT UEs | | 121 | | | USIM-only | ? |
| 128 | 5GS Mobility Management Information | | 122 | | | USIM-only | DF.5GS 5FC0 → EF.5GS3GPPLOCI 4F01 · EF.5GSN3GPPLOCI 4F02 |
| 129 | 5G Security Parameters | | 123 | | | USIM-only | EF.5GAUTHKEYS 4F05 (+ EF.5GS3GPPNSC 4F03 · EF.5GSN3GPPNSC 4F04) |
| 130 | Subscription identifier privacy support | | 124 | | | USIM-only | capability (SUCI data: EF.SUCI_Calc_Info 4F07, gated by 125) |
| 131 | SUCI calculation by the USIM | | 125 | | | USIM-only | EF.SUCI_Calc_Info 4F07 |
| 132 | UAC Access Identities support | | 126 | | | USIM-only | EF.UAC_AIC 4F06 |
| 133 | Expect control plane-based Steering of Roaming information during initial registration in VPLMN | | 127 | | | USIM-only | capability (SOR data: EF.SOR-CMCI 4F0E, gated by 138) |
| 134 | Call control on PDU Session by USIM | | 128 | | | USIM-only | capability |
| 135 | 5GS Operator PLMN List | | 129 | | | USIM-only | EF.OPL5G 4F08 |
| 136 | Support for SUPI of type NSI or GLI or GCI | | 130 | | | USIM-only | EF.SUPI_NAI 4F09 |
| 137 | 3GPP PS Data Off separate Home and Roaming lists | | 131 | | | USIM-only | capability (extends EF.3GPPPSDATAOFFservicelist 6FFA) |
| 138 | Support for URSP by USIM | | 132 | | | USIM-only | EF.URSP 4F0B |
| 139 | 5G Security Parameters extended | | 133 | | | USIM-only | capability (extends EF.5GAUTHKEYS 4F05) |
| 140 | MuD and MiD configuration data | | 134 | 21 | | shared | EF.MuDMiDConfigData 6FFE |
| 141 | Support for Trusted non-3GPP access networks by USIM | | 135 | | | USIM-only | capability |
| 142 | Support for multiple records of NAS security context storage for multiple registration | | 136 | | | USIM-only | EF.5GS3GPPNSC 4F03 · EF.5GSN3GPPNSC 4F04 (2nd record) |
| 143 | Pre-configured CAG information list | | 137 | | | USIM-only | EF.CAG 4F0D |
| 144 | SOR-CMCI storage in USIM | | 138 | | | USIM-only | EF.SOR-CMCI 4F0E |
| 145 | 5G ProSe | | 139 | | | USIM-only | DF.5G_ProSe 5FF0 → EF.5G_PROSE_ST 4F01 |
| 146 | Storage of disaster roaming information in USIM | | 140 | | | USIM-only | EF.DRI 4F0F |
| 147 | Pre-configured eDRX parameters | | 141 | | | USIM-only | EF.5GSEDRX 4F10 |
| 148 | 5G NSWO support | | 142 | | | USIM-only | EF.5GNSWO_CONF 4F11 |
| 149 | PWS configuration for SNPN in USIM | | 143 | | | USIM-only | EF.PWS_SNPN 4F01 (DF.SNPN 5FE0) |
| 150 | Multiplier Coefficient for Higher Priority PLMN search via NG-RAN satellite access | | 144 | | | USIM-only | EF.MCHPPLMN 4F15 |
| 151 | K_AUSF derivation configuration | | 145 | | | USIM-only | EF.KAUSF_DERIVATION 4F16 |
| 152 | Network Identifier for SNPN (NID) | | 146 | | | USIM-only | EF.NID 4F02 (DF.SNPN 5FE0) |

### A.3 ISIM-only features

| # | Feature | SST # | UST # | IST # | EST # | Class | Filesystem |
|---|---|---|---|---|---|---|---|
| 153 | P-CSCF address | | | 1 | | ISIM-only | EF.P-CSCF 6F09 |
| 154 | HTTP Digest | | | 3 | | ISIM-only | capability |
| 155 | Support of P-CSCF discovery for IMS Local Break Out | | | 5 | | ISIM-only | capability (address store: EF.P-CSCF 6F09) |
| 156 | WebRTC URI | | | 20 | | ISIM-only | EF.WebRTCURI 6FFA |
| 157 | IMS Data Channel indication | | | 22 | | ISIM-only | EF.IMSDCI 6F0B |

### A.4 RFU / reserved (non-editable)

| # | Feature | SST # | UST # | IST # | EST # | Class | Filesystem |
|---|---|---|---|---|---|---|---|
| 158 | RFU | 8, 20 | 26, 98 | | | RFU | — |
| 159 | Reserved and shall be ignored | 50 | 50 | | | RFU | — |
| 160 | shall be set to 1 | | 33 | | | RFU | — |

---

## B. Feature → filesystem (FS check)

The FS column in §A is deliverable B, merged for readability. Legend:

- `EF.<name> <FID>` — the file the service declares (FID from `uicc_files.json`
  or `UICC_FILES.md`; both agree unless noted).
- `DF.<name> <FID> → EF.<name> <FID>` — the service gates a whole DF; the
  principal EF is shown. Companion files after `(+ …)` or `·`.
- `capability` — a procedural / terminal-side feature with no single file.
- `?` — no clear file found in either source; needs a human decision.
- `—` — RFU/reserved.

### B.1 `capability` entries (no single file)

CHV1 disable · Data download via SMS-PP · Data download via SMS-CB (gate
EF.CBMID 6F48) · Menu selection (title EF.SUME 6F54) · Call control · Proactive
SIM · MO-SMS control · USSD string in Call Control · RUN AT COMMAND · Call
control on GPRS · Data download via USSD · Additional TERMINAL PROFILE ·
GBA-based Local Key Establishment · Terminal Applications · SM-over-IP (related
EF.PSISMSC 6FE5, ISIM) · CSG Display Control · Communication Control for IMS ·
Extended Terminal Applications · UICC access to IMS · URI support by UICC (URI
files EF.FDNURI/BDNURI/SDNURI) · Media Type support · IMS call disconnection
cause · URI for MO SHORT MESSAGE CONTROL · URI for SMS-PP DOWNLOAD · Call
control on EPS PDN · Call control on PDU Session · Subscription identifier
privacy (SUCI data in EF.SUCI_Calc_Info 4F07) · SOR expectation (data in
EF.SOR-CMCI 4F0E) · PS Data Off separate Home/Roaming (EF.3GPPPSDATAOFFservicelist
6FFA) · 5G Security Parameters extended (EF.5GAUTHKEYS 4F05) · Trusted non-3GPP
access · HTTP Digest · P-CSCF discovery Local Break Out (EF.P-CSCF 6F09).

### B.2 `?` entries (unresolved file mapping)

| Feature | UST # | Note |
|---|---|---|
| eCall Data | 89 | No EF_eCall in UICC_FILES.md; the file, if any, is not pinned. |
| eCall Data over IMS | 112 | Same as above (IMS variant). |
| EARFCN list for MTC/NB-IOT UEs | 121 | Distinct from "Extended EARFCN support" (100 → EF.EARFCNList 6FFD); UICC_FILES.md pins no MTC/NB-IoT-specific EARFCN list file. |

---

## Notes / open questions

### 1. Aliases used (case-insensitive exact name did not join these; merged manually)

| Canonical | SST wording | UST wording | IST wording |
|---|---|---|---|
| Local Phone Book (ADN) | "Abbreviated Dialling Numbers (ADN)" (2) | "Local Phone Book" (1) | |
| SMS Parameters (SMSP) | "SMS Parameters" (12) | "Short Message Service Parameters (SMSP)" (12) | |
| Short Message Status Reports (SMSR) | "Short Message Status Reports" (35) | "…Status Reports (SMSR)" (11) | "…Status Reports (SMSR)" (7) |
| Network's indication of alerting in the MS (NIA) | "Network's indication of alerting in the MS" (36) | "…in the MS (NIA)" (56) | |
| Call control | "Call control" (28) | "Call Control by USIM" (30) | |
| MO-SMS control | "Mobile Originated Short Message control by SIM" (37) | "MO-SMS Control by USIM" (31) | |
| De-personalisation Control Keys (DCK) | "De-personalization Control Keys" (33) | "Depersonalisation Control Keys" (36) | |
| SoLSA | "SoLSA (Support of Local Service Area)" (40) | "Support of Localised Service Areas (SoLSA)" (23) | |
| eMLPP / MExE | "enhanced Multi-Level…" (23), "MExE" (49) | "Enhanced Multi-Level…" (24), "MexE" (41) | |
| PLMN selector w/ Access Technology (×3) | "…PLMN Selector with Access Technology" (43–45) | "…PLMN selector with Access Technology" (20/42/43) | |
| Extension 2 | "Extension2" (11) | "Extension 2" (3) | |
| SM-over-IP | | "Support for SM-over-IP" (91) | "Support for SM-over-IP including data download via SMS-PP as defined in TS 31.111 [31]" (8) |
| Communication Control for IMS | | "…by USIM" (93) | "…by ISIM" (9) |
| URI support for SMS-PP DOWNLOAD | | "…TS 31.111 [12]" (113) | "…TS 31.111 [31]" (16) |

The other shared rows joined on a case-insensitive exact name (e.g. "Service
Provider Name" SST 17 / UST 19, "MSISDN" SST 9 / UST 21, "Short Message Storage
(SMS)" SST 4 / UST 10 / IST 6).

### 2. Kept separate, flagged for review

- **CCP / CCP2 / ECCP.** SST carries two bearer-capability services — "Capability
  Configuration Parameters (CCP)" (6, file EF.CCP 6F3D) and "Extended Capability
  Configuration Parameters (ECCP)" (48, EF.ECCP 6F4F); UST carries one —
  "Capability Configuration Parameters 2 (CCP2)" (14, EF.CCP2 6F4F). These were
  kept as three rows. Note the structural mismatch: the USIM's CCP2 shares FID
  **6F4F** with the SIM's **ECCP**, not with the SIM's CCP (6F3D), so a
  CCP↔CCP2 merge is *not* obviously correct — decide whether CCP2 ≙ CCP or
  CCP2 ≙ ECCP (or neither) before writing code.

### 3. FID / tree collisions to respect in the editor

- `6F4E` = EF.EXT4 in the GSM tree, EF.EXT5 in the USIM tree (UICC_FILES.md §2.3).
- `6F4F` = EF.ECCP (GSM) vs EF.CCP2 (USIM) — see §2.
- `6F07` = EF.IMSI (DF.GSM / ADF.USIM) vs EF.IST (ADF.ISIM).
- `6F38` = EF.SST (DF.GSM) vs EF.UST (ADF.USIM).
- `6FD7` = EF.MSK (USIM) vs EF.GBANL (ISIM).
- `EF.NIA` has different FIDs per tree: 6F51 (SIM) vs 6FD3 (USIM).
- `DF.GSM-ACCESS 5F3B` (ADF.USIM) vs `DF.MULTIMEDIA 5F3B` (DF.TELECOM) — same
  FID, different parents; the child-DF summary (UICC_FILES.md §4.13) is the
  authority for which DF a service gates.

The filesystem column already resolves these per-tree (e.g. EF.EXT4 shows both
FIDs); the editor's DF-context selection must do the same.

### 4. Reconciliation (completeness invariant)

- **SST 59:** 56 non-RFU service numbers (SST 1–59 minus {8,20,50}) spread over
  the 57 rows of §A.1 (one of those rows, CCP2, carries no SST number) + 3
  RFU/reserved slots (8, 20, 50) = 59. ✓
- **UST 146:** 46 shared-with-SIM + 15 shared-with-ISIM + 81 USIM-only (1 in
  §A.1 = CCP2, 80 in §A.2) + 4 RFU/reserved (26, 33, 50, 98) = 146. ✓
- **IST 22:** 2 shared with SIM (SMS 6, SMSR 7) + 15 shared with USIM
  (2,4,8,9,10,11,12,13,14,15,16,17,18,19,21) + 5 ISIM-only (1,3,5,20,22) = 22. ✓
- **EST 3:** FDN (1), BDN (2), ACL (3) — all present. ✓

**Discrepancy to resolve:** the PWA's `EF_UST_SERVICES` constant has **146**
entries, but `UICC_FILES.md` §6.1 pins the UST table through **n°150**. The four
services present in the spec summary but missing from the PWA constant are:
**147** 5MBS UE pre-config, **148** Operator-controlled signal threshold,
**149** A2X, **150** IMS Data Channel Indication. (148 would map to EF.OCST 6F02,
147 to DF.5MBSUECONFIG 5FF1 → EF.5MBSUECONFIG 4F01, 149 to DF.A2X 5F3F →
EF.AST 4F01, and 150 to EF.IMSDCI 6F0B — the USIM copy of the ISIM file already
listed for IST 22.) These four are **not** in the tables above; add them to
`EF_UST_SERVICES` (and re-verify the count line) or document the omission.

### 5. Source-coverage gaps

A few files/DFs are cited from `UICC_FILES.md` but are absent from
`frontend/uicc_files.json` (they are real spec files, just not emitted by the
cardless tree builder): EF.IMG 4F20 / DF.GRAPHICS 5F50, DF.MExE 5F3C +
EF.MExE-ST/ORPK/ARPK/TPRPK 4F40–4F43, DF.SoLSA 5F70 + EF.SAI 4F30/EF.SLL 4F31,
DF.BCAST 5F80, DF.ACDC 5FA0 + EF.ACDC_LIST 4F01, DF.TV 5FB0 + EF.TVUSD,
EF.TVCONFIG 6FFB, EF.3GPPPSDATAOFF 6FF9 / EF.3GPPPSDATAOFFservicelist 6FFA,
EF.IAL 6FF0 / EF.IPD 6FF2 (pairing), and EF.PSISMSC 6FE5. If the FS check must
resolve *every* FID against `uicc_files.json` alone, these need adding there
first (or the check must also consult UICC_FILES.md).

### 6. Deliberately unmapped

- "GSM Access" (UST 27) maps to **DF.GSM-ACCESS 5F3B** (UICC_FILES.md §4.13c
  "DF_GSM-ACCESS — UST n°27"), not to DF.GSM 7F20 — DF.GSM is the classic SIM
  tree and is not UST-gated. This corrects the DF.GSM hint in the drafting brief.
- EF.RPLMNAcT 6F65 (legacy, "deleted in Rel-6") has no current UST/SST service
  and is intentionally absent.
- EF.ACC 6F78 (access-control class) and EF.Phase 6FAE are not service-gated in
  either list, so they do not appear.
