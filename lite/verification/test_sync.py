"""在合成知识库和模拟 Notion 响应上运行双向同步。"""

from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import download
from notion_connection import SyncError
import path_engine
import upload
import write_guard


class LiteSync(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(sandbox.cleanup)
        self.home = Path(sandbox.name) / 'profile'
        self.vault = Path(sandbox.name) / 'vault'
        (self.vault / '.obsidian').mkdir(parents=True)
        self.account = {'workspace': {'id': 'fixture', 'name': '隔离工作区'}}

    def prep(self, module):
        stack = [patch.object(module, 'SingleRun', return_value=nullcontext()),
                 patch.object(module, 'Auth'), patch.object(module, 'MCP')]
        for replacement in stack:
            self.addCleanup(replacement.stop)
        _, _, mcp = (replacement.start() for replacement in stack)
        mcp.return_value.call.return_value = self.account
        return mcp.return_value

    def test_import_repairs_selected_notes_and_preserves_local_conflict(self):
        self.prep(download)
        page_id = 'a' * 32
        url = 'https://app.notion.com/p/' + page_id
        scope = [dict(title='示例笔记', url=url, kind='page')]
        fixture = {'workspace_id': 'fixture', 'scope': '选定页面', 'databases': {}, 'pages': {
            page_id: {'title': '示例笔记', 'url': url, 'metadata': {'type': 'page'},
                      'text': '<content><ul><li>列表</li></ul>\n\n`<br>`</content>'},
        }}
        with patch.object(download, 'collect', return_value=fixture) as collect:
            result = download.sync(self.vault, lambda _: None, threading.Event(), home=self.home,
                                   scope=scope, repair=True, expected_workspace='fixture')
            self.assertEqual(collect.call_args.kwargs['scope'], scope)
            self.assertEqual(result['repaired']['changed'], 1)
            note = next(self.vault.rglob('示例笔记.md'))
            body = note.read_text(encoding='utf-8')
            self.assertIn('- 列表', body)
            self.assertIn('`<br>`', body)
            note.write_text('个人修改', encoding='utf-8')
            fixture['pages'][page_id]['text'] = '<content>云端新正文</content>'
            result = download.sync(self.vault, lambda _: None, threading.Event(), home=self.home,
                                   scope=scope, repair=True, expected_workspace='fixture')
            self.assertEqual(result['counts']['conflicts'], 1)
            self.assertEqual(note.read_text(encoding='utf-8'), '个人修改')

    def test_import_refuses_workspace_change_before_collecting(self):
        self.prep(download)
        with patch.object(download, 'collect') as collect:
            with self.assertRaisesRegex(SyncError, '工作区'):
                download.sync(self.vault, lambda _: None, threading.Event(), home=self.home,
                              expected_workspace='other')
            collect.assert_not_called()

    def test_upload_requires_approved_preview_before_connecting(self):
        with patch.object(upload, 'Auth', side_effect=AssertionError('unexpected network')):
            with self.assertRaisesRegex(SyncError, '预览'):
                self.send(dry_run=False)

    def test_upload_preview_blocks_changed_note_and_confirms_matching_write(self):
        client = self.prep(upload)
        note = self.vault / '根目录笔记.md'
        note.write_text('最初的正文', encoding='utf-8')
        (self.vault / '另一个范围').mkdir()
        (self.vault / '另一个范围' / '不上传.md').write_text('独立范围', encoding='utf-8')
        with patch.object(path_engine.Resolver, 'find_note', return_value=None):
            preview = self.send()
            self.assertEqual([a['rel'] for a in preview['actions']], ['根目录笔记.md'])
            client.write.assert_not_called()
            note.write_text('预览后的新正文', encoding='utf-8')
            with self.assertRaisesRegex(SyncError, '清单已变化'):
                self.send(dry_run=False, expected_preview=preview['preview_fingerprint'])
            client.write.assert_not_called()
            self.assertFalse(write_guard.pending_path(self.vault).exists())
            preview = self.send()
            page_id = 'b' * 32
            client.write.return_value = {'pages': [{'id': page_id, 'url': 'https://app.notion.com/p/' + page_id}]}
            client.call.side_effect = [self.account, {'text': '<content>预览后的新正文</content>',
                                                     'page_last_edited_at': '2026-01-01T00:00:00Z'}]
            sent = self.send(dry_run=False, expected_preview=preview['preview_fingerprint'])
            self.assertEqual(sent['counts']['created'], 1)
            self.assertEqual(client.write.call_count, 1)
            self.assertFalse(write_guard.pending_path(self.vault).exists())
            self.assertIn(page_id, note.read_text(encoding='utf-8'))

    def test_interrupted_upload_is_blocked_until_reviewed(self):
        client = self.prep(upload)
        (self.vault / '测试.md').write_text('测试正文', encoding='utf-8')
        with patch.object(path_engine.Resolver, 'find_note', return_value=None):
            preview = self.send()
            client.write.side_effect = SyncError('结果无法确认', code='ambiguous_write')
            with self.assertRaisesRegex(SyncError, '结果无法确认'):
                self.send(dry_run=False, expected_preview=preview['preview_fingerprint'])
            self.assertTrue(write_guard.pending_path(self.vault).is_file())
            with self.assertRaisesRegex(SyncError, '未核对'):
                self.send(dry_run=False, expected_preview=preview['preview_fingerprint'])
            self.assertEqual(client.write.call_count, 1)

    def send(self, dry_run=True, expected_preview=None):
        return upload.push_folders(self.vault, [''], '', 'skip', lambda _: None, threading.Event(),
                                   home=self.home, dry_run=dry_run, expected_workspace='fixture',
                                   expected_preview=expected_preview)


if __name__ == '__main__':
    unittest.main()
