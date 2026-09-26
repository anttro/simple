#!/usr/bin/env python3
"""capmem - JavaCard CAP file memory analyzer (bundled).

Estimates NVRAM (persistent) and RAM (volatile) requirements by:
1. Parsing all CAP components (Header, Import, ConstantPool, Class, Descriptor,
   StaticField, Method, Applet)
2. Scanning method bytecode for allocation instructions
3. Computing object sizes using Class component declared sizes

Library API:
    report, memory = analyze_bytes(cap_bytes)   # CAP archive (ZIP) as bytes
    info = memory_json(report, memory)          # compact JSON shape for the API
    print(format_report(report, memory))        # human-readable report

The model's assumptions (2-byte references, 6-byte object header, NVM cell
rounding, runtime allocations excluded) are part of the report notes: the
numbers are an estimate, not a limit.
"""

import struct, os, sys, zipfile, io

# ═══════════════════════════════════════════════════════════════════
# JC 2.1 Opcode Table
# Key: opcode byte, Value: (name, operand_sizes_list)
# ═══════════════════════════════════════════════════════════════════
OPCODES = {
    0x00: ('nop', []),
    0x01: ('aconst_null', []),
    0x02: ('sconst_m1', []),
    0x03: ('sconst_0', []),
    0x04: ('sconst_1', []),
    0x05: ('sconst_2', []),
    0x06: ('sconst_3', []),
    0x07: ('sconst_4', []),
    0x08: ('sconst_5', []),
    0x09: ('iconst_m1', []),
    0x0a: ('iconst_0', []),
    0x0b: ('iconst_1', []),
    0x0c: ('iconst_2', []),
    0x0d: ('iconst_3', []),
    0x0e: ('iconst_4', []),
    0x0f: ('iconst_5', []),
    0x10: ('bspush', [1]),
    0x11: ('sspush', [2]),
    0x12: ('bipush', [1]),
    0x13: ('sipush', [2]),
    0x14: ('iipush', [4]),
    0x15: ('aload', [1]),
    0x16: ('sload', [1]),
    0x17: ('iload', [1]),
    0x18: ('aload_0', []),
    0x19: ('aload_1', []),
    0x1a: ('aload_2', []),
    0x1b: ('aload_3', []),
    0x1c: ('sload_0', []),
    0x1d: ('sload_1', []),
    0x1e: ('sload_2', []),
    0x1f: ('sload_3', []),
    0x20: ('iload_0', []),
    0x21: ('iload_1', []),
    0x22: ('iload_2', []),
    0x23: ('iload_3', []),
    0x24: ('aaload', []),
    0x25: ('baload', []),
    0x26: ('saload', []),
    0x27: ('iaload', []),
    0x28: ('astore', [1]),
    0x29: ('sstore', [1]),
    0x2a: ('istore', [1]),
    0x2b: ('astore_0', []),
    0x2c: ('astore_1', []),
    0x2d: ('astore_2', []),
    0x2e: ('astore_3', []),
    0x2f: ('sstore_0', []),
    0x30: ('sstore_1', []),
    0x31: ('sstore_2', []),
    0x32: ('sstore_3', []),
    0x33: ('istore_0', []),
    0x34: ('istore_1', []),
    0x35: ('istore_2', []),
    0x36: ('istore_3', []),
    0x37: ('aastore', []),
    0x38: ('bastore', []),
    0x39: ('sastore', []),
    0x3a: ('iastore', []),
    0x3b: ('pop', []),
    0x3c: ('pop2', []),
    0x3d: ('dup', []),
    0x3e: ('dup2', []),
    0x3f: ('dup_x', [1]),
    0x40: ('swap_x', [1]),
    0x41: ('sadd', []),
    0x42: ('iadd', []),
    0x43: ('ssub', []),
    0x44: ('isub', []),
    0x45: ('smul', []),
    0x46: ('imul', []),
    0x47: ('sdiv', []),
    0x48: ('idiv', []),
    0x49: ('srem', []),
    0x4a: ('irem', []),
    0x4b: ('sneg', []),
    0x4c: ('ineg', []),
    0x4d: ('sshl', []),
    0x4e: ('ishl', []),
    0x4f: ('sshr', []),
    0x50: ('ishr', []),
    0x51: ('sushr', []),
    0x52: ('iushr', []),
    0x53: ('sand', []),
    0x54: ('iand', []),
    0x55: ('sor', []),
    0x56: ('ior', []),
    0x57: ('sxor', []),
    0x58: ('ixor', []),
    0x59: ('sinc', [1, 1]),
    0x5a: ('iinc', [1, 1]),
    0x5b: ('s2b', []),
    0x5c: ('s2i', []),
    0x5d: ('i2b', []),
    0x5e: ('i2s', []),
    0x5f: ('icmp', []),
    0x60: ('ifeq', [1]),
    0x61: ('ifne', [1]),
    0x62: ('iflt', [1]),
    0x63: ('ifge', [1]),
    0x64: ('ifgt', [1]),
    0x65: ('ifle', [1]),
    0x66: ('ifnull', [1]),
    0x67: ('ifnonnull', [1]),
    0x68: ('if_acmpeq', [1]),
    0x69: ('if_acmpne', [1]),
    0x6a: ('if_scmpeq', [1]),
    0x6b: ('if_scmpne', [1]),
    0x6c: ('if_scmplt', [1]),
    0x6d: ('if_scmpge', [1]),
    0x6e: ('if_scmpgt', [1]),
    0x6f: ('if_scmple', [1]),
    0x70: ('goto', [1]),
    0x71: ('jsr', [2]),
    0x72: ('ret', [1]),
    0x73: ('stableswitch', [2, 2, 2]),   # + variable pairs
    0x74: ('itableswitch', [2, 2, 2]),    # + variable pairs
    0x75: ('slookupswitch', [2, 2]),      # + variable match/offset pairs
    0x76: ('ilookupswitch', [2, 2]),      # + variable match/offset pairs
    0x77: ('areturn', []),
    0x78: ('sreturn', []),
    0x79: ('ireturn', []),
    0x7a: ('return', []),
    0x7b: ('getstatic_a', [2]),
    0x7c: ('getstatic_b', [2]),
    0x7d: ('getstatic_s', [2]),
    0x7e: ('getstatic_i', [2]),
    0x7f: ('putstatic_a', [2]),
    0x80: ('putstatic_b', [2]),
    0x81: ('putstatic_s', [2]),
    0x82: ('putstatic_i', [2]),
    0x83: ('getfield_a', [1]),
    0x84: ('getfield_b', [1]),
    0x85: ('getfield_s', [1]),
    0x86: ('getfield_i', [1]),
    0x87: ('putfield_a', [1]),
    0x88: ('putfield_b', [1]),
    0x89: ('putfield_s', [1]),
    0x8a: ('putfield_i', [1]),
    0x8b: ('invokevirtual', [2]),
    0x8c: ('invokespecial', [2]),
    0x8d: ('invokestatic', [2]),
    0x8e: ('invokeinterface', [1, 2, 1]),
    0x8f: ('new', [2]),
    0x90: ('newarray', [1]),
    0x91: ('anewarray', [2]),
    0x92: ('arraylength', []),
    0x93: ('athrow', []),
    0x94: ('checkcast', [1, 2]),
    0x95: ('instanceof', [1, 2]),
    0x96: ('sinc_w', [1, 2]),
    0x97: ('iinc_w', [1, 2]),
    0x98: ('ifeq_w', [2]),
    0x99: ('ifne_w', [2]),
    0x9a: ('iflt_w', [2]),
    0x9b: ('ifge_w', [2]),
    0x9c: ('ifgt_w', [2]),
    0x9d: ('ifle_w', [2]),
    0x9e: ('ifnull_w', [2]),
    0x9f: ('ifnonnull_w', [2]),
    0xa0: ('if_acmpeq_w', [2]),
    0xa1: ('if_acmpne_w', [2]),
    0xa2: ('if_scmpeq_w', [2]),
    0xa3: ('if_scmpne_w', [2]),
    0xa4: ('if_scmplt_w', [2]),
    0xa5: ('if_scmpge_w', [2]),
    0xa6: ('if_scmpgt_w', [2]),
    0xa7: ('if_scmple_w', [2]),
    0xa8: ('goto_w', [2]),
    0xa9: ('getfield_a_w', [2]),
    0xaa: ('getfield_b_w', [2]),
    0xab: ('getfield_s_w', [2]),
    0xac: ('getfield_i_w', [2]),
    0xad: ('getfield_a_this', [1]),
    0xae: ('getfield_b_this', [1]),
    0xaf: ('getfield_s_this', [1]),
    0xb0: ('getfield_i_this', [1]),
    0xb1: ('putfield_a_w', [2]),
    0xb2: ('putfield_b_w', [2]),
    0xb3: ('putfield_s_w', [2]),
    0xb4: ('putfield_i_w', [2]),
    0xb5: ('putfield_a_this', [1]),
    0xb6: ('putfield_b_this', [1]),
    0xb7: ('putfield_s_this', [1]),
    0xb8: ('putfield_i_this', [1]),
    0xfe: ('impdep1', []),
    0xff: ('impdep2', []),
}

# array element sizes (for newarray atype) — JCDK 2.1.x converter encoding
ATYP_SIZES = {0x0a: 1, 0x0b: 1, 0x0c: 2, 0x0d: 4,  # JCDK: boolean, byte, short, int
              0x04: 1, 0x08: 1, 0x09: 2, 0x0a: 1}  # JVM-style fallback (boolean,byte,short,int)
ATYP_NAMES = {0x0a: 'boolean[]', 0x0b: 'byte[]', 0x0c: 'short[]', 0x0d: 'int[]',
              0x04: 'boolean[]', 0x08: 'byte[]', 0x09: 'short[]'}

# ═══════════════════════════════════════════════════════════════════
# CAP Component Parsers
# ═══════════════════════════════════════════════════════════════════

class Stream:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read_byte(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def read_u1(self):
        return self.read_byte()

    def read_u2(self):
        b = self.data[self.pos:self.pos+2]
        self.pos += 2
        return struct.unpack('>H', b)[0]

    def read_i1(self):
        v = self.data[self.pos]
        self.pos += 1
        return v if v < 128 else v - 256

    def read_i2(self):
        b = self.data[self.pos:self.pos+2]
        self.pos += 2
        return struct.unpack('>h', b)[0]

    def read_i4(self):
        b = self.data[self.pos:self.pos+4]
        self.pos += 4
        return struct.unpack('>i', b)[0]

    def read_bytes(self, n):
        b = self.data[self.pos:self.pos+n]
        self.pos += n
        return b

    def read_int(self):
        return self.read_i4()

    def available(self):
        return len(self.data) - self.pos


class CapComponent:
    """Base class for a parsed CAP component."""
    def __init__(self, tag, data):
        self.tag = tag
        self.raw_size = len(data)
        self.stream = Stream(data)
        self.stream.read_byte()  # tag
        self.component_size = self.stream.read_u2()


# ──────────────────────── Header ────────────────────────
class Header(CapComponent):
    TAG = 0x01
    def __init__(self, data):
        super().__init__(self.TAG, data)
        self.magic = self.stream.read_int()
        self.minor_version = self.stream.read_u1()  # cap format version (minor)
        self.major_version = self.stream.read_u1()  # cap format version (major)
        self.flags = self.stream.read_u1()
        self.package_minor = self.stream.read_u1()
        self.package_major = self.stream.read_u1()
        aid_len = self.stream.read_u1()
        self.aid = self.stream.read_bytes(aid_len)
        # JC VM spec 2.2.2 6.3: JC 2.2+ headers carry the package name as
        # package_name_info { u1 name_length; u1 name[name_length] } (UTF-8,
        # internal form, e.g. "javacard/framework").  CAP 2.1 files have no
        # bytes left after the AID.
        self.package_name = None
        if self.stream.available() >= 1:
            name_len = self.stream.read_u1()
            if 0 < name_len <= self.stream.available():
                try:
                    self.package_name = self.stream.read_bytes(name_len).decode('utf-8')
                except UnicodeDecodeError:
                    self.package_name = None

    def package_version_str(self):
        return f"{self.package_major}.{self.package_minor}"


# ──────────────────────── Import ────────────────────────
class PackageInfo:
    def __init__(self):
        self.minor = 0
        self.major = 0
        self.aid = b''
        self.aid_hex = ''

class Import(CapComponent):
    TAG = 0x04
    def __init__(self, data):
        super().__init__(self.TAG, data)
        count = self.stream.read_u1()
        self.packages = []
        for _ in range(count):
            p = PackageInfo()
            p.minor = self.stream.read_u1()
            p.major = self.stream.read_u1()
            aid_len = self.stream.read_u1()
            p.aid = self.stream.read_bytes(aid_len)
            p.aid_hex = p.aid.hex().upper()
            self.packages.append(p)


# ──────────────────────── ConstantPool ────────────────────────
class ConstantPoolEntry:
    TAGS = {
        1: 'ClassRef', 2: 'InstanceFieldRef',
        3: 'VirtualMethodRef', 4: 'SuperMethodRef',
        5: 'StaticFieldRef', 6: 'StaticMethodRef',
    }
    def __init__(self, tag):
        self.tag = tag
        self.tag_name = self.TAGS.get(tag, f'Unknown(0x{tag:02x})')


class CPClassRef(ConstantPoolEntry):
    def __init__(self, tag, stream):
        super().__init__(tag)
        self.class_ref = read_class_ref(stream)
        self.padding = stream.read_u1()


class CPStaticFieldRef(ConstantPoolEntry):
    def __init__(self, tag, stream):
        super().__init__(tag)
        self.static_field_ref = read_static_field_ref(stream)


class CPStaticMethodRef(ConstantPoolEntry):
    def __init__(self, tag, stream):
        super().__init__(tag)
        self.static_method_ref = read_static_method_ref(stream)


class CPMethodOrFieldRef(ConstantPoolEntry):
    """InstanceFieldRef, VirtualMethodRef, SuperMethodRef"""
    def __init__(self, tag, stream):
        super().__init__(tag)
        self.class_ref = read_class_ref(stream)
        self.token = stream.read_u1()


class ClassRef:
    def __init__(self):
        self.is_internal = True
        self.internal_ref = 0
        self.package_token = 0
        self.class_token = 0

    def __repr__(self):
        if self.is_internal:
            return f'ClassRef(internal={self.internal_ref})'
        return f'ClassRef(external pkg={self.package_token} class={self.class_token})'


class StaticFieldRef:
    def __init__(self):
        self.is_internal = True
        self.offset = 0
        self.padding = 0
        self.package_token = 0
        self.class_token = 0
        self.token = 0

    def __repr__(self):
        if self.is_internal:
            return f'StaticFieldRef(internal offset=0x{self.offset:x})'
        return f'StaticFieldRef(external pkg={self.package_token} class={self.class_token} token={self.token})'


class StaticMethodRef:
    def __init__(self):
        self.is_internal = True
        self.offset = 0
        self.padding = 0
        self.package_token = 0
        self.class_token = 0
        self.token = 0

    def __repr__(self):
        if self.is_internal:
            return f'StaticMethodRef(internal offset=0x{self.offset:x})'
        return f'StaticMethodRef(external pkg={self.package_token} class={self.class_token} token={self.token})'


def read_class_ref(stream):
    buf = stream.read_u2()
    ref = ClassRef()
    if (buf & 0x8000) == 0:
        ref.is_internal = True
        ref.internal_ref = buf
    else:
        ref.is_internal = False
        ref.class_token = buf & 0xFF
        ref.package_token = (buf >> 8) & 0xFF
    return ref


def read_static_field_ref(stream):
    buf = stream.read_u1()
    ref = StaticFieldRef()
    if (buf & 0x80) == 0:
        ref.is_internal = True
        ref.padding = buf
        ref.offset = stream.read_u2()
    else:
        ref.is_internal = False
        ref.package_token = buf
        ref.class_token = stream.read_u1()
        ref.token = stream.read_u1()
    return ref


def read_static_method_ref(stream):
    buf = stream.read_u1()
    ref = StaticMethodRef()
    if buf == 0:
        ref.is_internal = True
        ref.padding = buf
        ref.offset = stream.read_u2()
    else:
        ref.is_internal = False
        ref.package_token = buf
        ref.class_token = stream.read_u1()
        ref.token = stream.read_u1()
    return ref


class ConstantPool(CapComponent):
    TAG = 0x05
    def __init__(self, data):
        super().__init__(self.TAG, data)
        count = self.stream.read_u2()
        self.entries = []
        for _ in range(count):
            tag = self.stream.read_u1()
            if tag == 1:  # ClassRef
                self.entries.append(CPClassRef(tag, self.stream))
            elif tag in (2, 3, 4):  # InstanceFieldRef, VirtualMethodRef, SuperMethodRef
                self.entries.append(CPMethodOrFieldRef(tag, self.stream))
            elif tag == 5:  # StaticFieldRef
                self.entries.append(CPStaticFieldRef(tag, self.stream))
            elif tag == 6:  # StaticMethodRef
                self.entries.append(CPStaticMethodRef(tag, self.stream))
            else:
                raise ValueError(f"Unknown constant pool tag: {tag}")


# ──────────────────────── Applet ────────────────────────
class AppletInfo:
    def __init__(self):
        self.aid = b''
        self.aid_hex = ''
        self.install_method_offset = 0


class Applet(CapComponent):
    TAG = 0x03
    def __init__(self, data):
        super().__init__(self.TAG, data)
        count = self.stream.read_u1()
        self.applets = []
        for _ in range(count):
            aid_len = self.stream.read_u1()
            aid = self.stream.read_bytes(aid_len)
            info = AppletInfo()
            info.aid = aid
            info.aid_hex = aid.hex().upper()
            info.install_method_offset = self.stream.read_u2()
            self.applets.append(info)


# ──────────────────────── Class ────────────────────────
class ClassInfo:
    def __init__(self):
        self.offset = 0
        self.flags = 0
        self.interface_count = 0
        self.super_class_ref = None
        self.declared_instance_size = 0
        self.first_ref_token = 0
        self.ref_count = 0
        self.public_method_table_base = 0
        self.public_method_table_count = 0
        self.package_method_table_base = 0
        self.package_method_table_count = 0
        self.interfaces = []
        self.has_remote = False


class InterfaceInfo:
    def __init__(self):
        self.offset = 0
        self.flags = 0
        self.remote_interfaces = []


class ClassComponent(CapComponent):
    TAG = 0x06
    def __init__(self, data):
        super().__init__(self.TAG, data)
        self.classes = []
        self.interfaces = []
        self.records = []  # stream order: both classes and interfaces
        while self.stream.available() > 0:
            buf = self.stream.read_u1()
            if (buf & 0x80) != 0:
                # interface: bitfield then interfaceCount × ClassRef (no superClassRef)
                ii = InterfaceInfo()
                ii.offset = self.stream.pos - 1
                ii.flags = buf
                ii.interface_count = buf & 0x0F
                for _ in range(ii.interface_count):
                    ii.remote_interfaces.append(read_class_ref(self.stream))
                self.interfaces.append(ii)
                self.records.append(ii)
            else:
                # class
                ci = ClassInfo()
                ci.offset = self.stream.pos - 1
                ci.flags = buf
                ci.interface_count = buf & 0x0F
                ci.has_remote = (buf & 0x20) != 0
                ci.super_class_ref = read_class_ref(self.stream)
                ci.declared_instance_size = self.stream.read_u1()
                ci.first_ref_token = self.stream.read_u1()
                ci.ref_count = self.stream.read_u1()
                ci.public_method_table_base = self.stream.read_u1()
                ci.public_method_table_count = self.stream.read_u1()
                ci.package_method_table_base = self.stream.read_u1()
                ci.package_method_table_count = self.stream.read_u1()
                # public virtual method table
                pvmt = []
                for _ in range(ci.public_method_table_count):
                    pvmt.append(self.stream.read_u2())
                ci.public_virtual_method_table = pvmt
                # package virtual method table
                pmvt = []
                for _ in range(ci.package_method_table_count):
                    pmvt.append(self.stream.read_u2())
                ci.package_virtual_method_table = pmvt
                # interfaces (each = ClassRef u2 + count u1 + count × u1)
                for _ in range(ci.interface_count):
                    iref = read_class_ref(self.stream)
                    ci.interfaces.append(iref)
                    cnt = self.stream.read_u1()
                    for _ in range(cnt):
                        self.stream.read_u1()
                # remote interfaces info (only if ACC_REMOTE)
                if ci.has_remote:
                    remote_methods_count = self.stream.read_u1()
                    for _ in range(remote_methods_count):
                        # RemoteMethodInfo: nameOffset(u1) sigOffset(u1)
                        self.stream.read_u1()
                        self.stream.read_u1()
                    self.stream.read_u1()  # hashModifierLength
                    hash_len = self.stream.read_u1()
                    self.stream.read_bytes(hash_len)
                    # className info
                    name_len = self.stream.read_u1()
                    self.stream.read_bytes(name_len)
                self.classes.append(ci)
                self.records.append(ci)


# ──────────────────────── Descriptor ────────────────────────
class FieldDescriptor:
    def __init__(self):
        self.token = 0
        self.access_flags = 0
        self.is_static = False
        self.static_field_ref = None  # StaticFieldRef
        self.instance_class_ref = None  # ClassRef
        self.instance_token = 0
        self.type_offset = 0


class MethodDescriptor:
    def __init__(self):
        self.token = 0
        self.access_flags = 0
        self.method_offset = 0  # offset in Method.cap bytecode area
        self.type_offset = 0   # offset in Descriptor type array
        self.bytecode_count = 0
        self.exception_handler_count = 0
        self.exception_handler_index = 0


class ClassDescriptor:
    def __init__(self):
        self.token = 0
        self.access_flags = 0
        self.this_class_ref = None
        self.interface_count = 0
        self.field_count = 0
        self.method_count = 0
        self.fields = []
        self.methods = []


class TypeDescriptorInfo:
    def __init__(self):
        self.constant_pool_count = 0
        self.constant_pool_types = []
        self.type_descriptors = []


class Descriptor(CapComponent):
    TAG = 0x0b
    def __init__(self, data):
        super().__init__(self.TAG, data)
        self.class_count = self.stream.read_u1()
        self.class_descriptors = []
        for _ in range(self.class_count):
            cd = ClassDescriptor()
            cd.token = self.stream.read_u1()
            cd.access_flags = self.stream.read_u1()
            cd.this_class_ref = read_class_ref(self.stream)
            cd.interface_count = self.stream.read_u1()
            cd.field_count = self.stream.read_u2()
            cd.method_count = self.stream.read_u2()
            # interfaces
            for _ in range(cd.interface_count):
                read_class_ref(self.stream)  # consume
            # fields
            for _ in range(cd.field_count):
                fd = FieldDescriptor()
                fd.token = self.stream.read_u1()
                fd.access_flags = self.stream.read_u1()
                fd.is_static = (fd.access_flags & 0x08) != 0
                if fd.is_static:
                    fd.static_field_ref = read_static_field_ref(self.stream)
                else:
                    fd.instance_class_ref = read_class_ref(self.stream)
                    fd.instance_token = self.stream.read_u1()
                fd.type_offset = self.stream.read_u2()
                cd.fields.append(fd)
            # methods
            for _ in range(cd.method_count):
                md = MethodDescriptor()
                md.token = self.stream.read_u1()
                md.access_flags = self.stream.read_u1()
                md.method_offset = self.stream.read_u2()
                md.type_offset = self.stream.read_u2()
                md.bytecode_count = self.stream.read_u2()
                md.exception_handler_count = self.stream.read_u2()
                md.exception_handler_index = self.stream.read_u2()
                cd.methods.append(md)
            self.class_descriptors.append(cd)
        # Type descriptor info (read until end of component)
        self.type_info = TypeDescriptorInfo()
        self.type_info.constant_pool_count = self.stream.read_u2()
        for _ in range(self.type_info.constant_pool_count):
            self.type_info.constant_pool_types.append(self.stream.read_u2())
        # Read TypeDescriptor entries until end of component
        self.type_info.type_descriptors = []
        while self.stream.available() > 0:
            nibble_count = self.stream.read_u1()
            byte_count = (nibble_count + 1) // 2
            self.stream.read_bytes(byte_count)  # consume type bytes


# ──────────────────────── StaticField ────────────────────────
class ArrayInitRecord:
    def __init__(self):
        self.element_type = 0
        self.count = 0
        self.values = []


class StaticFieldComponent(CapComponent):
    TAG = 0x08
    def __init__(self, data):
        super().__init__(self.TAG, data)
        self.image_size = self.stream.read_u2()
        self.reference_count = self.stream.read_u2()
        self.array_init_count = self.stream.read_u2()
        self.array_init_records = []
        total_init_bytes = 0
        for _ in range(self.array_init_count):
            rec = ArrayInitRecord()
            rec.element_type = self.stream.read_u1()
            rec.count = self.stream.read_u2()
            rec.values = list(self.stream.read_bytes(rec.count))
            total_init_bytes += rec.count
            self.array_init_records.append(rec)
        self.total_init_bytes = total_init_bytes
        self.default_value_count = self.stream.read_u2()
        self.non_default_value_count = self.stream.read_u2()


# ──────────────────────── RefLocation ────────────────────────
class RefLocation(CapComponent):
    TAG = 0x09
    def __init__(self, data):
        super().__init__(self.TAG, data)
        # Not needed for allocation analysis — consume safely.
        self.raw_payload = self.stream.data[self.stream.pos:]


# ──────────────────────── ExceptionHandler ────────────────────────
class ExceptionHandler:
    def __init__(self):
        self.start = 0
        self.end = 0
        self.handler = 0
        self.catch_type = 0


# ──────────────────────── Method ────────────────────────
class MethodHeader:
    def __init__(self):
        self.flags = 0
        self.max_stack = 0
        self.nargs = 0
        self.max_locals = 0
        self.is_extended = False


class Method(CapComponent):
    TAG = 0x07
    def __init__(self, data):
        super().__init__(self.TAG, data)
        handler_count = self.stream.read_u1()
        self.handler_count = handler_count
        self.exception_handlers = []
        for _ in range(handler_count):
            eh = ExceptionHandler()
            eh.start = self.stream.read_u2()
            eh.end = self.stream.read_u2()
            eh.handler = self.stream.read_u2()
            eh.catch_type = self.stream.read_u1()
            self.exception_handlers.append(eh)
        # Method offsets are relative to the START OF CONTENT (index 0 = tag+size
        # boundary, i.e. the handler count byte at raw offset 3).
        self.bytecode_data = self.stream.data[3:]


# ═══════════════════════════════════════════════════════════════════
# CAP Reader
# ═══════════════════════════════════════════════════════════════════
class CAP:
    def __init__(self, data):
        """`data` is the .cap archive (a ZIP) as bytes."""
        self.data = data
        self.components = {}
        self.files = []          # [(component name, byte size)] in ZIP order
        self._read_cap()

    def _read_cap(self):
        # ZIP archive of nested *.cap components
        with zipfile.ZipFile(io.BytesIO(self.data), 'r') as z:
            for name in z.namelist():
                if name.endswith('.cap') and not name.lower().endswith('.capx'):
                    data = z.read(name)
                    self.files.append((name.split('/')[-1][:-4], len(data)))
                    self._add_component(data)

    def _add_component(self, data):
        tag = data[0]
        if tag == 0x01:
            self.components['header'] = Header(data)
        elif tag == 0x03:
            self.components['applet'] = Applet(data)
        elif tag == 0x04:
            self.components['import'] = Import(data)
        elif tag == 0x05:
            self.components['constant_pool'] = ConstantPool(data)
        elif tag == 0x06:
            self.components['class'] = ClassComponent(data)
        elif tag == 0x07:
            self.components['method'] = Method(data)
        elif tag == 0x08:
            self.components['static_field'] = StaticFieldComponent(data)
        elif tag == 0x09:
            self.components['ref_location'] = RefLocation(data)
        elif tag == 0x0b:
            self.components['descriptor'] = Descriptor(data)

    def has(self, name):
        return name in self.components


# ═══════════════════════════════════════════════════════════════════
# Bytecode Instruction Disassembler (linear scan, no control flow)
# ═══════════════════════════════════════════════════════════════════

class Instruction:
    def __init__(self, offset, opcode, name, operands, raw_bytes):
        self.offset = offset
        self.opcode = opcode
        self.name = name
        self.operands = operands  # list of operand values (ints)
        self.raw = raw_bytes
        self.size = len(raw_bytes)

    def __repr__(self):
        ops = ', '.join(f'0x{o:x}' if isinstance(o, int) else str(o) for o in self.operands)
        return f'[{self.offset:04x}] {self.name} {ops}'


def disassemble_region(bytecode_data, start_offset=0):
    """Linear disassembly of a bytecode region. Returns list of Instructions."""
    instructions = []
    pos = 0
    while pos < len(bytecode_data):
        opcode = bytecode_data[pos]
        entry = OPCODES.get(opcode)
        if entry is None:
            # Unknown opcode — stop disassembly
            break
        name, operand_sizes = entry
        operands = []
        raw = [opcode]
        pos += 1
        for size in operand_sizes:
            val_bytes = bytecode_data[pos:pos+size]
            if len(val_bytes) < size:
                break
            if size == 1:
                val = val_bytes[0]
            elif size == 2:
                val = struct.unpack('>H', val_bytes)[0]
            elif size == 4:
                val = struct.unpack('>i', val_bytes)[0]
            else:
                val = 0
            operands.append(val)
            raw.extend(val_bytes)
            pos += size

        # Handle variable-length switch instructions
        if opcode in (0x73, 0x74):  # stableswitch, itableswitch
            low = operands[1] if len(operands) > 1 else 0
            high = operands[2] if len(operands) > 2 else 0
            n_entries = high - low + 1
            if n_entries > 0 and n_entries < 10000:
                for _ in range(n_entries):
                    val_bytes = bytecode_data[pos:pos+2]
                    if len(val_bytes) < 2:
                        break
                    raw.extend(val_bytes)
                    pos += 2
        elif opcode in (0x75, 0x76):  # slookupswitch, ilookupswitch
            npair = operands[1] if len(operands) > 1 else 0
            if 0 < npair < 10000:
                for _ in range(npair):
                    # match (u2) + offset (u2)
                    val_bytes = bytecode_data[pos:pos+4]
                    if len(val_bytes) < 4:
                        break
                    raw.extend(val_bytes)
                    pos += 4

        instructions.append(Instruction(start_offset + pos - len(raw), opcode, name, operands, raw))

    return instructions


# ═══════════════════════════════════════════════════════════════════
# Constant Pool Resolver
# ═══════════════════════════════════════════════════════════════════

class ConstantPoolResolver:
    """Resolves constant pool references to human-readable strings."""
    def __init__(self, cap):
        self.cap = cap
        # Imported packages are addressed by their token (0x80 + index).
        # Store them keyed by full 0x00-0xff token space for CP references.
        self.import_packages = {}
        if cap.has('import'):
            for i, p in enumerate(cap.components['import'].packages):
                self.import_packages[0x80 + i] = p
        self.cp_entries = []
        if cap.has('constant_pool'):
            self.cp_entries = cap.components['constant_pool'].entries
        # Map internal class token (this_class_ref space) → declared_instance_size.
        # Class.cap stream records and Descriptor.cap class descriptors are emitted
        # by the converter in the same order, so they pair positionally.
        self.token_to_inst_size = {}
        if cap.has('descriptor') and cap.has('class'):
            cd = cap.components['descriptor'].class_descriptors
            cc = cap.components['class']
            records = cc.records if hasattr(cc, 'records') and cc.records else cc.classes + cc.interfaces
            for i, d in enumerate(cd):
                r = d.this_class_ref
                if not r.is_internal:
                    continue
                size = 0
                if i < len(records):
                    size = records[i].declared_instance_size if hasattr(records[i], 'declared_instance_size') else 0
                self.token_to_inst_size[r.internal_ref] = size

    def resolve_class_ref(self, ref):
        if ref.is_internal:
            return f'class[{ref.internal_ref}]'
        pkg = self.import_packages.get(ref.package_token)
        pkg_name = pkg.aid_hex if pkg else f'pkg[{ref.package_token}]'
        return f'{pkg_name}:class[{ref.class_token}]'

    def resolve_method_ref_token(self, cp_index):
        """Resolve a constant pool index to a method ref, return (class_desc, token)."""
        if cp_index < len(self.cp_entries):
            entry = self.cp_entries[cp_index]
            if hasattr(entry, 'class_ref') and hasattr(entry, 'token'):
                return (entry.class_ref, entry.token)
        return None

    def resolve_new_instance_size(self, cp_index):
        """Given a 'new' opcode's constant-pool index, return the declared instance
        size (bytes) of the class being instantiated, or 0 if unresolvable."""
        info = self.resolve_new_class(cp_index)
        return info['size'] if info else 0

    def resolve_new_class(self, cp_index):
        """Resolve a 'new' opcode's CP index to (size, external, token) info."""
        if cp_index >= len(self.cp_entries):
            return None
        entry = self.cp_entries[cp_index]
        if not isinstance(entry, CPClassRef):
            return None
        ref = entry.class_ref
        if not ref.is_internal:
            return {'size': 0, 'external': True, 'token': ref.class_token,
                    'pkg': ref.package_token}
        return {'size': self.token_to_inst_size.get(ref.internal_ref, 0),
                'external': False, 'token': ref.internal_ref}

    def is_make_transient_bytearray(self, cp_index):
        """Check if a constant pool method ref is JCSystem.makeTransient*.
        Returns a string key identifying which makeTransient variant, or None."""
        if cp_index >= len(self.cp_entries):
            return None
        entry = self.cp_entries[cp_index]
        if not isinstance(entry, CPStaticMethodRef):
            return None
        ref = entry.static_method_ref
        if ref.is_internal:
            return None
        # javacard.framework (api21): JCSystem class_token = 8
        #   makeTransientBooleanArray = 12, makeTransientByteArray = 13
        #   makeTransientObjectArray = 14, makeTransientShortArray = 15
        if ref.class_token != 8:
            return None
        pkg = self.import_packages.get(ref.package_token)
        if pkg is None:
            return None
        aid_bytes = pkg.aid
        if len(aid_bytes) >= 7 and aid_bytes[:7] == bytes([0xA0, 0x00, 0x00, 0x00, 0x62, 0x01, 0x01]):
            return {12: 'makeTransientBooleanArray',
                    13: 'makeTransientByteArray',
                    14: 'makeTransientObjectArray',
                    15: 'makeTransientShortArray'}.get(ref.token)
        return None


# ═══════════════════════════════════════════════════════════════════
# Method Structure Builder
# ═══════════════════════════════════════════════════════════════════

class MethodStructure:
    def __init__(self):
        self.class_index = 0
        self.method_token = 0
        self.access_flags = 0
        self.max_stack = 0
        self.nargs = 0
        self.max_locals = 0
        self.bytecode_count = 0
        self.is_static = False
        self.is_abstract = False
        self.type_offset = 0
        self.header_size = 0
        self.bytecode_data = b''
        self.start_offset = 0  # offset in Method.cap bytecode area
        self.name_guess = '?'  # heuristic name


def build_methods(cap):
    """Build ordered list of MethodStructure from CAP components."""
    if not cap.has('descriptor') or not cap.has('method'):
        return []

    desc = cap.components['descriptor']
    method_comp = cap.components['method']
    bytecode_data = method_comp.bytecode_data

    methods = []

    for cls in desc.class_descriptors:
        for md in cls.methods:
            ms = MethodStructure()
            ms.class_index = cls.token
            ms.method_token = md.token
            ms.access_flags = md.access_flags
            ms.is_static = (md.access_flags & 0x08) != 0
            ms.is_abstract = (md.access_flags & 0x04) != 0
            ms.bytecode_count = md.bytecode_count
            ms.type_offset = md.type_offset
            ms.start_offset = md.method_offset

            # Parse method header from bytecode area
            if md.bytecode_count > 0 and md.method_offset < len(bytecode_data):
                hdr_pos = md.method_offset
                bitfield = bytecode_data[hdr_pos]
                flags_nibble = (bitfield & 0xF0) >> 4
                if flags_nibble == 8:  # ACC_EXTENDED
                    ms.is_extended = True
                    ms.max_stack = bytecode_data[hdr_pos + 1]
                    ms.nargs = bytecode_data[hdr_pos + 2]
                    ms.max_locals = bytecode_data[hdr_pos + 3]
                    ms.header_size = 4
                elif flags_nibble == 0:  # ACC (normal)
                    ms.max_stack = bitfield & 0x0F
                    hdr_byte2 = bytecode_data[hdr_pos + 1]
                    ms.nargs = (hdr_byte2 & 0xF0) >> 4
                    ms.max_locals = hdr_byte2 & 0x0F
                    ms.header_size = 2
                else:
                    # abstract (4) or unknown — no bytecode
                    ms.header_size = 0
                    ms.bytecode_count = 0

                # Extract bytecode
                bc_start = hdr_pos + ms.header_size
                ms.bytecode_data = bytecode_data[bc_start:bc_start + md.bytecode_count]

            methods.append(ms)

    return methods


# ═══════════════════════════════════════════════════════════════════
# Method Name Heuristics
# ═══════════════════════════════════════════════════════════════════

def guess_method_names(cap, methods):
    """Heuristically assign method names based on static flags, signatures, bytecode patterns."""
    if not cap.has('applet') or not cap.has('descriptor'):
        return

    # Get applet install method offsets
    applet_offsets = set()
    for ai in cap.components['applet'].applets:
        applet_offsets.add(ai.install_method_offset)

    # Get class descriptors
    desc = cap.components['descriptor']
    class_by_token = {}
    for cls in desc.class_descriptors:
        class_by_token[cls.token] = cls

    # For each class, find the <clinit>, <init>, install methods
    for ms in methods:
        cls = class_by_token.get(ms.class_index)
        if cls is None:
            continue

        # <clinit>: static method whose bytecode writes static fields (putstatic)
        if ms.is_static and ms.bytecode_count > 0:
            bc = ms.bytecode_data
            has_putstatic = any(op in (0x7f, 0x80, 0x81, 0x82) for op in bc)  # putstatic_*
            if has_putstatic:
                ms.name_guess = '<clinit>'
            elif ms.bytecode_count < 50 and ms.max_locals == 0:
                ms.name_guess = '<clinit>(trivial)'
            else:
                ms.name_guess = '<static helper>'

        # Constructor <init>: non-static, has invokestatic or invokespecial to super <init>
        if not ms.is_static and ms.bytecode_count > 0:
            bc = ms.bytecode_data
            if b'\x8c' in bc or b'\x8b' in bc:  # invokespecial/invokevirtual
                ms.name_guess = '<init>'

    # Mark install() methods — methods whose offset matches the applet install offsets
    for ms in methods:
        if ms.start_offset in applet_offsets and ms.bytecode_count > 0:
            ms.name_guess = 'install'


# ═══════════════════════════════════════════════════════════════════
# Allocation Scanner
# ═══════════════════════════════════════════════════════════════════

class Allocation:
    def __init__(self, kind, size, context, detail):
        self.kind = kind        # 'new', 'newarray', 'anewarray', 'make_transient_bytearray', 'make_transient_objectarray', 'make_transient_shortarray'
        self.size = size        # byte count or element count * elem_size (0 if unknown)
        self.context = context  # method name or '<clinit>', 'install', etc.
        self.detail = detail    # human-readable description
        self.is_persistent = True  # False if transient
        self.is_transient = False
        self.element_count = 0
        self.element_size = 0

    def __repr__(self):
        return f'{self.kind}({self.detail}) size={self.size}B [{self.context}]'


def scan_method_bytecode(ms, cap, resolver, warnings=None):
    """Scan a method's bytecode for allocation instructions. Returns list of Allocation."""
    allocations = []
    bc = ms.bytecode_data
    if not bc:
        return allocations

    # Simple constant stack for tracking push values
    stack = []
    pos = 0

    while pos < len(bc):
        op = bc[pos]
        entry = OPCODES.get(op)
        if entry is None:
            if warnings is not None:
                warnings.append('method class[%d].token[%d]: unknown opcode 0x%02X, scan stopped'
                                % (ms.class_index, ms.method_token, op))
            break
        name, operand_sizes = entry
        operands = []
        pos += 1
        for sz in operand_sizes:
            val_bytes = bc[pos:pos+sz]
            if len(val_bytes) < sz:
                break
            if sz == 1:
                operands.append(val_bytes[0])
            elif sz == 2:
                operands.append(struct.unpack('>H', val_bytes)[0])
            elif sz == 4:
                operands.append(struct.unpack('>i', val_bytes)[0])
            pos += sz

        # Handle switch
        if op in (0x73, 0x74):
            low = operands[1] if len(operands) > 1 else 0
            high = operands[2] if len(operands) > 2 else 0
            n = high - low + 1
            if 0 < n < 10000:
                pos += n * 2
        elif op in (0x75, 0x76):
            npair = operands[1] if len(operands) > 1 else 0
            if 0 < npair < 10000:
                pos += npair * 4

        # Track stack for constant values
        # Push constants
        if op == 0x10:  # bspush
            stack.append(operands[0])
        elif op == 0x12:  # bipush
            stack.append(operands[0])
        elif op == 0x11:  # sspush
            stack.append(operands[0])
        elif op == 0x13:  # sipush
            stack.append(operands[0])
        elif op == 0x14:  # iipush
            stack.append(operands[0])
        elif op == 0x02:  # sconst_m1
            stack.append(-1)
        elif op in range(0x03, 0x09):  # sconst_0..sconst_5
            stack.append(op - 0x03)
        elif op == 0x09:  # iconst_m1
            stack.append(-1)
        elif op in range(0x0a, 0x10):  # iconst_0..iconst_5
            stack.append(op - 0x0a)
        elif op in (0x59, 0x96):  # sinc, sinc_w — local increment, don't track precisely
            stack.append('?')
        elif op == 0x5c:  # s2i
            pass  # type conversion, stack unchanged
        elif op == 0x5b:  # s2b
            pass
        elif op == 0x5d:  # i2b
            pass
        elif op == 0x5e:  # i2s
            pass
        elif op == 0x42:  # iadd
            if len(stack) >= 2:
                a, b = stack[-2], stack[-1]
                if isinstance(a, int) and isinstance(b, int):
                    stack[-2] = a + b
                else:
                    stack[-2] = '?'
                stack.pop()
        elif op == 0x44:  # isub
            if len(stack) >= 2:
                a, b = stack[-2], stack[-1]
                if isinstance(a, int) and isinstance(b, int):
                    stack[-2] = a - b
                else:
                    stack[-2] = '?'
                stack.pop()
        elif op == 0x3b:  # pop
            if stack: stack.pop()
        elif op == 0x3c:  # pop2
            if len(stack) >= 2:
                stack.pop(); stack.pop()
        elif op == 0x3d:  # dup
            if stack: stack.append(stack[-1])
        elif op in (0x3e, 0x3f):  # dup2, dup_x
            if len(stack) >= 2:
                stack.append(stack[-2])
        elif op == 0x40:  # swap_x
            if len(stack) >= 2:
                stack[-1], stack[-2] = stack[-2], stack[-1]
        elif op in (0x77, 0x78, 0x79):  # areturn, sreturn, ireturn
            if stack: stack.pop()
        elif op == 0x7a:  # return
            pass
        elif op == 0x93:  # athrow
            if stack: stack.pop()
        elif op in range(0x7b, 0x7f):  # getstatic_* — push field value
            stack.append('?')
        elif op in range(0x7f, 0x83):  # putstatic_* — pop value
            if stack: stack.pop()
        elif op in range(0x83, 0x87):  # getfield_*
            if stack: stack.pop()  # pop objectref
            stack.append('?')  # push field value
        elif op in range(0x87, 0x8b):  # putfield_*
            if len(stack) >= 2:  # pops value + objectref
                stack.pop()
                stack.pop()
            elif stack:
                stack.pop()
        elif op in range(0x18, 0x1c):  # aload_0..aload_3 — push local ref
            stack.append('?')
        elif op in range(0x1c, 0x20):  # sload_0..3
            stack.append('?')
        elif op in range(0x20, 0x24):  # iload_0..3
            stack.append('?')
        elif op in (0x15, 0x16, 0x17):  # aload/sload/iload — push local
            stack.append('?')
        elif op in range(0x28, 0x2b):  # astore/sstore/istore
            if stack: stack.pop()
        elif op in range(0x2b, 0x37):  # astore_0..3, sstore_0..3, istore_0..3
            if stack: stack.pop()
        elif op in (0x24, 0x25, 0x26, 0x27):  # aaload, baload, saload, iaload
            if len(stack) >= 2:
                stack.pop()  # index
                stack.pop()  # arrayref
            stack.append('?')  # pushed value
        elif op in (0x37, 0x38, 0x39, 0x3a):  # aastore, bastore, sastore, iastore
            if len(stack) >= 3:
                stack.pop(); stack.pop(); stack.pop()
            elif len(stack) >= 2:
                stack.pop(); stack.pop()
        elif op == 0x92:  # arraylength
            if stack: stack.pop()  # pop arrayref
            stack.append('?')  # push length
        elif op == 0x94:  # checkcast
            if stack: stack.pop()
            stack.append('?')
        elif op in (0x8b, 0x8c, 0x8e):  # invokevirtual/invokespecial/invokeinterface
            # approximate: args consumed, one implicit return pushed
            if stack: stack.pop()
            stack.append('?')
        elif op in (0x41, 0x43, 0x45, 0x47, 0x49):  # sadd, ssub, smul, sdiv, srem
            if len(stack) >= 2:
                a, b = stack[-2], stack[-1]
                if isinstance(a, int) and isinstance(b, int):
                    if op == 0x41: stack[-2] = a + b
                    elif op == 0x43: stack[-2] = a - b
                    elif op == 0x45: stack[-2] = a * b
                    elif op == 0x47:
                        stack[-2] = a // b if b != 0 else '?'
                    elif op == 0x49:
                        stack[-2] = a % b if b != 0 else '?'
                else:
                    stack[-2] = '?'
                stack.pop()

        # ─── Allocation Detection ───
        if op == 0x8f:  # new
            class_token = operands[0]
            # Compute object size from Class component instance_size if resolvable
            info = resolver.resolve_new_class(class_token)
            obj_size = info['size'] if info else 0
            if info and info.get('external'):
                size_desc = '(imported class)'
            elif obj_size:
                size_desc = f'={obj_size}B'
            else:
                size_desc = '(unknown size)'
            alloc = Allocation('new', obj_size, ms.name_guess,
                              f'new class[{class_token}] {size_desc}')
            alloc.detail = f'new class token {class_token}'
            if obj_size:
                alloc.element_count = 1
                alloc.element_size = obj_size
            allocations.append(alloc)
            stack.append('?')  # push object ref

        elif op == 0x90:  # newarray
            atype = operands[0]
            elem_count = stack[-1] if stack else '?'
            elem_size = ATYP_SIZES.get(atype, 1)
            type_name = ATYP_NAMES.get(atype, f'unknown[{atype}]')
            if stack: stack.pop()  # pop count
            stack.append('?')  # push array ref
            if isinstance(elem_count, int):
                total_bytes = elem_count * elem_size
                alloc = Allocation('newarray', total_bytes, ms.name_guess,
                                  f'new {type_name}[{elem_count}] = {total_bytes}B')
                alloc.element_count = elem_count
                alloc.element_size = elem_size
            else:
                alloc = Allocation('newarray', 0, ms.name_guess,
                                  f'new {type_name}[?] (size unknown)')
            allocations.append(alloc)

        elif op == 0x91:  # anewarray
            class_token = operands[0]
            elem_count = stack[-1] if stack else '?'
            if stack: stack.pop()  # pop element count
            stack.append('?')  # push array ref
            if isinstance(elem_count, int):
                alloc = Allocation('anewarray', elem_count * 2, ms.name_guess,
                                  f'new Object[{elem_count}] class[{class_token}] = {elem_count * 2}B refs')
                alloc.element_count = elem_count
                alloc.element_size = 2  # refs
            else:
                alloc = Allocation('anewarray', 0, ms.name_guess,
                                  f'new Object[?] class[{class_token}] (size unknown)')
            allocations.append(alloc)

        elif op == 0x8d:  # invokestatic
            cp_index = operands[0]
            result = resolver.is_make_transient_bytearray(cp_index)
            if result:
                # Identified as makeTransient* call
                # Size is the short argument on stack (popped before call)
                # Stack: [size, clearOnDeselect] → the method takes (short size, byte mode)
                elem_count = stack[-2] if len(stack) >= 2 else '?'
                mode = stack[-1] if len(stack) >= 1 else '?'
                mode_str = {0: 'CLEAR_ON_DESELECT', 1: 'CLEAR_ON_RESET'}.get(mode, f'mode={mode}')

                if result in ('makeTransientByteArray', 'makeTransientBooleanArray'):
                    if isinstance(elem_count, int):
                        alloc = Allocation(f'make_transient_{result.lower()}', elem_count, ms.name_guess,
                                          f'JCSystem.{result}({elem_count}, {mode_str}) = {elem_count}B')
                        alloc.is_transient = True
                        alloc.is_persistent = False
                        alloc.element_count = elem_count
                        alloc.element_size = 1
                    else:
                        alloc = Allocation(f'make_transient_{result.lower()}', 0, ms.name_guess,
                                          f'JCSystem.{result}(?, {mode_str})')
                        alloc.is_transient = True
                        alloc.is_persistent = False
                    allocations.append(alloc)
                elif result == 'makeTransientShortArray':
                    if isinstance(elem_count, int):
                        alloc = Allocation('make_transient_shortarray', elem_count * 2, ms.name_guess,
                                          f'JCSystem.{result}({elem_count}, {mode_str}) = {elem_count * 2}B')
                        alloc.is_transient = True
                        alloc.is_persistent = False
                        alloc.element_count = elem_count
                        alloc.element_size = 2
                    else:
                        alloc = Allocation('make_transient_shortarray', 0, ms.name_guess,
                                          f'JCSystem.{result}(?, {mode_str})')
                        alloc.is_transient = True
                        alloc.is_persistent = False
                    allocations.append(alloc)
                elif result == 'makeTransientObjectArray':
                    if isinstance(elem_count, int):
                        alloc = Allocation('make_transient_objectarray', elem_count * 2, ms.name_guess,
                                          f'JCSystem.{result}({elem_count}, {mode_str}) = {elem_count * 2}B refs')
                        alloc.is_transient = True
                        alloc.is_persistent = False
                        alloc.element_count = elem_count
                        alloc.element_size = 2  # reference size
                    else:
                        alloc = Allocation('make_transient_objectarray', 0, ms.name_guess,
                                          f'JCSystem.{result}(?, {mode_str})')
                        alloc.is_transient = True
                        alloc.is_persistent = False
                    allocations.append(alloc)
            # Don't pop stack for invokestatic — args are consumed
            # For makeTransient: pops the args
            if result:
                n_args = 2  # makeTransient*(short, byte)
                for _ in range(min(n_args, len(stack))):
                    stack.pop()
                stack.append('?')  # return value (object ref)

    return allocations


# ═══════════════════════════════════════════════════════════════════
# Main Analysis
# ═══════════════════════════════════════════════════════════════════

def _external_ref_counts(cap):
    """Distinct constant-pool references per imported package token (6.7):
    ClassRef/MethodRef/FieldRef/StaticRef entries whose package token points
    into the Import table.  A package linked against but never referenced
    stays at 0."""
    counts = {}
    cp = cap.components.get('constant_pool')
    if not cp:
        return counts
    for e in cp.entries:
        ref = None
        if isinstance(e, (CPClassRef, CPMethodOrFieldRef)):
            ref = e.class_ref
        elif isinstance(e, CPStaticFieldRef):
            ref = e.static_field_ref
        elif isinstance(e, CPStaticMethodRef):
            ref = e.static_method_ref
        if ref is not None and not ref.is_internal:
            counts[ref.package_token] = counts.get(ref.package_token, 0) + 1
    return counts


def analyze_bytes(cap_bytes, verbose=False):
    """Analyze a CAP archive (bytes) and return (report, memory)."""
    cap = CAP(cap_bytes)
    report = {}

    # Header
    if cap.has('header'):
        h = cap.components['header']
        report['cap_version'] = f'{h.major_version}.{h.minor_version}'
        report['package_version'] = h.package_version_str()
        report['package_aid'] = h.aid.hex().upper()

    # Import packages (JC VM spec 6.6): the libraries the package is linked
    # against, with the export-file versions recorded in the CAP
    if cap.has('import'):
        pkgs = cap.components['import'].packages
        refs = _external_ref_counts(cap)
        report['import_count'] = len(pkgs)
        report['imports'] = [
            {'aid': p.aid_hex, 'minor': p.minor, 'major': p.major,
             'refs': refs.get(0x80 + i, 0)}
            for i, p in enumerate(pkgs)]

    # Header package flags (Table 6-4) and the optional package name
    if cap.has('header'):
        h = cap.components['header']
        report['flags'] = h.flags
        report['package_name'] = h.package_name

    # Component sizes (the load file order) for the breakdown display
    report['components'] = [{'name': name, 'size': size} for name, size in cap.files]

    # Applets
    if cap.has('applet'):
        applets = cap.components['applet'].applets
        report['applet_count'] = len(applets)
        report['applets'] = []
        for a in applets:
            report['applets'].append({
                'aid': a.aid_hex,
                'install_method_offset': a.install_method_offset,
            })

    # Classes
    if cap.has('class'):
        classes = cap.components['class'].classes
        report['class_count'] = len(classes)
        report['classes'] = []
        total_instance_size = 0
        total_ref_count = 0
        for ci in classes:
            report['classes'].append({
                'flags': f'0x{ci.flags:02x}',
                'instance_size': ci.declared_instance_size,
                'ref_count': ci.ref_count,
                'public_methods': ci.public_method_table_count,
                'package_methods': ci.package_method_table_count,
            })
            total_instance_size += ci.declared_instance_size
            total_ref_count += ci.ref_count
        report['total_instance_size'] = total_instance_size
        report['total_ref_count'] = total_ref_count

    # Descriptor inventory
    if cap.has('descriptor'):
        desc = cap.components['descriptor']
        total_fields = 0
        static_fields = 0
        instance_fields = 0
        total_methods = 0
        for cls in desc.class_descriptors:
            total_fields += cls.field_count
            total_methods += cls.method_count
            for f in cls.fields:
                if f.is_static:
                    static_fields += 1
                else:
                    instance_fields += 1
        report['descriptor_classes'] = len(desc.class_descriptors)
        report['total_fields'] = total_fields
        report['static_fields'] = static_fields
        report['instance_fields'] = instance_fields
        report['total_methods_desc'] = total_methods

    # StaticField
    if cap.has('static_field'):
        sf = cap.components['static_field']
        report['static_image_size'] = sf.image_size
        report['static_ref_count'] = sf.reference_count
        report['static_array_init_count'] = sf.array_init_count
        report['static_array_init_bytes'] = sf.total_init_bytes
        report['static_default_value_count'] = sf.default_value_count
        report['static_non_default_value_count'] = sf.non_default_value_count
        # Show array init details
        report['array_inits'] = []
        for rec in sf.array_init_records:
            report['array_inits'].append({
                'element_type': rec.element_type,
                'byte_count': rec.count,
                'values_preview': rec.values[:20] if len(rec.values) > 20 else rec.values,
            })

    # Build methods
    methods = build_methods(cap)
    guess_method_names(cap, methods)
    report['method_count'] = len(methods)
    report['method_component_size'] = (
        len(cap.components['method'].bytecode_data) if cap.has('method') else 0)

    # Peak JCVM operand-stack frame estimate: worst (max_stack*2 + (nargs+max_locals)*2)
    worst_stack = max((m.max_stack for m in methods), default=0)
    worst_var = max((m.nargs + m.max_locals for m in methods), default=0)
    report['ram_frame_bytes'] = worst_stack * 2 + worst_var * 2
    report['max_operand_stack'] = worst_stack

    # Resolve constant pool for method names
    resolver = ConstantPoolResolver(cap)

    # Scan all methods for allocations
    warnings = []
    all_allocations = []
    method_details = []
    for ms in methods:
        allocs = scan_method_bytecode(ms, cap, resolver, warnings)
        if allocs:
            all_allocations.extend(allocs)
            method_details.append({
                'class': ms.class_index,
                'token': ms.method_token,
                'name': ms.name_guess,
                'static': ms.is_static,
                'bytecode_count': ms.bytecode_count,
                'max_stack': ms.max_stack,
                'nargs': ms.nargs,
                'max_locals': ms.max_locals,
                'allocations': allocs,
            })

    report['all_allocations'] = all_allocations
    report['methods_with_allocations'] = method_details
    report['warnings'] = warnings

    return report, compute_memory(report)


def compute_memory(report):
    """Compute NVRAM and RAM estimates from analysis report."""
    result = {}

    INSTALL_CTX = ('<init>', 'install', '<clinit>')

    # ── NVRAM (persistent) ──
    niram = report.get('static_image_size', 0)      # static field cells (incl. static refs)
    niram += report.get('static_array_init_bytes', 0)  # data of static arrays created at install

    # 3. Applet instance cells (instance_size per applet instance created at install)
    #    NOTE: `new` allocations resolved from instance sizes are already counted in
    #    persistent_alloc_bytes below, so do NOT add total_instance_size again here.
    total_instance = report.get('total_instance_size', 0)

    # Persistent allocations, split into install-time (persist from power-on) vs runtime
    persistent_alloc_bytes = 0
    persistent_alloc_details = []
    runtime_persistent_bytes = 0
    runtime_persistent_details = []
    for alloc in report.get('all_allocations', []):
        if not alloc.is_persistent:
            continue
        if alloc.context in INSTALL_CTX:
            persistent_alloc_bytes += alloc.size
            persistent_alloc_details.append(alloc)
        else:
            runtime_persistent_bytes += alloc.size
            runtime_persistent_details.append(alloc)

    niram += persistent_alloc_bytes

    # ── RAM (volatile) ──
    transient_alloc_bytes = 0
    transient_alloc_details = []
    runtime_transient_bytes = 0
    runtime_transient_details = []
    for alloc in report.get('all_allocations', []):
        if not alloc.is_transient:
            continue
        if alloc.context in INSTALL_CTX:
            transient_alloc_bytes += alloc.size
            transient_alloc_details.append(alloc)
        else:
            runtime_transient_bytes += alloc.size
            runtime_transient_details.append(alloc)

    # Object header overhead estimate (per object/array: ~6 bytes typical:
    # class/array header + length field + NVM cell rounding)
    OBJECT_HEADER_OVERHEAD = 6

    # Objects created during install: static arrays + install allocations + applet instance(s)
    inst_object_count = report.get('static_array_init_count', 0)
    for alloc in persistent_alloc_details:
        inst_object_count += 1
    inst_object_count += report.get('applet_count', 0)
    niram_overhead = inst_object_count * OBJECT_HEADER_OVERHEAD
    niram_with_overhead = niram + niram_overhead

    # Reference storage for instance reference fields (2 bytes each on-card)
    ref_storage = report.get('total_ref_count', 0) * 2

    result['nvram_static_image'] = report.get('static_image_size', 0)
    result['nvram_array_init'] = report.get('static_array_init_bytes', 0)
    result['nvram_persistent_objects'] = persistent_alloc_bytes
    result['nvram_persistent_details'] = persistent_alloc_details
    result['runtime_persistent_bytes'] = runtime_persistent_bytes
    result['runtime_persistent_details'] = runtime_persistent_details
    result['nvram_object_header_overhead'] = niram_overhead
    result['nvram_object_count'] = inst_object_count
    result['nvram_total'] = niram_with_overhead
    result['nvram_with_ref_storage'] = niram_with_overhead + ref_storage

    result['ram_transient_arrays'] = transient_alloc_bytes
    result['ram_transient_details'] = transient_alloc_details
    result['runtime_transient_bytes'] = runtime_transient_bytes
    result['runtime_transient_details'] = runtime_transient_details
    result['ram_frame_bytes'] = report.get('ram_frame_bytes', 0)   # peak method frame (operand stack + locals/args)

    result['object_header_overhead_model'] = f'{OBJECT_HEADER_OVERHEAD}B per object (header + length + NVM rounding)'
    result['reference_count'] = report.get('total_ref_count', 0)
    result['reference_storage'] = ref_storage

    return result


def memory_json(report, memory, load_file_bytes=None):
    """Compact, stable JSON shape for the API/PWA (all byte counts).

    ``load_file_bytes`` is the concatenated CAP load file (all components) -
    the package image the card stores, our proxy for the GP Card Spec v2.3.1
    Table 11-48 "non-volatile code" minimum memory requirement.  With it the
    suggested C6 becomes the load file and ``nvram.requirement`` reports the
    C6+C8-style total (code image + persistent data, per 11.5.2.3.7: "If both
    tags 'C6' and 'C8' are present and the implementation does not make any
    distinction between Non-Volatile Code and Non-Volatile Data Memory then
    the required minimum shall be the sum of both values"); without it the
    tool's bytecode-only C6 is kept."""
    ram_transient = memory.get('ram_transient_arrays', 0)
    ram_runtime = memory.get('runtime_transient_bytes', 0)
    ram_frame = memory.get('ram_frame_bytes', 0)
    nvram_total = memory.get('nvram_with_ref_storage', 0)
    load_file = max(0, int(load_file_bytes or 0))
    method_component = report.get('method_component_size', 0)
    return {
        'cap_version': report.get('cap_version'),
        'package_version': report.get('package_version'),
        'package_aid': report.get('package_aid'),
        'applet_count': report.get('applet_count', 0),
        'applets': [a.get('aid') for a in report.get('applets', [])],
        # libraries the CAP is linked against (JC VM spec 6.6) with the
        # distinct constant-pool reference counts (6.7)
        'imports': report.get('imports', []),
        # header package flags (Table 6-4: 0x01 int, 0x02 exports, 0x04 applet)
        'flags': {
            'raw': report.get('flags', 0),
            'int': bool(report.get('flags', 0) & 0x01),
            'export': bool(report.get('flags', 0) & 0x02),
            'applet': bool(report.get('flags', 0) & 0x04),
        },
        'package_name': report.get('package_name'),
        'components': report.get('components', []),
        'class_count': report.get('class_count', 0),
        'method_count': report.get('method_count', 0),
        'code': {
            'method_component': method_component,
            'load_file': load_file,
        },
        'nvram': {
            'static_image': memory.get('nvram_static_image', 0),
            'array_init': memory.get('nvram_array_init', 0),
            'install_objects': memory.get('nvram_persistent_objects', 0),
            'header_overhead': memory.get('nvram_object_header_overhead', 0),
            'ref_storage': memory.get('reference_storage', 0),
            'total': nvram_total,
            'runtime': memory.get('runtime_persistent_bytes', 0),
            # C6+C8-style total: package image + persistent data (None when
            # the caller did not provide the load file size)
            'requirement': (load_file + nvram_total) if load_file else None,
        },
        'ram': {
            'transient_arrays': ram_transient,
            'runtime_transient': ram_runtime,
            'peak_frame': ram_frame,
            'total': ram_transient + ram_runtime + ram_frame,
        },
        'suggested': {
            'c6': load_file or method_component,
            'c7': ram_transient + 256,
            'c8': nvram_total,
        },
        'warnings': list(report.get('warnings', [])),
    }


# ═══════════════════════════════════════════════════════════════════
# Report Formatter
# ═══════════════════════════════════════════════════════════════════

def format_report(report, memory):
    lines = []
    lines.append('=' * 72)
    lines.append('CAP MEMORY ANALYSIS: %s' % (report.get('package_aid') or '?'))
    lines.append('=' * 72)

    if 'cap_version' in report:
        lines.append(f'CAP format: {report["cap_version"]}')
    if 'package_version' in report:
        lines.append(f'Package version: {report["package_version"]}')
    if 'package_aid' in report:
        lines.append(f'Package AID: {report["package_aid"]}')

    lines.append('')

    # Applets
    if report.get('applets'):
        lines.append(f'Applets: {report["applet_count"]}')
        for a in report['applets']:
            lines.append(f'  AID: {a["aid"]}')
        lines.append('')

    # Classes
    lines.append(f'Classes: {report.get("class_count", 0)}')
    for i, ci in enumerate(report.get('classes', [])):
        lines.append(f'  [{i}] flags={ci["flags"]} instance_size={ci["instance_size"]}B refs={ci["ref_count"]} public_methods={ci["public_methods"]}')
    lines.append(f'  Total instance size: {report.get("total_instance_size", 0)}B')
    lines.append(f'  Total ref count: {report.get("total_ref_count", 0)}')
    lines.append('')

    # Descriptors
    lines.append(f'Descriptors: {report.get("descriptor_classes", 0)} classes, {report.get("total_fields", 0)} fields ({report.get("static_fields", 0)} static, {report.get("instance_fields", 0)} instance), {report.get("total_methods_desc", 0)} methods')
    lines.append('')

    # StaticField
    sf = report
    lines.append(f'Static Field Component:')
    lines.append(f'  Image size (persistent): {sf.get("static_image_size", 0)}B')
    lines.append(f'  Reference count: {sf.get("static_ref_count", 0)}')
    lines.append(f'  Array init records: {sf.get("static_array_init_count", 0)} ({sf.get("static_array_init_bytes", 0)}B total)')
    lines.append(f'  Default values: {sf.get("static_default_value_count", 0)}, Non-default: {sf.get("static_non_default_value_count", 0)}')
    if report.get('array_inits'):
        lines.append(f'  Array init details:')
        for i, ai in enumerate(report['array_inits'][:10]):
            vals_preview = ai['values_preview'][:10]
            lines.append(f'    [{i}] type={ai["element_type"]} size={ai["byte_count"]}B values={vals_preview}')
        if len(report['array_inits']) > 10:
            lines.append(f'    ... and {len(report["array_inits"]) - 10} more')
    lines.append('')

    # Methods
    lines.append(f'Methods: {report.get("method_count", 0)} total')
    if report.get('methods_with_allocations'):
        lines.append(f'  Methods with allocations: {len(report["methods_with_allocations"])}')
        for md in report['methods_with_allocations']:
            lines.append(f'    class[{md["class"]}].token[{md["token"]}] {md["name"]} '
                        f'static={md["static"]} bc={md["bytecode_count"]}B '
                        f'stack={md["max_stack"]} nargs={md["nargs"]} locals={md["max_locals"]}')
            for alloc in md['allocations']:
                lines.append(f'      {alloc}')
    lines.append('')

    # ── SUMMARY ──
    lines.append('=' * 72)
    lines.append('MEMORY SUMMARY')
    lines.append('=' * 72)
    lines.append('')
    lines.append(f'NVRAM (Persistent):')
    lines.append(f'  Static field image:       {memory["nvram_static_image"]:>6}B')
    lines.append(f'  Static array init data:   {memory["nvram_array_init"]:>6}B')
    lines.append(f'  Install-time allocs:      {memory["nvram_persistent_objects"]:>6}B (new objects + newarray/anewarray)')
    lines.append(f'  Object header overhead:   {memory["nvram_object_header_overhead"]:>6}B ({memory["nvram_object_count"]} objects × 6B)')
    lines.append(f'  ─────────────────────────────')
    lines.append(f'  Subtotal NVRAM:           {memory["nvram_total"]:>6}B')
    lines.append(f'  + Instance ref storage:   {memory["reference_storage"]:>6}B ({memory["reference_count"]} refs × 2B)')
    lines.append(f'  Total NVRAM estimate:     {memory["nvram_with_ref_storage"]:>6}B')
    lines.append(f'  {memory["object_header_overhead_model"]}')
    if memory.get('runtime_persistent_bytes'):
        lines.append(f'  NOTE: runtime (non-install) persistent allocs: {memory["runtime_persistent_bytes"]}B')
        for alloc in memory['runtime_persistent_details']:
            lines.append(f'    {alloc}')
    unknown_rp = [a for a in memory.get('runtime_persistent_details', []) if not a.size]
    if unknown_rp:
        lines.append(f'  NOTE: {len(unknown_rp)} runtime persistent alloc(s) with analysis-unknown size:')
        for a in unknown_rp:
            lines.append(f'    {a}')
    lines.append('')

    lines.append(f'RAM (Volatile):')
    lines.append(f'  Transient arrays (install): {memory["ram_transient_arrays"]:>4}B')
    if memory['ram_transient_details']:
        for alloc in memory['ram_transient_details']:
            lines.append(f'    {alloc}')
    if memory.get('runtime_transient_bytes'):
        lines.append(f'  Runtime transient allocs:   {memory["runtime_transient_bytes"]:>6}B (peak per session)')
    unknown = [a for a in memory.get('runtime_transient_details', []) if not a.size]
    if unknown:
        lines.append(f'  NOTE: {len(unknown)} runtime transient alloc(s) have analysis-unknown size:')
        for a in unknown:
            lines.append(f'    {a}')
    ram_total = (memory["ram_transient_arrays"]
                 + memory.get("runtime_transient_bytes", 0)
                 + memory["ram_frame_bytes"])
    lines.append(f'  + Peak method frame:      {memory["ram_frame_bytes"]:>6}B (max(max_stack×2, (nargs+max_locals)×2))')
    lines.append(f'  Total RAM estimate:       ~{ram_total:>6}B (applet-controlled; APDU buffer/JCRE stack are shared card RAM)')
    lines.append('')

    lines.append(f'Code size (Method.cap):     ~{report.get("method_component_size", 0)}B (component = bytecode + headers)')
    lines.append('')

    # C7/C8 byte values
    ram_total = memory['ram_transient_arrays'] + 256
    nvram_total = memory['nvram_with_ref_storage']
    lines.append(f'Suggested values for .oszn / .conf:')
    lines.append(f'  [LIMITS] code = {report.get("method_component_size", 0)}  (Method.cap component = bytecode + headers)')
    lines.append(f'  [LIMITS] data = 0x{nvram_total:04x} ({nvram_total})')
    lines.append(f'  [LIMITS] ram  = 0x{ram_total:04x} ({ram_total})')
    lines.append(f'  C7 02 {ram_total:04X}  (RAM)')
    lines.append(f'  C8 02 {nvram_total:04X}  (NVRAM)')

    lines.append('')
    lines.append('NOTES:')
    lines.append('  - Object sizes are based on declared instance sizes from Class component.')
    lines.append('  - NVM cell rounding varies by card (typically 4-16B per cell allocation).')
    lines.append('  - Transient arrays assume 2B per reference in object arrays.')
    lines.append('  - Runtime allocations (in process/processToolkit) not included in install-time NVRAM.')
    lines.append('  - Code size estimate is approximate; actual C6 code size = Method.cap component size.')
    lines.append('  - For exact code size, use: java -cp capdump.jar com.sun.javacard.capdump.CapDump <file>.cap')

    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    """Dev CLI: python -m pysim_simple_server.capmem <path_to.cap>"""
    if len(sys.argv) < 2:
        print(f'Usage: {sys.argv[0]} <path_to.cap>')
        sys.exit(1)

    cap_path = sys.argv[1]
    if not os.path.exists(cap_path):
        print(f'Error: {cap_path} not found')
        sys.exit(1)

    with open(cap_path, 'rb') as f:
        data = f.read()

    report, memory = analyze_bytes(data)
    print(format_report(report, memory))


if __name__ == '__main__':
    main()
