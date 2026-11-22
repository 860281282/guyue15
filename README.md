# 咕月15

咕月15是 Windows 上的 Notion 与 Obsidian 同步工具，可按类目导入 Notion 页面，也可预览并上传选定的本地笔记。

## 获取并运行

在 GitHub 的 Releases 下载 Windows 免安装 ZIP。全部解压，进入「咕月15」文件夹并双击 `咕月15.exe`。「同步组件」需和程序留在同一文件夹。

首次使用时，选择已有的 Obsidian 知识库，在程序内点击「连接 Notion」，在浏览器完成授权。建议先选择少量测试笔记，确认目录、链接和附件的转换结果。

## 同步与工具

- **Notion → Obsidian** 可选择全部或指定类别，把选中的页面导入为 Markdown。遇到两边同时编辑的内容，保留本地版本并另存云端版本供核对。
- **Obsidian → Notion** 可选择文件夹、预览上传计划并核对冲突。云端写入状态需要核实时，程序会提示用户先核对。
- 工具箱可整理笔记和附件、查看同步冲突、建立知识库快照，以及转换常用文档格式。

## 从源码运行和打包

使用带 Tcl/Tk 的 Windows x64 Python 3.13，在仓库根目录运行以下命令。

```powershell
py -3.13 .\rebuild-20261110\src\suite.py
py -3.13 -m pip install -r requirements-build.txt
py -3.13 .\build_release.py
```

程序目录与安装器输出到 `release-20261118`。在隔离的测试目录运行回归。

```powershell
py -3.13 -m unittest discover -s rebuild-20261110/src
```

通过 GitHub Issues 报告问题时，可附版本、复现步骤和脱敏诊断信息。请使用去除真实内容的示例笔记。

项目源代码采用 MIT 许可证，版权归「咕月15」。[许可证](LICENSE)
