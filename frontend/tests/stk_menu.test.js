const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name, asyncFn) {
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
	return (asyncFn ? 'async ' : '') + src.slice(m.index, i + 1);
}

let code = extractFunc(html, 'stkMenuRespond', true) + '\n';
code += extractFunc(html, 'stkMenuNaiSuffix') + '\n';
code += 'globalThis.esc = s => s;\n';
eval(code);

function setup(response) {
	const calls = { handled: null, rendered: 0 };
	globalThis.pysimFetch = async () => response;
	globalThis.stkMenuHandleResponse = d => { calls.handled = d; };
	globalThis.stkMenuRenderItems = () => { calls.rendered++; };
	const btns = { classList: { add: () => {} } };
	const back = { style: {} };
	globalThis.document = {
		getElementById: id => (id === 'stk-menu-buttons' ? btns : id === 'stk-back-btn' ? back : { innerHTML: '' }),
	};
	return calls;
}

test('the STK menu item suffix shows the item next action (8.24)', () => {
	assert.strictEqual(stkMenuNaiSuffix({ id: 1, text: 'Menu', nai: 0x25, nai_name: 'SET UP MENU' }),
		'\u25b8 SET UP MENU');
	// no NAI (or a reserved one, which the server drops) -> no suffix
	assert.strictEqual(stkMenuNaiSuffix({ id: 2, text: 'Info' }), '');
	assert.strictEqual(stkMenuNaiSuffix(null), '');
});

test('back with a fetched SELECT ITEM continues the card dialogue', async () => {
	const data = { type: 'select_item', items: [{ id: 1, text: 'Info' }] };
	const calls = setup(data);
	await stkMenuRespond('back');
	assert.strictEqual(calls.handled, data);
	assert.strictEqual(calls.rendered, 0);
});

test('back with a fetched DISPLAY TEXT shows it', async () => {
	const data = { type: 'display_text', text: 'hello' };
	const calls = setup(data);
	await stkMenuRespond('back');
	assert.strictEqual(calls.handled, data);
	assert.strictEqual(calls.rendered, 0);
});

test('timeout with a fetched SELECT ITEM continues the card dialogue', async () => {
	const data = { type: 'select_item', items: [] };
	const calls = setup(data);
	await stkMenuRespond('timeout');
	assert.strictEqual(calls.handled, data);
	assert.strictEqual(calls.rendered, 0);
});

test('back answered with SW 9000 falls back to the cached top menu', async () => {
	const calls = setup({ type: 'done', sw: '9000' });
	await stkMenuRespond('back');
	assert.strictEqual(calls.handled, null);
	assert.strictEqual(calls.rendered, 1);
});

test('cancel falls back to the cached top menu', async () => {
	const calls = setup({ sw: '9000' });
	await stkMenuRespond('cancel');
	assert.strictEqual(calls.handled, null);
	assert.strictEqual(calls.rendered, 1);
});

test('ok navigates with the server response', async () => {
	const data = { type: 'select_item', items: [{ id: 1, text: 'x' }] };
	const calls = setup(data);
	await stkMenuRespond('ok');
	assert.strictEqual(calls.handled, data);
	assert.strictEqual(calls.rendered, 0);
});
