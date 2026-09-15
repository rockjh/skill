# QA 脚本工具链

此目录由 `mno-bruno-qa scripts sync` 同步。除项目定制扩展外，不要手工复制或修改。

| 脚本 | 用途 |
| --- | --- |
| `mno_bruno_qa.py` | `init/generate/materialize/check/run/preflight/reconcile/scripts` 统一入口 |
| `parse_openapi.py` | 解析离线 OpenAPI，按模块增量生成 full-matrix 清单 |
| `analyze_source_logic.py` | 扫描跨语言 API 可观察逻辑候选 |
| `analyze_java_logic.py` | 追踪 Java/Spring Controller 到应用、领域及集成层调用链 |
| `materialize_missing_bru.py` | 从 `cases.yaml` 生成或核对仅带风险标签的 `.bru` 请求 |
| `check_api_coverage.py` | 严格核对 OpenAPI、清单、请求、断言、源码和执行证据 |
| `run_bruno.py` | 全量或按模块全量运行 Bruno，并在请求前校验风险确认 |
| `runtime_preflight.py` | 检查环境、凭据、fixture、契约指纹、CLI 和代表路由 |
| `normalize_bruno_report.py` | 将原始报告归一化为无敏感内容的执行证据 |
| `validate_flow_execution.py` | 校验有序流程、变量捕获和清理结果 |
| `check_version_compatibility.py` | 校验业务源码版本锁 |
| `qa_lock.py` | 校验 OpenAPI、模块、case 和生成状态指纹 |
| `check_artifact_safety.py` | 检查密钥、JWT、Cookie 和带凭据 URL |
| `scripts_manager.py` | 同步脚本并校验版本、来源和 SHA |
| `execution_config.py` | 管理结构化执行配置、环境 Header 和 collection 注入 |

`scripts-version.yaml` 由同步命令生成，记录版本、来源、聚合 SHA、逐文件 SHA 和同步时间。
