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

let code = 'var pysimFsSelected = null;\n'
	+ 'var pysimFsDecodedMode = false;\n'
	+ 'var pysimFsTreeRoot = null;\n';
code += extractFunc(html, 'pysimFsSyncContentVisibility') + '\n';
code += extractFunc(html, 'pysimFsRead') + '\n';
eval(code);

let content, status, fetchResult, errorShown;

function fakeEl() {
	const classes = new Set();
	return {
		innerHTML: '',
		textContent: '',
		classList: {
			add: (...cs) => cs.forEach(c => classes.add(c)),
			remove: (...cs) => cs.forEach(c => classes.delete(c)),
			toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); },
			contains: c => classes.has(c),
		},
	};
}

function setup() {
	content = fakeEl();
	status = fakeEl();
	globalThis.document = {
		getElementById: id => id === 'pysim-fs-content' ? content
			: (id === 'pysim-fs-status' ? status : null),
	};
	globalThis.t = s => s;
	globalThis.esc = s => s;
	globalThis.pysimFsFindNode = () => ({ name: 'EF.IMSI', fid: '6f07' });
	globalThis.pysimFsSelectBody = () => ({});
	globalThis.pysimFsShowError = (el, sw, err) => {
		errorShown = { sw, err };
		el.textContent = 'SW: ' + sw + ' — ' + err;
	};
	globalThis.pysimFetch = async () => {
		if (fetchResult instanceof Error) throw fetchResult;
		return fetchResult;
	};
	pysimFsSelected = 'EF.IMSI';
	pysimFsDecodedMode = false;
	pysimFsTreeRoot = { name: 'MF' };
	fetchResult = { success: true, sw: '9000', data: 'AABB' };
	errorShown = null;
}

test('pysimFsSyncContentVisibility hides an empty pane and shows loaded content', () => {
	setup();
	content.innerHTML = '';
	pysimFsSyncContentVisibility();
	assert.ok(content.classList.contains('hidden'));
	content.innerHTML = '   ';
	pysimFsSyncContentVisibility();
	assert.ok(content.classList.contains('hidden'), 'whitespace-only content stays hidden');
	content.innerHTML = '<textarea>x</textarea>';
	pysimFsSyncContentVisibility();
	assert.ok(!content.classList.contains('hidden'));
});

test('a successful read fills and shows the content pane', async () => {
	setup();
	const ok = await pysimFsRead();
	assert.strictEqual(ok, true);
	assert.ok(content.innerHTML.includes('AABB'));
	assert.ok(!content.classList.contains('hidden'));
	assert.strictEqual(status.textContent, 'SW: 9000 OK');
});

test('a failed read keeps the content pane hidden', async () => {
	setup();
	fetchResult = { success: false, sw: '6982', error: 'Security status not satisfied' };
	const ok = await pysimFsRead();
	assert.strictEqual(ok, false);
	assert.strictEqual(content.innerHTML, '');
	assert.ok(content.classList.contains('hidden'));
	assert.deepStrictEqual(errorShown, { sw: '6982', err: 'Security status not satisfied' });
});

test('a thrown read error keeps the content pane hidden', async () => {
	setup();
	fetchResult = new Error('boom');
	const ok = await pysimFsRead();
	assert.strictEqual(ok, false);
	assert.ok(content.classList.contains('hidden'));
	assert.ok(status.textContent.includes('boom'));
});

test('the pane starts hidden and every mutation keeps it in sync', () => {
	assert.match(html, /id="pysim-fs-content"[^>]*class="[^"]*\bhidden\b/);
	for (const fn of ['pysimFsClickFile', 'pysimFsRead', 'pysimFsEdit', 'pysimFsCancel']) {
		assert.ok(extractFunc(html, fn).includes('pysimFsSyncContentVisibility'),
			fn + ' must sync the content pane visibility');
	}
});
