# 用项目已有测试完成修复验收

v0.4 把准备案例、选环境和重复使用连成一个流程。

## 1. 列出可选测试

    python -m reprolab list-tests /path/to/project --ref HEAD

这是静态读取，不执行项目代码。支持常规 test_*.py / *_test.py 文件中的测试函数、Test 类方法。动态生成、自定义收集规则和参数化实例不能完全靠静态列表发现，可以手动输入 pytest 目标。

## 2. 引导创建案例

    python -m reprolab init /path/to/cases/export.json --interactive

按提示填写项目路径、修复前后版本，选择一个测试，选择 Python 解释器，并声明旧版应出现的异常类型及消息片段。案例保存在你选择的路径，已有文件不会被覆盖。

也可以一次传入参数：

    python -m reprolab init /path/to/cases/export.json --repository /path/to/project --before HEAD~1 --after HEAD --project-test tests/test_export.py::test_chinese_name --python /path/to/project-env/bin/python --failure-type ValueError --failure-message "unsupported filename"

init 只保存配置，不运行目标测试。两个版本会固定成完整提交编号，后续 HEAD 前进不会改变这个案例的对象。

## 3. 执行、保存、重跑

    python -m reprolab verify /path/to/cases/export.json --output /path/to/reports

换电脑或换环境时，可覆盖保存的解释器：

    python -m reprolab verify /path/to/cases/export.json --python /new/env/bin/python --output /path/to/reports

报告包含测试目标、Python/pytest 版本、解释器、实际结果和代码差异。导出的 evidence/case.json 可直接重放，不再依赖原 Git 仓库，但仍需要相应依赖环境。

## 也可以直接运行

    python -m reprolab verify-git /path/to/project --before HEAD~1 --after HEAD --project-test tests/test_export.py::test_chinese_name --python /path/to/env/bin/python --failure-type ValueError --failure-message "unsupported filename" --output /path/to/reports

独立测试文件仍使用 --test。--test 与 --project-test 二选一。

## 项目测试模式的含义

- 从 after 版本读取测试文件，让两边运行同一份测试。
- 仅在临时副本中覆盖相应测试路径；不改原仓库和保存的原始源码快照。
- 加载每个版本自己的 conftest 和 pytest 配置，沿用项目辅助 fixture。
- 禁用项目 addopts 和自动插件发现，避免意外扩大测试范围或并行执行。第三方插件需要通过 conftest 中的 pytest_plugins 明确声明。
- 最终只允许一个测试。参数化测试请指定 tests/test_x.py::test_name[具体参数]。
- 测试依赖文件、fixture 或环境不兼容时，报告为无法验收，不把它当成成功复现。

## Python 环境

选择已有解释器，不会自动安装任何依赖。需要 Python >=3.11、pytest >=8,<10，以及项目需要的库。虚拟环境里的 python 通常是符号链接，程序会保留这个路径，以免误用系统 Python。

这个版本仍只执行可信本地代码。案例保存不等于依赖锁定；同一个解释器的依赖以后改变，也可能影响重跑结果。
