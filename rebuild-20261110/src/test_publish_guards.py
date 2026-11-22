import contextlib
import importlib.util
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app_out
import app
import export_engine
import journal
import path_engine
import write_guard
from notion_connection import SyncError


class PublishGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        (self.vault / '.obsidian').mkdir(parents=True)
        (self.vault / 'notes').mkdir()
        self.home = self.root / 'app'
        self.home.mkdir()

    def upload(self):
        return app_out.push_folders(self.vault, ['notes'], '', 'skip', lambda _: None,
                                    threading.Event(), home=self.home)

    def test_folder_upload_refuses_a_different_workspace_before_planning(self):
        state = dict(export_engine.empty_state(), workspace_id='workspace-a')
        export_engine.write_json(export_engine.state_path(self.vault), state)
        with patch.object(app_out, 'SingleRun', return_value=contextlib.nullcontext()), \
             patch.object(app_out, 'Auth'), patch.object(app_out, 'MCP') as mcp, \
             patch.object(path_engine, 'plan_paths') as plan:
            mcp.return_value.call.return_value = {'workspace': {'id': 'workspace-b'}}
            plan.return_value = {'counts': {}, 'actions': []}
            with self.assertRaisesRegex(SyncError, '工作区'):
                self.upload()
            plan.assert_not_called()
            mcp.return_value.write.assert_not_called()

    def test_download_also_respects_a_vault_bound_by_upload(self):
        export_engine.write_json(export_engine.state_path(self.vault),
                                 dict(export_engine.empty_state(), workspace_id='workspace-a'))
        with patch.object(app, 'SingleRun', return_value=contextlib.nullcontext()), \
             patch.object(app, 'Auth'), patch.object(app, 'MCP') as mcp, \
             patch.object(app, 'collect') as collect:
            mcp.return_value.call.return_value = {'workspace': {'id': 'workspace-b'}}
            with self.assertRaisesRegex(SyncError, '上传绑定'):
                app.sync(self.vault, lambda _: None, threading.Event(), home=self.home)
            collect.assert_not_called()

    def test_ambiguous_attachment_stops_before_any_remote_write(self):
        for subdir in ('one', 'two'):
            folder = self.vault / subdir
            folder.mkdir()
            (folder / 'same.png').touch()
        (self.vault / 'notes' / 'a.md').write_text('before\n\n![[same.png]]\n\nend', encoding='utf-8')
        with patch.object(app_out, 'SingleRun', return_value=contextlib.nullcontext()), \
             patch.object(app_out, 'Auth'), patch.object(app_out, 'MCP') as mcp, \
             patch.object(path_engine.Resolver, 'folder_page', return_value='f' * 32), \
             patch.object(path_engine.Resolver, 'find_note', return_value=None):
            mcp.return_value.call.return_value = {'workspace': {'id': 'workspace-a'}}
            mcp.return_value.write.side_effect = AssertionError('remote write must not be attempted')
            with self.assertRaisesRegex(SyncError, '同名'):
                self.upload()
            mcp.return_value.write.assert_not_called()

    def test_explicit_note_relative_path_selects_the_intended_image(self):
        folder = self.vault / 'notes' / 'assets'
        folder.mkdir()
        (folder / 'picture.png').write_bytes(b'synthetic fixture')
        (self.vault / 'picture.png').write_bytes(b'another synthetic fixture')
        attachments = app_out.UploadAttachments(self.vault, None, None)
        self.assertEqual(attachments.resolve('notes/a.md', './assets/picture.png'), folder / 'picture.png')

    def test_upload_rejects_an_image_changed_since_preview(self):
        note = self.vault / 'notes' / 'a.md'
        note.write_text('before\n\n![[fixture.png]]\n\nend', encoding='utf-8')
        photo = self.vault / 'notes' / 'fixture.png'
        photo.write_bytes(b'first image')
        with patch.object(app_out, 'SingleRun', return_value=contextlib.nullcontext()), \
             patch.object(app_out, 'Auth'), patch.object(app_out, 'MCP') as mcp, \
             patch.object(path_engine.Resolver, 'folder_page', return_value=None), \
             patch.object(path_engine.Resolver, 'find_note', return_value=None):
            client = mcp.return_value
            client.call.return_value = {'workspace': {'id': 'workspace-a', 'name': 'Fixture'}}
            preview = app_out.push_folders(self.vault, ['notes'], '', 'skip', lambda _: None,
                                          threading.Event(), home=self.home, dry_run=True)
            photo.write_bytes(b'other image')
            with self.assertRaisesRegex(SyncError, '上传清单已变化'):
                app_out.push_folders(self.vault, ['notes'], '', 'skip', lambda _: None,
                                     threading.Event(), home=self.home,
                                     expected_preview=preview['preview_fingerprint'])
            client.write.assert_not_called()
        self.assertFalse(write_guard.pending_path(self.vault).exists())

    def test_interrupted_batch_preserves_per_note_outcomes_and_bound_workspace(self):
        names = ('notes/first.md', 'notes/second.md', 'notes/third.md')
        actions = [{'kind': 'create', 'rel': name, 'title': name, 'folder': '',
                    'hash': name, 'notion': 'plain test body'} for name in names]
        remote = {'text': '<content>plain test body</content>',
                  'page_last_edited_at': '2026-01-01T00:00:01Z'}
        with patch.object(app_out, 'SingleRun', return_value=contextlib.nullcontext()), \
             patch.object(app_out, 'Auth'), patch.object(app_out, 'MCP') as mcp, \
             patch.object(path_engine, 'plan_paths', return_value={'anchor': '', 'actions': actions, 'counts': {}}):
            client = mcp.return_value
            client.call.side_effect = [{'workspace': {'id': 'workspace-a'}}, remote]
            client.write.side_effect = [{'pages': [{'id': 'a' * 32}]},
                                        SyncError('simulated uncertain write', code='ambiguous_write')]
            with self.assertRaises(SyncError):
                self.upload()
        pending = write_guard.load(self.vault)
        self.assertEqual(pending['paths'], list(names))
        self.assertEqual(pending['entries'][names[0]], {'status': 'confirmed', 'page': 'a' * 32,
                                                       'body_hash': export_engine.digest('plain test body')})
        self.assertEqual(pending['entries'][names[1]], {'status': 'started', 'page': None})
        self.assertNotIn(names[2], pending['entries'])
        state = export_engine.load_state(self.vault)
        self.assertEqual(state['workspace_id'], 'workspace-a')
        self.assertEqual(list(state['files']), [names[0]])

    def test_review_reads_remote_body_without_changing_the_marker_or_state(self):
        rel = 'notes/a.md'
        export_engine.write_json(export_engine.state_path(self.vault),
                                 dict(export_engine.empty_state(), workspace_id='workspace-a'))
        operation = write_guard.begin(self.vault, [rel])
        write_guard.reporter(self.vault, operation)(rel, 'started', 'a' * 32, export_engine.digest('test body'))
        marker = write_guard.pending_path(self.vault)
        original_marker = marker.read_bytes()
        original_state = export_engine.state_path(self.vault).read_bytes()
        log = []
        with patch.object(app_out, 'SingleRun', return_value=contextlib.nullcontext()), \
             patch.object(app_out, 'Auth'), patch.object(app_out, 'MCP') as mcp:
            client = mcp.return_value
            client.call.side_effect = [{'workspace': {'id': 'workspace-a'}},
                                       {'text': '<content>test body</content>',
                                        'page_last_edited_at': '2026-01-01T00:00:01Z'}]
            app_out.review_upload(self.vault, log.append, threading.Event())
            client.write.assert_not_called()
        self.assertIn('云端正文与计划一致  ' + rel, log)
        self.assertEqual(marker.read_bytes(), original_marker)
        self.assertEqual(export_engine.state_path(self.vault).read_bytes(), original_state)

    def test_post_write_body_mismatch_does_not_acknowledge_local_hash(self):
        page = {'text': '<content>first\n\nold\n\nlast</content>',
                'page_last_edited_at': '2026-01-01T00:00:01Z'}
        changed = dict(page, text='<content>first\n\nconcurrent\n\nlast</content>')
        action = {'kind': 'update', 'rel': 'notes/a.md', 'title': 'first', 'page': 'a' * 32,
                  'folder': '', 'hash': 'local-new', 'notion': 'first\n\nnew\n\nlast',
                  '_remote_guard': export_engine.remote_guard(page)}
        state = export_engine.empty_state()
        with patch.object(path_engine, 'Resolver') as resolver, patch.object(app_out, 'MCP') as mock:
            resolver.return_value.anchor.return_value = (None, True)
            resolver.return_value.folder_page.return_value = None
            client = mock.return_value
            client.call.side_effect = [page, changed]
            client.write.return_value = {}
            with self.assertRaises(SyncError) as caught:
                path_engine.apply_paths(client, self.vault, {'anchor': '', 'actions': [action]},
                                        state, 'test', write_ids=False)
        self.assertEqual(caught.exception.code, 'ambiguous_write')
        self.assertEqual(state['files'], {})

    def test_notion_hosted_image_must_have_the_uploaded_bytes(self):
        digest = export_engine.digest(b'synthetic image bytes')
        reference = 'notion-file-block://00000000-0000-0000-0000-000000000000/fixture?name=fixture.png'
        expected, count = export_engine.prepared_body('first\n\n![fixture](file-upload://a)\n',
                                                     {'file-upload://a': digest})
        page = {'text': '<content>first\n\n<image source="' + reference + '">\nfixture\n</image></content>',
                'page_last_edited_at': '2026-01-01T00:00:01Z'}
        with patch.object(app_out, 'MCP') as mock, patch.object(export_engine, 'image_fingerprint') as fetch:
            client = mock.return_value
            client.call.return_value = page
            fetch.return_value = digest
            self.assertEqual(export_engine.confirm_written_version(client, 'a' * 32, expected, count),
                             page['page_last_edited_at'])
            fetch.assert_called_once_with(client, reference)
            fetch.return_value = export_engine.digest(b'other bytes')
            with self.assertRaises(SyncError) as caught:
                export_engine.confirm_written_version(client, 'a' * 32, expected, count)
        self.assertEqual(caught.exception.code, 'ambiguous_write')

    def test_default_diagnostics_does_not_share_private_log_lines(self):
        session = journal.Journal(self.home, 'test', vault=str(self.vault))
        self.addCleanup(session.close)
        session.write('正在读取：测试隐私标题')
        package = session.export(self.root / 'diagnostics.zip')
        with zipfile.ZipFile(package) as archive:
            data = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist())
        self.assertNotIn('测试隐私标题', data)
        self.assertNotIn(str(self.vault).replace('\\', '\\\\'), data)

    def installer_fixture(self):
        location = Path(__file__).resolve().parents[2] / 'installer' / 'installer.py'
        spec = importlib.util.spec_from_file_location('guyue_installer', location)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        payload = self.root / 'payload.zip'
        with zipfile.ZipFile(payload, 'w') as package:
            package.writestr(installer.MAIN_EXE, b'new launcher')
            package.writestr(installer.COMPONENTS + '/new.dll', b'new runtime')
        return installer, payload

    def test_installer_explains_destination_creation_denied(self):
        installer, payload = self.installer_fixture()
        destination = self.root / 'denied'
        with patch.object(Path, 'mkdir', side_effect=PermissionError(13, 'access denied', str(destination))):
            with self.assertRaisesRegex(installer.InstallPermissionError, '创建安装目录') as denied:
                installer.install_files(payload, destination)
        self.assertEqual(denied.exception.folder, destination.resolve())
        self.assertIn('使用默认位置', str(denied.exception))
        self.assertFalse(destination.exists())

    def test_installer_stages_inside_destination(self):
        installer, payload = self.installer_fixture()
        destination = self.root / 'installed'
        destination.mkdir()
        with patch.object(installer.tempfile, 'mkdtemp', wraps=tempfile.mkdtemp) as create_stage:
            exe = installer.install_files(payload, destination)
            create_stage.assert_called_once_with(prefix='.guyue-install-', dir=destination.resolve())
        self.assertEqual(exe.read_bytes(), b'new launcher')
        self.assertFalse(list(destination.glob('.guyue-install-*')))

    def test_installer_keeps_previous_files_when_registration_fails(self):
        installer, payload = self.installer_fixture()
        destination = self.root / 'installed'
        components = destination / installer.COMPONENTS
        components.mkdir(parents=True)
        old = destination / installer.MAIN_EXE
        old.write_bytes(b'old launcher')
        (components / 'old.dll').write_bytes(b'old runtime')

        def rejected(target, executable):
            self.assertEqual(executable.read_bytes(), b'new launcher')
            raise PermissionError('simulated rejected registry write')

        with self.assertRaisesRegex(PermissionError, '写入卸载信息'):
            installer.install_files(payload, destination, register=rejected)
        self.assertEqual(old.read_bytes(), b'old launcher')
        self.assertEqual([path.name for path in components.iterdir()], ['old.dll'])
