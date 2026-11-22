# -*- coding: utf-8 -*-
"""vault_tools 的自测：临时库里跑快照/对比/回滚/改名/孤立附件。"""
import shutil
import tempfile
import unittest
from pathlib import Path

import vault_tools as V


def build_vault(root):
    root = Path(root)
    (root / '知识库').mkdir(parents=True)
    (root / '.obsidian').mkdir()
    (root / '知识库' / 'A 笔记.md').write_text('---\ndate: 2026-11-19\n---\n# 标题甲\n\n内容\n', encoding='utf-8')
    (root / '知识库' / '未命名.md').write_text('# 真正的标题\n\n- 列表\n', encoding='utf-8')
    (root / '知识库' / ' bad  name .md').write_text('bad\n', encoding='utf-8')
    (root / '知识库' / 'a  b.md').write_text('spaces\n', encoding='utf-8')
    (root / '知识库' / '引用.md').write_text('![[pic.png]]\n', encoding='utf-8')
    (root / '知识库' / 'pic.png').write_bytes(b'\x89PNG\r\n\x1a\nfake')
    (root / '知识库' / '孤儿.png').write_bytes(b'\x89PNG\r\n\x1a\norphan')
    (root / '.obsidian' / 'app.json').write_text('{}', encoding='utf-8')
    return root


class VaultToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='vt-'))
        self.vault = build_vault(self.tmp / 'vault')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_note_files_skips_tool_dirs(self):
        rels = [rel for rel, _ in V.note_files(self.vault)]
        self.assertIn('知识库/引用.md', rels)
        self.assertFalse(any(r.startswith('.obsidian') for r in rels))

    def test_snapshot_and_list(self):
        info = V.snapshot(self.vault, keep=5)
        self.assertTrue(Path(info['path']).is_file())
        self.assertEqual(info['count'], len(V.vault_files(self.vault)))
        items = V.list_snapshots(self.vault)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['count'], info['count'])
        image = self.vault / '知识库' / 'pic.png'
        original = image.read_bytes()
        image.unlink()
        V.restore(self.vault, info['path'], rels=['知识库/pic.png'])
        self.assertEqual(image.read_bytes(), original)

    def test_snapshot_excludes_obsidian_program_but_not_notes_folder(self):
        program = self.vault / 'Obsidian'
        (program / 'resources').mkdir(parents=True)
        (program / 'Obsidian.exe').write_bytes(b'executable')
        (program / 'resources' / 'app.asar').write_bytes(b'app')
        (program / 'README.md').write_text('程序文件', encoding='utf-8')
        note_folder = self.vault / '学习' / 'Obsidian'
        note_folder.mkdir(parents=True)
        (note_folder / '笔记.md').write_text('笔记', encoding='utf-8')
        info = V.snapshot(self.vault)
        with __import__('zipfile').ZipFile(info['path']) as z:
            self.assertNotIn('Obsidian/Obsidian.exe', z.namelist())
            self.assertIn('学习/Obsidian/笔记.md', z.namelist())

    def test_diff_and_restore(self):
        snap = V.snapshot(self.vault)
        target = self.vault / '知识库' / 'A 笔记.md'
        original = target.read_text(encoding='utf-8')
        target.write_text('改坏了\n', encoding='utf-8')
        (self.vault / '知识库' / '新文件.md').write_text('new\n', encoding='utf-8')

        result = V.diff(self.vault, snap['path'])
        self.assertIn('知识库/A 笔记.md', result['modified'])
        self.assertIn('知识库/新文件.md', result['added'])

        V.restore(self.vault, snap['path'], rels=['知识库/A 笔记.md'])
        self.assertEqual(target.read_text(encoding='utf-8'), original)
        # 回滚前应当自动留了快照
        self.assertGreaterEqual(len(V.list_snapshots(self.vault)), 2)

    def test_rename_plan_and_apply(self):
        plan = V.rename_plan(self.vault, date_prefix=True)
        news = {rel: Path(new).name for rel, new, _reason in plan}
        self.assertEqual(V.clean_name('bad:name?.md'), 'bad name .md')
        self.assertEqual(news.get('知识库/a  b.md'), 'a b.md')
        self.assertEqual(news.get('知识库/ bad  name .md'), 'bad name.md')
        self.assertEqual(news.get('知识库/未命名.md'), '真正的标题.md')
        self.assertEqual(news.get('知识库/A 笔记.md'), '2026-11-19 A 笔记.md')

        result = V.rename_apply(self.vault, plan)
        self.assertEqual(len(result['skipped']), 0)
        self.assertTrue((self.vault / '知识库' / '真正的标题.md').is_file())

    def test_rename_skip_when_target_exists(self):
        (self.vault / '知识库' / 'a b.md').write_text('占位\n', encoding='utf-8')
        plan = [('知识库/a  b.md', str(self.vault / '知识库' / 'a b.md'), '多余空格')]
        result = V.rename_apply(self.vault, plan)
        self.assertEqual(len(result['done']), 0)
        self.assertEqual(len(result['skipped']), 1)

    def test_rename_replace_and_affixes_respect_selected_scope(self):
        plan = V.rename_plan(self.vault, files={'知识库/a  b.md'},
                             replace_from='a', replace_to='new', prefix='前-', suffix='-后')
        self.assertEqual(len(plan), 1)
        self.assertEqual(Path(plan[0][1]).name, '前-new b-后.md')
        with self.assertRaises(ValueError):
            V.rename_plan(self.vault, files={'知识库/a  b.md'}, prefix='bad:')

    def test_undo_partial_keeps_skipped_items_in_history(self):
        plan = V.rename_plan(self.vault, files={'知识库/a  b.md', '知识库/未命名.md'})
        V.rename_apply(self.vault, plan)
        (self.vault / '知识库' / 'a  b.md').write_text('新文件', encoding='utf-8')
        result = V.undo_rename(self.vault)
        self.assertEqual(len(result['restored']), 1)
        self.assertEqual(len(result['skipped']), 1)
        self.assertEqual(len(V.rename_batches(self.vault)[0]['items']), 1)

    def test_rename_undo(self):
        plan = V.rename_plan(self.vault)
        V.rename_apply(self.vault, plan)
        self.assertTrue((self.vault / '知识库' / '真正的标题.md').is_file())

        batches = V.rename_batches(self.vault)
        self.assertEqual(len(batches), 1)
        result = V.undo_rename(self.vault)
        self.assertEqual(len(result['skipped']), 0)
        self.assertTrue((self.vault / '知识库' / '未命名.md').is_file())
        self.assertFalse((self.vault / '知识库' / '真正的标题.md').is_file())
        self.assertEqual(V.rename_batches(self.vault), [])

    def test_orphans(self):
        plan = V.orphan_plan(self.vault)
        names = [Path(rel).name for rel, _size in plan]
        self.assertIn('孤儿.png', names)
        self.assertNotIn('pic.png', names)

        moved = V.orphan_apply(self.vault, plan)
        self.assertEqual(len(moved), len(plan))
        self.assertFalse((self.vault / '知识库' / '孤儿.png').exists())
        self.assertTrue(any((self.vault / V.ORPHAN_DIR).glob('孤儿*.png')))

    def test_prune_keeps_latest(self):
        for _ in range(3):
            V.snapshot(self.vault, keep=2)
        self.assertEqual(len(V.list_snapshots(self.vault)), 2)


if __name__ == '__main__':
    unittest.main()
