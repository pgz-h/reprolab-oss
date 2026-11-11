# Vitest 命令行预览

开发版可以对比两个 Git 版本的 JavaScript 或 TypeScript 测试。选择项目相对路径下的完整测试文件，两版运行对比版本保存的同一份测试文件，各自保留项目配置和锁定依赖。

需要 Node.js、npm，以及在根目录以 `package-lock.json` 锁定 Vitest 4 的独立 npm 项目。两版依赖在各自临时副本中执行 `npm ci`，安装脚本关闭。依赖需要来自锁文件记录的 registry，包含完整性值。需要 workspace、本地依赖或安装脚本的项目尚未覆盖。使用 `--offline` 时需要提前填充 npm 缓存，可用 `--npm-cache` 指定目录。

```sh
reprolab project vitest /path/to/repo --before HEAD --working-tree \
  --file tests/price.test.ts --file tests/calc.test.js \
  --output /path/to/reports
```

也可用 `--after` 指定第二个提交。返回的 `report_dir` 下有 HTML、JSON、两版日志和固定源码。Vitest 会按每版项目配置核对所选文件。文件缺失、收集失败、同一文件属于多个测试配置、两版所属项目不同、测试重名或测试清单变化时，文件列为受阻。能核对范围时，报告按用例比较，同一个文件可以同时显示改善和新增失败。文件汇总保留其中最需要处理的结果，用例统计单独显示。跳过、预期失败、重试后通过或执行不完整的用例保留为受阻或未定。报告内的原始记录保留各测试的名称、状态、异常和可用的重试诊断。旧证据仍按生成时的规则校验，重放时生成当前格式。退出码沿用项目检查的 0（所选范围通过）、1（有新增失败）和 2（需处理或命令未完成）。

```sh
reprolab project check /path/to/report
reprolab project pack /path/to/report --output /path/to/evidence.zip
reprolab project unpack /path/to/evidence.zip /path/to/imported-report
reprolab project replay /path/to/imported-report
reprolab project replay /path/to/imported-report --confirm-run --output /path/to/replayed
```

重放预览可查看原环境和时限。正式重放可用 `--node`、`--npm` 选择本机环境，并用 `--offline` 或 `--online` 切换依赖安装模式。首次运行也可用这两个可执行文件选项指定路径。仅对可信仓库和可信证据发起执行。导出材料包括保存的项目源码和日志。

目前可以在桌面导入并打开 Vitest 的 HTML 报告。选范围、发起对比及重放通过 CLI 操作。此次验证使用 macOS arm64 上的受控 Vitest 4 项目。
