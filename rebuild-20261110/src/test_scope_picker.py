"""范围窗口的读取、保存和取消回归。"""

from contextlib import nullcontext
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from notion_connection import SyncError
from scope_picker import ScopePicker


def category(title, code):
    pid = code * 32
    return {'id': pid, 'title': title, 'kind': 'page', 'url': 'https://app.notion.com/p/' + pid}


class ScopePickerTest(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest('Tk 窗口不可用：%s' % error)
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.saved = []

    def wait_for(self, predicate):
        limit = time.monotonic() + 2
        while time.monotonic() < limit:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail('未收到范围窗口的预期状态')

    def picker(self, saved=()):
        return ScopePicker(self.root, saved, lambda *_: None, lambda _: object(),
                           nullcontext, lambda _: None, self.saved.append)

    def test_loads_top_level_and_reuses_last_selection(self):
        chosen = category('已选类别', 'a')
        with patch('sync_engine.discover_roots', return_value=[chosen]) as discover:
            picker = self.picker([chosen])
            tree = picker.tree
            self.wait_for(lambda: bool(tree.get_children()))
            [row] = tree.get_children()
            self.assertEqual(tree.item(row, 'text'), '☑ 已选类别')
            discover.assert_called_once()
            picker.save_button.invoke()
            self.assertEqual(self.saved, [[chosen]])

    def test_initial_read_failure_blocks_save_and_allows_retry(self):
        chosen = category('重试后读到', 'b')
        with patch('sync_engine.discover_roots', side_effect=[SyncError('模拟连接失败'), [chosen]]):
            picker = self.picker([chosen])
            self.wait_for(lambda: '模拟连接失败' in picker.status.get())
            self.assertEqual(picker.tree.get_children(), ())
            self.assertTrue(picker.save_button.instate(['disabled']))
            picker.retry_button.invoke()
            self.wait_for(lambda: bool(picker.tree.get_children()))
            self.assertEqual(picker.tree.item(picker.tree.get_children()[0], 'text'), '☑ 重试后读到')

    def test_children_can_retry_and_empty_selection_does_not_save(self):
        parent = category('上级类别', 'a')
        leaf = category('目标子类别', 'b')
        with patch('sync_engine.discover_roots', return_value=[parent]), \
             patch('sync_engine.discover_children', side_effect=[SyncError('模拟读取失败'), [leaf]]):
            picker = self.picker()
            tree = picker.tree
            self.wait_for(lambda: bool(tree.get_children()))
            [row] = tree.get_children()
            tree.focus(row)
            picker.expand(None)
            self.wait_for(lambda: '重新展开可重试' in picker.status.get())
            picker.expand(None)
            self.wait_for(lambda: row in picker.loaded)
            [child] = tree.get_children(row)
            picker.toggle(child)
            picker.none_button.invoke()
            picker.save_button.invoke()
            self.assertFalse(self.saved)
            picker.toggle(child)
            picker.save_button.invoke()
            self.assertEqual(self.saved, [[leaf]])

    def test_close_signals_worker_and_discards_late_response(self):
        started, cancelled = threading.Event(), threading.Event()

        def read(_client, _log, cancel):
            started.set()
            if cancel.wait(1):
                cancelled.set()
            return [category('关闭后返回', 'a')]

        with patch('sync_engine.discover_roots', side_effect=read):
            picker = self.picker()
            self.wait_for(started.is_set)
            picker.window.destroy()
            self.wait_for(cancelled.is_set)
            self.assertIsNone(picker.poll_id)
            self.assertFalse(self.saved)


if __name__ == '__main__':
    unittest.main()
