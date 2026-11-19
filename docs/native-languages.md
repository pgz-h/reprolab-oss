# Rust, C and C++ / 原生项目测试

ReproLab `0.19.0a25` adds two CLI runners. They use the project's own toolchain on two fixed Git commits. The offline receiver only needs ReproLab and the evidence. The desktop's existing pytest, Go and Vitest paths remain available.

| Runner | Pilot scope | What identifies a test |
| --- | --- | --- |
| Cargo | One root package with a checked-in Cargo.lock. Build library unit tests with stable libtest. Dependencies must already be available for locked offline resolution. | The full libtest name, including modules. Each selected test runs separately. Integration tests, doctests, workspaces and custom harnesses are outside this pilot. |
| CMake/CTest | A root CMakeLists.txt, available compiler and project dependencies, CMake and CTest 3.29+. The runner configures a fresh Debug build with BUILD_TESTING enabled. | The full registered CTest name. The runner looks up exact names, records the expanded command, arguments and properties, and runs CTest with one test slot. |

使用前确认仓库可信。预览会解析两个 Git 版本；追加 `--confirm-run` 后，会运行项目自身的构建配置和测试。CTest 可以调用项目配置中的外部工具。Cargo 的离线参数作用于 Cargo 的依赖解析。

## Rust library example

In a Rust repository with tests in `src/tests.rs`, replace the test ID and paths with those from your project. List the files that define the checks you want to compare. Use a new output directory outside the repository.

```sh
reprolab project native . --runner cargo --before HEAD~1 --after HEAD \
  --test 'tests::floor' --shared-test-source src/tests.rs --shared-test-source Cargo.toml \
  --confirm-run --output ../cargo-review
```

The two revisions keep their own sources and build products. ReproLab checks the bytes and file permissions of the test definition files you declare. If the only test definition lives in an edited implementation file, that file has changed and cannot establish a shared check. Native observations remain useful, but they do not qualify for a passing gate under a shared test claim.

## C example from zlib

We tested upstream commit `066e0b68a6c26d6b737bfad2ba395eb1317ca645` against parent `ec9d91df6f5540bb12b5ec8834d98094c6863071`. The commit changes `inflate.c`. These two existing upstream examples exercise the shared and static zlib builds. Both examples passed on both revisions on macOS arm64 with CMake/CTest 3.29.6. This run covers two registered C tests; it does not establish that the original bug was reproduced.

```sh
review="$(mktemp -d)"
git init -q "$review/zlib"
git -C "$review/zlib" fetch --depth=2 https://github.com/madler/zlib.git \
  066e0b68a6c26d6b737bfad2ba395eb1317ca645
git -C "$review/zlib" checkout --detach FETCH_HEAD
reprolab project native "$review/zlib" --runner cmake \
  --before ec9d91df6f5540bb12b5ec8834d98094c6863071 \
  --after 066e0b68a6c26d6b737bfad2ba395eb1317ca645 \
  --test zlib_example --test zlib_examplestatic \
  --shared-test-source CMakeLists.txt --shared-test-source test/CMakeLists.txt \
  --shared-test-source test/example.c --confirm-run --output "$review/evidence"
reprolab project pack-native "$review/evidence" --output "$review/zlib.zip"
reprolab project check-native "$review/zlib.zip"
```

For a C++ executable or a project mixing C and C++, pass its registered CTest names the same way. Exact selection preserves punctuation such as brackets. If CTest would implicitly add other fixture setup or cleanup tests, the runner blocks that selected execution. Review the additional names before including them.

## 读结果 / Reading the gate

- `comparison.gate.exit_code = 0` 表示已核对的选定范围通过或改善。声明文件相同只覆盖声明的范围，其他辅助文件仍可能改变测试含义。
- `1` 表示观察到可比用例从通过转为失败；即使另一项被忽略，报告也保留已确认的新增失败。
- `2` 表示当前证据不足以放行，例如编译失败、未声明共同测试定义、用例缺失或跳过。CTest 的运行命令、参数或属性变化时，同名测试的两次结果仍显示，但不能据此确认修复。
- `check-native` 验证 ZIP 时不启动编译器、Cargo 或 CTest。核对命令成功表示材料一致；具体业务判定在 `comparison.gate`。包中含源码与原始构建日志，分享前应检查范围。
