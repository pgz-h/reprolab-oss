# 发布 ReproLab Alpha 预览

当前候选版本见 [README](../README.md)，验证范围见[验证与发布状态](release-readiness.md)。Alpha 使用 GitHub Pre-release 标记；本地构建成功不代表已经公开发布。

## 选择公开仓库

维护者可以使用现有仓库，或另建公开仓库。先确认目标仓库、许可材料和要公开的内容，再推送源码；不要把已有远程地址当成新的公开地址。仓库地址确定前，文档使用仓库内相对链接，不提供尚不存在的下载链接。

原开发仓库和历史继续保留。若选择公开完整历史，应审阅将推送的历史；若选择从审阅快照开始公开，使用 `SOURCE_REVIEW` 的文件，不包含本机设置、内部过程文档和 Git 历史。审阅副本是交付材料，不作为第二个日常开发目录。公开仓库准备好后，再为唯一开发仓库配置相应远程。

源码、版本标签、wheel、源码包与桌面应用应对应同一候选。先整理可审阅提交，再创建指向该提交的版本标签；修正代码后重新构建，不复用旧版本附件。

## 本地构建

构建目录与输出目录放在源码仓库外。下面的 `VERSION` 和路径须替换为实际候选版本。

```sh
python3 -m venv /path/to/reprolab-build/.venv
/path/to/reprolab-build/.venv/bin/python -m pip install -r packaging/requirements-macos.txt
/path/to/reprolab-build/.venv/bin/python packaging/build_macos.py --skip-dmg --work-root /path/to/reprolab-build/app --output-dir /path/to/releases/VERSION
/path/to/reprolab-build/.venv/bin/python -m build --outdir /path/to/releases/VERSION
/path/to/reprolab-build/.venv/bin/python packaging/prepare_release.py --output-dir /path/to/releases/VERSION
```

使用新输出目录。构建脚本只生成材料，运行验收记录另行保存。准备脚本生成的说明也需要结合本候选的实际验收结果审阅。

## 首次 Pre-release

1. 核对许可证、文档、公开内容与仓库可见性，确认所选提交包含本轮工作。
2. 同步版本声明，针对新候选完成必要检查，再构建与该源码一致的安装包。
3. 创建指向已推送候选提交的新标签，创建 Release 草稿并标记 Pre-release。
4. 上传应用 ZIP、wheel、Python 源码包、校验清单和对应验证记录。若同时提供公开源码审阅 ZIP，说明其与标签源码的关系。
5. 发布说明写明验证过的流程、系统范围和已知限制。macOS 构建当前为 arm64、ad-hoc 签名，Apple 公证仍未完成。
6. 核对真实下载入口、附件版本和校验值后发布，再将首页指向实际可下载的附件。

当前 GitHub 工作流执行定向核心检查、构建 Python 发行文件，并在独立环境中验收安装后的 wheel。远端结果须实际查看；桌面构建与界面验收另行完成。工作流产物与正式 Release 附件需要分别核对。

GitHub 的 Release 支持草稿和 Pre-release 标记，操作步骤可参考官方说明。[管理 Release](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)

## 发布说明与验证摘要

`prepare_release.py` 从 CHANGELOG 中提取当前版本的条目，并核对 wheel 的包内容、元数据以及源码包中的源码、文档、测试与构建配方。内容过期时会停止，不仅检查文件名。桌面应用源码对应检查仍需单独完成。

如果发行目录包含 `validation-summary.json`，它必须声明 schema_version=1、version、source_tree_sha256、checks 和 limitations。checks 每项包含 name、result（passed／failed／skipped／not_run）和 details；limitations 是字符串列表。源码指纹按脚本中 source_files 顺序，对 path、sha256、mode 记录执行 `sha256(json.dumps(records, sort_keys=True).encode())`，与 source-manifest.json 使用同一算法。

脚本校验摘要与当前源码对应，并原样呈现检查范围和限制，不自行认证测试通过。没有摘要时只写“未附验证摘要”，不声称从未测试或已经验收。摘要生成后如源码或文档变化，应重新核对候选与记录。

发布前实际从干净环境安装候选 wheel，执行 `python -I packaging/verify_install.py --output /path/to/new/installation-check`；该脚本检查版本、教学和历史案例、项目比较、证据导入导出及重放，拒绝用源码或 editable 安装冒充安装包验收。它只使用内置和本地受控代码，不调用模型。
