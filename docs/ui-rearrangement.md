# UI rearrangement — the deferred 4.4.0 work

Status: **planned, not started** (recorded 2026-11-23, after 4.3.0).  The
pin/PUK work shipped first; this is a separate change that must not be mixed
into it.

## 1. `File manager` becomes `Files` (RU «Файлы»)

A pure label change: the tab button and its first pill (`data-l10n="File
manager"`, one key used twice), the `LANG_RU` entry, the help section title and
prose (EN/RU), the READMEs, and the `html.test.js` markup pin
(`data-tab="files" data-l10n="File manager"`).  No logic depends on the string.

## 2. Profiles and Card snapshots move into `Files`; the Profiler tab goes

The Profiler's data layer does not move: `simple_profiles` / `simple_snapshots`
(localStorage) and every `profiler*` id/function stay as they are.  Only the
shell changes:

- **Markup**: drop `#tab-profiler`; move `#profiler-list/-editor/-results/-snapshot`
  into `#tab-files`; replace the `profiler-list-tab` pair with two Files pills
  (`data-files-sub="profiles"` / `"snapshots"`).
- **Pills — flat five, decided**: `Files · Services · Custom files · Profiles ·
  Card snapshots` (one row).
- **Cardless default — decided**: the tab keeps opening on `Files`; with no card
  it shows the empty tree, which is expected (the Profiles/Snapshots pills work
  offline).
- **Editor state — decided**: a tab or pill switch must *not* reset
  `profilerView`; the unsaved editor survives until an explicit **Back to list**
  / **Save**.  Clicking `Profiles` again re-shows the editor that was left.
- **Router**: `tab:'files'` carries `sub` (files/services/custom/profiles/
  snapshots) plus the profiler `view`/`item` when a profiler pill is active;
  `navApply` maps a legacy `tab:'profiler'` entry onto `files` (an open tab
  across a SW update can still hold one).
- **Help**: fold the Profiler section into the Files section (both languages),
  renumber the following sections, and **keep the existing ids** (`profiler`,
  `card-snapshots`) so old links and the "every UI anchor exists" test keep
  working.
- **Tests**: `html.test.js` (exact tab list, `id="tab-profiler"`, the pill
  wiring), `nav.test.js` (state shapes), `profiler.test.js`'s one
  `profilerListSwitch` test; the `fs_*` tests are unaffected.

## 3. Top-level tab order (owner, 2026-11-23)

Current:

1. Remote APDU
2. SCP80
3. SCP81
4. Cards
5. Profiler  (removed by §2)
6. File manager  (renamed by §1)
7. Card reader
8. Simulator

Target:

1. **Card reader**
2. **Files**
3. **Simulator**
4. **Cards**
5. **Remote APDU**
6. **SCP80**
7. **SCP81**

Notes:

- The `data-tab` ids stay (`pysim`, `files`, `phone`, `cards`, `c-apdu`,
  `scp80`, `scp81`), so only the button order (and the labels of §1) change —
  the nav/history state keys, the help-anchor maps and `switchTab` are
  unaffected by the reorder itself.
- `html.test.js`'s exact tab-list assertion must be updated to the new order.
- **Landing view — decided**: the app opens on **Card reader** (owner,
  2026-11-23), not Remote APDU.  Touch points: `_activeTab`'s initial value,
  the markup's `active` button and the un-hidden `#tab-*` container, the
  `navApply` catch fallback, and the tests that assume the initial tab
  (`nav.test.js`'s default/fallback cases).  The `switchTab('c-apdu')` call
  after a successful OTA send is unrelated — it jumps to the response on
  purpose and stays.
- **Card reader's default pill — decided (shipped in 4.3.0)**: the tab's pills
  are `PIN / ADM` first and selected by default, `Raw APDU` second; the landing
  view therefore shows the PIN / ADM pill, whose help anchor is `pin-adm`.

## 4. Release bookkeeping

Version **4.4.0**; `frontend/sw.js` cache bump; `CHANGELOG.md` section;
AGENTS/README/help updates; no new utility classes unless `npm run build` is run
and the regenerated `style.css` is included (the stale-style trap).
