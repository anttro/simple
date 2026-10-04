"""Test script engine (pure): validation, response checks and scripted TRs.

A test script drives a deterministic dialogue with the card:

* an **action** step sends something (ENVELOPE, Menu Selection, raw APDU,
  SCP80 secured packet, file update/read, STATUS) and checks the response
  (SW exact/mask, data exact/mask, PoR for SCP80);
* an **expectation** step fetches the proactive command announced by the
  previous step (SW ``91XX`` - TS 102 221 7.4.2.1 / TS 102 223 6.3: the UICC
  announces a pending command in the response to a command and re-announces
  it with ``91XX`` until it is fetched; it never pushes unsolicited), checks
  its contents and answers it with a scripted TERMINAL RESPONSE.

This module has no card access - ``server.py`` runs the steps; only the pure
parts live here so they can be tested without hardware.

Check syntax (SW, data, qualifier): a plain hex string is an exact match,
``?`` in mask mode is a per-nibble wildcard (same convention as the
profiler), e.g. ``{"mode": "mask", "value": "91??"}``.
"""

import re

import gsm0338  # registers the 'gsm03.38' codec (the default alphabet coding)

__all__ = [
    'ScriptError', 'ACTION_KINDS', 'FAIL_LEVELS', 'POR_CHECKS', 'RESULT_NAMES',
    'normalise_script', 'normalise_step', 'normalise_respond',
    'match_value', 'match_text', 'match_item', 'combine_levels', 'build_tr',
    'pack_gsm7',
]

ACTION_KINDS = ('envelope', 'menu-select', 'file-write', 'file-read', 'apdu',
                'scp80', 'status')
FAIL_LEVELS = ('error', 'warning')
POR_CHECKS = ('none', 'ok', 'any')

# TERMINAL RESPONSE result values commonly used by scripts (TS 102 223 8.12).
RESULT_NAMES = {
    'ok': 0x00, 'partial': 0x01, 'missing': 0x02, 'refused': 0x03,
    'not_understood': 0x04, 'modified': 0x06,
    'cancel': 0x10, 'back': 0x11, 'timeout': 0x12, 'no_response': 0x22,
}

_HEX_MASK_RE = re.compile(r'^[0-9A-F?]+$')
_HEX_RE = re.compile(r'^[0-9A-F]+$')


class ScriptError(ValueError):
    """Invalid test script - reported to the client before a run starts."""


# ─── normalisation helpers ──────────────────────────────────────────────

def _fail_level(value, default='error'):
    if value in (None, ''):
        return default
    v = str(value).lower()
    if v not in FAIL_LEVELS:
        raise ScriptError("on_fail must be 'error' or 'warning'")
    return v


def _int(value, what, lo, hi):
    try:
        text = str(value).strip()
        v = int(text, 16 if text.lower().startswith('0x') else 10)
    except (TypeError, ValueError):
        raise ScriptError('%s must be an integer' % what)
    if not lo <= v <= hi:
        raise ScriptError('%s must be %d..%d' % (what, lo, hi))
    return v


def _data_hex(value, what, allow_empty=False):
    if value in (None, ''):
        if allow_empty:
            return ''
        raise ScriptError('%s is required' % what)
    if not isinstance(value, str):
        raise ScriptError('%s must be a hex string' % what)
    v = re.sub(r'\s', '', value).upper()
    if not v:
        if allow_empty:
            return ''
        raise ScriptError('%s is required' % what)
    if not _HEX_RE.match(v) or len(v) % 2:
        raise ScriptError('%s must be hex with an even number of digits' % what)
    return v


def _check_spec(value, what, default_mode='exact'):
    """Normalise a check: hex string or {'mode', 'value'}."""
    if value is None:
        return None
    if isinstance(value, dict):
        mode = str(value.get('mode') or default_mode).lower()
        val = value.get('value')
    else:
        val = value
        # a plain string with '?' is a mask ("91??"), no need to spell it out
        mode = 'mask' if (default_mode == 'exact' and isinstance(val, str)
                          and '?' in val) else default_mode
    if mode not in ('exact', 'mask'):
        raise ScriptError('%s: mode must be exact or mask' % what)
    if not isinstance(val, str) or not val.strip():
        raise ScriptError('%s: value is required' % what)
    v = re.sub(r'\s', '', val).upper()
    if not _HEX_MASK_RE.match(v) or len(v) % 2:
        raise ScriptError('%s: value must be hex (even length, "?" = wildcard)' % what)
    if mode == 'exact' and '?' in v:
        raise ScriptError('%s: "?" is only allowed in mask mode' % what)
    return {'mode': mode, 'value': v}


# ─── matching (pure) ────────────────────────────────────────────────────

def match_value(spec, actual):
    """Exact/mask hex comparison; no spec means 'no check'."""
    if not spec:
        return True
    if actual is None:
        return False
    a = re.sub(r'\s', '', str(actual)).upper()
    v = spec['value']
    if len(a) != len(v):
        return False
    if spec['mode'] == 'exact':
        return a == v
    return all(vc == '?' or vc == ac for vc, ac in zip(v, a))


def match_text(spec, text):
    if not spec:
        return True
    if text is None:
        return False
    want, got = spec['value'], str(text)
    if not spec.get('case_sensitive', True):
        want, got = want.lower(), got.lower()
    return want == got if spec['mode'] == 'exact' else want in got


def match_item(items, spec):
    """Find a parsed item (SELECT ITEM / SET UP MENU) matching id and/or text.

    Returns ``(ok, detail)`` - the detail names the matching item or lists the
    decoded items so the report is useful without the raw bytes."""
    decoded = ', '.join('%s=%r' % (it.get('id'), it.get('text')) for it in (items or []))
    if not items:
        return False, 'no items decoded'
    for it in items:
        if spec.get('id') is not None and int(it.get('id', -1)) != spec['id']:
            continue
        if spec.get('text') is not None:
            text_spec = {'mode': spec['mode'], 'value': spec['text'],
                         'case_sensitive': spec.get('case_sensitive', True)}
            if not match_text(text_spec, it.get('text')):
                continue
        return True, 'item %s %r' % (it.get('id'), it.get('text'))
    return False, 'no matching item (decoded: %s)' % (decoded or 'none')


def combine_levels(levels):
    """Worst outcome of a step: any error wins, then warning, else ok."""
    if 'error' in levels:
        return 'error'
    if 'warning' in levels:
        return 'warning'
    return 'ok'


# ─── script validation ──────────────────────────────────────────────────

def normalise_script(raw, command_resolver=None):
    """Validate/normalise a script.  ``command_resolver(name) -> int|None``
    resolves proactive command names (provided by server.py)."""
    if not isinstance(raw, dict):
        raise ScriptError('script must be an object')
    name = str(raw.get('name') or '').strip() or 'test script'
    steps_raw = raw.get('steps')
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ScriptError('script must have at least one step')
    steps = [normalise_step(s, command_resolver) for s in steps_raw]
    return {'name': name, 'steps': steps}


def normalise_step(step, command_resolver=None):
    if not isinstance(step, dict):
        raise ScriptError('each step must be an object')
    typ = step.get('type')
    if typ == 'action':
        return _normalise_action(step)
    if typ == 'expect':
        return _normalise_expect(step, command_resolver)
    raise ScriptError("step type must be 'action' or 'expect'")


def _normalise_action(step):
    kind = str(step.get('kind') or '').lower()
    if kind not in ACTION_KINDS:
        raise ScriptError("unknown action kind %r" % step.get('kind'))
    params = _normalise_params(kind, step.get('params') or {})
    on_fail = _fail_level(step.get('on_fail'))
    check = _normalise_check(step.get('check'), kind, params)
    out = {'type': 'action', 'kind': kind, 'params': params,
           'check': check, 'on_fail': on_fail}
    if step.get('label'):
        out['label'] = str(step['label'])
    return out


def _normalise_params(kind, p):
    if not isinstance(p, dict):
        raise ScriptError('%s: params must be an object' % kind)
    if kind == 'envelope':
        if p.get('event') is None:
            raise ScriptError('envelope: event is required')
        return {'event': _int(p['event'], 'envelope event', 0, 255),
                'data': _data_hex(p.get('data'), 'envelope data', allow_empty=True)}
    if kind == 'menu-select':
        # The item is named by its id, or by its text - the id varies with the
        # applet's install parameters, so a script can match the text the card
        # actually shows (the runner resolves it against the cached menu).
        out = {}
        if p.get('item_id') not in (None, ''):
            out['item_id'] = _int(p.get('item_id'), 'menu item_id', 1, 255)
        text = p.get('text')
        if text is not None and str(text).strip():
            out['text'] = str(text)
            mode = str(p.get('mode') or 'exact').lower()
            if mode not in ('exact', 'contains'):
                raise ScriptError('menu-select: mode must be exact or contains')
            out['mode'] = mode
            out['case_sensitive'] = bool(p.get('case_sensitive', True))
        if not out:
            raise ScriptError('menu-select: item_id or text is required')
        if 'item_id' in out and 'text' in out:
            raise ScriptError('menu-select: give item_id or text, not both')
        return out
    if kind in ('file-write', 'file-read'):
        path = str(p.get('path') or '').strip()
        if not path:
            raise ScriptError('%s: path is required' % kind)
        mode = str(p.get('mode') or 'auto').lower()
        if mode not in ('auto', 'binary', 'record'):
            raise ScriptError('%s: mode must be auto, binary or record' % kind)
        out = {'path': path, 'mode': mode}
        if mode == 'record' or p.get('record') is not None:
            out['record'] = _int(p.get('record') or 1, '%s record' % kind, 1, 255)
        if kind == 'file-write':
            out['data'] = _data_hex(p.get('data'), 'file-write data')
        return out
    if kind == 'apdu':
        return {'apdu': _data_hex(p.get('apdu'), 'apdu')}
    if kind == 'scp80':
        out = {}
        source = str(p.get('source') or '').lower()
        if source not in ('', 'apdu', 'sp'):
            raise ScriptError('scp80: source must be apdu or sp')
        if source == 'sp' or (not source and p.get('sp')):
            if not p.get('sp'):
                raise ScriptError('scp80: secured packet is required')
            out['sp'] = _data_hex(p.get('sp'), 'secured packet')
        else:
            if not p.get('apdu'):
                raise ScriptError('scp80: apdu is required')
            out['apdu'] = _data_hex(p.get('apdu'), 'scp80 apdu')
        for key in ('tar', 'spi1', 'spi2'):
            if p.get(key) not in (None, ''):
                out[key] = _data_hex(p[key], 'scp80 %s' % key)
        if out.get('tar') and len(out['tar']) != 6:
            raise ScriptError('scp80: tar must be 3 bytes')
        for key in ('spi1', 'spi2'):
            if out.get(key) and len(out[key]) != 2:
                raise ScriptError('scp80: %s must be 1 byte' % key)
        # The keyset number (KIc/KID b8..b5, TS 102 225 5.1.2): the keys and
        # the counter stay preset-owned, the step only picks which keyset.
        if p.get('kvn') not in (None, ''):
            out['kvn'] = _int(p.get('kvn'), 'scp80 keyset number', 1, 15)
        return out
    if kind == 'status':
        attempts = p.get('attempts')
        attempts = _int(1 if attempts is None else attempts, 'status attempts', 1, 1000)
        interval = p.get('interval_ms')
        interval = _int(200 if interval is None else interval, 'status interval_ms', 0, 10000)
        return {'attempts': attempts, 'interval_ms': interval}
    raise ScriptError('unknown action kind %r' % kind)


def _normalise_check(check, kind, params):
    if check is None:
        check = {}
    if not isinstance(check, dict):
        raise ScriptError('check must be an object')
    sw = _check_spec(check.get('sw'), 'check.sw')
    if sw is None:
        # A STATUS poll for a pending proactive command ends on 91XX
        # (TS 102 221 7.4.2.1); a single STATUS normally ends on 9000.
        if kind == 'status' and params.get('attempts', 1) > 1:
            sw = {'mode': 'mask', 'value': '91??'}
        else:
            sw = {'mode': 'exact', 'value': '9000'}
    data = _check_spec(check.get('data'), 'check.data')
    por = check.get('por')
    if por is None:
        por = 'any'
    else:
        por = str(por).lower()
        if kind != 'scp80':
            raise ScriptError('check.por is only valid for scp80 actions')
        if por not in POR_CHECKS:
            raise ScriptError('check.por must be none, ok or any')
    return {'sw': sw, 'data': data, 'por': por}


def _normalise_expect(step, command_resolver=None):
    cmd = step.get('command')
    if cmd is None or isinstance(cmd, bool):
        raise ScriptError('expect: command is required')
    if isinstance(cmd, int):
        ctype, cname = cmd, None
    else:
        s = str(cmd).strip()
        up = s.upper()
        if up in ('ANY', '*'):
            ctype, cname = None, 'ANY'
        elif re.fullmatch(r'(0X)?[0-9A-F]{2}', up):
            ctype, cname = int(up.replace('0X', ''), 16), None
        elif command_resolver is not None:
            ctype = command_resolver(up)
            if ctype is None:
                raise ScriptError('expect: unknown proactive command %r' % s)
            cname = up
        else:
            raise ScriptError('expect: command must be a hex type code')
    qualifier = _check_spec(step.get('qualifier'), 'qualifier')
    if qualifier and '?' not in qualifier['value'] and len(qualifier['value']) != 2:
        raise ScriptError('qualifier must be one byte')
    on_fail = _fail_level(step.get('on_fail'))
    checks_raw = step.get('checks') or []
    if not isinstance(checks_raw, list):
        raise ScriptError('expect: checks must be a list')
    checks = [_normalise_content_check(c, on_fail) for c in checks_raw]
    respond = normalise_respond(step.get('respond') or {}, ctype)
    return {'type': 'expect', 'command': {'type': ctype, 'name': cname},
            'qualifier': qualifier, 'checks': checks, 'respond': respond,
            'on_fail': on_fail}


def _normalise_content_check(c, default_level):
    if not isinstance(c, dict):
        raise ScriptError('expect: each check must be an object')
    kind = str(c.get('kind') or '').lower()
    level = _fail_level(c.get('on_fail'), default_level)
    if kind == 'text':
        mode = str(c.get('mode') or 'contains').lower()
        if mode not in ('contains', 'exact'):
            raise ScriptError('text check: mode must be contains or exact')
        if c.get('value') is None:
            raise ScriptError('text check: value is required')
        return {'kind': 'text', 'mode': mode, 'value': str(c['value']),
                'case_sensitive': bool(c.get('case_sensitive', True)),
                'on_fail': level}
    if kind == 'item':
        item_id = c.get('id')
        if item_id is not None:
            item_id = _int(item_id, 'item check id', 1, 255)
        text = c.get('text')
        if item_id is None and text is None:
            raise ScriptError('item check: id or text is required')
        mode = str(c.get('mode') or 'contains').lower()
        if mode not in ('contains', 'exact'):
            raise ScriptError('item check: mode must be contains or exact')
        return {'kind': 'item', 'id': item_id,
                'text': str(text) if text is not None else None, 'mode': mode,
                'case_sensitive': bool(c.get('case_sensitive', True)),
                'on_fail': level}
    if kind == 'raw':
        spec = _check_spec(c.get('value') if 'value' in c else c, 'raw check')
        return {'kind': 'raw', 'mode': spec['mode'], 'value': spec['value'],
                'on_fail': level}
    if kind == 'por':
        out = {'kind': 'por', 'on_fail': level}
        status = c.get('status')
        if status is not None and str(status).strip():
            out['status'] = str(status).strip()
        if c.get('sw') not in (None, ''):
            out['sw'] = _check_spec(c.get('sw'), 'por check sw')
        if c.get('data') not in (None, ''):
            out['data'] = _check_spec(c.get('data'), 'por check data')
        if not (out.get('status') or out.get('sw') or out.get('data')):
            raise ScriptError('por check: status, sw or data is required')
        return out
    if kind == 'files':
        val = c.get('files', c.get('value'))
        if isinstance(val, str):
            val = [x for x in re.split(r'[,\s]+', val) if x]
        if not isinstance(val, list) or not val:
            raise ScriptError('files check: a list of file paths is required')
        files = []
        for f in val:
            path = re.sub(r'\s', '', str(f)).upper()
            if not re.fullmatch(r'(?:[0-9A-F]{2})+', path) or len(path) < 4:
                raise ScriptError('files check: each path must be hex FID bytes: %r' % f)
            files.append(path)
        return {'kind': 'files', 'files': files, 'on_fail': level}
    raise ScriptError('unknown check kind %r' % c.get('kind'))


def normalise_respond(respond, cmd_type=None):
    """Validate the scripted TERMINAL RESPONSE for an expected command."""
    if not isinstance(respond, dict):
        raise ScriptError('respond must be an object')
    res = respond.get('result', 0x00)
    if isinstance(res, str):
        key = res.strip().lower()
        if re.fullmatch(r'(0x)?[0-9a-f]{2}', key):
            res = int(key.replace('0x', ''), 16)
        elif key in RESULT_NAMES:
            res = RESULT_NAMES[key]
        else:
            raise ScriptError('respond: unknown result %r' % respond.get('result'))
    else:
        res = _int(res, 'respond result', 0, 255)
    out = {'result': res}
    if respond.get('item_id') is not None:
        out['item_id'] = _int(respond['item_id'], 'respond item_id', 1, 255)
    if respond.get('text') is not None:
        out['text'] = str(respond['text'])
        dcs = respond.get('dcs')
        out['dcs'] = _int(dcs, 'respond dcs', 0, 255) if dcs is not None else 0x00
    extra = respond.get('raw')
    if extra not in (None, ''):
        out['raw'] = _data_hex(extra, 'respond raw')
    return out


# ─── scripted TERMINAL RESPONSE ─────────────────────────────────────────

def pack_gsm7(septets):
    """Pack GSM 03.38 septets into 7-bit octets (TS 23.038 4, SMS packing)."""
    out = bytearray()
    acc = 0
    bits = 0
    for s in septets:
        acc |= (s & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def _encode_text(text, dcs):
    """Text string coding for a scripted TERMINAL RESPONSE (TS 102 223 8.15):
    '00' = GSM default alphabet 7 bits packed, '04' (or any other 8-bit
    scheme) = GSM default alphabet 8 bits (TS 23.038 via the gsm03.38 codec,
    bit 8 clear), '08' = UCS2.  Matches the interactive GET INKEY/GET INPUT
    response codings."""
    if (dcs & 0x0C) == 0x08:
        return text.encode('utf-16-be')
    if dcs == 0x00:
        return pack_gsm7(text.encode('gsm03.38'))
    return text.encode('gsm03.38')


def build_tr(cmd_num, cmd_type, dev_dst, dev_src, respond):
    """Flat COMPREHENSION-TLV TERMINAL RESPONSE payload (TS 102 223 6.8) in the
    6.8.0 object order: command details, device identities, Result, then the
    command-specific objects (Text string for GET INKEY/GET INPUT, Item
    identifier for SELECT ITEM) and any extra raw TLVs.  Matches the
    interactive menu TR layout."""
    out = bytearray([0x81, 0x03, cmd_num & 0xFF, cmd_type & 0xFF, 0x00])
    out += bytes([0x82, 0x02, dev_dst & 0xFF, dev_src & 0xFF])
    result = int(respond.get('result', 0))
    out += bytes([0x83, 0x02, result & 0xFF, 0x00])
    if respond.get('text') is not None:
        text = str(respond['text'])
        if text == '':
            # TS 102 223 8.15: a null text string is Length 00, no value part
            out += bytes([0x8D, 0x00])
        else:
            dcs = int(respond.get('dcs', 0x00))
            body = _encode_text(text, dcs)
            out += bytes([0x8D, len(body) + 1, dcs]) + body
    if respond.get('item_id') is not None and result == 0x00:
        out += bytes([0x90, 0x01, respond['item_id'] & 0xFF])
    if respond.get('raw'):
        out += bytes.fromhex(respond['raw'])
    return bytes(out)
