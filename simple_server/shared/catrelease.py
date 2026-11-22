"""CAT/USAT item introduction releases (Stage 1 of the release guard).

Which proactive command, event, data object and PROVIDE LOCAL INFORMATION
qualifier appeared in which release, curated from the change histories of the
three pinned specifications:

* 3GPP TS 31.111 (USAT) V18.12.0, Annex W;
* ETSI TS 102 223 (CAT) V18.3.0, Annex V;
* ETSI TS 101 220 (TLVs/TARs) V18.3.0, Annex Q.

The version major equals the release (V7.x = Rel-7 ... V18.x = Rel-18).  The
human-readable reference with the per-row sources lives in the workspace at
``docs/uicc/ETSI-3GPP-Releases.md``.  Release 4 is the specification baseline
(an item the histories never name); an item absent from this table is one the
histories do not pin down - never filter on it.

The PWA mirrors this table (``CAT_RELEASE`` in frontend/index.html); the drift
guard is tests/test_catrelease.py.  Releases below 6 display as "pre-Rel-6"
(``release_label``).  ``DEFAULT_RELEASE`` (Rel-12) is the release a card
preset's empty ``release`` resolves to (``preset_release``) - the safe level
for cards whose release is not known.
"""

# Keyed by: events - the event code (TS 102 223 8.25 / TS 31.111 7.5);
# commands - the proactive command name; objects - 'tag name' as in the
# reference document; pli - the PROVIDE LOCAL INFORMATION qualifier byte.
CAT_RELEASE = {
    'events': {
        0x00: 4, 0x01: 4, 0x02: 4, 0x03: 4, 0x04: 4, 0x05: 4, 0x06: 4, 0x07: 4,
        0x08: 4, 0x09: 4, 0x0A: 4, 0x0B: 4, 0x0C: 4, 0x0D: 4, 0x0E: 4, 0x0F: 4,
        0x10: 6, 0x11: 7, 0x12: 8, 0x13: 9, 0x14: 12, 0x15: 9, 0x16: 9, 0x17: 10,
        0x18: 10, 0x19: 10, 0x1B: 11, 0x1C: 12, 0x1D: 14, 0x1E: 17, 0x1F: 16,
    },
    'commands': {
        'REFRESH': 4, 'MORE TIME': 4,
        'POLL INTERVAL': 4, 'POLLING OFF': 4,
        'SET UP EVENT LIST': 4, 'SET UP CALL': 4,
        'SEND SS': 4, 'SEND USSD': 4,
        'SEND SHORT MESSAGE': 4, 'SEND DTMF': 4,
        'LAUNCH BROWSER': 4, 'PLAY TONE': 4,
        'DISPLAY TEXT': 4, 'GET INKEY': 4,
        'GET INPUT': 4, 'SELECT ITEM': 4,
        'SET UP MENU': 4, 'PROVIDE LOCAL INFORMATION': 4,
        'TIMER MANAGEMENT': 4, 'SET UP IDLE MODE TEXT': 4,
        'RUN AT COMMAND': 4, 'LANGUAGE NOTIFICATION': 4,
        'OPEN CHANNEL': 4, 'CLOSE CHANNEL': 4,
        'SEND DATA': 4, 'RECEIVE DATA': 4,
        'GET CHANNEL STATUS': 4, 'SERVICE SEARCH': 4,
        'GET SERVICE INFORMATION': 4, 'DECLARE SERVICE': 4,
        'SET FRAMES': 6, 'GET FRAMES STATUS': 6,
        'RETRIEVE MULTIMEDIA MESSAGE': 7, 'SUBMIT MULTIMEDIA MESSAGE': 7,
        'DISPLAY MULTIMEDIA MESSAGE': 7, 'GEOGRAPHICAL LOCATION REQUEST': 8,
        'ACTIVATE': 13, 'CONTACTLESS STATE CHANGED': 9,
        'COMMAND CONTAINER': 10, 'ENCAPSULATED SESSION CONTROL': 11,
        'LSI COMMAND': 17,
    },
    'objects': {
        '06/86 Address': 4,
        '08/88 Subaddress': 4,
        '1C/9C Transaction identifier': 4,
        '1A/9A Cause': 4,
        '34/B4 Browser termination cause': 4,
        '35/B5 Bearer description': 4,
        '37/B7 Channel data length': 4,
        '38/B8 Channel status': 4,
        '3E/BE Other address (local / destination)': 4,
        '40/C0 Display parameters': 4,
        '64/E4 Browsing status': 4,
        '65/E5 Network search mode': 4,
        '67/E7 Frames Information': 6,
        '4B/CB (I-)WLAN Access Status': 7,
        '55/D5 CSG cell selection status': 9,
        '56/D6 CSG ID': 9,
        '57/D7 HNB name': 9,
        '31/B1 IMS URI': 10,
        '76/F6 IARI': 10,
        '77/F7 IMPU list': 11,
        '78/F8 IMS status code': 11,
        '7E/FE Media Type': 12,
        '73/F3 URI truncated': 12,
        '09/89 PLMN ID': 4,
        '3F/BF Access technology': 4,
        'B4 Supported Radio Access Technologies': 13,
        '9D Data connection status': 14,
        'AA Data connection type': 14,
        'AE (E/5G)SM cause': 14,
        'C7 Network Access Name': 14,
        '0B/8B PDP/PDN/PDU type': 14,
        '26/A6 Date-Time and Time zone': 4,
        '13/93 Location Information': 4,
        '55/D5 CAG cell selection status': 17,
        '56/D6 CAG information list': 17,
        '57/D7 CAG Human-readable network name list': 17,
        '55/D5 Slices status': 16,
        '56/D6 Slices information (served)': 16,
        '78/F8 Allowed slices information': 16,
        '77/F7 Allowed slices with S-NSSAI mapping': 18,
        'D7 Rejected slices with S-NSSAI mapping': 18,
        'B1 Rejected slices information': 18,
        '79/F9 Partial NSSAI': 18,
        '70/F0 Last Envelope': 18,
    },
    'pli': {
        '00': 4, '01': 4, '02': 4, '03': 4, '04': 4, '06': 4, '07': 4, '08': 6,
        '09': 4, '0A': 4, '0D': 8, '0E': 12, '0F': 10, '10': 10, '11': 9, '12': 10,
        '13': 10, '14': 13, '15': 16, '16': 17, '17': 18, '1A': 13,
    },
}


def release_label(rel):
    """The display label of an introduction release.

    Everything below Rel-6 collapses to "pre-Rel-6" (the UI convention);
    0/None yields '' (unknown - the item is never filtered).
    """
    if not rel:
        return ''
    return 'pre-Rel-6' if rel < 6 else 'Rel-%d' % rel


def item_release(kind, key, default=None):
    """The introduction release of an item, or `default` when unknown."""
    return CAT_RELEASE.get(kind, {}).get(key, default)


# The release a preset's empty `release` resolves to: Rel-12 is the safe level
# for cards whose release is not known (the CAT items the tool sends stay
# within what a modern card implements).  Mirrored in the PWA
# (`CAT_RELEASE_DEFAULT`), drift-guarded by tests/test_catrelease.py.
DEFAULT_RELEASE = 12


def preset_release(preset):
    """The effective CAT release of a card preset.

    The preset's own `release` when it names one (4-18), else the default.
    Empty or invalid values resolve to the default - an unset release must
    never filter anything away.
    """
    try:
        rel = int((preset or {}).get('release') or 0)
    except (TypeError, ValueError):
        rel = 0
    return rel if 4 <= rel <= 18 else DEFAULT_RELEASE
