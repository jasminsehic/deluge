"""Verify Deluge's direct Python binding usage against the installed libtorrent ABI.

These tests deliberately inspect the real native extension. In particular, a
published 2.1 wheel can expose compatibility APIs that the custom ABI build
does not, so version-string checks alone are insufficient.
"""

import ast
from pathlib import Path

import pytest

from deluge._libtorrent import lt

from . import common

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _source_tree(path):
    return ast.parse((SOURCE_ROOT / path).read_text(encoding='utf-8'))


def _native_fields(path, native_attr):
    """Find direct self.<native_attr>.<field> accesses in production source."""
    fields = set()
    for node in ast.walk(_source_tree(path)):
        value = node.value if isinstance(node, ast.Attribute) else None
        if (
            isinstance(value, ast.Attribute)
            and value.attr == native_attr
            and isinstance(value.value, ast.Name)
            and value.value.id == 'self'
        ):
            fields.add(node.attr)
    return fields


def test_production_libtorrent_module_exports():
    """Audit every production Python module, including optional plugins."""
    if getattr(lt, 'api_version', 1) < 4:
        pytest.skip('This check targets the modern non-deprecated ABI')

    references = {}
    for source in SOURCE_ROOT.rglob('*.py'):
        if 'tests' in source.relative_to(SOURCE_ROOT).parts:
            continue
        tree = ast.parse(source.read_text(encoding='utf-8'))
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'deluge._libtorrent':
                aliases.update(
                    entry.asname or entry.name
                    for entry in node.names
                    if entry.name == 'lt'
                )
            elif isinstance(node, ast.Import):
                aliases.update(
                    entry.asname or entry.name
                    for entry in node.names
                    if entry.name == 'libtorrent'
                )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
            ):
                references.setdefault(node.attr, set()).add(
                    str(source.relative_to(SOURCE_ROOT))
                )

    # Only the guarded legacy path in metafile.py may use the removed API.
    optional_legacy = {'add_files': {'metafile.py'}}
    missing = {
        name: sorted(paths)
        for name, paths in references.items()
        if not hasattr(lt, name) and paths != optional_legacy.get(name)
    }
    assert not missing, f'Missing libtorrent module exports: {missing}'


def test_native_object_fields_used_by_core(tmp_path):
    """Check direct API fields on real session/handle/status/info/peer objects."""
    if getattr(lt, 'api_version', 1) < 4:
        pytest.skip('This check targets the modern non-deprecated ABI')

    with open(common.get_test_data_file('test.torrent'), 'rb') as source:
        params = lt.load_torrent_buffer(source.read())
    params.save_path = str(tmp_path)
    session = lt.session(
        {'enable_dht': False, 'enable_lsd': False, 'listen_interfaces': '127.0.0.1:0'}
    )
    handle = session.add_torrent(params)
    assert handle.is_valid()
    status = handle.status()
    ti = params.ti
    peer = lt.peer_info()

    contracts = [
        ('core/core.py', 'session', session, set()),
        ('core/torrentmanager.py', 'session', session, set()),
        ('core/torrent.py', 'handle', handle, set()),
        ('core/torrent.py', 'status', status, set()),
        (
            'core/torrent.py',
            'torrent_info',
            ti,
            {'creator', 'comment'},  # Guarded with hasattr() by Deluge.
        ),
    ]
    missing = {}
    for path, field, obj, conditional in contracts:
        required = _native_fields(path, field) - conditional
        absent = sorted(name for name in required if not hasattr(obj, name))
        if absent:
            missing[f'{path}:self.{field}'] = absent

    # The Peers tab previously failed because peer_info.ip was absent.
    peer_fields = set()
    for node in ast.walk(_source_tree('core/torrent.py')):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == 'peer':
                peer_fields.add(node.attr)
    peer_fields.discard('ip')  # Only read by Deluge's legacy fallback.
    absent = sorted(name for name in peer_fields if not hasattr(peer, name))
    if absent:
        missing['core/torrent.py:peer_info'] = absent

    assert not missing, f'Missing native object fields: {missing}'
