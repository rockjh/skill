---
name: business-flow-design-doc
description: Generate and maintain source-backed business-flow design documents. Use for implemented business behavior, entry coverage, module ownership, reachable errors, async outcomes, Mermaid sequences, and Git-valid incremental updates. Do not use for future designs, API parameter manuals, database dictionaries, or test cases.
---

# Business Flow Design Document

产物是“已实现业务流程文档”。需求/规范只能说明意图和约束；当前选定源码与配置才是行为事实，旧文档只提供排版参考。每次交付记录范围、来源类型、有效 Git 提交、工作区脏状态、源码指纹和无法确认的边界。

## Workflow

1. 阅读 `AGENTS.md` 和目标文档，初始化：`dev-ai business-flow init --project <path> --docs-root <path>`。
2. 发现并保存候选入口：`dev-ai business-flow discover`。它只确认注册证据，不把 SDK/Feign 客户端当入站入口；注释、包名、普通 `task`/`receiver` 方法和测试构建产物不是注册证据。检查 `business-flow-discovery.json` 的注册位置、完整绑定标识、处理器、候选/确认绑定/确认处理器计数。
3. 人工确认 `business-flow-modules.json`（初版留在 `business-flow-modules-draft.json`）：依据业务对象、目标、状态生命周期、数据归属和外部协作方合并/拆分模块，固定模块 ID、稳定文件名、职责、对象、协作方和待确认问题。每个入口恰好一个主归属；跨模块调用写在主入口内，使用引用而非复制入口。用 `entry_reviews` 为每个入口核验触发者、目的、输入、带证据的有序步骤、结果、失败/部分成功/结果未知后果。人工确认前只能交付候选清单，不能称为完成。
4. 生成：`dev-ai business-flow generate`；显式范围才使用 `--module`。未确认映射、重复/遗漏归属、空模块、文件名冲突、关键 unresolved 或过期指纹必须失败。`--commit` 分析只读 Git 快照，不把快照外的工作区内容混入事实。
5. 维护用 `dev-ai business-flow update`；先比较入口、共享依赖、错误映射、状态/事务/异步和配置，旧提交不可用时全量重核。
6. 生成后运行 `dev-ai business-flow check`。它必须同时通过入口、错误证据、模块文件、版本/指纹、Mermaid 数量与事实一致性、脏工作区确认。报告列候选入口、确认绑定/处理器、已完成、排除/待核验数；章节数量不能替代覆盖率。

## Source-backed rules

- 逐入口追踪到业务结果：顺序、前置状态、条件领取/锁/幂等、事务提交、本地写入、外部接口、消息/任务提交、同步返回和异步终态。解析接收者类型和委托；同名方法不能跨服务合并，远端内部实现不可见时停在接口契约。
- 每个错误绑定条件、抛出位置、捕获/转换边界、传播/落库去向；区分同步、单项、提交、工作线程、动态外部错误和日志后继续。明确接收、受理、本地提交、远端完成；未知结果不得改写成重试或明确失败。
- 模块文件原则上一个模块一个 Markdown；每个入口一个独立主 `sequenceDiagram`。图文由同一入口事实生成：`alt` 只表示互斥分支，`opt` 只表示源码确实可选，`loop` 包含真实循环体；不画远端表、未实现补偿、手动 ACK、DLQ、原子性或“成功”分支的猜测。
- 正文解释“谁因何发起、输入影响什么、顺序、改变什么、失败后谁继续”；源码只放文件/符号/行/快照索引。标记 `已核验`、`解析器不支持`、`外部实现不可见`、`证据冲突`、`当前未实现`，不能把无搜索结果解释成不存在。
- 工具不能理解时优先填写结构化人工覆盖/解析证据；本地关键处理器、分支、来源或证据冲突未解决时保持未完成。不得改业务源码迎合解析器，不得清空 unresolved 或只改计数绕过门禁。

## References

- 需要非 HTTP 入口、复杂模块边界、错误/增量审阅时读取 [analysis-policy.md](references/analysis-policy.md)。
- 需要正文和图文一致性样式时读取 [examples.md](references/examples.md)；其中同时给出合格逐入口示例与不能替代入口流程的不合格概览。
- 需要对照交付资产时读取 [generated-example/](references/generated-example/)；其中包含草稿模块清单、确认版清单、入口归属、迁移记录和按模块拆分的 Markdown。
- 版本迁移和 P1 状态读取 [migration.md](references/migration.md)。

## Ownership and safety

本 Skill 只写文档根目录和 `init` 创建的锁，不改业务代码。主 Agent 负责全量入口、模块清单、索引/报告和最终检查；协作者只能修改分配证据。生产、`prod`/`prd`/`live` 或受保护环境禁止写入。使用 `dev-ai schema business-flow.<command>`，不复制机器契约。
