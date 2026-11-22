import base64
import tempfile
import json
import hashlib
import io
import os
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import book
import bulk
import core
import change_cache
import export_engine
import path_engine
import toolbox
import notion_connection
import vault_tools
import suite


class AuditFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_cross_line_search_matches_replacement(self):
        (self.vault / 'a.md').write_bytes('甲\n乙\n'.encode('utf-8'))
        self.assertEqual(len(bulk.search(self.vault, r'甲\n乙', True)), 1)
        self.assertEqual(bulk.replace(self.vault, r'甲\n乙', '丙', True)['hits'], 1)

    def test_equal_size_and_timestamp_content_change_is_detected(self):
        (self.vault / '.obsidian').mkdir()
        note = self.vault / 'a.md'
        note.write_bytes(b'abc')
        before = change_cache.snapshot(self.vault)
        info = note.stat()
        note.write_bytes(b'xyz')
        os.utime(note, ns=(info.st_atime_ns, info.st_mtime_ns))
        after = change_cache.snapshot(self.vault)
        self.assertEqual(change_cache.changed(before, after)[0], ['a.md'])

    def test_portable_obsidian_program_is_excluded_from_upload_scans(self):
        (self.vault / '.obsidian').mkdir()
        program = self.vault / 'Obsidian'
        (program / 'resources').mkdir(parents=True)
        (program / 'Obsidian.exe').write_bytes(b'exe')
        (program / 'resources' / 'app.asar').write_bytes(b'app')
        (program / 'README.md').write_text('app', encoding='utf-8')
        (self.vault / 'note.md').write_text('note', encoding='utf-8')
        self.assertEqual(set(change_cache.snapshot(self.vault)), {'note.md'})
        self.assertEqual([n['rel'] for n in export_engine.scan(self.vault)], ['note.md'])
        self.assertNotIn('Obsidian', [entry['name'] for entry in path_engine.list_folders(self.vault)])
        with self.assertRaises(notion_connection.SyncError):
            export_engine.scan(self.vault, 'Obsidian')

    def test_only_success_or_unchanged_is_marked_synced(self):
        result = {'actions': [{'rel': 'same.md', 'kind': 'unchanged'},
                              {'rel': 'conflict.md', 'kind': 'conflict'}],
                  'records': [{'path': 'ok.md', 'action': 'update'},
                              {'path': 'failed.md', 'action': 'failed'},
                              {'path': 'conflict.md', 'action': 'conflict'}]}
        self.assertEqual(change_cache.successful_paths(result), {'same.md', 'ok.md'})

    def test_push_changed_does_not_acknowledge_conflict(self):
        (self.vault / '.obsidian').mkdir()
        ok, conflict = self.vault / 'ok.md', self.vault / 'conflict.md'
        ok.write_bytes(b'old')
        conflict.write_bytes(b'old')
        index = change_cache.index_path(self.vault)
        before = change_cache.snapshot(self.vault)
        change_cache.save(index, before)
        ok.write_bytes(b'new')
        conflict.write_bytes(b'new')
        result = {'actions': [{'rel': 'ok.md', 'kind': 'update'}, {'rel': 'conflict.md', 'kind': 'conflict'}],
                  'records': [{'path': 'ok.md', 'action': 'update'}, {'path': 'conflict.md', 'action': 'conflict'}]}
        fake = SimpleNamespace(root=None, cancel=None, workspace_name='test',
                               upload_options=lambda: {'vault': str(self.vault), 'folders': [],
                                                       'anchor': '', 'policy': 'skip', 'write_attributes': False},
                               write=lambda *args: None, set_status=lambda *args: None,
                               run_task=lambda _title, job: job())
        with patch.object(suite.O, 'push_folders', return_value=result), \
             patch.object(suite.messagebox, 'askyesno', return_value=True):
            suite.Suite.push_changed(fake)
        saved = change_cache.load(index)
        now = change_cache.snapshot(self.vault)
        self.assertEqual(saved['ok.md'], now['ok.md'])
        self.assertEqual(saved['conflict.md'], before['conflict.md'])
        self.assertIn('conflict.md', change_cache.changed(saved, now)[0])

    def test_corrupt_change_index_does_not_become_empty(self):
        index = self.vault / 'bad.json'
        index.write_text('{bad', encoding='utf-8')
        with self.assertRaises(notion_connection.SyncError):
            change_cache.load(index)

    def test_replace_preserves_bom_and_windows_newlines(self):
        note = self.vault / 'a.md'
        note.write_bytes(b'\xef\xbb\xbf' + '甲\r\n乙\r\n'.encode('utf-8'))
        preview = bulk.replace(self.vault, '甲', r'\literal', dry_run=True)
        bulk.replace(self.vault, '甲', r'\literal', dry_run=False, expected=preview['basis'])
        self.assertEqual(note.read_bytes(), b'\xef\xbb\xbf' + '\\literal\r\n乙\r\n'.encode('utf-8'))

    def test_replace_refuses_if_note_changed_after_preview(self):
        note = self.vault / 'a.md'
        note.write_text('old value', encoding='utf-8')
        preview = bulk.replace(self.vault, 'old', 'new')
        note.write_text('old value edited', encoding='utf-8')
        with self.assertRaises(ValueError):
            bulk.replace(self.vault, 'old', 'new', dry_run=False, expected=preview['basis'])
        self.assertEqual(note.read_text(encoding='utf-8'), 'old value edited')

    def test_cross_line_search_matches_crlf_replace(self):
        (self.vault / 'a.md').write_bytes('甲\r\n乙'.encode('utf-8'))
        self.assertEqual(len(bulk.search(self.vault, r'甲\r\n乙', True)), 1)
        self.assertEqual(bulk.replace(self.vault, r'甲\r\n乙', '丙', True)['hits'], 1)

    def test_existing_embedded_attachment_not_a_broken_link(self):
        (self.vault / 'a.md').write_text('![[pic.png]]\n[[不存在]]', encoding='utf-8')
        (self.vault / 'pic.png').write_bytes(b'png')
        self.assertEqual(toolbox.broken_links(self.vault), [('a.md', '不存在')])

    def test_book_docx_embeds_relative_image(self):
        (self.vault / 'pic.png').write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        dst = self.vault / 'out.docx'
        book.to_docx([{'rel': 'a.md', 'title': '标题', 'text': '# 标题\n\n![图](pic.png)'}], dst, vault=self.vault)
        with zipfile.ZipFile(dst) as zf:
            self.assertTrue(any(name.startswith('word/media/') for name in zf.namelist()))

    def test_remote_changes_stop_upload(self):
        class Client:
            def __init__(self): self.writes = []
            def call(self, *args, **kwargs): return {'text': '新云端内容', 'page_last_edited_at': 'new'}
            def write(self, *args, **kwargs): self.writes.append((args, kwargs)); return {}
        class Resolver:
            def __init__(self, *args, **kwargs): self.created = []
            def anchor(self, *args): return None, True
            def folder_page(self, *args): return None
        client = Client()
        action = {'kind': 'update', 'rel': 'a.md', 'title': 'a', 'page': 'a' * 32,
                  'folder': '', 'hash': 'h', 'notion': 'local', 'urls': {},
                  '_remote_guard': export_engine.remote_guard({'text': '旧云端内容', 'page_last_edited_at': 'old'})}
        with patch.object(path_engine, 'Resolver', Resolver):
            result = path_engine.apply_paths(client, self.vault, {'anchor': '', 'actions': [action]}, {}, 'test', write_ids=False)
        self.assertFalse(client.writes)
        self.assertEqual(result['counts']['failed'], 1)

    def test_uncertain_remote_write_stops_remaining_batch(self):
        class Client:
            def __init__(self): self.writes = 0
            def call(self, *args, **kwargs): return {'text': '<content>前文 原内容 后文</content>', 'page_last_edited_at': 'v1'}
            def write(self, *args, **kwargs):
                self.writes += 1
                raise notion_connection.SyncError('结果不确定', code='ambiguous_write')
        class Resolver:
            def __init__(self, *args, **kwargs): self.created = []
            def anchor(self, *args): return None, True
            def folder_page(self, *args): return None
        guard = export_engine.remote_guard({'text': '<content>前文 原内容 后文</content>', 'page_last_edited_at': 'v1'})
        actions = [{'kind': 'update', 'rel': f'{i}.md', 'title': str(i), 'page': str(i) * 32,
                    'folder': '', 'hash': 'h', 'notion': '前文 local 后文', 'urls': {}, '_remote_guard': guard}
                   for i in (1, 2)]
        client = Client()
        with patch.object(path_engine, 'Resolver', Resolver):
            with self.assertRaises(notion_connection.SyncError):
                path_engine.apply_paths(client, self.vault, {'anchor': '', 'actions': actions}, {}, 'test', write_ids=False)
        self.assertEqual(client.writes, 1)

    def test_incomplete_remote_refuses_guard(self):
        with self.assertRaises(export_engine.SyncError):
            export_engine.remote_guard({'text': '部分内容', 'truncated': True})

    def test_search_failure_is_not_treated_as_absent_page(self):
        class Client:
            def call(self, *args, **kwargs):
                raise notion_connection.SyncError('读取失败')
        resolver = path_engine.Resolver(Client())
        with self.assertRaises(notion_connection.SyncError):
            resolver.find_note([], '测试')
        with self.assertRaises(notion_connection.SyncError):
            resolver.search_page('测试', [])

    def test_remote_guard_allows_unchanged_content(self):
        page = {'text': '内容', 'page_last_edited_at': 'v1'}
        action = {'_remote_guard': export_engine.remote_guard(page)}
        export_engine.check_remote_guard(action, dict(page))

    def test_uncertain_write_is_not_replayed(self):
        with patch.object(notion_connection.urllib.request, 'urlopen',
                          side_effect=urllib.error.URLError('lost response')) as send:
            with self.assertRaises(notion_connection.SyncError):
                notion_connection.http_json('https://example.test/mcp', {'write': True},
                                            retry_uncertain=False)
        self.assertEqual(send.call_count, 1)

    def test_uncertain_server_error_stops_write(self):
        err = urllib.error.HTTPError('https://example.test/mcp', 503, 'bad', {}, io.BytesIO(b'{}'))
        with patch.object(notion_connection.urllib.request, 'urlopen', side_effect=err) as send:
            with self.assertRaises(notion_connection.SyncError) as ctx:
                notion_connection.http_json('https://example.test/mcp', {'write': True},
                                            retry_uncertain=False)
        self.assertEqual(ctx.exception.code, 'ambiguous_write')
        self.assertEqual(send.call_count, 1)

    def test_blocked_rate_limit_is_not_retried(self):
        body = b'{"additional_data":{"rate_limit_reason":"public_api_request_blocked"}}'
        err = urllib.error.HTTPError('https://example.test/mcp', 429, 'blocked', {}, io.BytesIO(body))
        with patch.object(notion_connection.urllib.request, 'urlopen', side_effect=err) as send:
            with self.assertRaises(notion_connection.SyncError) as ctx:
                notion_connection.http_json('https://example.test/mcp', {})
        self.assertEqual(ctx.exception.code, 'public_api_request_blocked')
        self.assertEqual(send.call_count, 1)

    def test_corrupt_export_state_is_not_treated_as_new(self):
        state = export_engine.state_path(self.vault)
        state.parent.mkdir(parents=True)
        state.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(export_engine.SyncError):
            export_engine.load_state(self.vault)
        self.assertEqual(state.read_text(encoding='utf-8'), '{invalid')

    def test_restore_rejects_path_traversal_without_modifying_files(self):
        good = self.vault / 'a.md'
        good.write_text('原内容', encoding='utf-8')
        archive = self.vault / 'evil.zip'
        payload = b'bad'
        manifest = {'files': {'../escape.txt': hashlib.sha1(payload).hexdigest()}}
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('../escape.txt', payload)
            zf.writestr(vault_tools.MANIFEST_NAME, json.dumps(manifest))
        with self.assertRaises(ValueError):
            vault_tools.restore(self.vault, archive)
        self.assertEqual(good.read_text(encoding='utf-8'), '原内容')
        self.assertFalse((self.vault.parent / 'escape.txt').exists())

    def test_snapshot_preserves_user_manifest_file(self):
        (self.vault / 'manifest.json').write_text('用户内容', encoding='utf-8')
        info = vault_tools.snapshot(self.vault)
        (self.vault / 'manifest.json').unlink()
        vault_tools.restore(self.vault, info['path'])
        self.assertEqual((self.vault / 'manifest.json').read_text(encoding='utf-8'), '用户内容')

    def test_snapshot_checksum_failure_blocks_restore(self):
        (self.vault / 'a.md').write_text('原内容', encoding='utf-8')
        with patch.object(vault_tools, 'digest', return_value='invalid'):
            info = vault_tools.snapshot(self.vault)
        self.assertEqual(info['skipped'], ['a.md'])
        with self.assertRaises(ValueError):
            vault_tools.restore(self.vault, info['path'])
        self.assertEqual((self.vault / 'a.md').read_text(encoding='utf-8'), '原内容')

    def test_same_position_insertions_preserve_both_sides(self):
        text, conflicts = core.merge3('base\n', 'local\nbase\n', 'remote\nbase\n')
        self.assertEqual(conflicts, 1)
        self.assertIn('local\n', text)
        self.assertIn('remote\n', text)
        self.assertIn('base\n', text)


if __name__ == '__main__':
    unittest.main()
