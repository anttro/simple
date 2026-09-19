const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const opens = (html.match(/<div\b/g) || []).length;
const closes = (html.match(/<\/div>/g) || []).length;

test('HTML <div> tags are balanced', () => {
    assert.strictEqual(opens, closes, `Unbalanced divs: ${opens} opens vs ${closes} closes`);
});

test('top-level tabs match the rearranged views', () => {
    const tabs = [...html.matchAll(/class="tab-btn[^"]*" data-tab="([^"]+)"/g)].map(m => m[1]);
    assert.deepStrictEqual(tabs, ['c-apdu', 'scp80', 'scp81', 'cards', 'profiler', 'pysim', 'phone']);
    assert.match(html, /data-tab="c-apdu">Remote APDU</);
});

test('cards list shows the SCP81 PSK column with blue/red row buttons', () => {
    assert.match(html, /data-l10n="SCP81">SCP81</);
    const fn = /function cardsRender\(\)[\s\S]*?\n\}/.exec(html);
    assert.ok(fn, 'cardsRender not found');
    assert.match(fn[0], /cardsEdit\(' \+ i \+ '\)" class="[^"]*bg-blue-600 text-white/);
    assert.match(fn[0], /cardsRemove\(' \+ i \+ '\)" class="[^"]*bg-red-600 text-white/);
});

test('the card form groups the SCP80 and SCP81 fields into labelled fieldsets', () => {
    const fieldsets = [...html.matchAll(/<fieldset[\s\S]*?<\/fieldset>/g)].map(m => m[0]);
    const scp80 = fieldsets.find(f => /data-l10n="SCP80 \(GSM 03\.48, ETSI TS 102 225\)"/.test(f));
    const scp81 = fieldsets.find(f => /data-l10n="SCP81 \(HTTP OTA\)"/.test(f));
    assert.ok(scp80, 'SCP80 card fieldset not found');
    assert.ok(scp81, 'SCP81 card fieldset not found');
    assert.match(scp80, /id="cards-kic"/);
    assert.match(scp80, /id="cards-kid-key"/);
    assert.match(scp80, /id="cards-tar"[^>]*value="000000"/);
    assert.match(scp80, /id="cards-uicc-tar"[^>]*value="B00000"/);
    assert.match(scp80, /id="cards-usim-tar"[^>]*value="B00001"/);
    // the TAR fields are labelled and sit to the right of Counter
    assert.match(scp80, />ISD TAR<\/label>/);
    assert.match(scp80, />UICC RFM TAR<\/label>/);
    assert.match(scp80, />ADF RFM TAR<\/label>/);
    assert.ok(scp80.indexOf('id="cards-cntr"') < scp80.indexOf('id="cards-tar"'),
        'TAR fields must follow the Counter field');
    assert.match(scp81, /id="cards-psk-id"/);
    assert.match(scp81, /id="cards-psk-key"/);
    // the PSK explanation lives inside the SCP81 group, not outside it
    assert.match(scp81, /data-l10n="SCP81 HTTP OTA: the listener picks the key/);
    // the ICCID field is not in either group
    assert.ok(!fieldsets.some(f => f.includes('id="cards-iccid"')));
});

test('the ADM field sits between From card and Add, outside the card fieldsets', () => {
    const fieldsets = [...html.matchAll(/<fieldset[\s\S]*?<\/fieldset>/g)].map(m => m[0]);
    assert.ok(!fieldsets.some(f => f.includes('id="cards-adm"')));
    assert.match(html, /id="cards-adm"[^>]*placeholder="ADM \(optional\)"/);
    const fromIdx = html.indexOf('id="cards-iccid-from-card"');
    const admIdx = html.indexOf('id="cards-adm"');
    const addIdx = html.indexOf('id="cards-add-btn"');
    assert.ok(fromIdx >= 0 && fromIdx < admIdx && admIdx < addIdx,
        'ADM input must sit between the From card and Add buttons');
});

test('card-dependent profiler buttons show the ICCID and need a readable one', () => {
    assert.match(html, /id="profiler-from-card-btn" data-needs="card-iccid"/);
    assert.match(html, /id="snapshot-new-btn" data-needs="card-iccid"/);
    assert.match(html, /data-needs="card-iccid" onclick="profilerCheck/);
    // both static buttons carry the dynamic ICCID span
    assert.match(html, /id="profiler-from-card-btn"[\s\S]*?<span class="profiler-iccid-label"><\/span><\/button>/);
    assert.match(html, /id="snapshot-new-btn"[\s\S]*?<span class="profiler-iccid-label" data-iccid-sep=": "><\/span><\/button>/);
});

test('labelled containers use fieldsets with embedded legends', () => {
    const legendCls = '<legend class="px-1 text-xs font-medium text-gray-500 dark:text-slate-400"';
    for (const label of ['STK menu', 'STATUS and Polling', 'TERMINAL PROFILE',
        'Events that the card monitors (SET UP EVENT LIST):', 'Fetched proactive commands:',
        'HTTP OTA log']) {
        assert.ok(html.includes(legendCls + ' data-l10n="' + label + '"'), 'no legend for ' + label);
    }
    // legends with dynamic state spans put data-l10n on the inner span so
    // translatePage() does not wipe the sibling
    assert.ok(html.includes('<legend class="px-1 text-xs font-medium text-gray-500 dark:text-slate-400"><span data-l10n="HTTP OTA listener">HTTP OTA listener</span> <span id="scp81-state"'));
    assert.ok(html.includes('<legend class="px-1 text-xs font-medium text-gray-500 dark:text-slate-400"><span data-l10n="Script results (R-APDUs)">Script results (R-APDUs)</span> <span id="scp81-results-state"'));
    // the Options block stays a details/summary, with the summary styled as
    // the embedded label (page-background patch over the border)
    assert.match(html, /<details id="scp81-opts" class="border border-gray-200 dark:border-slate-700 rounded mb-3">\s*<summary class="w-fit list-inside[^"]*bg-neutral-50 dark:bg-slate-900"/);
    assert.match(html, /<summary class="w-fit list-inside[^"]*">\s*<span data-l10n="Options \(applied at Start\)">/);
});

test('PLI qualifier tables cover all standard qualifiers', () => {
    // ESN (07), MEID (0B) and Supported RATs (1A) must at least be named, in
    // both the TR Config dictionary and the proactive-log short labels.
    const pli = /const PLI_QUALIFIERS = \[([\s\S]*?)\];/.exec(html);
    assert.ok(pli, 'PLI_QUALIFIERS not found');
    for (const code of ['07', '0B', '1A']) {
        assert.ok(pli[1].includes("{code:'" + code + "'"), 'PLI_QUALIFIERS missing ' + code);
    }
    const block = /const CMD_QUALIFIER_SHORT = \{([\s\S]*?)\n\};/.exec(html);
    assert.ok(block, 'CMD_QUALIFIER_SHORT not found');
    const short = /'26': \{([^}]*)\}/.exec(block[1]);
    assert.ok(short, "CMD_QUALIFIER_SHORT['26'] not found");
    for (const key of ['0x07', '0x0B', '0x1A']) {
        assert.ok(short[1].includes(key + ':'), 'CMD_QUALIFIER_SHORT 26 missing ' + key);
    }
});

test('profile rows have a Clone action', () => {
    assert.match(html, /onclick="profilerClone\(' \+ i \+ '\)"/);
    assert.match(html, /t\('Clone'\)/);
});

test('response parser is a Remote APDU pill', () => {
    assert.match(html, /data-sub="response" onclick="cApduSwitchSubtab\('response'\)"/);
    assert.ok(html.includes('id="c-apdu-sub-response"'));
});

test('profiler and phone simulator are top-level tab contents', () => {
    assert.ok(html.includes('id="tab-profiler" class="tab-content hidden"'));
    assert.ok(html.includes('id="tab-phone" class="tab-content hidden"'));
});

test('phone simulator has Phone / TR Config pills', () => {
    assert.match(html, /data-phone-sub="phone" onclick="phoneSwitchSubtab\('phone'\)"/);
    assert.match(html, /data-phone-sub="tr" onclick="phoneSwitchSubtab\('tr'\)"/);
    assert.ok(html.includes('id="phone-sub-phone"'));
    assert.ok(html.includes('id="phone-sub-tr"'));
});

test('scan name input starts scanning on Enter', () => {
    assert.match(html, /id="profiler-scan-name"[^>]*onkeydown="profilerScanNameKeydown\(event\)"/);
});

test('snapshot view has a timing summary block', () => {
    assert.ok(html.includes('id="snapshot-summary"'));
});

test('header state indicator and profiler custom-files tab', () => {
    assert.ok(html.includes('id="state-indicator"'));
    assert.ok(html.includes('id="profiler-list-custom"'));
    assert.ok(html.includes('data-list-tab="custom"'));
    assert.ok(!html.includes('data-pysim-sub="custom"'));
});

test('header status indicator has a compact ADM badge', () => {
    assert.ok(html.includes('id="state-indicator-adm"'));
});

test('file manager has FID / Name sort pills', () => {
    assert.match(html, /data-fs-sort="fid" onclick="pysimFsSetSort\('fid'\)"/);
    assert.match(html, /data-fs-sort="name" onclick="pysimFsSetSort\('name'\)"/);
    assert.ok(html.includes('pysim-fs-sort-pill'));
});

test('file manager keeps sort/probe controls above the scrolling tree', () => {
    // The sort pills and the Probe all files button/status must sit outside
    // the scrolling tree container so they stay visible while it scrolls.
    assert.ok(html.indexOf('id="pysim-fs-probe-btn"') < html.indexOf('id="pysim-fs-tree"'));
    assert.ok(html.indexOf('pysim-fs-sort-pill') < html.indexOf('id="pysim-fs-tree"'));
    assert.match(html, /style="max-height:65vh"[^>]*>\s*<div id="pysim-fs-tree">/);
    // the runtime fit caps it to the free viewport space; 65vh stays only as
    // the no-JS fallback
    assert.match(html, /function pysimFsFitTree\(/);
    assert.match(html, /addEventListener\('resize', pysimFsFitTree\)/);
});

test('custom-files init runs after the language init', () => {
    // pysimCustomLoad/RenderParents call t(): running them before
    // currentLang is initialized throws a TDZ error and aborts the rest
    // of the script (all later handlers fail with 'before initialization').
    assert.ok(html.indexOf('// Init custom files') > html.indexOf("let currentLang = 'en';"));
});

test('custom files form has add/save and cancel controls', () => {
    assert.match(html, /id="pysim-cf-add-btn"[^>]*data-l10n="Add"/);
    assert.match(html, /id="pysim-cf-cancel-btn"[^>]*class="hidden[^"]*"[^>]*data-l10n="Cancel"/);
    // canonical path form: root + parent DF + 4-hex FID, no free-form path
    assert.ok(html.includes('id="pysim-cf-root"'));
    assert.ok(html.includes('id="pysim-cf-parent"'));
    assert.ok(html.includes('id="pysim-cf-fid"'));
    assert.ok(!html.includes('id="pysim-cf-path"'));
    assert.ok(html.includes("event.key==='Enter')pysimCustomSubmit()"));
    assert.ok(!html.includes('pysimCustomAdd'));
});

test('file manager has a probe-all-files button and status line', () => {
    assert.match(html, /id="pysim-fs-probe-btn"[^>]*data-needs="card"/);
    assert.match(html, /id="pysim-fs-probe-btn"[^>]*data-l10n="Probe all files"/);
    assert.ok(html.includes('onclick="pysimFsProbeAll()"'));
    assert.ok(html.includes('id="pysim-fs-probe-status"'));
});

test('file manager shows FCI info and keeps the selection in state, not the DOM', () => {
    const detail = html.indexOf('id="pysim-fs-detail"');
    const info = html.indexOf('id="pysim-fs-info"');
    const content = html.indexOf('id="pysim-fs-content"');
    assert.ok(detail !== -1 && info > detail && info < content, 'pysim-fs-info must sit above the content');
    assert.ok(html.includes('function pysimFsInfoHtml'));
    assert.ok(html.includes("pysimFsInfoHtml(sel)"));
    assert.ok(!html.includes('pysim-fs-filename'));
    assert.ok(html.includes('let pysimFsSelected = null;'));
    assert.ok(html.includes('pysimFsSelected = name;'));
    assert.ok(!html.includes('pysimFsSelect()'));
});

test('profile list has a Profile from snapshot button', () => {
    assert.match(html, /data-l10n="Profile from snapshot">Profile from snapshot</);
    assert.ok(html.includes('onclick="profilerFromSnapshot()"'));
    assert.ok(html.includes('function profilerScanFromSnapshot(si)'));
    assert.ok(html.includes('function profilerBuildFileRuleFromSnapshot('));
});

test('every help anchor used by the UI exists in help.html', () => {
    const help = fs.readFileSync(path.join(__dirname, '..', 'help.html'), 'utf8');
    const ids = new Set([...help.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
    const anchors = new Set();
    // static maps in `xHelpAnchor = {...}` and inline `setHelpAnchor({...})`
    for (const re of [/HelpAnchor\s*=\s*\{([^}]*)\}/g, /setHelpAnchor\s*\(\s*\{([^}]*)\}/g]) {
        for (const m of html.matchAll(re)) {
            for (const v of m[1].matchAll(/'([a-z0-9-]+)'/g)) anchors.add(v[1]);
        }
    }
    for (const m of html.matchAll(/setHelpAnchor\('([^']+)'\)/g)) anchors.add(m[1]);
    for (const m of html.matchAll(/HelpAnchor\s*=[^;]*\|\|\s*'([a-z0-9-]+)'/g)) anchors.add(m[1]);
    for (const m of html.matchAll(/setHelpAnchor\s*\([^()]*\|\|\s*'([a-z0-9-]+)'/g)) anchors.add(m[1]);
    assert.ok(anchors.size >= 15, 'expected at least 15 help anchors, got ' + anchors.size);
    const missing = [...anchors].filter(a => !ids.has(a));
    assert.deepStrictEqual(missing, [], 'help.html lacks sections for: ' + missing.join(', '));
    // the SCP81 tab wires its own anchors and the targets exist
    assert.ok(ids.has('scp81') && ids.has('scp81-listener') && ids.has('scp81-scripts'));
    assert.ok(html.includes("'scp81-listener' : 'scp81-scripts'"));
    assert.ok(html.includes("? 'scp81-scripts' : 'scp81-listener'"));
});

test('phone simulator has the network-simulation fieldset', () => {
    assert.ok(html.includes('data-l10n="Network simulation"'));
    for (const s of ['cold_boot', 'attach_eps', 'attach_2g', 'service_lost',
        'limited_service', 'roaming_denied', 'churn', 'sms_received',
        'cb_reconfig', 'authenticate']) {
        assert.ok(html.includes("netSimRun('" + s + "')"), s);
    }
    assert.ok(html.includes('id="netsim-op-search"'));
    assert.ok(html.includes('id="netsim-log"'));
    assert.ok(html.includes('id="netsim-status"'));
    assert.match(html, /id="netsim-mcc" value="001"/);
    assert.ok(html.includes('data-l10n="Sets the PLMN written to'));
    // scenario buttons need the card (the server writes real EFs)
    assert.match(html, /data-needs="card" onclick="netSimRun\('service_lost'\)"/);
});

test('phone simulator action buttons are green and the TP status sits below them', () => {
    assert.match(html, /id="tp-send-btn"[^>]*bg-emerald-600/);
    assert.match(html, /id="pli-status-btn"[^>]*bg-emerald-600/);
    // #tp-status is outside the button row (the row's </div> comes after
    // the last button and before the status element)
    const block = html.slice(html.indexOf('id="tp-send-btn"'), html.indexOf('id="tp-status"'));
    assert.ok(block.lastIndexOf('</div>') > block.lastIndexOf('</button>'));
});

test('cards and custom files expose only file import/export', () => {
    assert.ok(!html.includes('data-l10n="Export as JSON"'));
    assert.ok(!html.includes('data-l10n="Paste & import"'));
    assert.ok(!html.includes('data-l10n="Import JSON from clipboard"'));
    for (const fn of ['cardsExportFile', 'cardsImportFile', 'pysimCustomExportFile', 'pysimCustomImportFile']) {
        assert.ok(html.includes('function ' + fn + '('), fn);
    }
    assert.ok(!html.includes('function cardsExport('));
    assert.ok(!html.includes('function pysimCustomExport('));
    assert.ok(!html.includes('cardsImportField('));
    assert.ok(!html.includes('pysimCustomImportField('));
    // import feedback goes to the transient status line
    assert.ok(html.includes('function ioStatus('));
    assert.ok(html.includes("ioStatus('cards-io'"));
    assert.ok(html.includes("ioStatus('pysim-cf-io'"));
});

test('SCP81 listener exposes its four modes with the matching notes', () => {
    for (const v of ['tls', 'redirect', 'passthru', 'dump']) {
        assert.ok(html.includes('value="' + v + '"'), v);
    }
    assert.ok(html.includes('id="scp81-redirect-note"'));
    assert.ok(html.includes('id="scp81-passthru-note"'));
    // redirect needs the configured target; passthru uses the card's one
    assert.ok(html.includes("mode === 'redirect' && (!hostVal || !portVal)"));
    assert.ok(html.includes("el.disabled = (mode === 'passthru')"));
});
