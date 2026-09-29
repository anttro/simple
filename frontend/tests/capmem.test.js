const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractBlock(startMarker, endMarker) {
	const start = html.indexOf(startMarker);
	const end = html.indexOf(endMarker, start);
	if (start < 0 || end < 0) throw new Error('block not found: ' + startMarker);
	return html.slice(start, end);
}

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

// Rewrite the top-level const so it leaks out of sloppy-mode eval.
eval(extractBlock('const _capAnalysis = {', 'function capFileKey').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'capFileKey'));
eval(extractFunc(html, 'capGateOk'));
eval(extractBlock('const JC_AID_NAMES = {', 'const JC_AID_RIDS = {').replace(/^const /gm, 'var '));
eval(extractBlock('const JC_AID_RIDS = {', '// Normalise an AID').replace(/^const /gm, 'var '));
eval(extractBlock('const JC_FRAMEWORK_SDK = {', '// ===== TLV parsers for GET STATUS').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'jcAidNorm'));
eval(extractFunc(html, 'jcAidName'));
eval(extractFunc(html, 'jcAidFamily'));
eval(extractFunc(html, 'capMemBytes'));
eval(extractFunc(html, 'capQuotaText'));
eval(extractFunc(html, 'capMemHtml'));
eval(extractFunc(html, 'capAnalyzeFile', true));
// The RAM CAP analysis also applies the detected toolkit mode and prefills
// the import-probe field (DOM-bound; their own logic is covered in
// ram.test.js) - stub them for these state tests.
globalThis.ramApplyCapToolkitMode = () => {};
globalThis.ramProbeFill = () => {};

globalThis.esc = s => s;
globalThis.t = s => s;
globalThis.capRenderAnalysis = () => {};
globalThis.pysimApplyAvailability = () => {};

function resetState() {
	_capAnalysis.ram = { key: null, status: 'idle', memory: null, error: null, token: 0 };
	_capAnalysis.scripts = { key: null, status: 'idle', memory: null, error: null, token: 0 };
}

function memFixture() {
	return {
		cap_version: '2.1',
		package_aid: 'A0000000620101',
		package_version: '1.0',
		package_name: null,
		flags: { raw: 4, int: false, export: false, applet: true },
		applets: ['A0000000620101'],
		imports: [{ aid: 'A0000000620101', minor: 0, major: 1, refs: 6 }],
		components: [{ name: 'Header', size: 20 }, { name: 'Method', size: 2800 },
		             { name: 'ConstantPool', size: 180 }],
		code: { method_component: 2048, load_file: 3000 },
		nvram: { static_image: 12, array_init: 4, install_objects: 100, header_overhead: 12, ref_storage: 8, total: 136, runtime: 0, requirement: 3136 },
		ram: { transient_arrays: 16, runtime_transient: 0, peak_frame: 8, total: 24 },
		suggested: { c6: 3000, c7: 272, c8: 136 },
		warnings: [],
	};
}

test('capMemBytes formats bytes and kilobytes', () => {
	assert.strictEqual(capMemBytes(0), '0 B');
	assert.strictEqual(capMemBytes(1023), '1023 B');
	assert.strictEqual(capMemBytes(1024), '1.0 kB');
	assert.strictEqual(capMemBytes(1587), '1.5 kB');
	assert.strictEqual(capMemBytes(null), '0 B');
});

test('capMemHtml renders the grouped report: package, requires, memory, components, warnings', () => {
	const mem = memFixture();
	mem.warnings = ['method class[0].token[1]: unknown opcode 0xAA, scan stopped'];
	const out = capMemHtml(mem);
	// the border legend titles the box now; the body starts with the sections
	assert.ok(!out.includes('CAP requirements (estimate)'), out);
	// grouped, labelled sections (always visible except components/notes)
	assert.ok(out.includes('>Package</div>'), out);
	assert.ok(out.includes('>Requires (1)</div>'), out);
	assert.ok(out.includes('>Memory</div>'), out);
	assert.ok(out.includes('>Components (3)'), out);
	// the static notes moved to the help; only scan warnings stay in the box
	assert.ok(!out.includes('>Notes'), out);
	assert.ok(!out.includes('Table 11-48'), out);
	assert.ok(out.includes('unknown opcode 0xAA'), out);
	// memory: the C6+C8-style total once, with indented children
	assert.ok(out.includes('NVRAM requirement'), out);
	assert.ok(out.includes('>3.1 kB</td>'), out);
	assert.ok(out.includes('code image + data'), out);
	assert.ok(out.includes('>2.9 kB</td>'), out);
	assert.ok(out.includes('Method.cap 2.0 kB'), out);
	assert.ok(out.includes('other components 952 B'), out);
	assert.ok(out.includes('>136 B</td>'), out);
	assert.ok(out.includes('static image 12 B'), out);
	assert.ok(out.includes('reference storage 8 B'), out);
	assert.ok(out.includes('>24 B</td>'), out);
	assert.ok(out.includes('peak method frame 8 B'), out);
	assert.ok(out.includes('C6=3000 B (0x0BB8) \u00b7 C7=272 B (0x0110) \u00b7 C8=136 B (0x0088)'), out);
	// requires: library name, AID, minimum version, family and refs
	const req = out.slice(out.indexOf('Requires (1)'), out.indexOf('Memory</div>'));
	assert.ok(req.includes('>AID</td>'), req);
	assert.ok(req.includes('>javacard.framework</td>'), req);
	assert.ok(req.includes('>A0000000620101</td>'), req);
	assert.ok(req.includes('>\u2265 1.0</td>'), req);
	assert.ok(req.includes('>Oracle JavaCard API</td>'), req);
	assert.ok(req.includes('>6</td>'), req);
	// package identity + platform requirement derived from the framework version
	assert.ok(out.includes('A0000000620101') && out.includes('>v1.0</td>'), out);
	assert.ok(out.includes('applet package'), out);
	assert.ok(out.includes('(javacard.framework)</span>'), out);
	assert.ok(out.includes('>Platform</td>'), out);
	assert.ok(out.includes('Java Card \u2265 2.1.1/2.1.2 \u00b7 CAP format 2.1'), out);
	// components in load-file order with sizes, shares and the total row
	assert.ok(out.includes('>Header</td>') && out.includes('>20 B</td>') && out.includes('>1%</td>'), out);
	assert.ok(out.includes('>Method</td>') && out.includes('>2.7 kB</td>') && out.includes('>93%</td>'), out);
	assert.ok(out.includes('>ConstantPool</td>') && out.includes('>180 B</td>') && out.includes('>6%</td>'), out);
	assert.ok(out.includes('Load file (total)') && out.includes('>100%</td>'), out);
	const c1 = out.indexOf('Header</td>'), c2 = out.indexOf('Method</td>'), c3 = out.indexOf('ConstantPool</td>');
	assert.ok(c1 >= 0 && c1 < c2 && c2 < c3, 'components keep the load-file order');
	assert.strictEqual(capMemHtml(null), '');
});

test('capMemHtml shows the server platform requirement and int support', () => {
	const mem = memFixture();
	mem.requires_java_card = '2.2.2';
	mem.requires_framework = '1.3';
	mem.needs_int = true;
	const out = capMemHtml(mem);
	assert.ok(out.includes('Java Card \u2265 2.2.2 \u00b7 CAP format 2.1 \u00b7 int support'), out);
	// the server value wins over the local fallback table (fixture import 1.0)
	assert.ok(!out.includes('2.1.1/2.1.2'), out);
});

test('capMemHtml names the ETSI release the UICC API imports come from', () => {
	const mem = memFixture();
	mem.requires_java_card = '2.2.2';
	mem.requires_etsi_release = 'REL-7';
	mem.requires_etsi_basis = 'uicc.toolkit 1.4';
	const out = capMemHtml(mem);
	assert.ok(out.includes('Java Card \u2265 2.2.2 \u00b7 ETSI REL-7 (uicc.toolkit 1.4) \u00b7 CAP format 2.1'), out);
	// a CAP without uicc.toolkit (or an unmapped version) shows no ETSI part
	const noEtsi = capMemHtml(memFixture());
	assert.ok(!noEtsi.includes('ETSI'), noEtsi);
});

test('capMemHtml falls back to the raw framework version outside the corpus', () => {
	const mem = memFixture();
	mem.imports = [{ aid: 'A0000000620101', minor: 0, major: 7, refs: 6 }];
	const out = capMemHtml(mem);
	assert.ok(out.includes('javacard.framework \u2265 7.0 \u00b7 CAP format 2.1'), out);
	assert.ok(!out.includes('Java Card'), out);
});

test('capMemHtml skips the import section on a response without imports', () => {
	const mem = memFixture();
	delete mem.imports;
	mem.code = { method_component: 2048 };
	delete mem.nvram.requirement;
	const out = capMemHtml(mem);
	assert.ok(!out.includes('>Requires'), out);
	assert.ok(!out.includes('Java Card'), out);          // no framework import -> no SDK hint
	assert.ok(out.includes('CAP format 2.1'), out);
	assert.ok(out.includes('NVRAM requirement'), out);
	assert.ok(out.includes('>2.1 kB</td>'), out);
});

test('capMemHtml shows a dash for an unresolved import name (the AID column carries it)', () => {
	const mem = memFixture();
	mem.imports = [{ aid: 'A1130001180001', minor: 0, major: 1, refs: 2 }];
	const out = capMemHtml(mem);
	const req = out.slice(out.indexOf('Requires (1)'), out.indexOf('Memory</div>'));
	assert.ok(req.includes('>\u2014</td>'), req);         // no resolved name: a dash
	assert.ok(req.includes('>A1130001180001</td>'), req); // the AID column carries it
	assert.ok(req.includes('>\u2265 1.0</td>'), req);
	assert.ok(req.includes('>2</td>'), req);              // refs
	assert.ok(!out.includes('Java Card'), out);           // no framework import
	assert.ok(out.includes('CAP format 2.1'), out);
});

test('capMemHtml falls back to the bytecode size on older server responses', () => {
	const mem = memFixture();
	delete mem.code.load_file;
	delete mem.nvram.requirement;
	const out = capMemHtml(mem);
	assert.ok(out.includes('NVRAM requirement'), out);
	assert.ok(out.includes('>2.1 kB</td>'), out);
});

test('capMemHtml drops the components section without a component list', () => {
	const mem = memFixture();
	delete mem.components;
	const out = capMemHtml(mem);
	assert.ok(!out.includes('>Components'), out);
	assert.ok(out.includes('>Memory</div>'), out);
});

test('capQuotaText renders decimal bytes with the hex coding', () => {
	const txt = capQuotaText({ c6: 27246, c7: 270, c8: 5143 });
	assert.strictEqual(txt, 'C6=27246 B (0x6A6E) \u00b7 C7=270 B (0x010E) \u00b7 C8=5143 B (0x1417)');
	assert.strictEqual(capQuotaText({}), 'C6=0 B (0x0000) \u00b7 C7=0 B (0x0000) \u00b7 C8=0 B (0x0000)');
});

test('capGateOk gates the RAM install op and the scripts form', () => {
	resetState();
	globalThis.document = { getElementById: id => (id === 'ram-op' ? { value: 'install-cap' } : null) };
	assert.strictEqual(capGateOk('ram'), false);
	_capAnalysis.ram.status = 'ok';
	assert.strictEqual(capGateOk('ram'), true);
	_capAnalysis.scripts.status = 'error';
	assert.strictEqual(capGateOk('scripts'), false);
	// other RAM ops (explore/delete) are not gated
	globalThis.document = { getElementById: id => (id === 'ram-op' ? { value: 'explore' } : null) };
	assert.strictEqual(capGateOk('ram'), true);
});

test('capAnalyzeFile stores the estimate and ignores stale responses', async () => {
	resetState();
	const pending = [];
	globalThis.ramReadFileHex = async f => f.name + 'hex';
	globalThis.pysimFetch = (url, body) => new Promise(resolve => pending.push(resolve));
	const f1 = { name: 'a.cap', size: 1, lastModified: 1 };
	const f2 = { name: 'b.cap', size: 2, lastModified: 2 };
	const p1 = capAnalyzeFile(f1, 'ram');
	const p2 = capAnalyzeFile(f2, 'ram');
	assert.strictEqual(_capAnalysis.ram.status, 'analyzing');
	// let both file reads settle so the fetches are in flight
	for (let i = 0; i < 10 && pending.length < 2; i++) await Promise.resolve();
	// resolve the first (stale) response after the second selection
	pending[0]({ ok: true, memory: { code: { method_component: 111 } } });
	pending[1]({ ok: true, memory: { code: { method_component: 222 } } });
	await Promise.all([p1, p2]);
	assert.strictEqual(_capAnalysis.ram.status, 'ok');
	assert.strictEqual(_capAnalysis.ram.memory.code.method_component, 222);
	assert.strictEqual(_capAnalysis.ram.key, capFileKey(f2));
	globalThis.pysimFetch = undefined;
});

test('capAnalyzeFile records server and network failures', async () => {
	resetState();
	globalThis.ramReadFileHex = async () => '00';
	globalThis.pysimFetch = async () => ({ ok: false, error: 'cap parse failed: File is not a zip file' });
	await capAnalyzeFile({ name: 'bad.cap', size: 1, lastModified: 1 }, 'scripts');
	assert.strictEqual(_capAnalysis.scripts.status, 'error');
	assert.ok(_capAnalysis.scripts.error.includes('not a zip file'), _capAnalysis.scripts.error);
	globalThis.pysimFetch = async () => { throw new Error('boom'); };
	await capAnalyzeFile({ name: 'net.cap', size: 1, lastModified: 1 }, 'scripts');
	assert.strictEqual(_capAnalysis.scripts.status, 'error');
	assert.strictEqual(_capAnalysis.scripts.error, 'boom');
});

test('the CAP inputs and their action buttons are wired', () => {
	assert.match(html, /id="ram-cap-file"[^>]*onchange="ramCapFileChanged\(\)"/);
	assert.match(html, /id="scripts-cap-file"[^>]*onchange="scriptsCapFileChanged\(\)"/);
	assert.match(html, /data-cap-gate="ram"/);
	assert.match(html, /data-cap-gate="scripts"/);
	assert.match(html, /id="ram-cap-info"/);
	assert.match(html, /id="scripts-cap-info"/);
	assert.match(html, /\/api\/cap-info/);
	// the report lives in a bordered fieldset with the title in the border
	// (the simulator/eSIM pattern); the JS fills the body div
	assert.match(html, /<fieldset id="ram-cap-info"[^>]*>\s*<legend[^>]*data-l10n="CAP details">/);
	assert.match(html, /<fieldset id="scripts-cap-info"[^>]*>\s*<legend[^>]*data-l10n="CAP details">/);
	assert.match(html, /id="ram-cap-body"/);
	assert.match(html, /id="scripts-cap-body"/);
});

test('capRenderAnalysis toggles the border box and fills its body', () => {
	eval(extractFunc(html, 'capRenderAnalysis'));
	const box = { hidden: true, classList: {
		add() { box.hidden = true; },
		remove() { box.hidden = false; },
	} };
	const body = { innerHTML: '' };
	globalThis.document = { getElementById: id => ({
		'ram-cap-info': box, 'ram-cap-body': body,
	}[id] || null) };
	// idle: hidden and empty
	_capAnalysis.ram = { key: null, status: 'idle', memory: null, error: null, token: 0 };
	capRenderAnalysis('ram');
	assert.ok(box.hidden && body.innerHTML === '');
	// a finished analysis: shown, body carries the report
	_capAnalysis.ram = { key: null, status: 'ok', memory: memFixture(), error: null, token: 0 };
	capRenderAnalysis('ram');
	assert.ok(!box.hidden, 'the box is shown');
	assert.ok(body.innerHTML.includes('>Requires (1)</div>'), body.innerHTML);
	// a failure: shown with the error and a retry
	_capAnalysis.ram = { key: null, status: 'error', memory: null, error: 'bad zip', token: 0 };
	capRenderAnalysis('ram');
	assert.ok(!box.hidden && body.innerHTML.includes('bad zip') && body.innerHTML.includes('Retry'), body.innerHTML);
	// analyzing: shown with the progress line
	_capAnalysis.ram = { key: null, status: 'analyzing', memory: null, error: null, token: 0 };
	capRenderAnalysis('ram');
	assert.ok(body.innerHTML.includes('Analyzing CAP file...'), body.innerHTML);
});
