# coding=utf-8
"""The card's file model: names, FIDs, paths and the tree to select through.

One tree holds the standard file list (the committed
``frontend/uicc_files.json`` asset, generated cardless by
``pysim_simple_server/uicc_files.py`` - it already carries the ``SUPPLEMENT``
files and every ADF subtree), the files a card turned out to have through its
EF.PBR, and **custom files**.  A file is just an entry; ``source`` says where
it came from (``standard``/``pbr``/``supplement``/``custom``), so "custom" is
a tag on one tree instead of a special case in four places.

The entry shape is the asset's: one dict per file with
``{path, name, sym, fid, kind, root[, aid]}``.  ``path`` is the FID chain
(``MF/7F10/5F3A/4F30``), ``sym`` the symbolic one
(``MF/DF.TELECOM/DF.PHONEBOOK/EF.PBR``), ``kind`` one of ``ef``/``df``/``adf``
(``mf`` is added for the implicit root).

Resolution is case-insensitive and two-level: a bare FID is looked up across
the whole tree (a bare FID that matches several files is ambiguous - use a
path), a name likewise.  :meth:`FileModel.path_chain` turns any file into the
ordered selectors :class:`simple_server.commands.FileOps` needs.

Pure module: no transport, no card.
"""

import json
import os
import re

from dataclasses import dataclass

__all__ = [
    'DEFAULT_ASSET', 'FileInfo', 'FileModel', 'FileModelError',
    'UnknownFile', 'AmbiguousFile',
]

# The committed asset, relative to the repository root (this file lives in
# <repo>/simple_server/).
DEFAULT_ASSET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'frontend', 'uicc_files.json')

KINDS = ('ef', 'df', 'adf', 'mf')
SOURCES = ('standard', 'custom', 'pbr', 'supplement')

_FID_RE = re.compile(r'^[0-9A-F]{4}$')


class FileModelError(Exception):
    """Base class for file-model resolution failures."""


class UnknownFile(FileModelError):
    """The selector names nothing the model knows."""


class AmbiguousFile(FileModelError):
    """The selector matches more than one file; use a full path."""

    def __init__(self, selector, candidates):
        self.selector = selector
        self.candidates = list(candidates)
        super().__init__(
            'ambiguous file selector %r: %s (use a full path)'
            % (selector, ', '.join(sorted(c.path for c in self.candidates))))


def _norm(value):
    """Upper-case/strip a string; ``None`` becomes ``''``."""
    return str(value if value is not None else '').strip().upper()


def _norm_path(path):
    """Canonical path: upper-case, single '/' separators, ``3F00`` -> ``MF``."""
    segments = [s for s in _norm(path).split('/') if s]
    if segments and segments[0] == '3F00':
        segments[0] = 'MF'
    return '/'.join(segments)


def _fid(value):
    """A 2-byte FID as an upper-case hex string; ``''`` stays empty."""
    fid = _norm(value)
    if not fid:
        return ''
    if not _FID_RE.match(fid):
        raise ValueError('%r is not a 2-byte hex FID' % value)
    return fid


def _kind(value):
    kind = _norm(value).lower() or 'ef'
    if kind not in KINDS:
        raise ValueError('unknown file kind %r (one of %s)'
                         % (value, ', '.join(KINDS)))
    return kind


@dataclass
class FileInfo(object):
    """One file of the model.

    ``parent`` is the parent DF's *name* (``None`` for MF and for an ADF,
    which sits beside MF), ``path`` the canonical FID path and ``aid`` the ADF
    application identifier when known.  ``parent_known`` flags a custom file
    whose parent DF the model does not know yet - it is kept (the standard
    list may simply not describe that DF) but cannot be selected until the
    parent appears.
    """

    name: str
    fid: str = ''
    kind: str = 'ef'
    root: str = 'MF'
    aid: str = None
    source: str = 'standard'
    parent: str = None
    path: str = ''
    parent_known: bool = True

    @property
    def is_df(self):
        """True for a DF/ADF/MF (a node that can hold children)."""
        return self.kind in ('df', 'adf', 'mf')


class FileModel(object):
    """A tree of :class:`FileInfo` built from an entry list.

    ``entries`` uses the ``frontend/uicc_files.json`` shape; the tests build
    small hand-written lists, the server loads the asset with
    :meth:`from_asset`.
    """

    def __init__(self, entries=None):
        self._files = []
        self._by_path = {}
        self._by_name = {}
        self._by_fid = {}
        self._by_aid = {}
        self._children = {}
        self._mf = None
        entries = list(entries or [])
        if not any(_norm(e.get('name')) == 'MF' for e in entries):
            # The root is implicit in the asset (it lists MF's children, not
            # MF itself); add it so 'MF' resolves and children('MF') works.
            self._mf = FileInfo(name='MF', fid='3F00', kind='mf', root='MF',
                                path='MF')
            self._files.append(self._mf)
        for entry in entries:
            self._files.append(self._file_from_entry(entry))
        if self._mf is None:
            self._mf = self._single(self._by_name.get('MF'), 'MF')
        for info in self._files:
            self._index(info)
        for info in self._files:
            self._link_parent(info)

    # ---- construction ---------------------------------------------------

    @classmethod
    def from_asset(cls, path=None):
        """Load the committed asset (``frontend/uicc_files.json``)."""
        with open(path or DEFAULT_ASSET, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        entries = data.get('files') if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ValueError('%s carries no file list'
                             % (path or DEFAULT_ASSET))
        return cls(entries)

    def _file_from_entry(self, entry):
        if not isinstance(entry, dict):
            raise ValueError('a file entry must be a dict: %r' % (entry,))
        name = _norm(entry.get('name'))
        if not name:
            raise ValueError('a file entry needs a name: %r' % (entry,))
        path = _norm_path(entry.get('path') or '')
        symbol = _norm(entry.get('sym') or '')
        parent = None
        segments = [s for s in symbol.split('/') if s]
        if len(segments) > 1:
            parent = segments[-2]
        info = FileInfo(
            name=name,
            fid=_fid(entry.get('fid')),
            kind=_kind(entry.get('kind')),
            root=_norm(entry.get('root')) or 'MF',
            aid=_norm(entry.get('aid')) or None,
            source=_norm(entry.get('source')).lower() or 'standard',
            parent=parent,
            path=path,
            parent_known=True,
        )
        return info

    def _index(self, info):
        if info.path:
            self._by_path.setdefault(info.path, []).append(info)
        self._by_name.setdefault(_norm(info.name), []).append(info)
        if info.fid:
            self._by_fid.setdefault(info.fid, []).append(info)
        if info.aid:
            self._by_aid.setdefault(info.aid, []).append(info)

    def _link_parent(self, info):
        """Attach ``info`` to its parent DF (by the FID path, never by name).

        The name is not a safe key: DF.PHONEBOOK exists under both DF.TELECOM
        and ADF.USIM, and the two copies hold different EF.PBR files.
        """
        if info is self._mf or info.kind == 'adf' or not info.path:
            if info.kind == 'adf':
                info.parent = None
            return
        parent_path = info.path.rsplit('/', 1)[0] if '/' in info.path else ''
        parents = self._by_path.get(parent_path) if parent_path else None
        if parents:
            info.parent_known = True
            if info.parent is None:
                info.parent = parents[0].name
            for parent in parents:
                self._children.setdefault(parent.path, []).append(info)
            return
        info.parent_known = False
        if info.parent is None:
            info.parent = parent_path.rsplit('/', 1)[-1] if parent_path else None

    @staticmethod
    def _single(candidates, selector):
        candidates = list(candidates or [])
        if len(candidates) > 1:
            raise AmbiguousFile(selector, candidates)
        return candidates[0] if candidates else None

    # ---- custom files ---------------------------------------------------

    def add_custom(self, name, fid, parent_df, kind='ef', aid=None):
        """Inject a custom file under ``parent_df`` and return its info.

        ``parent_df`` is a DF/ADF name or path.  When the model does not know
        it yet the file is still added and flagged ``parent_known = False``
        (the same "a parent not seen in the tree yet is allowed" behaviour the
        Profiler's custom files have).  The entry is marked
        ``source = 'custom'``.
        """
        name = _norm(name)
        if not name:
            raise ValueError('a custom file needs a name')
        kind = _kind(kind)
        fid = _fid(fid)
        if kind == 'adf':
            if not _norm(aid):
                raise ValueError('an ADF custom file needs an AID')
        elif not fid:
            raise ValueError('a custom %s needs a 4-hex FID' % kind.upper())
        parent = self._find_parent(parent_df)
        root = 'MF'
        if kind == 'adf':
            parent_path = ''
            parent_name = None
            parent_known = True
            root = name
        elif parent is not None:
            parent_path = parent.path
            parent_name = parent.name
            parent_known = True
            root = parent.root
        else:
            parent_path = _norm_path(parent_df)
            if parent_path and '/' not in parent_path:
                parent_path = 'MF/' + parent_path
            parent_name = parent_path.rsplit('/', 1)[-1] if parent_path else None
            parent_known = False
        info = FileInfo(
            name=name,
            fid=fid,
            kind=kind,
            root=root,
            aid=_norm(aid) or None,
            source='custom',
            parent=parent_name,
            path=name if kind == 'adf'
                 else '/'.join(p for p in (parent_path, fid) if p),
            parent_known=parent_known,
        )
        self._files.append(info)
        self._index(info)
        if parent is not None:
            self._children.setdefault(parent.path, []).append(info)
        return info

    def _find_parent(self, parent_df):
        """Resolve a parent DF by path, name, FID or AID; None when unknown."""
        selector = _norm(parent_df)
        if not selector:
            return None
        if '/' in selector:
            candidate = self._single(self._by_path.get(_norm_path(selector)),
                                     selector)
            return candidate
        for index in (self._by_name, self._by_fid, self._by_aid,
                      self._by_path):
            candidate = self._single(index.get(selector), selector)
            if candidate is not None:
                return candidate
        return None

    # ---- lookup ---------------------------------------------------------

    def by_name(self, name):
        """Every file whose name matches (case-insensitive), in model order."""
        return list(self._by_name.get(_norm(name), []))

    def by_fid(self, fid):
        """Every file with that FID (a FID is only unique within one DF)."""
        try:
            fid = _fid(fid)
        except ValueError:
            return []
        return list(self._by_fid.get(fid, [])) if fid else []

    def by_aid(self, aid):
        """Every ADF with that application identifier."""
        return list(self._by_aid.get(_norm(aid), []))

    def resolve(self, selector):
        """Resolve a name, bare FID or path to one :class:`FileInfo`.

        Raises :class:`UnknownFile` when nothing matches and
        :class:`AmbiguousFile` when a bare name/FID matches several files.
        """
        if selector is None:
            raise UnknownFile('empty file selector')
        text = str(selector).strip()
        if not text:
            raise UnknownFile('empty file selector')
        if '/' in text:
            return self.resolve_path(text)
        key = _norm(text)
        for index in (self._by_fid, self._by_name, self._by_aid):
            matches = index.get(key)
            if matches:
                return self._single(matches, selector)
        raise UnknownFile('unknown file %r' % selector)

    def resolve_path(self, path):
        """Resolve a slash-separated path (names or FIDs, rooted at MF/ADF)."""
        text = str(path or '').strip()
        if not text:
            raise UnknownFile('empty file path')
        exact = self._by_path.get(_norm_path(text))
        if exact:
            return self._single(exact, path)
        segments = [s for s in _norm_path(text).split('/') if s]
        if not segments:
            raise UnknownFile('empty file path')
        first = segments[0]
        if first == 'MF':
            current = self._mf
        else:
            current = self._lookup_one(first, path)
        for segment in segments[1:]:
            children = self._children.get(current.path, []) if current else []
            matches = [c for c in children
                       if _norm(c.name) == segment or c.fid == segment]
            if not matches:
                raise UnknownFile('unknown path %r (no %r under %s)'
                                  % (path, segment, current.path if current
                                     else '?'))
            current = self._single(matches, path)
        if current is None:
            raise UnknownFile('unknown file path %r' % path)
        return current

    def _lookup_one(self, key, selector):
        for index in (self._by_name, self._by_fid, self._by_aid):
            matches = index.get(key)
            if matches:
                return self._single(matches, selector)
        raise UnknownFile('unknown path %r' % selector)

    # ---- tree -----------------------------------------------------------

    def children(self, parent):
        """The direct children of a DF/ADF/MF (name, FID or path), in order."""
        if isinstance(parent, FileInfo):
            parents = [parent]
        else:
            text = str(parent or '').strip()
            if not text:
                return []
            parents = []
            for candidate in self._by_path.get(_norm_path(text), []):
                parents.append(candidate)
            if not parents:
                key = _norm(text)
                for index in (self._by_name, self._by_fid, self._by_aid):
                    if index.get(key):
                        parents.extend(index[key])
                    if parents:
                        break
        out = []
        seen = set()
        for info in parents:
            for child in self._children.get(info.path, []):
                if child.path not in seen:
                    seen.add(child.path)
                    out.append(child)
        return out

    def iter_all(self):
        """Every known file, in model order (the MF root first)."""
        return iter(self._files)

    def __len__(self):
        return len(self._files)

    def path_chain(self, selector):
        """The ordered selectors to SELECT to reach a file.

        A 2-byte FID is selected by FID (``00A4 0004 02 <fid>``); an ADF is
        selected by its AID (its ``84`` DF name), because an ADF has no FID.
        ``MF`` is ``3F00``, so ``EF.ADN`` under DF.TELECOM yields
        ``['3F00', '7F10', '6F3A']`` and a USIM file starts with the USIM AID.
        """
        info = self.resolve(selector)
        chain = []
        node = info
        while node is not None:
            if node.kind == 'mf':
                chain.append('3F00')
                break
            if node.kind == 'adf':
                if not node.aid:
                    raise FileModelError(
                        'ADF %s has no AID; it cannot be selected' % node.name)
                chain.append(node.aid)
                break
            segment = node.fid or node.name
            if not segment:
                raise FileModelError('file %s has no FID; it cannot be '
                                     'selected' % node.name)
            chain.append(segment)
            node = self._parent_info(node)
        chain.reverse()
        return chain

    def _parent_info(self, info):
        if info.kind == 'adf':
            return None
        parent_path = info.path.rsplit('/', 1)[0] if '/' in info.path else ''
        if not parent_path:
            return None
        parents = self._by_path.get(parent_path)
        if parents:
            return parents[0]
        raise FileModelError(
            'parent %s of %s is not in the model'
            % (parent_path or '?', info.name))
