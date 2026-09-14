# QA 脚本工具链

此目录由 `mno-bruno-qa scripts sync` 同步；除项目定制扩展外不要手工复制或修改。

| 脚本 | 用途 |
| --- | --- |
| `mno_bruno_qa.py` | 统一的 `init/generate/check/run/preflight/reconcile/scripts` 命令入口 |
| `scripts_manager.py` | 同步脚本并校验版本、来源和 SHA |
| `parse_openapi.py` | 解析离线 OpenAPI、分模块并增量生成契约草稿 |
| `materialize_missing_bru.py` | 从 `cases.yaml` 生成或核对业务化命名的 `.bru` 请求 |
| `check_api_coverage.py` | 对账 OpenAPI、manifest、Bruno 请求、断言和执行证据 |
| `run_bruno.py` | 按模块、风险等级或执行计划运行 Bruno |
| `runtime_preflight.py` | 检查环境、依赖、凭据、契约指纹和代表性路由 |
| `normalize_bruno_report.py` | 将 Bruno 原始报告归一化为无敏感内容的执行证据 |
| `validate_flow_execution.py` | 校验有序流程、变量捕获和清理结果 |
| `check_version_compatibility.py` | 校验业务源码版本锁，排除 `qa/**` 资产变更 |
| `qa_lock.py` | 校验或刷新独立的 OpenAPI、模块、case 与生成状态指纹 |
| `check_artifact_safety.py` | 检查报告和资产中的密钥、JWT、带凭据 URL |
| `analyze_source_logic.py` | 跨语言扫描源码分支、错误码、必需 Header 和异常候选 |
| `analyze_java_logic.py` | 深入扫描 Java/Spring Controller、校验和业务异常候选 |
| `fetch_local_openapi.py` | 从已运行的本机服务获取并保存离线 OpenAPI |
| `manifest_io.py` | 共享 JSON/YAML 读写辅助 |
| `execution_config.py` | 解析简化配置、环境 `headers {}` 和集合级注入配置 |
| `tool_version.py` | 统一工具、skill 和脚本版本 |

`scripts-version.yaml` 由同步命令生成，记录版本、来源、聚合 SHA、逐文件 SHA 和同步时间。
