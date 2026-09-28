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
code += extractFunc(html, 'stkNoPendingError') + '\n';
code += extractFunc(html, 'stkMenuPanelReset') + '\n';
code += extractFunc(html, 'stkMenuNaiSuffix') + '\n';
code += extractFunc(html, 'stkMenuItemsHtml') + '\n';
code += extractFunc(html, 'stkInputHtml') + '\n';
code += extractFunc(html, 'stkInputHint') + '\n';
code += extractFunc(html, 'stkInputValidate') + '\n';
code += 'globalThis.esc = s => s;\n';
eval(code);

function setup(response) {
	const calls = { handled: null, rendered: 0 };
	globalThis.pysimFetch = async () => response;
	globalThis.stkMenuHandleResponse = d => { calls.handled = d; };
	globalThis.stkMenuRenderItems = () => { calls.rendered++; };
	globalThis.stkCheckMenu = () => {};
	const btns = { classList: { add: () => {} } };
	const back = { style: {} };
	globalThis.document = {
		getElementById: id => (id === 'stk-menu-buttons' ? btns : id === 'stk-back-btn' ? back
			: { innerHTML: '', classList: { add: () => {} } }),
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

test('the overlay item list shows the next action and the row handler', () => {
	const list = stkMenuItemsHtml([
		{ id: 1, text: 'Menu', nai: 0x25, nai_name: 'SET UP MENU' },
		{ id: 2, text: 'Info' },
	], 'stkSubItemClick');
	assert.match(list, /onclick="stkSubItemClick\(1\)"/);
	assert.match(list, /onclick="stkSubItemClick\(2\)"/);
	assert.ok(list.includes('Menu'));
	assert.ok(list.includes('\u25b8 SET UP MENU'));
	// the second item has no indicator -> exactly one suffix in the list
	assert.strictEqual((list.match(/\u25b8/g) || []).length, 1);
	assert.strictEqual(stkMenuItemsHtml(null, 'stkMenuItemClick'), '<div class="space-y-1"></div>');
});

test('both overlay lists render through the shared row helper', () => {
	// the cached top menu and the pending SELECT ITEM items must use the same
	// renderer, otherwise the latter silently loses the NAI suffix
	assert.match(html, /stkMenuItemsHtml\(data\.items, 'stkMenuItemClick'\)/);
	assert.match(html, /stkMenuItemsHtml\(data\.items, 'stkSubItemClick'\)/);
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

test('stkNoPendingError detects the already-answered reply', () => {
	assert.strictEqual(stkNoPendingError({ error: 'no pending command' }), true);
	assert.strictEqual(stkNoPendingError({ sw: '9000', type: 'done' }), false);
	assert.strictEqual(stkNoPendingError({ error: 'other' }), false);
	assert.strictEqual(stkNoPendingError(null), false);
});

test('an already-answered respond does not reach the dialog handler', async () => {
	const calls = setup({ error: 'no pending command' });
	await stkMenuRespond('ok');
	assert.strictEqual(calls.handled, null);
	assert.strictEqual(calls.rendered, 0);
});

test('stkInputHtml builds the card-driven input field', () => {
	globalThis.t = s => s;
	const out = stkInputHtml({ type: 'get_input', text: 'PIN', min: 4, max: 16,
		digits_only: true, hidden: true });
	assert.ok(out.includes('PIN'), out);
	assert.ok(out.includes('type="password"'), out);          // hidden entry
	assert.ok(out.includes('inputmode="numeric"'), out);      // digits only
	assert.ok(out.includes('maxlength="16"'), out);
	assert.ok(out.includes('4\u201316 characters'), out);
	assert.ok(out.includes('hidden entry'), out);
	// UCS2 alphabet entry: a plain text field without the numeric keyboard
	const ucs2 = stkInputHtml({ type: 'get_input', text: 'Name', min: 0, max: 10, ucs2: true });
	assert.ok(ucs2.includes('type="text"'), ucs2);
	assert.ok(!ucs2.includes('inputmode'), ucs2);
	assert.ok(ucs2.includes('up to 10 characters'), ucs2);
	// 0xFF = no maximum (TS 102 223 8.11): no maxlength and no range hint
	const noMax = stkInputHtml({ type: 'get_input', text: 'x', min: 0, max: 255 });
	assert.ok(!noMax.includes('maxlength'), noMax);
	assert.ok(!noMax.includes('characters'), noMax);
	// Yes/No: buttons instead of a field
	const yesNo = stkInputHtml({ type: 'get_inkey', text: 'Continue?', yes_no: true });
	assert.ok(yesNo.includes("stkInputSubmit('01')"), yesNo);
	assert.ok(yesNo.includes("stkInputSubmit('00')"), yesNo);
	assert.ok(!yesNo.includes('stk-input-field'), yesNo);
	// the card's default text is prefilled; help adds the help action
	const def = stkInputHtml({ type: 'get_input', text: 'x', default: '1234', help: true });
	assert.ok(def.includes('value="1234"'), def);
	assert.ok(def.includes("stkMenuRespond('help')"), def);
});

test('stkInputValidate mirrors the server input rules', () => {
	globalThis.t = s => s;
	const pd = { type: 'get_input', min: 4, max: 16, digits_only: true, hidden: true };
	assert.strictEqual(stkInputValidate(pd, '1234').ok, true);
	assert.strictEqual(stkInputValidate(pd, '12').ok, false);
	assert.strictEqual(stkInputValidate(pd, '12345678901234567').ok, false);
	assert.strictEqual(stkInputValidate(pd, 'abcd').ok, false);
	// hidden entry allows only the digits set even without digits_only
	assert.strictEqual(stkInputValidate({ type: 'get_input', hidden: true }, 'ab').ok, false);
	// GET INKEY: exactly one character, digits-only honours * # +
	assert.strictEqual(stkInputValidate({ type: 'get_inkey' }, 'a').ok, true);
	assert.strictEqual(stkInputValidate({ type: 'get_inkey' }, 'ab').ok, false);
	assert.strictEqual(stkInputValidate({ type: 'get_inkey', digits_only: true }, '*').ok, true);
	assert.strictEqual(stkInputValidate({ type: 'get_inkey', digits_only: true }, 'a').ok, false);
});
