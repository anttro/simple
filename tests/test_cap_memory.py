"""Tests for the bundled CAP memory analyzer (capmem) and /api/cap-info helper."""

import io
import struct
import unittest
import zipfile

from pysim_simple_server import capmem
from pysim_simple_server.server import _cap_info_body


# ─── synthetic CAP builder ───────────────────────────────────────────────

def _u1(v):
    return bytes([v])


def _u2(v):
    return struct.pack('>H', v)


def _u4(v):
    return struct.pack('>I', v)


def _component(tag, payload):
    return bytes([tag]) + _u2(len(payload) + 3) + payload


def _header(aid):
    # magic, cap minor/major, flags, package minor/major, aid (LV)
    return _component(0x01, _u4(0xDECAFFED) + bytes([1, 2, 0, 1, 2]) + _u1(len(aid)) + aid)


def _applet(aid, install_offset):
    return _component(0x03, _u1(1) + _u1(len(aid)) + aid + _u2(install_offset))


def _import(packages):
    payload = _u1(len(packages))
    for minor, major, aid in packages:
        payload += bytes([minor, major, len(aid)]) + aid
    return _component(0x04, payload)


def _cp_class_ref_internal(internal_ref):
    # tag 1: ClassRef (u2) + padding (u1)
    return _u1(1) + _u2(internal_ref) + _u1(0)


def _cp_static_method_external(pkg_token, class_token, token):
    # tag 6: StaticMethodRef; first byte != 0 -> external pkg/class/token
    return _u1(6) + bytes([pkg_token, class_token, token])


def _constant_pool(entries):
    return _component(0x05, _u2(len(entries)) + b''.join(entries))


def _class_record(instance_size, ref_count=0, super_ref=0):
    # flags (bit7=0 class, no interfaces, no remote), super ref, instance size,
    # first ref token, ref count, public/package method table base+count
    p = _u1(0) + _u2(super_ref) + _u1(instance_size) + _u1(0) + _u1(ref_count)
    p += _u1(0) + _u1(0) + _u1(0) + _u1(0)
    return p


def _class_component(records):
    return _component(0x06, b''.join(records))


def _descriptor(class_token, this_class_ref, methods, fields=()):
    payload = _u1(1)
    payload += _u1(class_token) + _u1(0) + _u2(this_class_ref) + _u1(0)
    payload += _u2(len(fields)) + _u2(len(methods))
    for token, access_flags, static_ref, type_offset in fields:
        payload += _u1(token) + _u1(access_flags)
        if access_flags & 0x08:
            payload += bytes([static_ref])
        else:
            payload += _u2(0) + _u1(0)
        payload += _u2(type_offset)
    for token, access_flags, offset, bytecode_count in methods:
        payload += _u1(token) + _u1(access_flags) + _u2(offset) + _u2(0)
        payload += _u2(bytecode_count) + _u2(0) + _u2(0)
    payload += _u2(0)   # constant_pool_count (no type descriptors)
    return _component(0x0b, payload)


def _method_component(bytecode):
    # handler_count = 0; method offsets are relative to the bytecode area
    # start (index 0 = handler count byte), so the first method sits at 1.
    return _component(0x07, _u1(0) + bytecode)


def _method_header(max_stack, nargs, max_locals):
    return bytes([max_stack & 0x0F, ((nargs & 0x0F) << 4) | (max_locals & 0x0F)])


def _static_field(image_size, ref_count, array_inits=()):
    payload = _u2(image_size) + _u2(ref_count) + _u2(len(array_inits))
    for element_type, values in array_inits:
        payload += _u1(element_type) + _u2(len(values)) + bytes(values)
    payload += _u2(1) + _u2(0)
    return _component(0x08, payload)


def build_cap(header_aid=b'\x01\x02\x03\x04\x05', applet_aid=b'\x01\x02\x03\x04\x05',
              imports=(), cp_entries=(), classes=(), descriptor=None, method_bytecode=None,
              static=None):
    components = {'Header': _header(header_aid), 'Applet': _applet(applet_aid, 1)}
    if imports:
        components['Import'] = _import(imports)
    if cp_entries:
        components['ConstantPool'] = _constant_pool(cp_entries)
    if classes:
        components['Class'] = _class_component(classes)
    if descriptor is not None:
        components['Descriptor'] = descriptor
    if method_bytecode is not None:
        components['Method'] = _method_component(method_bytecode)
    if static is not None:
        components['StaticField'] = static
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in components.items():
            z.writestr('pkg/%s.cap' % name, data)
    return buf.getvalue()


# A CAP with one class (instance size 6) and one install() method that:
#   new class[cp0]              -> 6 B persistent
#   newarray byte[10]           -> 10 B persistent
#   JCSystem.makeTransientByteArray(16, CLEAR_ON_RESET) -> 16 B RAM
JAVACARD_FRAMEWORK = bytes.fromhex('A0000000620101')


def rich_cap(with_static=True):
    body = (b'\x8f\x00\x00'            # new cp0 (class ref -> instance size 6)
            b'\x10\x0a'                # bspush 10
            b'\x90\x0b'                # newarray byte[10]
            b'\x11\x00\x10'            # sspush 16
            b'\x10\x01'                # bpush 1 (clearOnReset)
            b'\x8d\x00\x01'            # invokestatic cp1 (makeTransientByteArray)
            b'\x7a')                   # return
    bytecode = _method_header(2, 0, 0) + body
    return build_cap(
        imports=[(1, 2, JAVACARD_FRAMEWORK)],
        cp_entries=[_cp_class_ref_internal(1), _cp_static_method_external(0x80, 8, 13)],
        classes=[_class_record(instance_size=6, ref_count=2)],
        descriptor=_descriptor(0, 1, [(0, 0, 1, len(body))]),
        method_bytecode=bytecode,
        static=_static_field(8, 2, [(0x0C, [0, 1, 0, 2])]) if with_static else None,
    )


class TestCapAnalyzer(unittest.TestCase):
    def test_minimal_cap_reports_zero_code_and_applet_overhead(self):
        report, memory = capmem.analyze_bytes(build_cap())
        self.assertEqual(report['package_aid'], '0102030405')
        self.assertEqual(report['applet_count'], 1)
        self.assertEqual(report['method_component_size'], 0)
        # one applet instance -> one 6-byte object header, nothing else
        self.assertEqual(memory['nvram_object_header_overhead'], 6)
        self.assertEqual(memory['nvram_with_ref_storage'], 6)
        self.assertEqual(memory['ram_transient_arrays'], 0)

    def test_rich_cap_splits_nvram_and_ram(self):
        report, memory = capmem.analyze_bytes(rich_cap())
        # new (6) + newarray (10) are install-time persistent allocations
        self.assertEqual(memory['nvram_persistent_objects'], 16)
        # static image 8 + static array init 4
        self.assertEqual(memory['nvram_static_image'], 8)
        self.assertEqual(memory['nvram_array_init'], 4)
        # 4 objects: static array + 2 install allocations + applet instance
        self.assertEqual(memory['nvram_object_count'], 4)
        self.assertEqual(memory['nvram_object_header_overhead'], 24)
        # 2 references x 2 bytes
        self.assertEqual(memory['reference_storage'], 4)
        self.assertEqual(memory['nvram_with_ref_storage'], 8 + 4 + 16 + 24 + 4)
        # makeTransientByteArray(16) is the only transient allocation
        self.assertEqual(memory['ram_transient_arrays'], 16)
        # peak frame: max_stack 2 -> 4 B, no locals/args
        self.assertEqual(memory['ram_frame_bytes'], 4)
        # code size = Method.cap bytecode area (handler count + header + body)
        self.assertEqual(report['method_component_size'], 1 + 2 + len(b'\x8f\x00\x00\x10\x0a\x90\x0b\x11\x00\x10\x10\x01\x8d\x00\x01\x7a'))

    def test_memory_json_shape_and_suggested_quotas(self):
        report, memory = capmem.analyze_bytes(rich_cap())
        info = capmem.memory_json(report, memory)
        self.assertEqual(info['package_aid'], '0102030405')
        self.assertEqual(info['code']['method_component'], report['method_component_size'])
        self.assertEqual(info['nvram']['total'], memory['nvram_with_ref_storage'])
        self.assertEqual(info['ram']['total'],
                         memory['ram_transient_arrays'] + memory['runtime_transient_bytes'] + memory['ram_frame_bytes'])
        self.assertEqual(info['suggested']['c6'], report['method_component_size'])
        self.assertEqual(info['suggested']['c7'], memory['ram_transient_arrays'] + 256)
        self.assertEqual(info['suggested']['c8'], memory['nvram_with_ref_storage'])
        self.assertEqual(info['warnings'], [])
        self.assertIn('applets', info)

    def test_unknown_opcode_stops_the_scan_with_a_warning(self):
        bytecode = _method_header(1, 0, 0) + b'\xee'   # 0xEE is not a JC 2.1 opcode
        cap = build_cap(classes=[_class_record(instance_size=4)],
                        descriptor=_descriptor(0, 1, [(0, 0, 1, 1)]),
                        method_bytecode=bytecode)
        report, _ = capmem.analyze_bytes(cap)
        self.assertEqual(len(report['warnings']), 1)
        self.assertIn('unknown opcode 0xEE', report['warnings'][0])
        info = capmem.memory_json(*capmem.analyze_bytes(cap))
        self.assertEqual(len(info['warnings']), 1)

    def test_corrupt_input_raises(self):
        with self.assertRaises(Exception):
            capmem.analyze_bytes(b'not a zip at all')


class TestCapInfoBody(unittest.TestCase):
    def test_ok_response_carries_aids_and_estimate(self):
        resp = _cap_info_body({'cap_hex': rich_cap().hex()})
        self.assertTrue(resp['ok'], resp)
        self.assertEqual(resp['load_file_aid'], '0102030405')
        self.assertEqual(resp['module_aid'], '0102030405')
        self.assertTrue(resp['load_file_bytes'] > 0)
        self.assertEqual(resp['memory']['package_aid'], '0102030405')
        self.assertTrue(resp['memory']['nvram']['total'] > 0)

    def test_bad_hex_and_corrupt_archives_are_rejected(self):
        self.assertFalse(_cap_info_body({})['ok'])
        bad = _cap_info_body({'cap_hex': '00'})
        self.assertFalse(bad['ok'])
        self.assertIn('cap parse failed', bad['error'])

    def test_structural_but_unanalyzable_archive_reports_analysis_error(self):
        # _cap_parse accepts Header+Applet; a broken ConstantPool component
        # (unknown tag) fails in the analyzer instead.
        cap = build_cap(cp_entries=[b'\x7f\x00\x00'])   # unknown CP tag 0x7f
        resp = _cap_info_body({'cap_hex': cap.hex()})
        self.assertFalse(resp['ok'])
        self.assertIn('cap analysis failed', resp['error'])


if __name__ == '__main__':
    unittest.main()
