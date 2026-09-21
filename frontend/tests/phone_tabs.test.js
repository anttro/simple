const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name) {
	const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
	const m = re.exec(src);
	if (!m) throw new Error('function ' + name + ' not found');
	let i = m.index + m[0].length - 1;
	let depth = 0;
	for (; i < src.length; i++) {
		if (src[i] === '{') depth++;
		else if (src[i] === '}') {
			depth--;
			if (depth === 0) break;
		}
	}
	return src.slice(m.index, i + 1);
}

const code = extractFunc(html, 'phoneSwitchSubtab') + '\n' +
	'globalThis.setHelpAnchor = a => { globalThis._anchor = a; };\n' +
	'globalThis.stkCheckMenu = () => { globalThis._stk = (globalThis._stk || 0) + 1; };\n' +
	'globalThis.pysimEventsRender = () => { globalThis._events = (globalThis._events || 0) + 1; };\n' +
	'globalThis.pysimProactiveLogRender = () => { globalThis._log = (globalThis._log || 0) + 1; };\n' +
	'globalThis.pysimPollStatusInit = () => { globalThis._poll = (globalThis._poll || 0) + 1; };\n' +
	'globalThis.pysimPliRender = () => { globalThis._pli = (globalThis._pli || 0) + 1; };\n' +
	'globalThis.tpRefresh = () => { globalThis._tp = (globalThis._tp || 0) + 1; };\n' +
	'globalThis.esimFetchAll = () => { globalThis._esim = (globalThis._esim || 0) + 1; };\n' +
	'globalThis.netStateFetch = () => { globalThis._netstate = (globalThis._netstate || 0) + 1; };\n';
eval(code);

function makeClassList() {
	const set = new Set();
	return {
		toggle: (c, on) => { on ? set.add(c) : set.delete(c); },
		has: c => set.has(c),
	};
}

function setup() {
	const buttons = [
		{ dataset: { phoneSub: 'phone' }, classList: makeClassList() },
		{ dataset: { phoneSub: 'tr' }, classList: makeClassList() },
		{ dataset: { phoneSub: 'esim' }, classList: makeClassList() },
	];
	const panels = {
		'phone-sub-phone': { classList: makeClassList() },
		'phone-sub-tr': { classList: makeClassList() },
		'phone-sub-esim': { classList: makeClassList() },
	};
	globalThis.document = {
		querySelectorAll: sel => (sel === '.phone-subtab' ? buttons : []),
		getElementById: id => panels[id] || null,
	};
	globalThis._anchor = null;
	globalThis._stk = globalThis._events = globalThis._log = globalThis._poll = globalThis._pli = globalThis._tp = 0;
	globalThis._esim = globalThis._netstate = 0;
	return { buttons, panels };
}

test('TR Config pill shows the TR panel and renders PLI data', () => {
	const { buttons, panels } = setup();
	phoneSwitchSubtab('tr');
	assert.ok(!panels['phone-sub-tr'].classList.has('hidden'));
	assert.ok(panels['phone-sub-phone'].classList.has('hidden'));
	assert.ok(buttons[1].classList.has('bg-blue-600'));
	assert.ok(!buttons[0].classList.has('bg-blue-600'));
	assert.strictEqual(globalThis._anchor, 'pli-dict');
	assert.strictEqual(globalThis._pli, 1);
	assert.strictEqual(globalThis._stk, 0);
});

test('Phone pill shows the phone panel and renders CAT views', () => {
	const { buttons, panels } = setup();
	phoneSwitchSubtab('phone');
	assert.ok(!panels['phone-sub-phone'].classList.has('hidden'));
	assert.ok(panels['phone-sub-tr'].classList.has('hidden'));
	assert.ok(buttons[0].classList.has('bg-blue-600'));
	assert.strictEqual(globalThis._anchor, 'stk-menu');
	assert.strictEqual(globalThis._stk, 1);
	assert.strictEqual(globalThis._events, 1);
	assert.strictEqual(globalThis._log, 1);
	assert.strictEqual(globalThis._poll, 1);
	assert.strictEqual(globalThis._pli, 0);
	assert.strictEqual(globalThis._tp, 1);
});
