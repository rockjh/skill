# dev-ai 项目规范

本文件适用于整个仓库。子目录可增加更严格的 `AGENTS.md`，但不得放宽本规范。

## 1. 架构红线

- 仓库和公共 CLI 均为 `dev-ai`，Python 包名为 `seres-dev-ai`。
- 唯一 console entry point 是 `dev-ai = dev_ai.cli:console_main`；`python -m dev_ai` 只是同一入口的模块调用，不是第二套 CLI。
- 各开发类 Skill 独立触发，但共享 `dev_ai` 内核、wheel、npm 安装包、输出协议和退出码。
- 命令使用静态注册。禁止动态插件发现、运行时扫描或第二套扩展机制。
- 公共能力放在 `src/dev_ai/core/`，领域实现放在 `src/dev_ai/domains/<domain>/`，Skill 放在 `skills/<skill-name>/`。
- 领域文件必须承载真实职责。禁止兼容 facade、转发壳、占位模块或单体 `_engine.py`。
- 不得恢复旧 CLI、独立 `pyproject.toml`、旧安装方式、兼容分支或向生成项目复制工具源码。

## 2. 工作原则

- 修改前阅读本文件、目标 `SKILL.md`、相关实现和测试；reference 只读当前任务需要的部分。
- 修改共享行为前搜索调用方、schema、模板、锁文件、测试和发布脚本。
- 保留既有能力与安全规则。不得靠删除检查、放宽断言、改成 warning 或跳过测试完成重构。
- 优先使用现有模式和标准库；CLI 使用 `argparse`。不为单个用例增加接口、工厂、插件层或新依赖。
- 跨领域能力才进入 `core/`；改动保持最小，不处理无关代码，也不覆盖用户已有改动。

## 3. Skill 与领域变更

每个 Skill 至少包含 `SKILL.md` 和 `agents/openai.yaml`。目录名、frontmatter `name` 及默认提示词中的 `$skill-name` 必须一致。

`SKILL.md` 只描述适用边界、`dev-ai <domain>` 工作流、规则权威、安全限制、Agent 所有权和 reference 路由。完整目录、字段 schema、状态、门禁顺序、报告格式及 CLI 选项应由 `dev-ai schema`、模板或机器校验提供。文件默认不超过 5 KB。

reference 仅在确有场景细节时创建，并由 `SKILL.md` 明确路由；不要创建空 reference，也不要在多个文件复制同一契约。命令示例只能使用 `dev-ai`。

新增 Skill 优先复用已有领域。新增领域时必须同时完成真实领域实现、CLI 静态注册与帮助、项目根路径和锁处理、领域 schema 版本、scoped schema、`version`/`doctor` 相关元数据以及测试。未知领域必须拒绝，不得通过默认分支被当成某个已有领域。

## 4. 公共契约与领域规则

以下文件是公共契约权威：

- `src/dev_ai/core/envelope.py`：成功/失败信封与渲染；
- `src/dev_ai/core/errors.py`：错误分类和语义退出码；
- `src/dev_ai/core/schema.py`：命令及文档的 scoped schema；
- `src/dev_ai/core/redaction.py`：敏感信息识别与脱敏；
- `src/dev_ai/core/artifacts.py`：artifact 路径、脱敏写入和项目锁。

所有领域命令必须经公共内核返回结果。管道默认 JSON、TTY 默认 Markdown；进度写 stderr，stdout 只写最终结果。输出和落盘内容先脱敏，大结果默认只返回摘要和权威路径，完整内容仅由 `--full` 请求。`dev-ai schema` 只列领域和命令，指定 scope 时只返回该契约。不得在文档中复制上述代码已经定义的完整信封或退出码表。

Bruno 规则以 `dev_ai.domains.api_test.constraints` 及其业务模块为权威。数据库只在公共 API 无法建立或观察状态时使用，且必须限定自有测试数据、参数化、可恢复，并硬阻断生产和受保护环境；并行 worker 不得越过模块所有权。

E2E 规则由 `discovery.py`、`contracts.py`、`static_checks.py`、`source_versions.py`、`runtime.py` 和 `runner.py` 分责承载。场景 Agent 只能修改分配的 `scenarios/<scenario>/`，共享资产、运行与最终报告归主 Agent。不得修改业务源码来让测试通过；生产、`prod`、`prd`、`live` 及受保护环境禁止写入。危险控制需要绑定当前环境的逐次授权；每次写入都要有数据所有权、精确关联、隔离、幂等清理和恢复验证。运行失败必须是测试失败，固定门禁只能由 `dev-ai e2e run` 执行。

规则或机器契约变化时，同步修改适用的 schema、模板、reference 和回归测试。Python/npm 工具版本必须一致；领域 schema 版本独立管理。发布行为变化递增工具版本，领域契约、持久格式或校验语义变化递增相应领域 schema 版本，不能用工具版本替代领域版本。

## 5. 生成资产与发布

- 生成项目只包含业务资产和轻量启动器，调用已安装的 `dev-ai`。当前允许生成的 E2E 代码直接导入的公开运行接口仅为 `dev_ai.domains.e2e.runtime`；增加其他公开导入必须同时定义契约和测试。
- 项目锁记录并校验工具版本、领域和领域 schema 版本；不匹配即前置条件失败，不得回退到项目内旧代码。
- Bruno 权威报告位于 `qa/results/`，E2E 权威报告位于 `artifacts/e2e-run.json`，公共临时 artifact 使用 dev-ai 状态目录。
- npm 安装链保持为内嵌 wheel -> pipx -> 同步 `skills/*` -> `dev-ai doctor`。wrapper 必须调用 pipx 安装后的绝对路径，不能通过 PATH 递归启动自身。
- `dist/`、npm vendor、tgz、缓存及本地状态不得提交。

## 6. 验证

测试应直接命中新实现，不得通过旧别名或兼容层通过。按变更风险执行：

- 纯文档：检查路径、命令和权威引用，并运行 `git diff --check`。
- 代码、schema 或模板：补充对应回归测试，先跑相关测试，再运行 `python -m pytest -q`、`python -m compileall -q src tests` 和 `git diff --check`。
- 安装或发布：另运行 `python scripts/release.py`，检查 wheel 唯一入口与内容、npm payload、Python/npm 版本，并在隔离目录验证 npm -> pipx -> Skill 同步 -> `doctor`，以及 `version` 和 scoped schema。

新命令测试覆盖注册、帮助、scoped schema、信封和退出码；新规则至少包含通过和拒绝用例；安全或脱敏修改必须覆盖真实泄漏形态；生成资产测试必须证明不含工具源码；runner 修改必须覆盖门禁顺序、执行证据、JUnit、恢复和最终报告。外部环境不可用时，明确列出未执行项和原因。

只有在规则未削弱、旧入口和 fallback 未恢复、文档/schema/模板/实现/测试一致且适用验证通过后，才能声明完成。需求若与本规范冲突，先说明冲突和影响，不得静默引入例外。
