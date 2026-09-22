# Migration Note

领域契约从 schema `2` 升至 `3`，新增候选/确认/完成计数、入口事实 `entry_reviews`、显式人工确认状态、结构化错误后果、稳定模块文件与迁移字段，以及正文/图文事实检查。生成的补充资产包括入口归属、迁移记录和阶段进度。旧的 discovery、module-map、index、report 不能直接混用；先对当前源码重新 `discover`，人工确认模块和入口事实，再 `generate`。

历史 Markdown 可保留作表达参考，但不是事实来源。若旧提交不可获取，报告必须标记全量重核；若源码只改变版本而未改变被引用业务证据，`update` 可只更新有效 Git 版本。旧文档中无法定位的错误、外部内部实现、事务/补偿和异步终态继续标为未确认，不能通过删除证据或改覆盖计数迁移。

P1 状态：已实现提交/工作区指纹、入口稳定 ID、模块稳定文件名、版本-only 与业务变更区分、迁移字段、入口归属/比较资产、阶段进度、写入锁、失败日志和带指纹的完整证据缓存/依赖图；同一快照的 `--resume` 会先做轻量指纹校验，再从缓存重建解析结果，不会重复扫描，缓存缺失或不完整时硬失败。辅助 ownership、migration、comparison、cache、dependency 和 progress 产物均有 scoped schema，`check` 会逐项校验。旧 Markdown 会按稳定入口标记比较业务正文、控制结构、错误、参与方、异步语义和证据，并输出 added/missing/contradictory/unknown 分类，不生成伪精确总分；关键 unknown receiver/handler/多定义覆盖必须补齐 path、controls、unknowns 和源码证据。当前仅 Python 走语法感知调用/错误遍历；Java、Kotlin、Proto、GraphQL 及其他声明扩展仍可发现候选绑定，但会明确保留启发式解析 unresolved，直到人工核验。尚未实现跨进程任务调度器，以及覆盖所有声明语言的框架级 DI/泛型重载/完整 AST-CFG 恢复；这些场景必须保留未确认项或阻止完成，不能按已实现能力推断。
