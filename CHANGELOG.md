# Changelog

All notable changes to SIMple (the PWA and `simple-server`).
The format follows [Keep a Changelog](https://keepachangelog.com/);
versions use `MAJOR.MINOR.PATCH` (see the README).

History before the first packaged release (v3.31.1) lives in the git log —
every change carries a descriptive commit message.

## [Unreleased]

## [4.4.0] — 2026-11-23

### Added

- **The Card reader is the landing view.** The app opens on the Card reader
  tab and its **PIN / ADM** pill (`_activeTab = 'pysim'`, the button and the
  panel marked accordingly); a failed history replay falls back there. The
  initial help link follows the same pill (§6.3).
- **Profiles and Card snapshots live in the Files tab** as its fourth and
  fifth pills (the Profiler tab is gone, the `#profiler-list/-editor/-results/
  -snapshot` blocks moved into `#tab-files`). `_filesSub` is the single source
  of the active pill; `filesSwitchSubtab` handles all five (an unknown legacy
  value folds onto `files`) and the profiler overlay is shown per
  `profilerView` only while a profiler pill is active. A tab or pill switch
  never resets `profilerView` - an unsaved profile editor survives until
  **Back to list** / **Save**, and returning to **Profiles** re-shows it;
  `profilerEdit`/`snapshotOpen` (and a scan's saved editor) select their pill,
  so Back returns to the right list. The router carries
  `{tab:'files', sub, view, item}`, and a state recorded before this release
  (`tab:'profiler'` plus its `listtab`) is replayed as a Files state.

### Fixed

- **The landing view's PIN / ADM form is built on the first paint.** The
  fields are rendered by `pysimChvRender`, which only ran on a pill click or a
  Code/Action change, so the seeded landing state showed an empty form until
  the operator touched it.  The init now runs the pill switcher for
  `_pysimSub` (guarded for a DOM-less fragment; the seeded state is unchanged,
  so no history entry is added) and entering the Card reader tab re-applies
  the current pill.
- **The PIN / ADM form survives its own rebuilds.** `pysimChvRender` keeps
  each field's value by name across the `innerHTML` rebuild (a Code/Action
  switch that keeps a field, or a language refresh, no longer clears a typed
  PIN), and the language switch re-renders the form so its labels follow the
  new language.  The snapshot summary's `Files:` label has its own key
  (`'Files:'` -> «Файлов:») now that `'Files'` is the tab/pill label.

### Changed

- **New top-level tab order:** Card reader, Files, Simulator, Cards, Remote
  APDU, SCP80, SCP81 (`data-tab` ids and containers unchanged).
- **`File manager` is renamed `Files`** (RU «Файлы») - the tab button and the
  tab's first pill, which now shares the name with the tab.
- **Help and READMEs:** section 6 is "Card reader and Files tabs", its 6.1 is
  the Files tab with the five pills; the old Profiler section became **6.7
  Profiles and card snapshots** (ids `profiler` and `card-snapshots` kept),
  the usage scenarios moved to 6.8, and the Simulator, Server installation and
  Version compatibility sections were renumbered to 7, 8 and 9 (their
  subsections follow; the `&sect;8.9`/`&sect;8.10` cross-references became
  `&sect;7.9`/`&sect;7.10`). READMEs carry the new order and fold the Profiler
  text under the Files section.  Known limitation (inherited, pre-existing):
  a recorded `results` view replays after a page reload with an empty panel -
  the result data lives in memory only and is not part of the history state.
- **A clean setup output:** setuptools emits an `InformationOnly`
  "Editable installation." warning for *every* editable build
  (`editable_wheel.py`), whose "folders in your working directory with the
  same name as your package" advice cannot apply here (the import package is
  `simple_server`, and the distribution name `simple` has no directory of its
  own).  `setup.sh`, `setup.bat` and `tools/update.py`'s in-place re-install
  now run the editable install with that one message appended to
  `PYTHONWARNINGS` - a message-prefix filter, so no other build warning is
  hidden.  An inherited value is kept (the variable is a comma-separated
  list): `setup.sh` and `tools/update.py` append to it, `setup.bat` saves and
  restores it (clearing it when nothing was inherited).  pip's `-q` did not
  hide the warning on pip 26.2.1 (newer pip shows backend warnings only with
  `-v`).

## [4.3.0] — 2026-11-23

### Added

- **ENABLE / DISABLE PIN** on `POST /api/chv`: `action: "enable"` (INS `28`)
  and `"disable"` (INS `26`), the PIN in the 8-byte CHV field, P1 `00` and the
  same P2 reference as VERIFY (TS 102 221 V18.4.0 §11.1.11.1/.2 and
  §11.1.12.1/.2: "shall not be executed ... when PIN is already disabled or
  blocked" / "... enabled or blocked"; a correct PIN resets the counter to 3
  and flips the state, a false one decrements).  A **2G SIM** enables/disables
  CHV1 only (TS 51 011 §9.2.11/.12: class `A0`, P1 `'00'`, P2 fixed at
  `'01'`), so `code: pin2` answers `400` naming those clauses.  ADM is
  **Verify only** (`/api/verify-adm`): §9.5.1 Table 9.3 names `0A` as ADM1 but
  no pinned clause covers changing or unblocking it.  Live-confirmed on the
  bench card (2026-11-23): `DISABLE PIN1` -> `9000` flips the MF `C6` PS_DO
  from `0xC0` to `0x40` (PIN1's bit cleared, ADM's kept) and `ENABLE PIN1`
  restores it, while the empty-data counter query answers `63CX` in both
  states - the `C6` flag, not the probe, decides enabled/disabled.
- **The Card reader tab** (the old Status tab, renamed): three rows
  **PIN1 / PIN2 / ADM** with their state and a **Verify** button that uses the
  matched preset's code (without one it is disabled and points at the pill),
  and a new **PIN / ADM** pill for the manual card operations - Code
  (PIN1/PIN2/ADM) + Action (Verify/Change/Unblock/Enable/Disable; ADM folds
  onto Verify), the per-action fields, "Use the matched preset's codes", the
  result line and the selected code's live state/attempts.  Every attempt is
  confirmed first (the existing confirmation/retry texts).  Enable/Disable are
  gated by the attempt-free enabled bit (already disabled/enabled -> refused,
  blocked -> unblock first, an unreported flag -> allowed with an amber
  warning; a 2G SIM's PIN2 enable/disable is unavailable).  After a
  successful change/unblock the PWA offers once to store the new PIN in the
  preset matching the equipped card's ICCID (the PUK never changes).

### Fixed

- **The PIN state readout on a UICC without a selectable ADF.USIM** falls back
  to the **MF** instead of reporting both PINs `unknown`: its FCP carries the
  mandatory `C6` too (TS 102 221 §11.1.1.3.1 Table 11.3), and only the
  **global** reference (PIN1) is probed there - PIN2 stays `unknown` as a
  local reference (TS 31.102 §6.4, TS 102 221 §9.5.1/§9.3.0); a failing MF
  select leaves every code `unknown`.  Live 2026-11-23: such a card's MF `C6`
  (PS_DO `0xC0`) reported PIN1 and ADM enabled, and the empty-data probe
  answered `63C3`.
- **A CHV command is only sent with a well-defined application context**: a
  UICC without a selectable ADF.USIM may still use a **global** reference
  (PIN1) - the endpoint selects the MF (TS 102 221 §9.3.0, TS 31.102 §6.4) -
  while a **local** reference (PIN2), a 2G SIM whose DF.GSM is not selectable
  (TS 51 011 §9.1) and a failed MF fallback answer `400` with **no** APDU
  sent.
- **The enable/disable success line** no longer reports "unblocked": each
  action names its own result (`changed` / `enabled` / `disabled` /
  `unblocked`).
- **The `6984` line no longer claims the PIN is not enabled**: the bench card
  answered `6984` to a **wrong** PIN1 value (the counter stayed `63C3`) and
  `9000` to the correct one, so the message now states that the card reported
  no attempt count and the value may be wrong (or the reference disabled / not
  usable on the card).

### Changed

- **The `63CX` mapping is action-aware**: TS 102 221 §10.2.1.3 Table 10.9
  (printed p. 82) makes `63CX` the remaining attempts for VERIFY only (with
  UNBLOCK as the spec's explicit exception, §11.1.13.1.1); for
  CHANGE/ENABLE/DISABLE it is labeled *"Command successful but after using an
  internal update retry routine 'X' times"*, so `enable`/`disable` report
  **success** with `internal_retries` (the outcome check is the refreshed
  `chv.enabled`) and `change` stays a failure with both readings in its
  warning - none sets `attempts_left` (the attempt-free PIN state is the
  authoritative counter), and the PWA composes the line itself (the server's
  English `warning` is only a fallback).  `63C0` stays "blocked" for every
  action.
- **The header PIN badge is monochrome glyphs** (`PIN1 ✓` green / `PIN1 !`
  amber / `PIN1 ✗` red / `PIN1 ⊘`, `PIN1 –`, `PIN1 ?` gray) with the words in
  the tooltip; the `PIN1 locked/unlocked/...` translations are gone.
- **The Cards editor only edits presets**: its Verify/Change/Unblock and the
  ADM Verify buttons, the per-PIN state line and the result line moved to the
  Card reader's PIN / ADM pill (the file manager's inline **Verify ADM** hint
  stays).  A changed/unblocked PIN no longer fills the preset form silently -
  the pill asks once.

## [4.2.0] — 2026-11-23

*The v4.1.0 release was withdrawn shortly after publication (the PIN/PUK
operations were not ripe enough); this section supersedes it and folds in the
4.1.1 changes made afterwards.*

### Added

- **PIN/PUK (CHV) operations.**  `POST /api/chv` runs VERIFY CHV (INS `20`),
  CHANGE CHV (`24`) and UNBLOCK CHV (`2C`) on the equipped card: PIN1/PIN2
  (4-8 decimal digits) and the PUK (exactly 8), coded as the 8-byte T.50 CHV
  field (`FF`-padded).  The CHV reference follows the card class - a UICC's
  application PIN `01`/`81`, a 2G SIM's CHV `01`/`02` with the exceptional
  UNBLOCK CHV1 `00` - and the PIN's application is selected first (ADF.USIM /
  DF.GSM).  The result carries the attempts left (`63Cx`; `63C0` = blocked),
  the 2G SIM's `9804` (a failure with attempts left) / `9840` (blocked) and
  the 6Axx/6982/6985 errors.  The Cards editor's PIN/ADM block gains
  **Verify / Change / Unblock** per PIN (and **Verify** for the ADM): every
  attempt is confirmed first, the attempts left and the blocked state are
  tracked per card session, a successful change/unblock fills the form field
  for saving, and the Status tab's PIN row verifies the matched preset's
  PIN1/PIN2.
- **The PIN lock state** - `PIN1 locked` / `PIN1 unlocked` / `PIN1 blocked` /
  `PIN1 not usable` / `PIN1 n/a` / `PIN1 ?` - read **without presenting a
  PIN**.  The server reads each PIN at every equip and serves it as
  `status.chv`; `POST /api/chv-status` re-reads one or both PINs (and
  `POST /api/chv` returns the refreshed block).  The probe alone cannot tell a
  **disabled** PIN from a **locked** one (a disabled PIN may answer the same
  `63CX` with a meaningless counter), so the application DF's **PS template DO
  `C6`** decides the `enabled` flag first: it is **mandatory** in an MF/DF/ADF
  FCP (ETSI TS 102 221 V18.4.0 §11.1.1.3.1 Table 11.3, printed p. 89) and its
  PS_DO bitmap says which key references are enabled (§9.5.2, printed p. 77:
  "For each bit set to '1' the corresponding key reference (PIN) is enabled.
  … Bit b8 in the most significant byte corresponds to the first key reference
  indicated by tag '83' following the PS_DO", an optional usage qualifier
  `95` preceding its reference).  A UICC answers a VERIFY PIN
  with an **empty data field** (a case-1 APDU `00 20 00 01|81`, no Lc/Le)
  with its retry counter: ETSI TS 102 221 V18.4.0 §11.1.9.1.2 "The VERIFY PIN
  with empty data field is used to retrieve the PIN retry counter from the
  UICC.  This function is performed whether or not the relevant PIN is
  disabled or blocked." ... "The number of retries, if any, is indicated in
  the response by SW1 SW2 = '63CX'" ... "applied to a blocked PIN shall
  return SW1 SW2 = '63C0' or ... '6983'".  ISO/IEC 7816-4:2005 §7.5.6 reads
  the absent data field as verification required (`63CX`) or not (`9000`),
  §7.5.1 makes `6A88` "reference data not found", Table 6 prints `6984` as
  "Reference data not usable".  A 2G SIM cannot probe (TS 51 011 V4.15.0
  §9.2.9: VERIFY CHV "can not be used to retrieve the retry counter value"),
  so its states come from the DF.GSM SELECT bytes (§9.2.1: byte 14 Detail 1
  b8 CHV1 enabled/disabled; bytes 19/21 the CHV1/CHV2 status, `b8` initialised
  and `b4-b1` the remaining presentations), `enabled` from that b8 for CHV1
  and `null` for CHV2.  A failed ADF selection sends **no** probe (the
  previous selection stays and `81` is a local reference, §9.5.1); a
  reference absent from the `C6` list stays probe-only - `unknown` unless the
  card answers, never "disabled".  A successful VERIFY **latches** the PIN as
  `not_required` for the card session (the ADM pattern): every later
  attempt-free re-read keeps it even when the card keeps answering `63CX`,
  and an equip/disconnect forgets it.  The PWA does not claim "verified" when
  a verify's refreshed entry says the PIN is disabled (`enabled: false`) -
  it says the PIN is not required and nothing was verified.  The
  `C6`-over-probe precedence is
  **hardware-confirmed**: on the bench card (2026-11-23) PIN1's `C6` bit is
  clear while the empty-data VERIFY PIN1 answers `63C3` without consuming an
  attempt - a disabled PIN therefore reads `not_required`, not "locked".
  The PWA shows the state as a
  header badge next to the ADM one (clickable: re-read) and as a per-PIN line
  under the Cards editor's PIN fields and in the Status tab, distinguishing
  "disabled on this card" from "already verified"; a PIN nothing
  needs to verify (`not_required`/`not_usable`/`not_initialized`/`blocked`)
  disables the Verify actions with the reason in the tooltip (Change/Unblock
  stay available).
- **The size-agnostic read fallback** for a card that returns no file
  information at all: `/api/read` accepts `length` (transparent) and
  `record_len`, and otherwise reads READ BINARY/RECORD with `Le = 256` until
  the card signals the end (a short read, the `6282` warning, the 2G SIM's
  `9402` out-of-range address; an immediate `6B00` is an error, not an empty
  file), marking the response `size_inferred: true`.  The PWA uses the
  inferred size for the profile/snapshot metadata and notes it in the
  file-manager status line.
- The standard file list (`uicc_files.json`) now carries each EF's
  `structure` (regenerated from the `simple-pysim` fork's pySim generator);
  it picks the transparent/record path when neither the card nor the FCP
  gives one.

### Fixed

- **A CHV operation answering `6984`** (VERIFY / CHANGE / UNBLOCK PIN) is
  reported as "Referenced data invalidated" instead of the generic `Error`:
  the result is `{"ok": false, "sw": "6984", "invalidated": true, "error":
  "Referenced data invalidated"}`, with no `blocked`/`attempts_left` field.
  `6984` is ETSI TS 102 221 V18.4.0 §10.2.1.5.2 Table 10.13's "Referenced
  data invalidated" (the `69` "command not allowed" group; ISO/IEC
  7816-4:2005 §5.1.3 Table 6 prints the same pair as "Reference data not
  usable").  Table 10.16 lists it for the three PIN commands, but no clause
  assigns it a CHV-specific meaning - it reports neither attempts left nor a
  blocked state (those are `63C0`/`6983`, and `9840` on a 2G SIM).  A card
  whose PIN is **disabled / not initialized for verification** answers this
  way.  The CHV command bytes and the PIN reference coding are unchanged.
- **A 2G SIM's `9804` is no longer reported as a blocked ADM** on
  `/api/verify-adm`: TS 51 011 §9.4.5 defines it as "unsuccessful
  verification, at least one attempt left" (`failed: true`); `9840` (no
  attempt left) and `6983` are the blocked answers, and `63C0` reports 0
  attempts left + blocked.
- **2G SIM file reads.**  A 2G SIM's SELECT returns no TS 102 221 FCP; its
  TS 51 011 §9.2.1 file information (file size at bytes 3-4, the structure
  at byte 14, the record length at byte 15) is now parsed, so
  `/api/select` reports the card's real size and record layout and
  `/api/read` works through the normal size-driven path (EF.ICCID, EF.ARR
  31 × 76 B, EF.ADN 250 × 39 B, ...).  The file manager, profiles, snapshots
  and the network-state monitor now work on a 2G SIM.  `fci_form: "gsm"`
  says what the raw SELECT response is; the PWA
  labels it instead of decoding it as a BER FCP.

### Changed

- **The shared T=0 transport keeps a case-1 `62xx`/`63xx` answer final** (no
  dummy GET RESPONSE): a case-1 APDU has no `Le` and can carry no response data
  (TS 102 221 §7.3.1.1.0 prescribes the GET RESPONSE "for case 4 commands
  only"; §7.3.1.1.2/.3 discontinue on the status word for cases 2/3 - the
  case-2/3 fallback here stays by design, a compatibility choice the live
  cards need).  `/api/apdu` and raw test-script APDUs therefore no longer emit
  a spurious GET RESPONSE after a `63CX`, which is what makes the empty-data
  VERIFY PIN probe usable; cases 2-4 behave exactly as before.
- The PIN actions' result line names the refusal ("refused by the card
  (referenced data invalidated)") and says PIN verification is not enabled for
  that PIN on this card, or the PIN is otherwise not usable, instead of
  `SW: 6984 — Error`; it carries the SW and does not promise UNBLOCK.  A retry
  after the refusal is confirmed with a warning that a new attempt may consume
  one, and a later successful verify clears the state.  `docs/api.md` and the
  help (EN/RU) document the answer, `DISABLE PIN` / `ENABLE PIN` (INS
  `26`/`28`, Remote APDU builder) as the PIN-state commands, and UNBLOCK as
  the separate case of a PIN the card reports as blocked.

## [4.0.0] — 2026-11-23

### Changed

- **The server is pysim-free.**  The PWA now talks to `simple-server`
  (`simple_server`, the new entry point) instead of the pySim-wrapping
  `pysim-simple-server`: own PC/SC transport, file model and FCP parser,
  SCP80/SCP81, RAM/CAP, CAT/STK, test scripts/suites, eSIM ES10 and the
  network simulator, all written from the pinned specs.  `GET /api/command`,
  `/api/commands`, `/api/cardinfo` and `/api/sp-verify` are gone; the PWA's
  Status tab shows the server's own session model and equip/reset/status-poll
  have dedicated endpoints.  The GSMTAP tracer now hooks the transport's
  `send_tpdu`, so the init phase and the T=0 GET RESPONSE TPDUs are in the
  capture (as far as PC/SC exposes them).
- **The runtime dependency floor is `pyscard`, `pycryptodomex` and
  `gsm0338`** (`requirements.txt`); pySim and `requirements-pysim.txt` are no
  longer installed.  `setup`/`start` create the venv, install the floor and
  launch `simple-server`; the serial `-d/--device` transport and the `-p`
  reader-slot form are gone (PC/SC only; `-r NAME` names a reader).
- **Updating in place refuses a major-version jump.**  `tools/update.py`
  will not move a 3.x install to 4.0.0 (`--check` included): the backend, the
  scripts and the dependencies changed, so a 3.x install stays on the last 3.x
  release.
- The distribution is named `simple` (the console script stays
  `simple-server`).

### Fixed

- **2G SIM detection and selection.**  The first 4.0.0 probe sent the USIM
  AID before the SIM probe; the bench 2G SIM answers an unknown AID with
  `6999` and then refuses **every** command until a reset, and it also
  refuses an A0 `SELECT MF` until a DF has been selected.  `detect_card` now
  probes `SELECT MF` (CLA 00) → `SELECT DF.GSM` (CLA A0, `7F20`, pySim's
  `SimCardBase.probe()`) → USIM ADF (last), and the TS 51 011 select form
  carries the response-data octet (`A0A4 0000 02 <fid> 00`).

### Verified

- Live smoke on the bench cards before the release: the UICC (status, tree,
  select/read, the STK menu, send-ota with the PoR and the counter, tar-probe,
  the PWA), the eUICC (EID, EUICCInfo1/2, addresses, GetRat, profiles,
  notifications and an enable/disable roundtrip with the re-init and the
  verified state), the SCP81 TLS-PSK listener (the HTTP OTA trigger, the PSK
  handshake, the 204 dialog and a command script with its R-APDU) and the 2G
  SIM (detection as `sim`/CLA A0, EF.ICCID `897010272147119764`, the
  `MegaFonPRO` STK menu with the CAT CLA A0).  Reading a 2G SIM's files still
  refuses without an FCP (the recorded post-4.0.0 fallback).

### Legacy

- The pySim-based 3.x line is frozen at its final 3.x release in the
  **`simple-pysim`** fork (GPL-2.0-or-later, maintenance mode).  The legacy
  `pysim_simple_server/` package and `requirements-pysim.txt` were removed
  from this repository - they live in the fork - so the 4.x tree, the tests
  and the release workflow carry no pySim dependency at all.
- **The license is now MIT.**  simtrace-analyser (the source of the
  client-side EF decoders) is MIT, and the 4.x server no longer imports
  pySim, so the repository moves from GPL-2.0-or-later to MIT.  The
  `simple-pysim` fork keeps its GPL license.

## [3.40.11] — 2026-11-11

### Fixed

- **`--gsmtap` now captures every APDU, not just the session body.**  The
  v3.40.10 fix re-attached the tracer after `PysimApp` was built, but pySim
  nulls/replaces the transport tracer during its own `equip()` (the `apdu_trace`
  settable defaults to False), so the startup **physical-reset re-probe** and any
  APDU pySim sends during app construction still fell through the gap.  The
  transport's `send_apdu` is now wrapped so the configured tracer (GSMTAP /
  APDU / fan-out) is re-attached before every exchange, whatever pySim does to
  the slot in between - the capture is sticky across equip, reset and the
  PC/SC rebuild path.

## [3.40.10] — 2026-11-11

### Fixed

- **`--gsmtap` no longer misses the startup init reads.**  The init block
  re-attached a plain stderr APDU tracer unconditionally, replacing the
  `--gsmtap` (or fan-out) tracer that `init_reader` had installed and that
  `PysimApp.__init__` had just nulled - so the EF.ICCID, network-state,
  EF.ARR and EF.PBR reads and the TERMINAL PROFILE drain were traced to
  stderr but never reached a GSMTAP capture (the pcap showed a gap between
  the startup and the first STATUS poll).  The block now re-attaches the
  configured tracer, falling back to the plain APDU tracer only when no
  tracer was requested.

## [3.40.9] — 2026-11-11

### Fixed

- **PLI IMEI / IMEISV are coded as the TS 24.008 Mobile Identity** (10.5.1.4),
  not plain nibble-swapped BCD.  The first byte now carries identity digit 1
  in the high nibble and the odd/even + type (010 IMEI / 011 IMEISV) in the
  low nibble, so an applet that drops the leading object nibble no longer
  prints the IMEI one digit short (tracker `90154203237518` vs
  `490154203237518`).  Fixed in the server defaults (`PLI_DEFAULTS`), the
  server-side decode (`_dec_imei`, `_decode_tr`) and the PWA's PLI codecs
  (`decImei` / `encImeiPli` / `encImeisvPli`); the IMEISV value is 9 bytes
  (`62 09 …`, carrying the SVN).

- **Test-script parameters are no longer silently dropped.**  An scp80 step
  now refuses any parameter it does not consume (`kic`, `kid`, `cntr`, a
  typo like `tarr`), and a builder event (0x03/0x0B/0x12/0x1D) refuses a
  stray `data` value - both used to vanish with no error.  The keyless
  SCP80 form is expressed through the preset's keyset/TAR, not step-level
  `kic`/`kid` (the documented knob is `kvn`); the raw `envelope` action
  carries raw bytes.

- **An equip / `start_reset` no longer re-enables a poller the operator
  disabled.**  `_apply_equipped_card` reset the operator's `/api/poll-toggle`
  preference (`_POLL_ENABLED`) via `_poll_enable()`; it now clears only the
  card's POLLING OFF (`_POLL_DISABLED_BY_CARD`) and re-arms the timer only
  when polling was left enabled.

- **Commands drained by a test run land in `/api/proactive-log`.**  The
  `proactive-drain` step and the run cleanup fetched/answered commands without
  appending them to the proactive history, so drained SMS texts were
  invisible outside the run report; the drain now logs each FETCH like the
  expect step.

- **`setup.bat` no longer breaks at the removal notice.**  The "Removing an
  incomplete virtual environment (…)" echo sat inside a parenthesised block
  and its `)` ended the block early (Windows `Непредвиденное появление: ....`);
  both parentheses are now `^`-escaped.

- **`pyproject.toml` license metadata modernised (PEP 639).**  The deprecated
  `license = { text = "..." }` table and `tool.setuptools.license-files` were
  replaced with the SPDX string `license = "GPL-2.0-or-later"` and
  `project.license-files` — setuptools ≥ 77 now emits a deprecation warning for
  the old forms and will reject them after 2027-02-18, so a Windows `setup.bat`
  run logged `BuildBackendWarning` for both during `pip install -e .`.  The
  wheel metadata is now `License-Expression: GPL-2.0-or-later` with
  `License-File: LICENSE` / `THIRD-PARTY-NOTICES` under `dist-info/licenses/`.

## [3.40.7] — 2026-11-11

### Fixed

- **FCP file descriptor byte decoded per TS 102 221 Table 11.5** (not ISO
  7816-4 Table 14, the file-descriptor audit): the shareable flag is **b7**
  (`0x40`), not b8; the EF-structure bits are `000` no info / `001` transparent
  / `010` linear fixed / `110` cyclic with `011`/`100`/`101`/`111` RFU (the old
  decode fabricated "linear fixed TLV" / "linear variable" / "linear variable
  TLV" / "cyclic TLV"); and `b6–b4 = 111` with `b3–b1 = 010` is RFU, not
  "SIMPLE-TLV" (TS 102 221 has no SIMPLE-TLV row).
- **FCP life cycle status byte**: any value with `b8–b5` non-zero is now
  proprietary (was: b8 only), so `b6`/`b5`-set values are no longer misread
  as RFU.
- **Application power consumption voltage class E** decoded at `b6` (0x20),
  per Table 6.1 ATR coding (was `b5`).
- **UICC characteristics** now decodes the clock-stop preference/condition
  bits `b4–b3` (no preferred level / high / low when allowed; never / unless
  high / unless low when not allowed).

## [3.40.6] — 2026-11-10

### Changed

- **Equip/init reads no longer re-select MF and the ADF per file.**  The
  ICCID, network-state, EF.ARR and EF.PBR readers called `_select_path`, which
  re-selects MF and walks the parent chain for every file, so a startup issued
  one `SELECT MF` + one `SELECT ADF` per file (~20 MF and ~12 ADF selects).
  A new `_select_path_minimal` resolves model-known files in memory and lets
  pySim's `select_file` navigate the shortest route (an unknown segment still
  falls back to the old probing path).  Fewer SELECTs per init; the data read
  is unchanged.

## [3.40.5] — 2026-11-08

### Fixed

- **Server request/access log leaked into captured file content.**  The HTTP
  access log (`log_message`) and the `--log-requests` `REQUEST`/`RESPONSE`
  lines wrote to the rebindable `sys.stderr`, so a concurrent card-free request
  (the 2 s `/api/status` poll) dropped its log line into the `StringIO` a
  card command was capturing — corrupting file content read during a snapshot
  scan (the log line appeared in the snapshot, e.g.
  `127.0.0.1 - - […] "GET /api/status HTTP/1.1" 200 -ffff…`).  The log now
  writes to fd 2 via `_log_stderr`, exactly as the APDU tracer already does.

### Changed

- **Deduplicated the pySim output capture.**  The six handlers that captured
  pySim command output by rebinding `sys.stdout`/`sys.stderr` to a `StringIO`
  now share one `_capture_app_output` context manager (the same save/set/
  restore semantics, in one place).

## [3.40.4] — 2026-11-04

### Added

- **Windows guard in the helper scripts.**  `setup.sh`, `start.sh` and
  `update.sh` now detect a Windows-hosted bash (WSL via `/proc/version`, and
  Git Bash / MSYS2 / MinGW / Cygwin via `uname -s`) and exit with a clear
  error pointing at the matching `.bat` script, instead of failing deep in the
  venv/PC-SC logic.  Under WSL the Linux guest cannot reach the Windows PC/SC
  readers, and a mingw/msys shell uses the wrong (`bin/`) venv layout; users
  who deliberately want the WSL/Cygwin flow can run the underlying commands
  directly, without the helper scripts.

## [3.40.3] — 2026-10-27

### Fixed

- **Equipped card not always auto-selected in SCP80/RAM.**  The ICCID-based
  preset auto-selection latched its "already handled" guard even when no
  matching preset was found, so a card equipped before `/api/presets` had
  loaded (the connect-time race) never got auto-selected — navigating to
  SCP80/RAM showed no preset until the card was re-inserted.  The guard is
  now latched only after a successful selection, and `cardsFetch` re-runs
  the auto-selection once the store arrives.

## [3.40.2] — 2026-10-26

### Added

- **File filter: reset (✕) button.**  A red ✕ to the right of the filter
  selector appears whenever the filter is not "All" and resets it to "All"
  in one click.

## [3.40.1] — 2026-10-26

### Changed

- **File filter: drop the "SMS-related" / "CB-related" categories.**  Those
  files are service-declared and already reachable through their per-service
  filter options, which carry the per-spec service names ("Short Message
  Storage (SMS)", "SMS Parameters (SMSP)", "Short Message Status Reports
  (SMSR)", "Cell Broadcast Message Identifier (CBMI)", "Cell Broadcast
  Message Identifier Ranges (CBMIR)").

## [3.40.0] — 2026-10-26

### Added

- **File-tree filter** (the file manager gains a **Filter** dropdown, next to
  Sort).  A collapsed checkbox multiselect with: **All** (default), **Custom
  files**, **Identifiers** (ICCID/IMSI/MSISDN/IMPI/IMPU/SUPI_NAI/GID1/GID2),
  **PLMN selection** / **Provider names** / **WLAN/I-WLAN** / **SMS-related** /
  **CB-related** (cell broadcast), and one option per file-backed service of
  the cross-reference.  When a filter is active, directories that are not on
  the path of the selected files are hidden; the button turns blue to stand
  out.
- **Expand all / Collapse all** buttons beside the sort pills; **Expand all**
  walks the tree loading children on demand and skips directories the probe
  found absent, and selecting any filter other than "All" auto-expands the
  tree so the matched files are visible.
- **"To EF filter"** cross-link: each file-backed service row in the
  SST/UST/IST/EST decoded editors carries a compact glyph that narrows the
  file-tree filter to exactly that service's files.

## [3.39.0] — 2026-10-26

### Added

- **EF.SST tri-state editor.**  The SIM service table (TS 51.011 10.3.7, two
  bits per service — allocated + activated) now edits per service as
  *deleted / allocated-off / on*, preserving unmodelled pairs and the file
  length; it stops being read-only.
- **Cross-file Services editor** (a new Card-reader pill): SST / UST / EST /
  IST shown side by side from a curated 161-row feature cross-reference, a
  column rendered only for a file present on the card, per-file Save buttons,
  and a **Check filesystem** action (with a throbber) that verifies the
  declared services against the card's actual files.
- **Path-exact filesystem check.**  Each feature's files are resolved by
  (symbolic name, root) → canonical path through `uicc_files.json`, probed at
  that exact path, and reported in both directions (declared-but-missing,
  present-but-undeclared) plus an "unchecked" bucket for files not yet in the
  registry.

### Changed

- **EF resolution is DF-context aware.**  `efDfFromPath` derives the parent DF
  and `efFindDecoder`/`efFindEncoder` resolve DF → name → FID, so same-name/
  same-FID files in different DFs are distinct; EF.ECC gained a USIM
  linear-fixed decoder beside the SIM transparent one, and EF.AD is registered
  per DF.
- **The file manager selects files by canonical path** (not node name), so two
  same-named files under different DFs (EF.ECC/EF.AD, the DF.PHONEBOOK copies)
  are distinguishable, and snapshot/profile paths root ADFs by AID are
  normalized (`A0000000871002`→USIM, `…1004`→ISIM, `…100B`→USIM-non-IMSI).
- **The USIM service table is complete**: UST 147–150 (5MBS UE pre-configuration,
  Operator Controlled Signal Threshold per Access Technology, A2X, IMS Data
  Channel Indication) were missing.
- **`uicc_files.json`** gained the 13 optional files pySim does not model
  (DF.MExE, DF.SoLSA, DF.BCAST, DF.ACDC, DF.TV, DF.A2X and their children),
  emitted with distinct (name, path) so same-FID files in other DFs stay
  separate.

### Fixed

- **The start scripts name the address they were asked to bind.**  The opening
  `Starting pysim-simple-server on http://127.0.0.1:8080` line was hardcoded
  while `--http-host`/`--http-port` were passed through to the server:
  `./start.sh --http-host 10.0.28.161` bound correctly (the server's own
  `listening on` banner named the address), but the first line still said
  127.0.0.1.  `start.sh` and `start.bat` now parse both flags (space and `=`
  forms, the last value wins like argparse) and echo the effective address;
  the defaults are unchanged.
- eCall Data's `EF.FDN` reference was a typo (`6F3A` is EF.ADN); corrected to
  EF.SDN 6F49 / EF.FDN 6F3B.

## [3.38.1] — 2026-10-25

### Performance

- **Equip/initialization reads no longer select the file twice.**  pySim's
  `scc.read_binary`/`scc.read_record` call `select_path(ef)` first, but the
  ICCID, network-state, EF.ARR and EF.PBR readers had already made a
  parent-scoped selection and held its FCP.  A new `_read_selected_ef()` reads
  the selected file directly — READ BINARY chunked at `max_cmd_len`, READ
  RECORD at the FCP record length — removing one SELECT per file read and one
  per record (~106 files + 94 records on the bench card: ~0.7 s of the ~3.4 s
  traced startup init).  The pySim helpers stay as the fallback when the FCP
  lacks the size/record length.
- The startup TERMINAL PROFILE drain uses the chain's final SW, so an idle
  chain no longer costs one extra STATUS poll (~128 ms).

### Fixed

- **A PC/SC access denial is reported instead of a silent serial fallback.**
  When no reader was named and `readers()` failed (e.g. `SCARD_E_NO_ACCESS` —
  the user may not use `pcscd`), the error was swallowed and pySim fell back to
  the serial transport, failing on `/dev/ttyUSB0` while the server still printed
  "listening".  The startup now probes PC/SC and reports **`PC/SC access
  denied`** (naming the pcsc-lite PolicyKit actions and the
  `99-pcscd-allow.rules` rule — a world-writable `pcscd.comm` does not bypass
  polkit's active-session policy — plus the `pcsc_scan` cross-check),
  **`pcscd is not running`** or **`no PC/SC reader found`**, then exits 2;
  `start.sh` runs the same access pre-flight before launching.  An
  explicit `-d <device>` keeps the serial path, and a reader present but no card
  is still a normal cardless start.
- **`setup.sh`/`setup.bat` recover from an incomplete `.venv`.**  A failed venv
  creation (e.g. the Debian/Ubuntu `python3-venv` package missing) left the
  directory behind; the next run saw `.venv` and skipped creation, then tried to
  activate a venv without `activate`/`pip`.  The scripts now validate
  `bin/activate`/`bin/python`/`bin/pip` (`Scripts\…` on Windows), remove an
  incomplete directory and create it again; a creation failure removes the
  partial directory and points at the venv package.  The READMEs note it.

## [3.38.0] — 2026-10-24

### Added

- **Known-file picker in the test-script file actions.**  The `UPDATE file` /
  `READ file` step editor's path field gains the selector the APDU builder's
  SELECT rows use: the file-manager tree, the custom files and the standard
  file list (`uicc_files.json`), grouped by root with a filter, filling the
  path.  Only EFs are offered (the action reads/writes a file; a DF/ADF is not
  a target); the text field stays authoritative, so a name path or a file the
  lists do not carry can still be typed, and the picker preselects the current
  path when it is known.
- The **live load-test stats** now include the timing: while a script or suite
  run repeats, the **run banner** (visible on every Test-script view), the
  script editor's result head and the suite summary show the **elapsed time**,
  the **average per completed iteration** and the **estimated time left**,
  recomputed on every status poll (`testLoadTiming`/`testRunBannerText`; the
  final report keeps the server's `elapsed_ms`/`avg_ms`).  No completed
  iteration yet means no bogus average/estimate - only the elapsed time.

### Changed

- The load-test **iteration cap is raised to 1,000,000,000** (from 1,000,000) in
  both layers: the PWA's `TEST_ITERATIONS_MAX` and the two field `max`
  attributes, and the server's `_TEST_LOAD_MAX`.  The value is well inside the
  JavaScript safe-integer range (2^53-1), so the counters and the progress
  display stay exact; the practical ceiling is the run duration (at 50 ms per
  iteration the cap is ~1.6 years).

## [3.37.0] — 2026-10-23

### Added

- **Load testing** for test scripts and suites.  `POST /api/test/run` takes an optional
  `iterations` count (the PWA's **Iterations** field next to *Start* / *Run suite*): the
  run repeats N times, successful iterations are discarded (only the iteration counter and
  the timing are kept; the per-step log is not rendered or retained), and the first
  iteration that ends in an **error** stops the loop and keeps that iteration's full
  report.  A manual stop ends it after the current iteration, and a card session change
  between iterations is an error (a swapped card is never run against another card's
  script).  The SCP80 counter state is shared across iterations, so packets continue from
  what the previous run consumed.  The run status gains a `load` block (`iteration`,
  `done`, `failed_iteration`, `elapsed_ms`, `avg_ms`); the count is a run parameter, not
  part of the stored script or suite.

- The **stuck-card watchdog** for test runs.  A card that stops answering (no status
  word, no driver error) used to leave the runner inside a blocking `SCardTransmit`
  holding the card lock — *Stop* could not take effect, the presence monitor waited for
  the lock and a load test hung in that iteration without explanation.  After 30 s
  without runner progress (a bounded STATUS/drain loop beats once per attempt) the
  watchdog marks the run `stuck`, records `the card stopped responding (step N, no
  answer for N s)`, sets the stop flag and **tries** to cancel the blocked transmit by
  disconnecting the PC/SC link on a background thread.  Where PC/SC can cancel it, the
  step fails through the ordinary `on_fail` path and the run finalises as an **error**
  (never `stopped`); where it cannot (pcsc-lite serializes the disconnect behind the
  pending transmit), the status already names the step until the driver's own card
  timeout returns.  The transport is marked stale once the disconnect returns, so the
  next *Equip* rebuilds it.  The status gains `stuck`; the help EN/RU,
  `docs/test-scripts.md` and `docs/api.md` describe the behaviour and the
  background-disconnect caveat.

### Fixed

- The **PLI location answer** now uses the 7-byte PLI form (TS 31.111 8.19.1):
  PLMN 001/01, LAC `0001`, Cell ID `0001`.  The former default was the 5-byte
  *Network Rejection event* form (no Cell ID) with LAC `0000`; a card applet
  that re-reads its location after a `*LOCI` status change (the Alfa MultiIMSI
  applet) treated it as empty and re-requested PLI on every STATUS tick — the
  PLI storm that slowed RAM Explore.  The TR data is now
  `13 07 00F110 0001 0001`.
- The **proactive chain is bounded on every call site**: `_handle_proactive_chain`'s
  `max_rounds` defaults to `_POLL_CHAIN_MAX` (the background poll always passed it;
  the manual STATUS poll and the ENVELOPE/menu/TP chains did not).  A card that
  re-queues a command (the PLI storm) could keep the manual poll looping forever
  while it held the card lock — `/api/status-poll` never returned and every card
  request waited.  The manual poll also passes the bound explicitly.
- The load-test loop closes properly on a raised runner error (it used to leave the run
  `running` forever - the PWA polled a dead run and *Clear report* stayed refused), on a
  card session change inside a suite iteration, and on a member that was stopped; a
  warning in any iteration is reflected in the final status, `avg_ms` covers the attempted
  iterations (the failing one included), and each suite iteration's `counters_before` is
  read fresh from the preset store.

## [3.36.0] — 2026-10-23

### Added

- The batch **"waiting for the next card"** state is now a deliberate cue: the status
  modal shows a card-into-reader animation next to the prompt, a short chime sounds
  **once per card** when the run enters the waiting phase (Web Audio — no audio asset;
  edge-triggered in the 2 s poll, so it does not repeat), and while the tab is hidden the
  title hints "Insert next card".  The speaker button next to the status mutes the chime
  and the choice is remembered (`simple_ram_batch_sound`); the chime is unlocked by the
  Start click (browser autoplay policy); reduced-motion users get the static card-above-
  the-slot picture.  The card follows the app's SIM silhouette (`sim.svg`): a cut corner
  at the top and the contact pad near the bottom edge, sliding behind the reader bar.

### Changed

- A **batch delete that finds the object already gone** is still a success — the goal is met
  and the run continues — but it is no longer reported as a plain "OK": the attempt's verdict
  reads **Already absent**, the delete run's counts separate real deletions from no-ops
  (`Deleted: N · Already absent: M`), and the notice says the card was already absent.  The
  counts gain an `already_absent` bucket (the attempt result is `already_absent`); a
  reinstall's tolerated pre-delete is *not* affected — its outcome is still the install.
- The READMEs link the project's news channel: [Telegram — @youeyeseesee](https://t.me/youeyeseesee).

### Fixed

- The prebuilt `style.css` had gone stale: a rebuild restored utility classes the UI
  already used but the output lacked (e.g. `bg-blue-50` on the batch notice, `inline-flex`,
  `mx-2`, `max-h-48`, `w-44`, `min-w-24`).

## [3.35.0] — 2026-10-23

### Added

- **EF.SMSP decoded editor** — the full SMS-parameter form (TS 31.102 §4.2.27): alpha,
  TP-Destination Address and TS-Service Centre Address (each with its own length
  convention and a TON select; an empty SCA means the default SMSC), TP-PID, TP-DCS and
  the relative TP-VP — every parameter with an *absent (the ME default applies)* toggle.
  Live hints decode the DCS (TS 23.038 §4: coding group, alphabet, class, compression,
  message-waiting) and the relative validity period (TS 23.040 §9.2.3.12.1).  Unchanged
  parameters keep their exact bytes (a card quirk such as a DCS marked absent with byte
  `00` survives), alphanumeric TON addresses are preserved read-only, and all checked
  records are validated before anything is written.
- **Edit decoded, wave 2** — new editors for the service tables **EF.UST / EF.IST /
  EF.EST** (a filterable bit grid preserving services the table does not model), the
  scalars **EF.ACC**, **EF.PHASE**, **EF.HPPLMN** and **EF.AD**, the PLMN lists
  **EF.PLMNsel / EF.FPLMN / EF.EHPLMN**, the identities **EF.ICCID / EF.IMSI** (behind a
  confirmation — writing them changes the card identity) and the cell-broadcast lists
  **EF.CBMI / EF.CBMID / EF.CBMIR**.  **EF.SST** is deliberately left read-only: its
  SIM layout is 2 bits per service (allocated + activated), which needs its own editor.
- The file-format survey (`docs/decoded-editing.md`) now records the shipped waves and
  marks **EF.DIR / EF.ARR / EF.SMS / EF.PBR** as postponed by decision (read-only).

### Fixed

- `efDecSmsp` mapped the parameter indicators shifted (SCA on bit 5, PID on bit 2, DCS
  on bit 3, VP on bit 4); the spec order is DA/SCA/PID/DCS/VP on bits 1–5 with bits 6–8
  reserved — a record with a mixed indicator (e.g. `E9`) showed DCS and hid VP.
- The read-only EF.SMSP view decoded the TP-DA length as octets, although it is the digit
  count (TS 23.040 §9.1.2.5); a slot padded with `00` gained phantom digits.
- The **Read decoded** pill no longer looks disabled when it is enabled: an inactive-but-
  enabled pill is now an outlined button (blue on white/slate), and the grey fill and
  dimming are left to the actually disabled state.
- **EF.ACC** class numbering (TS 31.102 4.2.15): byte 1 b8…b1 codes classes 15…8 and
  byte 2 classes 7…0 — the decoder counted class 0 at the MSB (an all-FF ACC read as
  class 0 instead of classes 0–15).
- **EF.AD** (TS 31.102 4.2.18): the operation mode is a whole-byte enum
  (00/80/01/81/02/04), not bits 0-1 plus a test bit; the MNC length may be 0 (service
  n°130 available) or absent (3-byte SIM form) — the editor previously refused both.
- **EF.SST** (TS 51.011 10.3.7): the SIM service table is decoded with its 2-bits-per-
  service layout (allocated + activated) instead of the UST 1-bit codec.
- **EF.IMSI**: the parity/type nibble is `9` for an odd digit count and `1` for an even
  one (TS 24.008 10.5.1.4); the encoder always wrote `9`.
- Saving a PLMN/CB list with no edits no longer rewrites it as all-FF (the collected rows
  and the list encoders disagreed on the value shape).

## [3.33.0] — 2026-10-23

### Added

- **Edit decoded** in the file manager: a file is read raw, decoded into its own editable
  form, re-encoded in the browser and written through the normal update path.  The first
  wave covers **EF.SPN**, **EF.PLMNwAcT / EF.OPLMNwAcT / EF.HPLMNwAcT**, the dialling-number
  files **EF.ADN / EF.FDN / EF.SDN / EF.MSISDN / EF.LND / EF.BDN** and **EF.PNN**.  Shapes:
  a flat transparent form (SPN), a transparent list editor with add/remove rows (PLMN+ACT),
  per-record forms with a selection checkbox that is auto-checked when a field changes
  (dialling numbers, PNN) and per-record TLV item rows with add/remove/edit (PNN's full and
  short names plus the PLMN additional information).  All checked records are encoded and
  validated before anything is written; unchanged fields, record lengths, extension
  pointers (EF.EXT1–EXT5), the BDN comparison-method byte and unknown PNN TLVs are
  preserved, and an edit that would break a reference (a number continued in an extension
  file, a dropped mandatory PNN full name) is refused.
- `docs/decoded-editing.md`: the file-format survey behind the design (every known EF
  classified by structure/coding into the editor's shapes S0–S10, with the release waves).

### Removed

- The file manager's decoded view no longer carries the collapsed **pySim JSON (server)**
  disclosure — the view is the client-side EF decoder's table only.  The disclosure was an
  unrelated *second* decode: opening it issued another `/api/read` with `mode: 'decoded'`
  to run pySim's `read_binary_decoded`/`read_records_decoded`, and it was reachable only
  when our decoder covered the file (`Read decoded` is decoder-gated), so the one case
  where a second opinion would matter — a file our decoders do not cover — never offered
  it.  `/api/read` keeps `mode: 'decoded'` for API callers; the help, READMEs and project
  notes say the decoded table is built in the browser from the same raw bytes.

### Fixed

- **EF.BDN** decodes and encodes with its own `X+15` layout (the trailing Comparison Method
  Pointer); the shared ADN codec had treated it as `X+14`.
- `efPlmnAct` decoded the EC-GSM-IoT-only ACT (`b8=1,b4=1,b3=0`, `0x0088`) as nothing while
  checking the impossible `0x0086` (TS 31.102 Table 4.2.5.2).
- PNN network names: the decoder now trusts a non-zero spare-bit count before the
  "all bytes < 0x80 → uncompressed" heuristic, so a valid packed name can no longer be
  misread (and later written back wrong).
- `/api/write` and `/api/read` report a cmd2 exception in the captured output as a failure
  instead of `success: true` with `SW: 9000`.
- The empty amber **preset-mismatch banner** no longer flashes on the SCP80/RAM view.
  `ramOpChanged()` (v3.28.0) un-hid `#ram-preset-warning` by the operation kind, but the
  banner's text *and* visibility belong to `spPresetWarningRender()` (which also handles
  the batch suppression): with no warning text the empty bordered box stayed visible until
  the next status poll re-hid it — on every RAM view and operation switch.  The visibility
  is content-driven again; `frontend/tests/ram.test.js` pins it (the pre-fix code fails
  the test).

## [3.32.5] — 2026-10-21

### Fixed

- A card removed during the **startup initialization** no longer leaves the server
  reporting a phantom equipped card.  The init caught the PC/SC error (and printed a
  traceback) but kept the card object it had already built, so `card_present` and
  `_CARD_CONNECTED` stayed True — and the auto-equip then skipped every later insertion
  (its `_CARD_CONNECTED` early return), leaving only a manual Equip or a restart.  A
  PC/SC error now prints one line (`INIT: card removed during initialization …`), drops
  the half-initialized session and lets the presence monitor equip the next card.

## [3.32.4] — 2026-10-21

### Fixed

- The auto-equip's captured traceback now always includes the **exception line**: cmd2
  renders a rich-box traceback longer than the logged head, so the actual error was cut
  off.  The tail is appended after a `...` marker; the "releasing the old link failed"
  message now says it continues with a fresh link (a removed card's dead PC/SC handle
  always fails to disconnect).
- A card insertion now marks the PC/SC link for a rebuild, so the **first** auto-equip
  attempt already uses a fresh link.  On Windows the old link cannot connect after a
  swap — v3.32.2 recovered it, but only on the second attempt, after a failed init, a
  2–4 s delay and a traceback in the log.  A link created at a cardless startup has
  never connected either, which the same mark covers.  The rebuild still runs in the
  equip worker (never in the pyscard monitor thread) and `_ensure_transport` releases
  the old link first.

## [3.32.2] — 2026-10-21

### Fixed

- **The auto-equip loop after a card swap on Windows.** A failed initialization with
  the card still present was reported as "card gone during initialization" and treated
  as a no-op success — so the three retries never ran and the watchdog re-armed every
  few seconds forever.  It is a failure now, and **every failed attempt marks the
  PC/SC transport for a rebuild**, so the retry starts from a fresh link (the release
  of the old link happens first, so it cannot inherit a stale card handle).  The error
  pySim swallowed is printed as `AUTO-EQUIP: init output:` with its traceback.

## [3.32.1] — 2026-10-21

### Added

- **"Start counter value"** in the outfile import: the counter every created keyset
  starts from.  It defaults to the store's value (`1`); raise it for cards whose
  counters are already past it.  The API takes `start_counter` (1–10 hex digits;
  a bad value is a 400), and the preview carries the value.

## [3.32.0] — 2026-10-21

### Added

- **Updating a packaged install** — `update.sh` / `update.bat` (and `tools/update.py`
  beneath them): downloads the latest release package, replaces the application files
  in place and re-installs the app into the **existing `.venv`**.  pysim and its
  prerequisites (the slow part: pyscard builds from source on Linux) are kept, so an
  update takes seconds.  `--check` reports the current and latest versions, `--version`
  picks a specific release, `--zip` updates from a local package (offline), `--no-pip`
  refreshes the files only, `--force` re-applies the same version.
- The release workflow now takes the release notes from this file's matching version
  section (falling back to generated notes when it is missing).

### Changed

- Installs made from the v3.31.x packages have no updater yet: copy `update.sh`,
  `update.bat` and `tools/update.py` from this package once (or re-run `setup` from it).
  Later updates use `update.sh` / `update.bat` — or `git pull` for a clone.

## [3.31.2] — 2026-10-20

### Changed

- The **SCP81 TLS-PSK listener states its Python requirement**: it needs Python 3.13+
  (`ssl`'s PSK callbacks exist only since 3.13).  Below that it raises a clear error
  instead of `AttributeError`, and the PSK tests are skipped (they run on 3.13+).
- The READMEs and the setup scripts: **Windows strongly recommends Python 3.13**
  (pyscard has wheels up to 3.13; on 3.14 pyscard and some pysim dependency wheels are
  unavailable and build from source with MSVC).

## [3.31.1] — 2026-10-20

The first packaged release (`simple-3.31.1.zip`).

### Added

- **EF_PBR (phonebook reference) support** — the PBR is decoded in the decoded file
  views (the `A8`/`A9`/`AA` containers, the `C0`–`CC` kinds, the master EF), and the
  phonebook files it references (they are not in pySim's model) are added to the file
  tree server-side, selectable and readable.
- **In-app dialogs** — the native alert/confirm/prompt dialogs are replaced by a shared
  modal (queued, keyboard-aware, focus-trapped, ARIA-labelled).
- **Windows console encoding guard** — the `start.bat` PowerShell pipe can no longer
  kill the server on a character the ANSI code page lacks.
- **GPL-2.0-or-later licensing** (`LICENSE`, `THIRD-PARTY-NOTICES`), matching pysim.
- The curated **release package** (`tools/build_release.py`) and the tag-triggered
  `.github/workflows/release.yml`.

### Changed

- `setup.sh` / `setup.bat` and the READMEs: on Linux `pyscard` builds from source and
  needs the PC/SC headers (`libpcsclite-dev`).
