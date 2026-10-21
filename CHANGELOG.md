# Changelog

All notable changes to SIMple (the PWA and `pysim-simple-server`).
The format follows [Keep a Changelog](https://keepachangelog.com/);
versions use `MAJOR.MINOR.PATCH` (see the README).

History before the first packaged release (v3.31.1) lives in the git log —
every change carries a descriptive commit message.

## [Unreleased]

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
