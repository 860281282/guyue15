import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

import organizer
import toolbox
import ui_kit


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class RenameUITest(unittest.TestCase):
    def test_selected_files_limit_preview(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            a, b = vault / 'a  b.md', vault / 'c  d.md'
            a.write_text('a', encoding='utf-8')
            b.write_text('b', encoding='utf-8')
            root = tk.Tk()
            root.withdraw()
            try:
                class Suite:
                    def write(self, *args): pass
                win = organizer.open_rename(root, Suite(), str(vault))
                widgets = list(descendants(win))
                tree = next(w for w in widgets if isinstance(w, ttk.Treeview))
                self.assertEqual(len(tree.get_children()), 0)
                choose = next(w for w in widgets if isinstance(w, ui_kit.RButton) and w.text == '选择文件')
                with patch.object(organizer.filedialog, 'askopenfilenames', return_value=(str(a),)):
                    choose.command()
                rows = [tree.item(i, 'values') for i in tree.get_children()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], 'a  b.md')
                self.assertTrue(a.exists() and b.exists())
                with patch.object(organizer.filedialog, 'askopenfilenames', return_value=(str(a), str(b))):
                    choose.command()
                rows = tree.get_children()
                self.assertEqual(len(rows), 2)
                tree.selection_set(rows[0])
                apply = next(w for w in widgets if isinstance(w, ui_kit.RButton) and w.text == '执行选中')
                with patch.object(organizer.messagebox, 'askyesno', return_value=True), \
                     patch.object(organizer.messagebox, 'showinfo'):
                    apply.command()
                self.assertTrue((vault / 'a b.md').exists())
                self.assertTrue(b.exists())
            finally:
                root.destroy()

    def test_conversion_source_is_detected_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'note.md'
            note.write_text('# Hello', encoding='utf-8')
            root = tk.Tk()
            root.withdraw()
            try:
                class Suite:
                    def __init__(self): self.root = root
                    def write(self, *args): pass
                    def run_task(self, *args): pass
                panel = toolbox.build_panel(Suite(), root)
                widgets = list(descendants(panel.master.master))
                combos = [w for w in widgets if isinstance(w, ttk.Combobox)]
                self.assertEqual(combos[0].get(), 'Word')
                button = next(w for w in widgets if isinstance(w, ttk.Button) and w.cget('text') == '选择文件')
                with patch.object(organizer.filedialog, 'askopenfilename', return_value=str(note)):
                    button.invoke()
                self.assertEqual(combos[0].get(), 'Markdown')
                self.assertNotIn('PDF', combos[0].cget('values'))
            finally:
                root.destroy()


if __name__ == '__main__':
    unittest.main()
