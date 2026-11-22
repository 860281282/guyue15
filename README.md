# 咕月15｜Notion 与 Obsidian 同步工具

**[下载完整版 2.9.7](https://github.com/860281282/guyue15/releases/download/v2.9.7/guyue15-2.9.7-windows-x64-portable.zip)** · **[下载轻量版 1.0.0](https://github.com/860281282/guyue15/releases/download/lite-v1.0.0/guyue15-lite-1.0.0-windows-x64-portable.zip)** · [English](README.en.md)

把 Notion 中的资料导入本地 Obsidian 知识库，或把写好的 Markdown 笔记上传回 Notion。咕月15可以限定每次同步的范围，手动上传前列出计划，两边都改了同一篇笔记时也能逐条核对。

## 选哪个版本

两版均为 Windows x64 免安装程序。

| 版本 | 适合的用法 | 解压后打开 |
| --- | --- | --- |
| **完整版 2.9.7** | 同步笔记，也用工具箱检查、整理和转换文档 | `咕月15.exe`。将「同步组件」保留在同一目录 |
| **轻量版 1.0.0** | 主要用来导入 Notion 页面和预览、上传本地笔记 | `咕月15轻量版.exe`。程序为单文件 EXE |

轻量版的设置和 Notion 授权单独保存。若用两版打开同一个知识库，它们会读取该库中的同一份同步状态，并通过同一把同步锁约束并发操作。

## 快速开始

1. 下载对应的 ZIP，在资源管理器中选择「全部解压」，打开表中对应的 EXE。
2. 点击「连接 Notion」，在浏览器完成授权，再在程序中选择已有的 Obsidian 知识库。
3. **Notion → Obsidian** 时，选择要导入的类别或子类别，点击「同步到本地」或轻量版的「导入到本地」。**Obsidian → Notion** 时，选择本地范围，点击「预览上传」，核对清单和目标后再确认。

## 同步如何实现

1. **连接工作区。** 程序通过浏览器完成 Notion OAuth 授权，经由 Notion MCP 读取和写入页面，并用 Windows 凭据管理器保存授权。
2. **读取并生成笔记。** 按所选范围发现子页面，逐页读取数据，在暂存目录生成 Markdown 和受支持的附件。导入时用 Notion 页面 ID 找回原来对应的笔记路径，再比较前次同步、本地文件与云端内容。对 Notion 数据库，请保留一个能显示全部记录的无筛选表格或列表视图。
3. **先生成上传计划。** 手动上传的预览会列出新建、更新或冲突项。确认时重新核对目标工作区、笔记内容和附件。若计划已经变化，程序会要求重新预览。
4. **保留可核对的版本。** 导入遇到同一篇笔记的双端改动时，保留本地笔记并另存云端副本。上传中断或云端写入待核实时，把状态留在知识库内的待核对记录里，供下次继续检查。

实现入口可从 [Notion 连接层](rebuild-20261110/src/notion_connection.py)、[导入与合并](rebuild-20261110/src/sync_engine.py)、[上传入口](rebuild-20261110/src/app_out.py) 和 [待核对记录](rebuild-20261110/src/write_guard.py) 开始阅读。轻量版的桌面入口及同步模块位于 [`lite/src/`](lite/src/)。

## 其他功能

| 在哪里 | 可以做什么 |
| --- | --- |
| **完整版 · 知识库检查** | 查找断链、同名笔记和未引用的附件，查看知识库统计 |
| **完整版 · 文件整理** | 建立快照、查看与处理同步冲突、批量改名及查找替换 |
| **完整版 · 文档处理** | 转换受支持的文档格式，把多篇笔记合并成 Word 或 PDF |
| **轻量版 · 同步选项** | 导入时清理 Notion 标记，上传时选择根目录笔记、指定顶层页面，并可选择写入标签和日期 |
| **轻量版 · 核对上次上传** | 查看中断后已知页面的状态，核对完成后清除待核对记录 |

## 从源码运行和打包

使用带 Tcl/Tk 的 Windows x64 Python 3.13。以下命令均在仓库根目录执行。

### 完整版

从源码启动可运行 `py -3.13 .\rebuild-20261110\src\suite.py`。构建程序和安装包使用以下命令。

```powershell
py -3.13 -m pip install -r requirements-build.txt
py -3.13 .\build_release.py
```

构建产物位于 `release-20261118`。

### 轻量版

从源码启动可运行 `py -3.13 .\lite\src\lite.py`。构建单文件程序使用以下命令。

```powershell
py -3.13 -m pip install -r requirements-build.txt
py -3.13 .\lite\build.py
```

单文件程序输出到 `lite/dist`。

### 回归测试

用独立测试目录运行测试。

```powershell
py -3.13 -m unittest discover -s rebuild-20261110/src
py -3.13 -m unittest discover -s lite/verification
```

## 反馈与许可

- 遇到问题，可附程序版本、复现步骤和脱敏诊断信息，并使用示例笔记复现。[提交问题](https://github.com/860281282/guyue15/issues)
- 两个版本的源代码均采用 MIT 许可证，版权归「咕月15」。[查看许可证](LICENSE)
