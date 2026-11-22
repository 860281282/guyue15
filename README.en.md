# Guyue15 | Notion and Obsidian Sync Tool

**[Download Full 2.9.7](https://github.com/860281282/guyue15/releases/download/v2.9.7/guyue15-2.9.7-windows-x64-portable.zip)** · **[Download Lite 1.0.0](https://github.com/860281282/guyue15/releases/download/lite-v1.0.0/guyue15-lite-1.0.0-windows-x64-portable.zip)** · [简体中文](README.md)

Bring selected Notion pages into a local Obsidian vault, or upload Markdown notes from Obsidian to Notion. Choose the scope of each sync, review the plan before a manual upload, and compare versions when the same note changes on both sides.

## Choose an edition

Both editions are portable Windows x64 apps.

| Edition | Best for | Open after extracting |
| --- | --- | --- |
| **Full 2.9.7** | Syncing notes, checking and organizing a vault, and converting documents | `咕月15.exe`. Keep the `同步组件` components folder alongside it. |
| **Lite 1.0.0** | Importing Notion pages and previewing and uploading local notes | `咕月15轻量版.exe`. The app runs from a single executable. |

Lite stores its own settings and Notion authorization. When both editions use the same vault, they read the same sync state from that vault and use a shared lock to coordinate operations.

## Quick start

1. Download the ZIP for your edition and select **Extract All** in File Explorer. Open the executable listed above.
2. Click **连接 Notion** and complete authorization in your browser, then select an existing Obsidian vault.
3. To import, choose the Notion categories or subcategories, then click **同步到本地** in Full or **导入到本地** in Lite. To upload, choose the local folders, click **预览上传**, and review the changes and destination before confirming.

### Chinese controls

The Windows app uses Chinese menus. These are the labels to look for.

| Look for | Meaning |
| --- | --- |
| **连接 Notion** | Connect to Notion |
| **更换知识库** (Full) / **选择知识库** (Lite) | Choose an Obsidian vault |
| **下载到本地** (Full) / **导入到本地** (Lite) | Open the local import view |
| **上传到 Notion** | Open the upload view |
| **选择类别** / **设上传范围** (Full), **选择范围** (Lite) | Select the categories or folders to sync |
| **同步到本地** (Full) / **导入到本地** (Lite) | Run the import |
| **预览上传** / **确认上传** | Preview the upload / Confirm the upload |
| **修掉 Notion 标记** (Full) / **清理 Notion 标记** (Lite) | Clean up Notion markup during import |
| **查看待核对的上传** (Full) / **核对上次上传** (Lite) | Review the previous upload |
| **处理冲突** / **工具箱** | Review sync conflicts / Open the vault tools in Full |

## How sync works

1. **Connect to the workspace.** The app completes Notion OAuth in your browser, uses Notion MCP to read or write pages, and stores authorization in Windows Credential Manager.
2. **Read and render.** The importer discovers subpages in the selected scope, reads data page by page, and renders Markdown and supported attachments in a staging directory. It uses Notion page IDs to find existing note paths and compares the previous sync state, local file, and remote content. For a Notion database, keep an unfiltered table or list view that shows all records.
3. **Plan an upload.** A manual upload starts with a preview of creations, updates, and conflicts. On confirmation, the app checks the workspace, note contents, and attachments again. If the plan has changed, it requests a new preview.
4. **Keep versions available for review.** If a note changed locally and remotely, the importer keeps the local file and saves a separate copy of the Notion version for comparison. Interrupted uploads or writes that need confirmation leave a record in the vault for review.

Start with the [Notion connection](rebuild-20261110/src/notion_connection.py), [import and merge](rebuild-20261110/src/sync_engine.py), [upload entry point](rebuild-20261110/src/app_out.py), and [pending upload records](rebuild-20261110/src/write_guard.py). Lite's desktop entry point and sync modules are in [`lite/src/`](lite/src/).

## More tools and options

| Edition | What you can do |
| --- | --- |
| **Full · Inspect** | Find broken links, duplicate note names, and unreferenced attachments. View vault statistics. |
| **Full · Organize** | Create snapshots, review conflicts, rename files in batches, and find and replace text. |
| **Full · Export** | Convert supported document formats or combine notes into a Word or PDF document. |
| **Lite · Sync options** | Clean up Notion markup on import. Choose root-level notes and a top-level Notion page for uploads, and optionally write tags and dates. |
| **Lite · Review** | Check known page states after an interrupted upload and clear the review record once you have checked them. |

## Run and build from source

Use Windows x64 Python 3.13 with Tcl/Tk. Run the commands from the repository root.

### Full

To start the app from source, run `py -3.13 .\rebuild-20261110\src\suite.py`. To build the app and installer, run the following commands.

```powershell
py -3.13 -m pip install -r requirements-build.txt
py -3.13 .\build_release.py
```

Build output is written to `release-20261118`.

### Lite

To start the app from source, run `py -3.13 .\lite\src\lite.py`. To build the single executable, run the following commands.

```powershell
py -3.13 -m pip install -r requirements-build.txt
py -3.13 .\lite\build.py
```

The single-file executable is written to `lite/dist`.

### Tests

Use a separate test directory.

```powershell
py -3.13 -m unittest discover -s rebuild-20261110/src
py -3.13 -m unittest discover -s lite/verification
```

## Feedback and license

- Include the app version, steps to reproduce, and redacted diagnostic information. Use sample notes to demonstrate the issue. [Open an issue](https://github.com/860281282/guyue15/issues)
- Source code for both editions is available under the MIT License. Copyright belongs to 咕月15. [View license](LICENSE)
