# 真实开源项目适配记录（0.19.0a13）

范围：macOS arm64、CPython 3.14.7；公开可信项目，在项目外独立环境执行。以下是固定样本验证，不代表所有版本或复杂外部项目兼容。原生测试先运行，再通过桌面与 CLI 检查；不调用模型、不向上游发送内容。

## 样本与来源

| 样本 | 固定来源 | 验证目的 |
| --- | --- | --- |
| python-dotenv | [仓库](https://github.com/theskumar/python-dotenv)，v1.0.0 `d0684d1c092fb6a9a208a09d43f02e4876ee8196` 与 v1.0.1 `d6c0b9638349a7dd605d60ee555ff60421c1a594`；BSD-3-Clause | src 布局、editable 环境、console script、普通 Python 子进程及真实历史修复 |
| async-lru | [仓库](https://github.com/aio-libs/async-lru)，v2.4.0 `0a27c5210d8a99219371ef6fc504e3215c707abe`；MIT | 常规包布局、异步／超时插件、条件 xfail、完整范围与复查 |

python-dotenv 的修复依据为 [PR #456](https://github.com/theskumar/python-dotenv/pull/456)；对应合入提交 `137bc3dc0b8cf3d417a1e800c4065c526e3fb96a`，父提交 `dd1af684f2586d2c2fdd722f9c45d3212e1e4e59`。先对合入提交建立原生基线，再复用工作区已有官方 1.0.0／1.0.1 源码包开展 ReproLab 历史执行。源码包 SHA-256 分别为 `a8df96034aae6d2d50a4ebe8216326c61c3eb64836776504fcca410e5937a3ba` 和 `e324ee90a023d808f1959c46bcbc04446a10ced277783dc6ee09987c37ec10ca`；两包的 8 个 Python 业务模块逐字节匹配各自标签提交。案例使用上游测试，没有新增或修改上游断言。

## 原生环境和结果

- python-dotenv：按贡献说明 editable 安装，采用 tox 声明的 pytest、pytest-cov、sh>=2.0.2,<3、click、IPython；实用版本 pytest 9.2.0、sh 2.4.0。激活专用环境 PATH 后运行 `python -m pytest --cov --cov-report=term-missing --cov-config=setup.cfg`，修复提交及 1.0.1 源码包各自原生 152 项通过（分别记录，不累计）。有旧版 fixture 的弃用警告和 IPython 退出时资源警告，未将其隐藏。首次未激活 PATH 时有 6 项 CLI 测试找不到 dotenv 命令，属于原生环境准备问题；激活后消失。
- 在 1.0.0 源码路径下执行 1.0.1 的 `tests/test_zip_imports.py` 两项原有回归测试，2 项失败；候选原生两项均通过。对子 Python 同时提供对应源码 PYTHONPATH，保证原生历史基线不串用 editable 候选。
- async-lru：按固定提交 `requirements.txt` 安装；pytest 9.1.1、pytest-asyncio 1.4.0、pytest-timeout 2.4.0、pytest-cov 7.1.0。按 Makefile 的测试入口 `python -m pytest -s ./tests/` 执行，93 项通过。未运行 lint、benchmark 或其他平台矩阵。

## ReproLab 配置和实际差异

async-lru 使用原生已有环境，先正常准备并发现。未声明插件时收集失败，提示未知 asyncio 标记。经“检查项目环境 → pytest 插件”手动加入以下两项，预览应用到原项目后刷新；基线、原会话保留，再发现完整 93 项并明确运行。

```toml
[tool.reprolab.pytest]
plugins = ["pytest_asyncio.plugin", "pytest_timeout"]
```

原生 addopts 中的覆盖率、输出、缓存和重复收集选项仍清空；asyncio_mode=auto、timeout=15、filterwarnings 等原生配置保留。没有启用所有插件，没有删除失败项或缩小范围。两版与随后复查均为 93 项通过。这里是固定提交与仅加入 ReproLab 配置的工作区比较，用于适配及复查基线，不宣称验证了 async-lru 的业务修复。

python-dotenv 的 Git 树存在 `docs/changelog.md` 等文档符号链接，当前 Git 冻结明确拒绝，未忽略或转换链接。历史案例因此使用已有官方源码包，通过正常案例入口及 CLI 执行。上游父进程 ZIP 导入测试严格验收为 verified。子进程测试在修复后实际为故障版失败、候选版通过；其预先声明的消息 `Starting path not found` 被 sh 的异常字符串长度限制截断，严格结论保持 inconclusive。未在看过候选结果后放宽条件。

## 两个已修复的产品缺陷

1. **普通子进程串用环境中的项目副本。** 0.19.0a12 的历史子进程测试两版均通过；只读导入审计记录证明两次均导入 editable 环境指向的候选源码。0.19.0a13 在实际执行副本上构造专用 PYTHONPATH（本版 src、根目录），并让 PATH 优先使用所选解释器的命令目录。父 worker 仍以 -I 启动并显式设置自身路径；普通子进程和环境中的 console script 可继续使用本版源码。两版环境字典分别构造，不修改共享本机值。
2. **条件未生效的 xfail 被误判受阻。** async-lru 的一个标记仅对 PyPy 生效，原生 CPython 通过，原 ReproLab 显示 92 通过／1 受阻。现在复用 pytest 已求值的 xfail 决定，不重新执行条件表达式；条件为假按实际阶段判断。生效的 xfail、非严格／严格 XPASS、run=False 和跳过仍不计通过。

源码与子进程审计在独立环境通过专用 .pth 注册只读退出探针，记录实际模块文件及 SHA-256，不改变导入路径或上游测试。修复后父进程和普通子 Python 的 dotenv.main 分别来自各自执行副本，哈希匹配各自官方版本；async_lru 来自两版不同执行副本，业务源码哈希相同并匹配固定提交。探针和完整日志不进入产品。

## 接入成本与限制

实际决策是：选择已安装依赖的 Python、确认比较来源／范围、明确启用两个必需插件。async-lru 只新增一份 pyproject.toml 配置，无需重装两套环境、修改上游测试、排除那个 CPython 正常通过的测试，或手工给每个子进程拼快照路径。条件变化由记录的 ReproLab 执行版本继续触发复核。

原生测试、准备／发现、环境配置与刷新、两版运行和复查的耗时分别保存在本轮记录。桌面计时来自自动化控件操作，不代表真人操作时间或满意度；首次 pip 安装没有完整独立计时，不混入工具运行成本。原生覆盖率与 ReproLab 无覆盖率运行的条件不同，不据此宣称速度提升。

显式使用 -I／-E、重建子进程环境、手动改写导入路径、在启动阶段预加载项目、非标准源码布局、原生扩展或复杂 editable 导入器仍需单独核对。此修复不是全进程导入隔离或环境冻结。需要通过 sys.executable 启动 Python 的项目应选择独立 Python，打包应用自身是分派器。Git 符号链接、子模块和 LFS 自动下载边界没有扩大。源码快照与子进程不是安全沙箱。

本轮定向／CI 契约检查和新安装候选的最终验收结果，以对应发行目录 validation-summary.json、BUILD.json 和验证记录为准；不累计上一轮 821 项或本轮不同源码阶段的数量。
