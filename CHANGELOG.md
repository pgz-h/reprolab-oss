# Changelog

## 开发中

- 新增 Go CLI 预览，可对比两个 Git 版本中所选的顶层测试，导出、校验和重放证据。
- 修正 Windows 工作进程启动和模型配置权限测试，本地 HTTP 模型请求按本机连接处理。

## Alpha 候选 — 0.19.0a21

- 新增 `reprolab project review`，一条命令完成 pytest 项目的冻结、发现和两版比较，可直接接入 CLI 与 CI。报告保留会话路径，发现或选择错误后可沿用已冻结会话。
- 提供 Vitest 4 命令行预览。独立 npm 项目可按文件选择、按用例比较，并校验、导入与重放证据。桌面可导入并打开报告。
- 首发候选提供 Python wheel、源码与 macOS arm64 应用。Windows x64 原生验收单独记录。

## 未发布 — 0.19.0a20
- 项目报告可连同完整复查来源导出。导入时逐轮校验并恢复处理记录，可在原报告目录不可用时比较和重放；单份 ZIP 仍可导入。
- 项目接入显示已找到的 Python 候选，收集异常直接指出已识别的缺失模块，并保留完整错误和恢复操作。
- 清空勾选后提示如何继续；搜索隐藏的勾选项在有历史报告时仍提示是否参与运行。
- CLI 复查默认继承来源报告的每版限时，显式指定仍可覆盖。旧报告无有效限时时使用 120 秒。

## 未发布 — 0.19.0a19

- macOS arm64 和 Windows x64 共用版本与源码。纳入 Windows 文件锁、私密文件访问控制、受管进程清理，以及原生构建与验收流程。
- 项目发现支持按相对文件或目录收集。范围随会话和报告保存，刷新、复查、重放继续沿用。
- 复查使用报告保存的基线快照，在测试配置、fixture 或辅助文件变化时停下供核对。
- 报告重放默认使用原限时，并显示本次解释器与限时。桌面历史显示整次运行的结论。
- 大测试清单合并连续搜索输入，按最新查询执行批量选择。手动达到 2000 项时保留已有选择并提示。

## 未发布 — 0.19.0a16

- 按 DeepSeek Harness 的 MIT 许可移植模型目录与档位映射规则，重建第三方／自定义服务表单及原生模型卡片。
- 支持编辑 ID、名称、K/M 容量、输入类型、推理档位与 API 值、拖动和键盘排序、删除与恢复目录。
- 自定义网关可自行声明模型能力；日常选择器使用卡片档位，取消官方型号白名单对自定义声明的限制。
- 刷新目录保留已编辑参数和顺序；旧思考配置可迁移为声明并另存，保存校验与凭据去向保护继续保留。
- 最大输出与推理映射用于实际请求，来源记录保存模型声明；图片与上下文容量明确标为能力信息。

## 未发布 — 0.19.0a15

- 增加按模型保存的思考强度：OpenAI 两种协议、DeepSeek、Anthropic effort，以及 GLM Chat Completions 强度与旧版本思考开关。默认不附加参数。
- 官方直连仅对已核对的模型显示对应档位；自定义网关提供显式兼容设置，不从模型名称推断网关能力。
- 连接窗口和 AI 入口均可调整、切换并恢复设置；更换服务或协议清除旧兼容设置，原配置副本保留。
- 发送前预览显示强度并绑定实际参数；请求来源记录保存发送的设置。服务拒绝参数时不自动去掉参数重试，不声称服务实际采用了某个强度。
- 连接测试使用所选模型的同一思考设置，时限延长至 60 秒；仍只发送固定公开短句。

## 未发布 — 0.19.0a14

- 常用模型服务预填地址、协议和认证方式；自定义网关保留显式配置，切换预设不携带旧凭据或目录。
- 获取模型后提供可搜索多选列表，支持全选可见、取消可见、已选计数、取消保留原目录和管理已选模型。
- 保存目录与当前请求模型分开；设置和 AI 计划入口可直接切换已添加模型，原配置和历史报告不改写。
- 增加测试并保存，连接测试失败或取消不保存；高级配置按需展开，明确 AI 用途及请求范围。
- 整理公开仓库文档导航、模型操作指南、贡献入口和候选验证记录；新公开仓库由维护者另行提供。

## 未发布 — 0.19.0a13

- 通过 python-dotenv 历史子进程测试发现并修复源码串用：普通子 Python 与环境命令使用本版快照路径及所选解释器命令目录，保留插件和变量边界。
- 修复 async-lru 中条件不成立的 xfail 被误判受阻；使用 pytest 已求值决定，生效 xfail、XPASS 和跳过仍不计通过。
- 增加固定真实项目适配记录、最小受控回归测试及显式安装版适配验收入口；保留 Git 符号链接等未支持边界。

## 未发布 — 0.19.0a12

- 修复说明增加本机自动草稿：保存表单、材料选择、手工全文及同步状态，按项目、来源报告指纹、测试 ID 和测试指纹隔离；重新进入同一失败项可恢复，更多操作提供草稿列表。
- 草稿使用原子替换、私有文件权限和写入冲突检测；保存失败可见且可重试，明确放弃后删除。来源不可用或证据不一致时只读查看，不能交付旧预览。运行变量与模型配置不加入草稿。
- 严格验收可复用同一失败项草稿的问题与期望，提供从历史异常提取的待确认消息片段建议，排除明显临时路径、时间和随机标识；不推断正确行为，不自动执行或确认。
- 候选摘要显示冻结时间和源码指纹；重新准备同一来源时保留问题与期望、清除确认。保存前再次检查条件，旧快照和证据不改写。
- 新增打包应用双进程中断／恢复、严格验收及项目复查入口；本地候选包含此前尚未进入安装包的发现、复查进展、说明同步和环境恢复改进。

## 未发布 — 0.19.0a11

- 冻结会话的环境检查仍直接列出原项目已有环境，保留当前选择及虚拟环境路径；切换 Python 不再丢失插件与导入方式草稿。
- 静态依赖检查新增已有环境复用条件：未解析声明经明确核对后可采用已有 Python，已知缺包、版本不符和无效运行配置仍需先处理；原检查状态、CLI 退出码和新环境准备边界不变。
- 发现问题处可“使用环境并重新发现”，沿用原固定源码，完成后停在范围核对；未应用或未刷新的配置会停止，不自动执行测试主体。

- 修复说明填写完整后自动更新未手工修改的预览，期望行为、修改范围及日志／差异选择同步，不必反复点击生成。
- 修复已改表单或取消材料选择后仍可复制／导出旧预览的问题；手工预览遇到输入变化先保留并暂停交付，明确重新生成后恢复。
- 关闭保护同时覆盖未生成的表单输入及已交付后继续修改的内容；导出在文件选择返回后再次检查状态，生成结果绑定实际读取的输入。

- 复查报告打开或完成后自动核对绑定来源，结果页直接显示同条件转绿、需复核、仍失败和未复查范围；复查页以进展摘要替代重复统计卡片，保留逐项结果和筛选。
- 来源丢失或证据不一致时就地提示，并提供重新核对／重新定位；停止进展核对不改变本次执行报告，不替换为其他运行。
- CLI `project recheck` 返回同源 `review_progress`，作为派生信息附加到命令输出；保存报告与执行退出码保持原语义。

- 项目与单项准备的默认测试发现遵循 pytest 原生 `testpaths`，避免强制递归仓库根目录带入范围外示例；明确测试目标仍按精确 ID 执行。
- 部分／截断发现不再自动勾选或作为 CLI 默认运行范围；`project collect` 对不完整清单返回 2，保留可用测试供用户明确选择。
- 发现记录补充实际退出状态、超时与诊断，桌面区分空范围、超时、进程异常和部分发现，并保留完整日志复制。
- 发现问题处可直接“更新源码并发现”：沿用原基线另存快照、带入待核对范围，失败或取消保留已准备材料；不自动执行测试主体。

- 清理首页宣传式介绍、步骤口号和卡片说明，保留功能名称与操作；没有历史项目时不展示空的“继续上次检查”。
- 移除全局副标题、侧栏技术标签及重复 Alpha 标记，版本与构建阶段集中到设置页。
- 项目页不再常驻教程文字；范围缺失、超限、筛选隐藏已选项等必要提示继续显示，运行进度和错误保留。
- 精简环境、运行变量、修复验收及报告页的重复说明，将背景解释放到相关控件提示中；保留实际依赖问题、配置待刷新和变量保存方式。
- 项目页空闲时收起取消按钮和进度条，运行及中断观察按实际状态显示。

## 未发布 — 0.19.0a10

- 新增运行变量声明：普通值随候选快照保存，本机值只保存名称，由启动环境或受支持桌面入口在本次会话提供。
- 环境窗口增加“运行变量”，支持掩码输入、预览应用、撤销、清除本次值和刷新；插件编辑保留变量声明与其他 TOML 配置。
- 发现与两版执行使用本次解析的同一份变量输入；缺值、保留变量覆盖或无效声明会停止。使用本机值时正式运行重新核对发现范围。
- 项目和严格验收记录变量声明，工作进程、阶段事件、证据校验和离线报告共用；重放预览列出需重新提供的名称。
- 历史比较将普通值／名称变化标为条件变化，本机值跨轮一致性保持未知；一键复查遇到声明变化停下供核对。
- 单个严格快照案例及导入项目报告重放提供本机值确认窗口，取消保留既有状态；值不写入桌面设置或条件记录。
- 保留旧无变量配置和报告兼容性，新增运行变量端到端应用验收。测试代码自行输出的值仍可能进入日志，工具不自动脱敏。

## 未发布 — 0.19.0a9

- 项目接入增加“准备并发现测试”：连续冻结与实际收集，完成后停在选择范围，不自动执行测试主体；仍可单独冻结和发现。
- 发现失败或取消时保留准备好的快照，提供直接检查环境与重试入口，避免重新接入项目。
- 按项目记住上次使用的解释器和时限，重开可明确采用；读取仓库不自动切换或执行，新项目恢复默认输入。
- 增大解释器路径显示空间，提前显示发现／运行时限，核对主操作在 960×680 窗口可见。
- 结果列表按新增失败、受阻、已有失败、改善、通过排序；仅改变显示，原执行顺序和证据保持一致。
- 修复从结果页新建项目仍停留在空结果页的问题，同时清除旧会话选择和结果计数。
- 新增安装包首次使用流程验收，覆盖范围核对、空选择、明确执行、设置恢复和发现错误处理。

## 未发布 — 0.19.0a8

- 首页增加真实历史案例入口，明确适用任务与三种保证：材料可核对、检查可重放、结果可重复；同步中英文和安装说明。
- 补充 CLI 输出／退出码、自动读取会话目录、CI 接入范围、报告字段与贡献模块地图。
- 发布说明改为读取当前版本 CHANGELOG；验证摘要绑定版本和源码指纹，不再生成过期功能或虚构测试状态。
- 发布准备逐文件核对 wheel 和 Python 源码包中的源码、文档与构建配方，拒绝仅文件名相同的旧产物；桌面源码对应检查单独记录。
- 增加干净环境安装后的 CLI 验收入口，并接入 CI，覆盖教学／历史案例、项目两版比较、证据导出导入与重放。
- 修正两处过时测试替身，恢复冻结环境来源和准备期间权限变化的回归检查。
- 公开仓库与下载地址待确定，保留原开发仓库和历史；本轮不创建公开仓库或发布。

## 未发布 — 0.19.0a7

- 结果页增加多轮“处理记录”：沿精确报告身份追溯来源，分别展示各轮结果、相邻来源比较、候选文件变化和每项测试最近一次观察。
- 连续多轮没有再选择的测试仍列为本轮未复查，注明观察来源轮次；历史通过或失败不充当当前结果。复用原有测试／环境／基线／时限等条件比较规则。
- 支持每轮最多 4000 字符的本机处理说明，独立于原报告保存；复制和导出包含已保存说明，切换轮次或关闭窗口可保留未保存草稿。
- 来源丢失、不一致、重复引用或达到读取上限时明确展示已核对区间；重新定位仍须匹配身份与证据，只保存本机位置偏好。
- 从当前明确报告普通刷新工作区时也保存来源关联，继续尊重当前勾选与继承意图；原报告和固定基线保留。
- CLI 增加只读 project history，可导出 JSON、读取本机说明并提供移动后的来源目录。默认 20 轮，最多 50 轮；不运行测试或调用模型。

## 未发布 — 0.19.0a6

- 环境窗口支持手动加入项目插件、预览原项目 pyproject.toml 差异、明确应用及撤销；保留其他配置、注释和文件权限，不修改冻结源码。
- 应用前后核对原文件；外部编辑、只读或链接文件、并发修改和损坏备份会明确停止。原始配置保存在项目外的私有撤销记录中，重开窗口后可继续撤销。
- 配置保存后可以直接“刷新工作区使用配置”，沿用固定基线和原选择，原报告保留；刷新只准备新快照，不自动发现或运行测试。
- 新增“运行差异”页面，说明实际插件、导入方式、被清空的项目附加参数和环境变量继承范围。CLI environment inspect 共用静态说明，不加载项目或插件。
- 使用 TOML Kit 保留配置结构与注释，打包时包含其许可证。完整验证范围见 docs/release-readiness.md。

## 未发布 — 0.19.0a5

- 项目执行持续保存有序逐项事件，显示当前版本、测试、准备／主体／清理阶段、完成数量和耗时。等待期间更新距最近事件的时间，不猜测剩余耗时。
- 超时、取消、运行中 Ctrl-C 和进程异常退出后保留已收到的完整阶段观察与最后执行位置；本版未完整结束时仍为受阻，不把中断前观察升级为正式通过。
- 进度事件随报告校验、ZIP 导出／导入、重放与离线 HTML 保存；核对来源选择、事件顺序与最终记录，末尾未写完的事件单独说明。旧报告继续按原记录读取。
- 桌面结果页按实际问题突出检查环境、查看准备／清理或中断日志、准备修复说明、查看测试内容；运行期间隐藏旧统计，中断后保留实际进度。
- CLI 的 project collect／run／recheck／replay 增加 --progress，进度写到标准错误，标准输出仍为最终 JSON。
- 本轮验证范围和交付状态见 docs/release-readiness.md。

## 未发布 — 0.19.0a4

- 结果页新增按失败现象查看：依据记录中的版本、阶段、异常类型与消息归类，保留新增失败、原有失败和受阻分类，不推断共同根因。
- 可复制本组全部测试 ID、实际异常、归类依据及建议；导入异常接入环境检查，收集／fixture／超时／不完整记录接入日志，测试主体失败可定位代表项。
- “复查本组”明确使用整组范围，包含搜索暂时隐藏的项；范围绑定来源报告，保持原执行顺序，未知 ID 和空范围停止。未复查项继续保留。
- 显示当前可见数量，提供清除筛选；小窗口的结果页可滚动，保留列表高度。
- 离线 HTML 增加现象概览与组内筛选。CLI 新增只读 project diagnose，project recheck 支持重复 --test 指定来源报告中的子集。
- 本轮仅做直接相关检查与小型受控桌面流程，未重复完整测试矩阵或调用模型。

## 未发布 — 0.19.0a3

- 环境接入窗口分为“检查与使用”“pytest 插件”“准备新环境”，减少首屏拥挤；提供需处理依赖筛选与可复制摘要。
- 支持选择标准 dependency-groups 与 project.optional-dependencies，展开组引用、识别循环／缺失组，并将选择绑定安装预览及安装后核对。未选择的组不自动加入安装范围。
- 只读列出所选解释器的 pytest 插件入口元数据，区分已安装、项目内、入口冲突与待确认；勾选后生成可复制配置，保持手工插件预览，不加载插件或改写冻结源码。
- CLI 的 environment inspect / plan 新增可重复的 --group 和 --extra，与桌面共用语义。
- 修复部分项目偏好写入后重启未读回的问题，保留依赖组、指定报告和重新定位的报告路径；损坏配置明确报错，原文件保留。
- 本轮采用少量直接相关检查和窗口操作核对，不重复项目测试矩阵，不联网安装演示依赖或调用模型。

## 未发布 — 0.19.0a2

- 桌面新增“复查并运行”：从准确的来源报告冻结当前工作区、发现并核对原范围、继续两版执行。缺失测试、发现不完整、测试或插件配置变化时停在可继续的新会话；支持取消，保留原报告。CLI 共用 `project recheck`。
- 支持在冻结的 `pyproject.toml` 中明确声明 pytest 插件和导入方式，两版共享候选配置。项目发现、回归、严格案例与证据重放使用同一路径；环境页、发现日志和离线报告显示配置。
- 历史比较分别显示实际转绿与比较条件：基线、环境、时限、选择顺序、测试辅助文件和执行版本变化或未知时需复核；业务源码正常变化仍可比较。详情列出辅助文件，比较依据按需展开。
- 自动构建增加相关核心检查，覆盖 Python 3.11／pytest 8 与 Python 3.14／pytest 9。远端是否通过以实际工作流结果为准。
- 本轮保持定向验证，不重复完整测试矩阵；验收记录与安装包版本分别保存。未提交、推送或发布 GitHub Release。

## 未发布 — 0.19.0a1

- 重做桌面导航与首页，使用浅白、半透明面板、细边线和柔和阴影，统一深灰主操作。
- 项目配置移入“项目与版本”，测试范围与结果使用独立页面。低频选择方案、证据和来源操作收进菜单。
- 结果摘要可按类别筛选。测试名称与文件分行显示，完整 ID 继续用于执行、搜索和复制。
- 右侧详情独立滚动，避免长异常把结果列表推出窗口。修复确认页默认突出问题和验收条件，来源指纹按需展开，保存操作固定在底部。
- 增加从桌面失败结果到正式修复验收的完整流程用例，并核对导航、筛选和复查范围。
- 延续编辑内容保护、候选目录核对和报告版本记录，当前验证范围见验证与发布状态。

## 未发布 — 0.18.0a3.dev2

- 修复说明在替换手工编辑及关闭尚未复制／导出的文字前询问，重新生成可撤销恢复。
- 严格修复案例即时校验问题、期望行为和历史异常片段，桌面与 CLI 共用字段规则。候选目录改变后要求重新冻结。
- 项目报告、HTML 与复制摘要记录生成报告的 ReproLab 版本，旧报告缺少该信息时显示未记录。
- 修正来源证据回归用例中的替身函数恢复，开放本地定向运行验收。
- 更新源码安装路径和首次 Pre-release 指南，具体运行范围见验证与发布状态。

## 未发布 — 0.18.0a3.dev1：从实际失败处理并验收修复

- 结果详情和上下文菜单接入本地修复说明：核对来源证据，组织具体测试、异常阶段、历史共享测试、可选差异与日志；全文可编辑、标记省略、复制和原样导出，当前补充材料单列。
- 从报告真正失败的变更后快照准备严格修复案例，另存当前候选工作区，展示原回归基线背景与测试变化；固定原完整测试，确认问题、期望行为、实际异常与稳定片段后保存，不自动执行。
- 共用业务逻辑提供 repair-note / repair-prepare / repair-show / repair-save CLI；新 project_failure_origin 结构保留报告身份、历史观察、冻结来源、哈希和用户确认，旧案例字段继续读取。正式报告与证据交付、导入、重放保留新来源。
- 整理结果页主次，实际状态与异常摘要直接显示，固定快照重跑、来源及证据细项归并次级菜单；准备支持后台、取消、结果归属核对、独立重开与失败后重试。
- 补充业务修复、只放宽测试、批量与单项差异、诊断、文本一致性、取消和交付的针对性待运行用例及验收材料。
- 开发版标识用于独立未发布验证构建；不修改 v0.18.0a2 标签或原发行附件。本轮未运行 pytest、桌面 smoke、项目收集／执行或模型请求。

## 未发布 — 报告驱动复查

- 结果页区分固定快照重跑与当前工作区复查，按指定来源报告的全部实际选择或失败／受阻项准备新快照，显示将带入的项数，保留固定基线与具体会话／运行／报告指纹。
- 原范围区分未指定、主动清空与等待核对；保存待核对原 ID，连续刷新、重开和切换环境时不误当空选择。CLI 刷新与只读核对接受 `[]`，正式运行仍需 1–2000 项。
- 新报告可与准确的来源报告比较，支持移动后重新定位并核对身份。逐项展示实际状态、共享测试文件指纹、本轮未执行／受阻与未复查项；文件变化或指纹未知时不计入同一检查转为通过。基线、环境、限时单独提示差异或未知。
- 购物车示例附带独立补丁与预期检查点；补充少量待运行回归用例与直接相关使用说明。
- 本轮仅静态与必要构建核对，不运行功能测试、桌面 smoke、真实项目收集／执行或模型请求；未提交、推送、改标签或发布，保留既有发行材料。


## 未发布 — 0.18.0a2

- 新增“刷新当前工作区”：沿用原固定基线、环境和问题，另存新的冻结会话，记录来源 ID、内容及权限指纹；不移动基线、不改写旧报告、不自动发现或运行测试。
- 带入当前勾选（含空选择），重新发现后逐项核对完整 ID。有缺失时先不自动勾选；提供完整核对窗口、搜索、只看缺失项和复制全部缺失 ID，明确采用可用项后才替换选择。
- 导入选择清单和应用命名方案共用核对窗口，取消保留当前勾选；不自动替换改名或缺失目标。重新发现时保留主动清空的选择，保存偏好出现缺失时同样提示核对。
- CLI 新增 `project refresh` 与离线 `project selection-check`。运行默认尊重继承选择，空选择不扩大为全选，缺失项阻止两版运行；显式选择可覆盖继承意图。
- HTML 报告展示刷新来源；首次使用、错误处理和项目工作台文档明确修改源码后须刷新，旧会话重跑仍使用旧快照。
- 本轮按用户要求仅做静态检查与打包，不运行功能测试、桌面流程或模型请求，不声称当前候选已通过运行验收。

## 未发布 — 0.18.0a1

- 收口为首个开源 Alpha 候选，提供本地应用 ZIP、Python 安装包、公开源码审阅副本、源码 ZIP、发布说明和内容清单；不自动提交、推送或发布。
- 增加同一仓库跨会话复用的命名测试选择方案，可保存、更新、应用与删除；部分目标不可用时先说明范围，由使用者决定是否只应用可用项。
- 增加两次保存运行的只读对比，先核对证据，区分共同目标的结果变化与仅一次选中的项目，并说明输入、环境和会话差异。支持桌面查看、复制／导出和 CLI `project compare`。
- 结果详情增加基于实际异常与阶段的下一步建议，可复制诊断、检查对应固定会话环境，并核对共享测试指纹后预览保存的测试代码。
- 整理中英文首页、安装和发布指南，清除文档中的本机用户路径与旧包装入口；补充问题反馈模板和只构建发行文件的 GitHub 工作流。
- 本候选未进行完整运行回归；构建与源码整理不代表功能验收通过。

## 未发布 — 0.18.0.dev1

- 开始页增加“继续上次项目”和购物车项目示例。创建示例只准备独立 Git 项目及冻结会话，不自动发现或执行测试。
- 测试选择支持目录／文件范围、仅选可见、反选可见、复制 ID、JSON／文本选择清单导入导出；CLI `project run --selection-file` 可复用同一清单。
- 自动保存当前会话的勾选、搜索、文件范围、筛选和限时；重开时按固定输入与发现环境核对后恢复，保留清空选择的状态。
- 改动浏览增加测试、Python、配置依赖和文档筛选；工作台支持系统查找快捷键。
- 结果页支持复制筛选结果摘要、复制选中 ID 和导出筛选结果 CSV；筛选后不再继续显示被隐藏行的详情。
- 参数化测试名称中的反斜杠不再被误认为源码路径字符；路径限制继续作用于实际文件部分。
- 新增“开始使用”页，按项目回归、目标修复、AI 检查准备和证据查看区分入口，可直接运行内置示例；示例使用应用 Python 并保留用户的项目环境选择。
- 项目工作台增加随进度变化的下一步提示，独立显示 Python 环境，按需展开可选标题和问题说明。小窗口输入区支持滚动，避免输入框和按钮被压缩。
- 读取项目时只列出常见虚拟环境路径供选择，不自动切换或启动候选解释器。冻结后的环境检查使用同一份变更后源码的依赖声明。
- 测试选择明确提示被搜索或筛选隐藏的勾选项；结果优先定位新增失败，并提供“定位需处理项”筛选。
- 操作错误完整显示、可复制；测试发现失败自动展开日志。切换 Python 后可从原冻结输入重新发现和运行。
- 前半轮补充首次使用、小窗口、环境选择、收集失败恢复和项目证据交付／重放的流程测试。后半轮按用户要求停止测试，新增扩展功能未经运行验证；交付不声明本版本全量回归通过。

## 未发布 — 0.18.0.dev0

- 新增完整项目回归工作台，集中完成版本/工作区冻结、全部改动浏览、真实参数化测试发现、多选执行、结果矩阵和历史会话恢复。
- 项目模式支持一次选择 1–2000 个精确测试，前后版本各一个 pytest 进程；使用 after 的同一份所选测试文件，同时保留每版配置和 fixture。原单项执行器继续严格选择一个测试。
- 逐项区分两版通过、新增失败、已有失败、改善与受阻；跳过、xfail、fixture/收集/进程错误、超时和取消不能变为项目通过。
- 保存可搜索筛选、展开阶段与异常的中文离线 HTML，包含源码、权限、共享测试、选择清单、日志/事件和环境记录。报告页接入项目报告的查看、检查与导出。
- 项目证据支持 ZIP 导出、权限保留的校验导入、只读重放预览和明确执行。重放先复制会话，不改写来源报告证据。
- 从工作台的同一固定源码直接准备现有 AI 验收计划；模型发送仍需审阅。切换 Python 保留源码会话，只需重新发现测试。
- CLI 新增 project prepare/show/collect/run/check/pack/unpack/replay。项目总时限按每版计，默认 120 秒，上限 3600 秒。
- 按本轮用户要求以功能开发为主，只做集中关键流程与打包验证，不新增成批细碎测试、不重复旧全量回归。

## 未发布 — 0.17.2.dev3

- AI 计划新增选中检查的旧版试运行，复用同一固定源码、测试和正式 worker，支持独立测试及精确参数化项目目标，只运行旧版。
- 单独保存试运行输入、日志、事件和来源绑定，可打开旧记录；只有真实测试主体故障且不是已识别的缺模块问题、消息非空时提供条件建议。
- 明确确认后可将观察到的条件带入编辑窗口，人工审阅后另存独立计划。检查、Python 或计划变化会清除观察；预填前重查来源和证据。
- 试运行支持取消与关闭清理，旧版通过、收集/fixture/清理失败、跳过、超时或取消均不作为可采用故障或验收通过。
- 生成或导入失败后即时显示可用的响应恢复操作，保留原错误说明；无需重新打开计划。
- CLI 新增 suite probe 和 suite probe-show；补充复制期间内容/权限变化及恢复再次保存失败的永久回归。
- 修正清空已保存 Python 后试运行仍选旧解释器的边界；旧计划的新试运行也绑定观察时的权限，缺失执行记录保持无法判断。
- 重开观察时从校验后的原始事件重建诊断，从真实日志重建节选；缓存摘要不能绕过缺模块限制。取消或保存未完成的观察不会在重开时升级为可采用故障。
- 预填绑定到用户实际看到并确认的记录指纹；解释器检查期间更换观察目录、切换检查或打开尚未生成的计划时，旧确认不能复用。

## 未发布 — 0.17.2.dev2

- AI 项目验收新增结构化检查编辑：可修改测试代码、精确项目测试目标、检查用途、预期行为、假设及预声明异常，支持添加和移除检查。
- 人工审阅另存为独立固定计划，保留原计划与已有报告；新报告记录父计划 ID、指纹及修订时间，修订不代表验收通过。
- 新增完整响应检查点与离线恢复；响应、来源、提示、上下文、版本和固定输入必须匹配。中断留下的未完成产物会单独保留，已完成计划不覆盖。
- 生成、恢复和修订在发布前重新核对源码与已记录的权限，输入在模型请求期间变化时拒绝发布计划。
- 审阅区明确展示旧版异常类型、消息片段与测试目标；切换到修订版后清除旧结果入口，需重新实际验收。
- CLI 新增 suite export-plan、suite revise 和 suite recover，与桌面共用核心，不额外调用模型。

## 未发布 — 0.17.2.dev1

- 修改已保存连接的域名、端口或 HTTP/HTTPS 方式时，复用原 Key 需要明确勾选；也可输入新 Key 或选择新的环境变量。更换服务后获取的模型目录与原目录分开。
- 显式改用环境变量保存的新连接不再复制旧明文 Key；损坏的模型配置不会阻止其他连接打开，异常嵌套 JSON 返回可读错误。
- 命令行显式指定 `--api-key-env` 时使用指定变量，变量缺失则停止，不再回退到连接文件中的旧 Key；草稿命令的 `--protocol` 可直接选择 Responses。
- 源码快照新增普通文件权限指纹，输入准备、固定案例及报告发布时会核对权限变化，避免把运行行为已变的证据视为一致。
- 新报告使用 schema 4，回放案例绑定源码、测试与权限指纹；缺失新报告的必要权限记录会被判为不一致。
- 证据包格式 2 保留普通文件读写执行权限；继续读取格式 1 及不含权限记录的旧报告，并说明旧记录的检查范围。
- 源码差异展示仅有权限的改动；桌面案例状态能识别权限变化。升级后旧的桌面状态可能需要重新验收。

## v0.17.1 — 2026-10-31

- 修正 macOS 打包的 LSBackgroundOnly 标记，应用可正常成为前台窗口并接收键盘输入。
- 保留工作进程所需的标准输入输出，同时在构建时拒绝带后台应用标记的桌面发行包。
- 增加正常设置入口的输入实测，检查系统前台激活、鼠标焦点、Tab、逐字输入、中文输入法提交及保存。

## v0.17.0 — 2026-10-31

- 自定义模型API支持按协议获取模型目录，兼容标准data列表和网关models字典，保留实际路由别名。
- 模型可搜索点选、手动添加和移除，服务显示名称、自动Provider ID、目录与当前模型共同保存，支持已保存服务切换。
- 新填Key明确用于发现、测试和生成，旧配置继续原凭据规则。列表失败、空目录、部分目录均显示对应状态。
- 常用连接信息集中展示，环境变量和兼容参数收进高级设置。打包实测覆盖获取目录、点选、测试、保存重开和AI验收。

## v0.16.0 — 2026-10-31

- 新增模型连接表单，支持URL、Key、模型、协议、固定公开文本连接测试及0600权限另存配置。增加OpenAI Responses，保留原协议。
- 新增AI项目验收计划，从固定前后版本及有限上下文生成1至20项检查，支持已有项目测试或生成独立测试。
- 区分目标修复与回归保护，实际记录两版通过、新增失败、两版均失败和表现改善。整体通过要求目标修复及所选回归项全部可接受，沿用取消与仅重跑需处理项。
- 新增AI结果解读，预览发送实际记录，校验报告状态及请求前后指纹，单独保存辅助解释。
- 新增项目环境检查、候选解释器、静态依赖声明预览及项目外独立环境准备。
- 接入原桌面页面，支持打开已有计划、恢复所选环境、模型连接和报告解读。CLI增加suite和environment命令组。


## v0.15.0 — 2026-10-31

- 按本次修改的测试文件、模块命名和conftest影响范围排序已有测试，展示推荐原因。
- 新增旧版故障试运行，复用正式worker记录实际异常、阶段、日志和固定输入。
- 用户确认采用故障后，自动填写异常条件并从试运行时的同一份代码创建案例。正式验收重新运行两版，记录确认来源与固定指纹。
- 原创建窗口接入识别、采用、保存流程，手工故障字段可展开调整，支持关闭时取消试运行。CLI增加probe-failure和case-from-probe --confirm-failure，list-tests --before支持相关性排序。
- 已确认案例绑定测试目标、异常条件和输入。损坏试运行记录返回结构化错误。兼容只含一个测试的文件目标。


## v0.14.0 — 2026-10-31

- 新增当前工作区修复验收：比较固定Git基线和未提交修改，包含未忽略的新文件与测试，不改原仓库或暂存区。
- 保存为固定snapshot案例，记录两版源码及测试指纹；之后改动原仓库不影响案例，已保存快照被改写则拒绝运行。原批量、预检、重跑、证据导出/导入/重放可直接使用。
- 新增实际pytest收集，在独立副本中使用所选Python、项目配置和conftest，列出可直接选择的参数化实例；标准收集不进入fixture准备或测试主体，失败/超时不产生验收结论。
- 原新建案例窗口加入当前工作区选项和实际收集入口，无新增主页面。CLI新增collect-tests和init --working-tree。
- 只保留一轮必要核心审查和一次完整回归；新增7个关键集成测试。真实python-dotenv历史修复已通过未提交工作区流程，未宣称新发现或上游采用。


## v0.13.0 — 2026-10-31

- 动态收集与项目钩子调整后，执行前均确认恰好一个测试；拒绝时保存数量、目标和原因，不启动 fixture 或测试主体。
- 校验 worker 准备、执行、清理阶段的字段及测试标识，缺失或错配记录保持 inconclusive。
- 对准备前后的源码内容/目录清单、案例和测试进行核对，输入变化或目录读取失败时停止执行。
- 日志、事件、重放案例和源码证据绑定实际准备/执行记录；完整保存 HTML 和证据后原子发布 report.json。
- 保存失败保留阶段、已返回结果和材料路径；桌面现有报告页提示未完成材料。取消和旧证据保留语义不变。
- 新报告检查日志、事件、HTML、案例及测试内容指纹；旧 schema 3 报告仍可读取，并明确旧校验范围。
- 实测 python-dotenv #456 官方历史修复，完整 1.0.0 / 1.0.1 源码包、项目配置和同一官方测试；不是新发现或上游采用。
- 本轮只有一个版本和一次统一桌面构建；没有 v0.14/v0.15 发行。


## v0.12.0 — 验收诊断与可靠重跑

- 桌面及批次自动预检配置、Git版本、路径、输出和环境元数据；独立测试按所选解释器做只读语法解析。
- 真实pytest异常与阶段诊断，解释缺模块、收集/fixture/超时/目标错误及下一步；缺失worker记录不冒充零收集。
- 报告记录所选Python与发行包版本，比较历史环境；保留schema3证据包兼容，不声明依赖锁或身份认证。
- 从旧批次计划重跑未通过项/继续未完成项，校验来源，生成独立批次并保留旧证据。
- 新CLI preflight和retry（默认只读计划，--run明确执行）；原单项verify行为保持。
- 隔离损坏历史记录，保留venv路径、取消清理和每案单测试规则。


## v0.11.0 — 核心工作流与交互增强

- 在桌面创建 Git 案例：选择仓库与提交、静态查找测试、声明故障、选择环境并保存；固定提交且不自动执行。
- 在桌面准备离线草稿：填写问题、自动或手工选择有限上下文，完成后选中新记录；未调用模型。
- 勾选多个案例顺序验收；实时进度、逐项结果、取消清理、保留已完成报告和批次总览。
- 案例/草稿/报告搜索，案例状态筛选，按钮按有效选择启用；新建向导环境同步、草稿目录同步。
- 修正批次和单项完成边界；所有取消状态与未运行项都不算通过。
- 创建表单支持滚动和宽字段；改进原生 macOS 下列表与选择框的可读性。
- 维持 CLI、原案例/报告兼容、一个案例一个测试、明确同意后发送模型及仅可信本地代码的边界。


## v0.10.0 — macOS 桌面开发预览

- 新增 PySide6 原生中文桌面：案例选择、内置/项目 Python、后台验收、两版结果、打开报告和证据。
- 运行进度与可取消工作进程；取消清理进程组，不产生新成功结论。完成边界原子化，保存结果时不再错误接受取消。
- 案例目录/环境/最近选择持久化，输入变化标记历史；异常退出标记未完成；列表与详情同步最新状态。
- 草稿问题、上下文、测试和假设查看，离线恢复/导入/验收；模型生成前预览并明确同意，检查预览后输入是否变化。
- 报告浏览、证据检查、ZIP 导出与校验后导入；重放由用户另行触发。
- 设置与离线 doctor；保留 CLI 和旧案例兼容。
- PyInstaller arm64 .app、ZIP/DMG 构建配方与真实 Qt 交互 smoke；冻结 worker 和外部虚拟环境分派分离。
- 未提供新建案例/草稿 GUI、批量 GUI、Keychain、Developer ID 签名、公证或其他平台支持。


## 0.9.0 — 2026-10-30 (installable preview and environment diagnostics)

- Add --version and doctor for Python/pytest, Git, explicitly requested distribution metadata and optional offline model-configuration checks.
- Keep diagnostic readiness separate from verification verdicts; preserve selected virtual-environment paths and never print provider keys.
- Prepare wheel and source distributions with installation, diagnosis and GitHub Release instructions.

## 0.8.0 — 2026-10-30 (portable evidence)

- Add report check to compare saved source/test fingerprints and replay metadata without executing code.
- Add report pack with a bounded file manifest, original evidence, empty directories and replay instructions; reject overwrites and mid-export content changes.
- Add validated ZIP import with path/type/duplicate/size/content checks and temporary staging before publication.
- Keep evidence consistency distinct from the recorded verification verdict; imported snapshots replay through the existing verify command.

## 0.7.0 — 2026-10-30 (draft management and offline recovery)

- Save a response/input binding record before draft validation and publication; recover matching saved responses without another model call.
- Add draft list/status with separate generation and verification states, readable errors and stale-result detection after test/case edits.
- Add draft verify using the existing runner, preserving a separate latest-verification record and report link.
- Preserve checks, expected behavior and assumptions in case configurations, HTML/JSON reports and replay evidence.
- Keep old cases readable; require explicit external import for old responses lacking binding records.

## 0.6.2 — 2026-10-30 (generation diagnostics)

- Explain Anthropic max_tokens truncation and non-final stop reasons without exposing raw response bodies.
- Accept a single JSON code fence and module-level import fallbacks while preserving the original model response.
- Keep incomplete model responses separate from runnable drafts and verification verdicts.
- Document a real GLM-generated teaching test that reproduced the declared old failure and passed on the fixed code.

## 0.6.1 — 2026-10-30 (Anthropic Messages)

- Add explicit Anthropic Messages protocol selection without changing existing Chat Completions defaults.
- Normalize Messages endpoints, support API-key/Bearer authentication and configure bounded max_tokens.
- Parse completed text blocks and provider usage; reject truncation, tool-use and empty responses.

## 0.6.0 — 2026-10-30 (AI-assisted test drafts)

- Prepare bounded, reviewable issue/code context from two pinned local Git commits without importing or changing the project.
- Generate one pytest draft through a configurable OpenAI-compatible API URL, key and model; support explicitly labeled external-response imports.
- Save original response, explanations, assumptions, expected failure and a directly runnable Git recipe outside the target repository.
- Distinguish prepared/generated/failed drafting from runtime verification verdicts; never substitute templates after model failures.
- Reject obvious invalid drafts and source-text-only checks; stage publication and roll back failed writes for retry.
- Extend saved Git recipes and batch discovery to standalone tests; preserve draft provenance in reports and replay evidence.
- Keep existing project-test overlays, single-test enforcement and virtual-environment path handling unchanged.

## 0.5.0 — 2026-10-30 (case management)

- Add cases to inspect saved case configurations without running projects.
- Add sequential batch execution with independent per-case outcomes.
- Save progress after each case and preserve finished results when interrupted.
- Generate offline HTML/JSON overviews with counts and detailed report links.
- Continue after per-case errors; never label an empty batch as verified.


## 0.4.0 — 2026-10-30 (project workflow)

- Select an existing pytest node with project conftest/configuration support.
- Use the same after-version test file in both temporary source copies.
- Select an existing Python environment and explain missing pytest requirements.
- Add static list-tests discovery without importing project code.
- Add guided init and replayable Git recipes with pinned commit IDs.
- Record runtime versions, interpreter and test target in evidence and reports.


## 0.3.0 — 2026-10-30 (local Git workflow)

- Add verify-git to compare two committed versions of a trusted local repository.
- Preserve branch and uncommitted files by reading raw Git blobs without checkout.
- Record resolved commit IDs in reports and replayable evidence.
- Separate project source, workstation files and deliverables.


## 0.2.0 — 2026-10-30 (local feature update)

- Add `history` command with the original, licensed humanize #250 before/after modules.
- Show input, actual and expected values recorded by a regression test.
- Preserve declared upstream provenance in evidence and render source links/version references.
- Show bounded source-file diffs directly in the report.
- Render an inconclusive report even when the test file has an invalid encoding declaration.
- Preserve single-run behavior; no repeated-run mode or additional integrity-check command.

## 0.1.0 — 2026-10-30 (local initial version)

- Compare one pytest test against two trusted Python source snapshots.
- Require a declared exception type/message in the old version and a passing identical test in the new version.
- Preserve source/test fingerprints, source snapshots, logs, structured events and Chinese HTML/JSON reports.
- Include an explicitly labeled Unicode CSV filename demo.
- Distinguish verified, not reproduced, still failing and inconclusive outcomes.
- No model integration, arbitrary remote repository execution, or public release yet.
