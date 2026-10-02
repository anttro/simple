const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
// The help pages are HTML too: the same structural walk validates them.
const helpPages = ['help.html', 'help-ru.html'].map(f => ({
    name: f, text: fs.readFileSync(path.join(__dirname, '..', f), 'utf8'),
}));

// Tags that never take an end tag.
const VOID_TAGS = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img',
    'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
// End tags HTML allows to be omitted: a close tag may legally skip these.
const OPTIONAL_END = new Set(['p', 'li', 'dt', 'dd', 'option', 'optgroup',
    'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'rt', 'rp', 'colgroup', 'caption']);
// Start tags that implicitly close the listed open element (the subset used here).
const IMPLIED_END = {
    li: ['li'], dt: ['dt', 'dd'], dd: ['dt', 'dd'], p: ['p'],
    option: ['option'], optgroup: ['optgroup'],
    tr: ['tr'], td: ['td', 'th'], th: ['td', 'th'],
    thead: ['thead', 'tbody', 'tfoot'], tbody: ['thead', 'tbody', 'tfoot'],
    tfoot: ['thead', 'tbody', 'tfoot'],
};

// The markup without comments and embedded script/style bodies: only this
// participates in tag nesting. JavaScript template strings may hold
// unbalanced <div> fragments, which is exactly how a missing </div> stayed
// invisible to the old whole-file count (the Simulator panels were nested
// from v3.5.11 to v3.6.0 while the total stayed balanced).
function htmlOnly(src) {
    return src.replace(/<!--[\s\S]*?-->/g, '')
        .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
        .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '');
}

// Walk the tag stream and report cross-nesting, stray end tags and unclosed
// elements. Run on every HTML page for every change. HTML optional end tags
// are honoured so a legitimately omitted </p>/</li> is not an error.
function htmlStructureProblems(src) {
    const markup = htmlOnly(src);
    const problems = [];
    const stack = [];
    const re = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?(\/?)>/g;
    let m;
    while ((m = re.exec(markup))) {
        const closing = m[1] === '/';
        const tag = m[2].toLowerCase();
        const selfClosing = m[3] === '/';
        if (VOID_TAGS.has(tag) || selfClosing) continue;
        if (!closing) {
            for (const t of (IMPLIED_END[tag] || [])) {
                while (stack.length && stack[stack.length - 1].tag === t) stack.pop();
            }
            stack.push({ tag, at: m.index });
            continue;
        }
        if (!stack.length) { problems.push(`stray </${tag}> at offset ${m.index}`); continue; }
        if (stack[stack.length - 1].tag === tag) { stack.pop(); continue; }
        let i = stack.length - 1;
        while (i >= 0 && stack[i].tag !== tag) i--;
        if (i < 0) { problems.push(`stray </${tag}> at offset ${m.index}`); continue; }
        const between = stack.slice(i + 1);
        if (between.every(e => OPTIONAL_END.has(e.tag))) stack.length = i;
        else {
            problems.push(`cross-nesting: </${tag}> at offset ${m.index} closes over ` +
                between.map(e => `<${e.tag}>`).join(' '));
        }
    }
    for (const e of stack) {
        if (!OPTIONAL_END.has(e.tag)) problems.push(`unclosed <${e.tag}> at offset ${e.at}`);
    }
    return problems;
}

// Depth of a <div id="..."> in the parsed div stream: sibling panels share
// one depth, a nested panel sits one level deeper.
function divDepthOf(src, id) {
    const markup = htmlOnly(src);
    const re = /<(\/?)div\b[^>]*>/g;
    let depth = 0, m;
    while ((m = re.exec(markup))) {
        if (m[1] === '/') { depth -= 1; continue; }
        depth += 1;
        if (m[0].includes('id="' + id + '"')) return depth;
    }
    return -1;
}

test('HTML <div> tags are balanced in the markup (scripts excluded)', () => {
    const markup = htmlOnly(html);
    const opens = (markup.match(/<div\b/g) || []).length;
    const closes = (markup.match(/<\/div>/g) || []).length;
    assert.strictEqual(opens, closes, `Unbalanced divs: ${opens} opens vs ${closes} closes`);
});

test('HTML tag nesting is well-formed on every page', () => {
    for (const page of [{ name: 'index.html', text: html }, ...helpPages]) {
        assert.deepStrictEqual(htmlStructureProblems(page.text), [],
            page.name + ' has structure problems');
    }
});

test('the Simulator panels are siblings of the tab container, never nested', () => {
    const ids = ['phone-sub-phone', 'phone-sub-tr', 'phone-sub-bip',
        'phone-sub-esim', 'phone-sub-test'];
    const depths = ids.map(id => {
        const d = divDepthOf(html, id);
        assert.ok(d > 0, 'panel missing: ' + id);
        return d;
    });
    assert.strictEqual(new Set(depths).size, 1,
        'Simulator panels are nested at different depths: ' +
        ids.map((id, i) => id + '=' + depths[i]).join(', '));
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

test('the card editor groups its fields into labelled fieldsets', () => {
    const fieldsets = [...html.matchAll(/<fieldset[\s\S]*?<\/fieldset>/g)].map(m => m[0]);
    const pinAdm = fieldsets.find(f => /data-l10n="PIN\/ADM"/.test(f));
    const tar = fieldsets.find(f => /data-l10n="TAR"/.test(f));
    const scp80 = fieldsets.find(f => /data-l10n="SCP80 \(GSM 03\.48, ETSI TS 102 225\)"/.test(f));
    const scp81 = fieldsets.find(f => /data-l10n="SCP81 \(HTTP OTA\)"/.test(f));
    assert.ok(pinAdm, 'PIN/ADM fieldset not found');
    assert.ok(tar, 'TAR fieldset not found');
    assert.ok(scp80, 'SCP80 card fieldset not found');
    assert.ok(scp81, 'SCP81 card fieldset not found');
    // the optional card codes (plain text; PIN/PUK are 4-8 decimal digits)
    for (const id of ['cards-pin1', 'cards-puk1', 'cards-pin2', 'cards-puk2', 'cards-adm']) {
        assert.ok(pinAdm.includes('id="' + id + '"'), id + ' not in the PIN/ADM fieldset');
    }
    // the TAR table: uniform rows rendered into the body, Add TAR below
    assert.match(tar, /<tbody id="cards-tars">/);
    assert.match(tar, /onclick="cardsTarAdd\(\)"/);
    assert.match(tar, />TAR<\/th>/);
    assert.match(tar, />MSL<\/th>/);
    assert.match(tar, />Description<\/th>/);
    // the keyset editor and the PSK pair stay as implemented
    assert.match(scp80, /id="cards-keysets"/);
    assert.match(scp80, /onclick="cardsKeysetAdd\(\)"/);
    assert.match(scp81, /id="cards-psk-id"/);
    assert.match(scp81, /id="cards-psk-key"/);
    // the PSK explanation lives inside the SCP81 group, not outside it
    assert.match(scp81, /data-l10n="SCP81 HTTP OTA: the listener picks the key/);
    // the preset-wide SPI1/SPI2 inputs are gone: each TAR row carries its MSL
    assert.ok(!html.includes('id="cards-spi1"'), 'the preset-wide SPI1 input is gone');
    assert.ok(!html.includes('id="cards-spi2"'), 'the preset-wide SPI2 input is gone');
    // the name/ICCID fields are in the editor but not inside the fieldsets
    assert.ok(!fieldsets.some(f => f.includes('id="cards-iccid"')));
    assert.ok(!fieldsets.some(f => f.includes('id="cards-name"')));
});

test('the ADM input allows the full 4-16 hex digit key', () => {
    // the server's _verify_adm takes 4-16 hex digits (padded to 8 CHV bytes);
    // the input must not cap it at the PIN length
    const m = /<input id="cards-adm"[^>]*>/.exec(html);
    assert.ok(m, 'the ADM input is missing');
    const limit = /maxlength="(\d+)"/.exec(m[0]);
    assert.ok(!limit || parseInt(limit[1], 10) >= 16,
        'the ADM input must allow 4-16 hex digits (server _verify_adm)');
});

test('the Cards tab splits the preset list and the editor into views', () => {
    assert.match(html, /id="cards-list-view"/);
    assert.match(html, /id="cards-editor-view" class="hidden"/);
    assert.match(html, /id="cards-new-btn"[^>]*onclick="cardsNew\(\)"/);
    assert.match(html, /id="cards-editor-title"/);
    const listView = html.slice(html.indexOf('id="cards-list-view"'),
        html.indexOf('id="cards-editor-view"'));
    const editorView = html.slice(html.indexOf('id="cards-editor-view"'),
        html.indexOf('id="event-send-modal"'));
    // the list view holds the table, the Add preset button and export/import
    assert.match(listView, /id="cards-tbody"/);
    assert.match(listView, /id="cards-file"/);
    assert.match(listView, /id="cards-io"/);
    assert.match(listView, /cardsExportFile\(\)/);
    assert.ok(!listView.includes('id="cards-keysets"'), 'the editor must not be in the list view');
    // the editor view holds the fieldsets and the save/cancel buttons
    assert.match(editorView, /id="cards-keysets"/);
    assert.match(editorView, /id="cards-pin1"/);
    assert.match(editorView, /id="cards-tars"/);
    assert.match(editorView, /id="cards-add-btn"[^>]*onclick="cardsAdd\(\)"/);
    assert.match(editorView, /onclick="cardsCancelEdit\(\)"/);
    assert.ok(!editorView.includes('id="cards-tbody"'), 'the list table must not be in the editor');
});

test('the ADM field sits in the PIN/ADM fieldset of the editor', () => {
    const fieldsets = [...html.matchAll(/<fieldset[\s\S]*?<\/fieldset>/g)].map(m => m[0]);
    const pinAdm = fieldsets.find(f => /data-l10n="PIN\/ADM"/.test(f));
    assert.ok(pinAdm, 'PIN/ADM fieldset not found');
    assert.match(pinAdm, /id="cards-adm"[^>]*placeholder="ADM \(optional\)"/);
    // the From card button sits in the editor header, above the code fields
    assert.ok(html.indexOf('id="cards-iccid-from-card"') < html.indexOf('id="cards-pin1"'),
        'the From card button must precede the code fields');
});

test('card-dependent profiler buttons show the ICCID and need a readable one', () => {
    assert.match(html, /id="profiler-from-card-btn" data-needs="card-iccid"/);
    assert.match(html, /id="snapshot-new-btn" data-needs="card-iccid"/);
    assert.match(html, /data-needs="card-iccid" onclick="profilerCheck/);
    // both static buttons carry the dynamic ICCID span
    assert.match(html, /id="profiler-from-card-btn"[\s\S]*?<span class="profiler-iccid-label"><\/span><\/button>/);
    assert.match(html, /id="snapshot-new-btn"[\s\S]*?<span class="profiler-iccid-label" data-iccid-sep=": "><\/span><\/button>/);
});

test('network state monitor panel lives next to the network simulation', () => {
    assert.match(html, /data-l10n="Network state"/);
    assert.match(html, /id="netstate-badge"/);
    assert.match(html, /id="netstate-refresh-btn" data-needs="card"/);
    assert.match(html, /id="netstate-body"/);
    assert.match(html, /id="netsim-write-fplmn"/);
    // the monitor is rendered from the cached server state (no file polling)
    assert.ok(html.includes("netStateFetch()"));
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

test('the C-APDU and R-APDU parsers are sub-pills of the Parser pill', () => {
    // The Parser pill is a top-level Remote APDU subtab...
    assert.match(html, /data-sub="parser" onclick="cApduSwitchSubtab\('parser'\)"/);
    // ...and both parser panels live inside its container, one after the other.
    const container = html.indexOf('id="c-apdu-sub-parser"');
    const parse = html.indexOf('id="c-apdu-sub-parse"');
    const response = html.indexOf('id="c-apdu-sub-response"');
    assert.ok(container > 0, 'Parser container missing');
    assert.ok(parse > container, 'C-APDU panel must be inside the Parser container');
    assert.ok(response > parse, 'R-APDU panel must follow the C-APDU panel');
    // Their sub-pill buttons exist and switch through parserSwitchSubtab.
    assert.match(html, /id="parser-btn-parse" onclick="parserSwitchSubtab\('parse'\)"/);
    assert.match(html, /id="parser-btn-response" onclick="parserSwitchSubtab\('response'\)"/);
});

test('the document and its inline script are complete', () => {
    // A truncated index.html (missing </script></body></html>) makes the whole
    // inline script fail to parse in the browser: every onclick handler then
    // reports "function is not defined" while the Node tests still pass,
    // because they extract functions from the text without ever running the
    // document's script.
    assert.ok(html.trimEnd().endsWith('</html>'), 'index.html must end with </html>');
    assert.strictEqual((html.match(/<script\b/g) || []).length,
        (html.match(/<\/script>/g) || []).length, 'every <script> must be closed');
    const inline = html.match(/<script>([\s\S]*)<\/script>\s*<\/body>/);
    assert.ok(inline, 'inline script not found');
    assert.ok(inline[1].length > 100000, 'inline script looks truncated');
    new Function(inline[1]);   // throws on a syntax error
    assert.ok(inline[1].includes('function cApduSwitchSubtab'), 'key function missing');
});

test('profiler and simulator are top-level tab contents', () => {
    assert.ok(html.includes('id="tab-profiler" class="tab-content hidden"'));
    assert.ok(html.includes('id="tab-phone" class="tab-content hidden"'));
});

test('simulator has Phone / TR Config pills', () => {
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
    assert.ok(html.includes('pysimFsInfoHtml(sel, name)'));
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

test('simulator has the network-simulation fieldset', () => {
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

test('simulator action buttons are green and the TP status sits below them', () => {
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

test('the network monitor labels its refresh timestamp and has no area line', () => {
    assert.match(html, /data-l10n="Last refresh">Last refresh<\/span>: <span id="netstate-read"><\/span>/);
    assert.ok(!/id="netstate-area"/.test(html), 'the area line is gone; LAI/RAI/TAI live in the EF rows');
});

test('every data-l10n attribute resolves in LANG_RU after HTML decoding', () => {
    // translatePage() looks up el.getAttribute('data-l10n') — the browser
    // decodes entities in attribute values, but NOT inside the <script>
    // block where LANG_RU lives. Mirror that lookup exactly: a key written
    // as &quot; in the markup and &quot; in the dict never matches.
    const dictMatch = html.match(/const LANG_RU = \{([\s\S]*?)\n\};/);
    assert.ok(dictMatch, 'LANG_RU literal not found');
    const dict = eval('({' + dictMatch[1] + '})');
    const entities = {
        quot: '"', '#34': '"', amp: '&', '#38': '&', apos: "'", '#39': "'",
        lt: '<', '#60': '<', gt: '>', '#62': '>', nbsp: ' ',
    };
    const decode = s => s.replace(/&([a-z]+|#[0-9]+);/gi,
        (m, e) => entities[e] !== undefined ? entities[e] : m);
    const attrs = [...html.matchAll(/data-l10n="([^"]*)"/g)].map(m => decode(m[1]));
    const titles = [...html.matchAll(/data-l10n-title="([^"]*)"/g)].map(m => decode(m[1]));
    const missing = [...new Set([...attrs, ...titles].filter(k => !(k in dict)))];
    assert.deepStrictEqual(missing, [], 'data-l10n keys with no LANG_RU entry:\n' + missing.join('\n'));
});

test('every tab-content lives inside the max-w-7xl page container', () => {
    // SCP81 and Cards used to sit outside the container (its closing </div>
    // came before them), so they stretched to the whole window while the
    // other tabs were capped - v3.6.46.
    const markup = htmlOnly(html);
    const re = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)\b([^>]*?)>/g;
    let m;
    let depth = 0;
    let containerDepth = null;
    const seen = new Set();
    while ((m = re.exec(markup))) {
        const closing = m[1] === '/';
        const tag = m[2].toLowerCase();
        const attrs = m[3] || '';
        const selfClosing = attrs.trim().endsWith('/');
        if (VOID_TAGS.has(tag) || selfClosing) continue;
        if (closing) {
            if (containerDepth !== null && depth === containerDepth) containerDepth = null;
            depth--;
            continue;
        }
        if (tag === 'div' && /class="[^"]*max-w-7xl/.test(attrs)) containerDepth = depth + 1;
        if (tag === 'div' && /class="[^"]*tab-content/.test(attrs)) {
            const id = (/id="([^"]+)"/.exec(attrs) || [])[1];
            assert.notStrictEqual(containerDepth, null,
                id + ' must be inside the max-w-7xl page container');
            seen.add(id);
        }
        depth++;
    }
    assert.strictEqual(seen.size, 7, 'expected all seven tabs, saw ' + seen.size);
});

test('the RAM form shows Card preset and Operation in one row', () => {
    const row = /<div class="mb-3 flex gap-2 items-end">\s*<div class="flex-1">\s*<label[^>]*data-l10n="Card preset"[\s\S]*?id="ram-card-sel"[\s\S]*?data-l10n="Operation"[\s\S]*?id="ram-op"[\s\S]*?data-l10n="Execute"/.exec(html);
    assert.ok(row, 'Card preset + Operation + Execute must share one row');
});

test('the RAM operation modal is wide enough for the step lines', () => {
    const m = /<div id="ram-op-modal"[\s\S]*?<div class="([^"]*max-w-[^"]*)"/.exec(html);
    assert.ok(m, 'RAM op modal not found');
    assert.ok(m[1].includes('max-w-3xl'), 'the modal was widened to fit the step lines: ' + m[1]);
});

test('the toolkit parameters grid is three columns with the access row last', () => {
    // 12 narrow fields fit in 4 rows (the MSL select makes a 13th cell, so
    // the file-access group spans the full width as its own last row) - v3.6.50
    const grid = /<div class="grid grid-cols-3 gap-x-3 gap-y-2 text-sm">[\s\S]*?id="rc-tk-access-row"/.exec(html);
    assert.ok(grid, 'toolkit grid must be 3 columns with the access row in it');
    const row = /<div id="rc-tk-access-row" class="col-span-3 flex items-center gap-4">([\s\S]*?)<\/div>\s*<\/div>/.exec(html);
    assert.ok(row, 'the access row spans the grid width');
    for (const id of ['rc-tk-fsaccess-row', 'rc-tk-adfaccess-row', 'rc-tk-adfaid-row']) {
        assert.ok(row[1].includes(id), id + ' must sit in the access row');
    }
});
