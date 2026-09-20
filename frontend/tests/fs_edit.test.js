const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name) {
	const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
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

let code = 'var pysimFsDecodedMode = false;\n'
	+ 'var pysimFsEditMode = false;\n'
	+ 'var pysimFsEditData = null;\n'
	+ 'var pysimFsSelected = null;\n'
	+ 'var pysimFsTreeRoot = null;\n';
for (const fn of ['pysimFsSetMode', 'pysimFsPillsEnabled', 'pysimFsEdit',
	'pysimFsCancel', 'pysimFsResetEdit', 'pysimFsFindNode', 'pysimFsHasDecoder']) {
	code += extractFunc(html, fn) + '\n';
}
eval(code);

let events = [];
let readCalls = 0;
let readResult = true;
let content, buttons, pills, editables, checkboxes, inputs;

function fakeClassList() {
	const set = new Set();
	return {
		add: (...c) => c.forEach(x => set.add(x)),
		remove: (...c) => c.forEach(x => set.delete(x)),
		toggle: (c, on) => { if (on) set.add(c); else set.delete(c); },
		contains: c => set.has(c),
	};
}

function fakeEl() {
	return { classList: fakeClassList(), dataset: {} };
}

function setup(opts) {
	opts = opts || {};
	events = [];
	readCalls = 0;
	readResult = opts.readResult !== false;
	editables = [{ removeAttribute: () => events.push('readonly-removed') }];
	checkboxes = [fakeEl()];
	inputs = [{ addEventListener: () => {} }];
	content = fakeEl();
	content.innerHTML = opts.contentHtml || '';
	content.querySelectorAll = sel => {
		if (sel === '.pysim-fs-record-cb') return checkboxes;
		if (sel.indexOf('textarea') === 0) return editables;
		return inputs;
	};
	buttons = {
		'pysim-fs-edit-btn': fakeEl(),
		'pysim-fs-save-btn': fakeEl(),
		'pysim-fs-cancel-btn': fakeEl(),
	};
	buttons['pysim-fs-save-btn'].classList.add('hidden');
	buttons['pysim-fs-cancel-btn'].classList.add('hidden');
	pills = [fakeEl(), fakeEl()];
	pills[0].dataset.mode = 'raw';
	pills[1].dataset.mode = 'dec';

	pysimFsDecodedMode = !!opts.decoded;
	pysimFsEditMode = false;
	pysimFsEditData = null;
	pysimFsSelected = 'EF.IMSI';
	pysimFsTreeRoot = { name: 'MF', fid: '3f00', children: [{ name: 'EF.IMSI', fid: '6f07' }] };
	globalThis.efFindDecoder = (name, fid) => {
		const n = (name || '').toUpperCase();
		const f = (fid || '').toLowerCase();
		return (n === 'EF.IMSI' || f === '6f07') ? { name: 'EF.IMSI', fid: '6f07' } : null;
	};

	globalThis.document = {
		getElementById: id => id === 'pysim-fs-content' ? content : (buttons[id] || null),
		querySelectorAll: sel => sel === '.pysim-mode-pill' ? pills : [],
	};
	globalThis.pysimFsRead = async () => {
		readCalls++;
		events.push('read');
		return readResult;
	};
	return {
		content,
		btn: id => buttons['pysim-fs-' + id + '-btn'],
		pill: mode => pills.find(p => p.dataset.mode === mode),
		reads: () => readCalls,
		readonlyRemoved: () => events.filter(e => e === 'readonly-removed').length,
		events,
	};
}

test('Read raw / Read decoded pills switch the view and read the file', () => {
	const h = setup();
	pysimFsSetMode('raw', true);
	assert.strictEqual(pysimFsDecodedMode, false);
	assert.strictEqual(h.reads(), 0, 'skipRead updates the pills only');
	assert.ok(h.pill('raw').classList.contains('bg-blue-600'));
	assert.ok(!h.pill('dec').classList.contains('bg-blue-600'));

	pysimFsSetMode('dec');
	assert.strictEqual(pysimFsDecodedMode, true);
	assert.strictEqual(h.reads(), 1, 'a pill click reads the selected file');
	assert.ok(h.pill('dec').classList.contains('bg-blue-600'));
	assert.ok(!h.pill('raw').classList.contains('bg-blue-600'));
});

test('pill clicks are ignored while editing', () => {
	const h = setup();
	pysimFsDecodedMode = true;
	pysimFsEditMode = true;
	pysimFsSetMode('raw');
	assert.strictEqual(pysimFsDecodedMode, true, 'the view mode must not change mid-edit');
	assert.strictEqual(h.reads(), 0);
});

test('Edit raw keeps a loaded raw view and enters edit mode without re-reading', async () => {
	const h = setup({ contentHtml: '<textarea readonly>aa</textarea>' });
	await pysimFsEdit();
	assert.strictEqual(h.reads(), 0);
	assert.strictEqual(pysimFsEditMode, true);
	assert.ok(h.btn('edit').classList.contains('hidden'));
	assert.ok(!h.btn('save').classList.contains('hidden'));
	assert.ok(!h.btn('cancel').classList.contains('hidden'));
	assert.strictEqual(h.readonlyRemoved(), 1);
	assert.ok(h.pill('raw').classList.contains('opacity-50'), 'pills are dimmed');
	assert.ok(h.pill('raw').classList.contains('pointer-events-none'));
});

test('Edit raw switches from the decoded view to raw and reads before editing', async () => {
	const h = setup({ decoded: true });
	await pysimFsEdit();
	assert.strictEqual(pysimFsDecodedMode, false);
	assert.strictEqual(h.reads(), 1);
	assert.deepStrictEqual(h.events, ['read', 'readonly-removed'],
		'the raw view must be rendered before the readonly attributes are stripped');
	assert.ok(h.pill('raw').classList.contains('bg-blue-600'));
	assert.strictEqual(pysimFsEditMode, true);
});

test('Edit raw reads a fresh selection and aborts if the read fails', async () => {
	const h = setup({ readResult: false });
	await pysimFsEdit();
	assert.strictEqual(h.reads(), 1);
	assert.strictEqual(pysimFsEditMode, false);
	assert.ok(h.btn('save').classList.contains('hidden'));
	assert.ok(!h.pill('raw').classList.contains('opacity-50'));
});

test('Cancel restores the view and re-enables the pills', async () => {
	const h = setup({ contentHtml: '<textarea readonly>aa</textarea>' });
	await pysimFsEdit();
	pysimFsCancel();
	assert.strictEqual(pysimFsEditMode, false);
	assert.strictEqual(h.content.innerHTML, '<textarea readonly>aa</textarea>');
	assert.ok(!h.pill('raw').classList.contains('opacity-50'));
	assert.ok(!h.pill('raw').classList.contains('pointer-events-none'));
	assert.ok(!h.btn('edit').classList.contains('hidden'));
	assert.ok(h.btn('save').classList.contains('hidden'));
	assert.ok(h.btn('cancel').classList.contains('hidden'));
});

test('Reset edit re-enables the pills', () => {
	const h = setup();
	pysimFsEditMode = true;
	pysimFsPillsEnabled(false);
	pysimFsResetEdit();
	assert.strictEqual(pysimFsEditMode, false);
	assert.ok(!h.pill('dec').classList.contains('opacity-50'));
});

test('Read decoded is ignored when the file has no decoder', () => {
	const h = setup();
	globalThis.efFindDecoder = () => null;
	pysimFsSetMode('dec');
	assert.strictEqual(pysimFsDecodedMode, false);
	assert.strictEqual(h.reads(), 0);
	assert.ok(!h.pill('dec').classList.contains('bg-blue-600'));
});

test('pysimFsHasDecoder resolves the selected file by name or FID', () => {
	setup();
	assert.strictEqual(pysimFsHasDecoder(), true, 'EF.IMSI is in the tree');
	// alias name: the decoder is matched by the FID fallback
	pysimFsTreeRoot = { name: 'MF', children: [{ name: 'EF.ALIAS', fid: '6f07' }] };
	pysimFsSelected = 'EF.ALIAS';
	assert.strictEqual(pysimFsHasDecoder(), true);
	// unknown file -> no decoder
	globalThis.efFindDecoder = () => null;
	assert.strictEqual(pysimFsHasDecoder(), false);
	// nothing selected -> no decoder
	pysimFsSelected = null;
	assert.strictEqual(pysimFsHasDecoder(), false);
});

test('the file manager row exposes the read pills and Edit raw', () => {
	assert.ok(!html.includes('id="pysim-fs-read-btn"'), 'the redundant Read button is gone');
	assert.ok(html.includes('data-l10n="Read raw">Read raw<'));
	assert.ok(html.includes('data-l10n="Read decoded">Read decoded<'));
	assert.ok(html.includes('data-l10n="Edit raw">Edit raw<'));
	assert.ok(html.includes('id="pysim-fs-read-raw-btn"') && html.includes('data-needs="card"'));
	const dec = /<button[^>]*id="pysim-fs-read-dec-btn"[^>]*>/.exec(html);
	assert.ok(dec && dec[0].includes('data-needs-decoder'), 'the decoded pill is decoder-gated');
});
