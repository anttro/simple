# Changelog

All notable changes to SIMple (the PWA and `pysim-simple-server`).
The format follows [Keep a Changelog](https://keepachangelog.com/);
versions use `MAJOR.MINOR.PATCH` (see the README).

History before the first packaged release (v3.31.1) lives in the git log —
every change carries a descriptive commit message.

## [Unreleased]

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
  run repeats, the status line shows the **elapsed time**, the **average per
  completed iteration** and the **estimated time left**, recomputed on every
  status poll (`testLoadTiming`; the final report keeps the server's
  `elapsed_ms`/`avg_ms`).  No completed iteration yet means no bogus
  average/estimate - only the elapsed time.

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
