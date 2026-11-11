# 验收当前修改与精确选择测试（v0.14.0）

AI或开发者修改代码后，可以直接保存验收案例，无需先提交修复。此功能继续适用于Python/pytest项目，不会自动修复代码或安装项目依赖。

## 桌面流程

1. 在“案例验收”点击“新建验收案例”。
2. 勾选“验收当前未提交的修改”，选择仓库。默认用HEAD作为修复前基线，也可选择更早提交。
3. 选择项目Python环境。先从默认静态列表选一个测试；参数化测试可点击“列出实际测试与参数”，然后点选一个具体实例。
4. 填写预期旧版异常及消息，保存案例。保存时冻结当前代码和测试，不运行测试。
5. 回到主窗口开始验收。之后继续编辑原仓库不会改变已保存的案例；想验收更新的修复，另存一个新案例。

不勾选当前工作区时，原有两个固定Git提交的流程保持不变。默认静态读取不会导入项目。实际收集是用户明确点击的独立操作，会导入测试、conftest和项目钩子，仅适合可信代码；标准pytest collect-only不启动fixture准备或测试主体，也不代表测试通过。收集失败或环境改变会清除过期参数列表。收集日志可在状态文字提示中查看。

## CLI

    reprolab collect-tests /path/to/repo --working-tree --target tests/test_fix.py --python /path/to/env/bin/python
    reprolab init /path/to/cases/fix.json --repository /path/to/repo --working-tree --before HEAD --project-test 'tests/test_fix.py::test_fix[value]' --failure-type ValueError --failure-message 'reported bug' --python /path/to/env/bin/python
    reprolab verify /path/to/cases/fix.json --output /path/to/reports

collect-tests --ref COMMIT 可收集固定提交。省略--target时按项目配置收集，最多展示5000个具体目标，超出时应缩小范围。默认收集时限15秒，可指定--timeout 0.1至300；超时会清理worker进程组。所有收集及验收均不调用模型。

## 保存的内容

fix.json旁边生成fix.inputs目录，包含before、after和同一份regression.py。两者应一起保留。案例记录Git基线提交、本次工作区文件指纹和测试指纹；核心会拒绝已保存快照或测试被改写的案例。它是原有snapshot类型，直接支持现有预检、批量、重跑及证据打包。报告展示工作区来源和固定基线。分享推荐使用报告页导出的证据ZIP。

工作区只包含Git跟踪文件以及未被Git忽略的新文件，并继续排除.git、虚拟环境、缓存、build/dist、node_modules和以.env开头的路径。Git忽略文件及未跟踪的空目录不参与快照。特殊文件、符号链接和子模块不支持；最多5000个条目、100 MiB。原仓库不被提交、切换分支、暂存或修改。准备期间发现内容变化时停止保存。

这不是安全沙箱或文件系统事务，也不保证依赖当前工作区外部文件的项目可重放。所选Python应已具备项目依赖；应用内置环境适合示例，真实项目通常应选择完整外部环境。自动插件发现与addopts仍禁用。

## 验证范围

本轮用本地教学多文件项目验证参数收集、fixture不提前运行、未提交修复、新增测试、原Git不变、保存后继续编辑及固定案例验收。另使用官方PyPI的python-dotenv 1.0.0作为本地Git基线，将1.0.1内容保留为未提交修改，使用官方ZIP导入测试实际得到旧版OSError、新版通过。来源与许可证沿用v0.13历史案例记录；本地构造的Git提交不是上游原提交，属于历史修复接入验证。
