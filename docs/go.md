# Go 项目对比预览

`reprolab project go` 用实际的 `go test -json` 比较基线与候选的结果。需要本机 Go 工具链和两版根目录都有 `go.mod` 的 Git 仓库。每次选择一个包目录和一个或多个顶层测试函数。

```sh
reprolab project go /path/to/repo --before HEAD --working-tree \
  --package internal/cart --test TestCheckout --test TestRefund \
  --output /tmp/reprolab-go
```

根包可省略 `--package`。已提交的候选使用 `--after COMMIT`，可用 `--go /path/to/go` 选择工具链。两版在独立副本中使用候选版本所选包的全部 `*_test.go` 文件，各自保留实现、模块依赖和其他文件。报告以顶层函数为单位，原始日志包含子测试。编译失败、跳过、找不到所选函数或执行不完整时，相关结果保留为受阻。退出码与 pytest 项目比较相同。

目前支持根模块中的纯 Go 包。本机需提前缓存依赖，预览以只读模块模式执行，并关闭模块下载、自动工具链下载、CGO 和工作区模式。含 `replace` 的模块和嵌套模块需要其他运行方式。只对可信仓库或证据发起执行。

```sh
reprolab project check /path/to/report
reprolab project pack /path/to/report --output /tmp/go-evidence.zip
reprolab project unpack /tmp/go-evidence.zip /tmp/go-imported
reprolab project replay /tmp/go-imported
reprolab project replay /tmp/go-imported --confirm-run --output /tmp/go-replayed
```

桌面可以导入 Go 证据并打开 HTML 报告。开始比较和重放使用 CLI。
