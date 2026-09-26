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
eval(extractFunc(html, 'capMemHtml'));
eval(extractFunc(html, 'capAnalyzeFile', true));

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

test('capMemHtml renders the NVRAM requirement, breakdown and warnings', () => {
	const mem = memFixture();
	mem.warnings = ['method class[0].token[1]: unknown opcode 0xAA, scan stopped'];
	const out = capMemHtml(mem);
	assert.ok(out.includes('CAP requirements (estimate)'), out);
	// C6+C8-style total: load file + persistent data
	assert.ok(out.includes('NVRAM requirement ≈ 3.1 kB (code image 2.9 kB + data 136 B)'), out);
	assert.ok(out.includes('RAM (volatile): 24 B'), out);
	assert.ok(out.includes('Code image (load file): 2.9 kB'), out);
	assert.ok(out.includes('bytecode (Method.cap): 2.0 kB'), out);
	assert.ok(out.includes('other components: 952 B'), out);
	assert.ok(out.includes('Persistent data (NVRAM): 136 B'), out);
	assert.ok(out.includes('Static image: 12 B'), out);
	assert.ok(out.includes('Reference storage: 8 B'), out);
	assert.ok(out.includes('Peak method frame: 8 B'), out);
	assert.ok(out.includes('C6=3000 C7=0x0110 C8=0x0088'), out);
	assert.ok(out.includes('Table 11-48'), out);
	assert.ok(out.includes('Estimate only'), out);
	assert.ok(out.includes('unknown opcode 0xAA'), out);
	// the required libraries with the family, reference count and the
	// compiled-against hint derived from the framework version
	assert.ok(out.includes('Requires: javacard.framework \u2265 1.0'), out);
	assert.ok(out.includes('Compiled against: Java Card 2.1.1/2.1.2 \u00b7 CAP format 2.1'), out);
	assert.ok(out.includes('javacard.framework 1.0 — Oracle JavaCard API — 6 refs — A0000000620101'), out);
	assert.ok(out.includes('Package: A0000000620101 v1.0 (applet package)'), out);
	assert.ok(out.includes('Applets: A0000000620101 (javacard.framework)'), out);
	assert.ok(out.includes('Components: Header 20 B (1%) \u00b7 Method 2.7 kB (93%) \u00b7 ConstantPool 180 B (6%)'), out);
	assert.strictEqual(capMemHtml(null), '');
});

test('capMemHtml skips the import section on a response without imports', () => {
	const mem = memFixture();
	delete mem.imports;
	mem.code = { method_component: 2048 };
	delete mem.nvram.requirement;
	const out = capMemHtml(mem);
	assert.ok(!out.includes('Requires:'), out);
	assert.ok(out.includes('NVRAM requirement \u2248 2.1 kB'), out);
});

test('capMemHtml keeps unknown import AIDs bare and labels the family only when known', () => {
	const mem = memFixture();
	mem.imports = [{ aid: 'A1130001180001', minor: 0, major: 1, refs: 2 }];
	const out = capMemHtml(mem);
	assert.ok(out.includes('Requires: A1130001180001 \u2265 1.0'), out);
	assert.ok(out.includes('A1130001180001 1.0 — 2 refs'), out);
	assert.ok(!out.includes('Compiled against: Java Card'), out);   // no framework import
});

test('capMemHtml falls back to the bytecode size on older server responses', () => {
	const mem = memFixture();
	delete mem.code.load_file;
	delete mem.nvram.requirement;
	const out = capMemHtml(mem);
	assert.ok(out.includes('NVRAM requirement ≈ 2.1 kB (code image 2.0 kB + data 136 B)'), out);
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
});
