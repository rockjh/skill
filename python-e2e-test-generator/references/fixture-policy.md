# E2E Fixture and Configuration Policy

Use the narrowest fixture scope that preserves isolation:

- **session:** parsed technical configuration and immutable service metadata;
- **run:** active environment, unique run ID, isolation namespace, and report context;
- **scenario:** actor/session, Kafka or EMQ observer, scenario-owned records, and mutable settings;
- **step:** short-lived values that cannot safely share scenario scope.

## Configuration split

Keep shared technical defaults separate from environment-specific connectivity and business data.

`config/runtime.yaml`:

```yaml
# 用途：选择本次 E2E 运行环境并定义公共技术默认值；禁止保存凭据、地址或其他敏感值。
# 激活环境：值必须对应 environments/<环境>.yaml 和业务数据.json 的同名根键。
active_environment: local

# 公共默认值：与激活环境配置递归深合并；enabled=false 时场景不得重新启用。
defaults:
  integrations:
    kafka:
      enabled: true
    mysql:
      enabled: true
    redis:
      enabled: false
    emq:
      enabled: false
  # 轮询时间单位均为秒。
  polling:
    interval_seconds: 1
    timeout_seconds: 60
```

`config/environments/local.yaml` is the placeholder-only example. Never generate `example.yaml`:

```yaml
# 用途：定义 local 环境的服务认证、请求 Header 和中间件连接；只允许保存环境变量占位符，不得保存真实敏感值。
# HTTP 服务：服务名关联场景 integrations.http 和对应客户端。
services:
  mno-traffic:
    # 服务根地址，格式为 http(s)://host[:port]，来源为 LOCAL_MNO_TRAFFIC_BASE_URL。
    base_url: ${LOCAL_MNO_TRAFFIC_BASE_URL}
    # 认证契约：type 与字段、Header 名必须由服务源码确认。
    auth:
      # 认证枚举：ak_sk 表示使用访问密钥和签名密钥生成请求签名。
      type: ak_sk
      # 访问密钥文本，来源为 LOCAL_MNO_TRAFFIC_AK；真实值不得写入文件或日志。
      ak: ${LOCAL_MNO_TRAFFIC_AK}
      # 签名密钥文本，来源为 LOCAL_MNO_TRAFFIC_SK；真实值不得写入文件或日志。
      sk: ${LOCAL_MNO_TRAFFIC_SK}
      # 认证 Header 映射：值保持源码协议字段原名。
      headers:
        access_key: accesskey
        signature: sign
        timestamp: timestamp
    # 自定义 Header：键保持源码协议字段原名，值来自 local 环境变量。
    headers:
      # 租户标识文本，来源为 LOCAL_MNO_TRAFFIC_TENANT_ID。
      x-tenant-id: ${LOCAL_MNO_TRAFFIC_TENANT_ID}
  mno-operator:
    # 服务根地址，格式为 http(s)://host[:port]，来源为 LOCAL_MNO_OPERATOR_BASE_URL。
    base_url: ${LOCAL_MNO_OPERATOR_BASE_URL}
    # 认证契约：none 表示源码确认该测试入口不需要认证信息。
    auth:
      type: none
    # 源码确认无需自定义 Header 时保留显式空映射。
    headers: {}

# 中间件连接：仅保存精确环境变量占位符和非敏感协议选项。
integrations:
  kafka:
    # Broker 列表，格式为 host:port[,host:port]，来源为 LOCAL_KAFKA_BOOTSTRAP_SERVERS。
    bootstrap_servers: ${LOCAL_KAFKA_BOOTSTRAP_SERVERS}
  mysql:
    # 连接串格式由项目选定驱动定义，来源为 LOCAL_MYSQL_DSN；不得打印真实值。
    dsn: ${LOCAL_MYSQL_DSN}
  redis:
    # Redis URI，格式为 redis(s)://...，来源为 LOCAL_REDIS_URL；凭据只能存在于环境变量。
    url: ${LOCAL_REDIS_URL}
    # 测试键前缀，格式为获批的非空命名空间，来源为 LOCAL_REDIS_TEST_KEY_PREFIX。
    test_key_prefix: ${LOCAL_REDIS_TEST_KEY_PREFIX}
  emq:
    # Broker 主机名或 IP，来源为 LOCAL_EMQ_HOST。
    host: ${LOCAL_EMQ_HOST}
    # Broker 端口，格式为 1-65535 的十进制整数，来源为 LOCAL_EMQ_PORT。
    port: ${LOCAL_EMQ_PORT}
    # 测试账号，来源为 LOCAL_EMQ_USERNAME；不得打印真实值。
    username: ${LOCAL_EMQ_USERNAME}
    # 测试密码，来源为 LOCAL_EMQ_PASSWORD；不得打印真实值。
    password: ${LOCAL_EMQ_PASSWORD}
    # TLS 开关，格式为严格布尔值 true/false，来源为 LOCAL_EMQ_TLS_ENABLED。
    tls: ${LOCAL_EMQ_TLS_ENABLED}
    # MQTT 服务质量枚举：1 表示至少一次投递。
    qos: 1
    # retain=false 表示测试消息不由 Broker 保留。
    retain: false
    # 订阅主题前缀，格式为 MQTT 主题层级，来源为 LOCAL_EMQ_TOPIC_PREFIX。
    topic_prefix: ${LOCAL_EMQ_TOPIC_PREFIX}
    # 发布白名单前缀，格式为测试专属 MQTT 主题层级，来源为 LOCAL_EMQ_TEST_TOPIC_PREFIX。
    test_topic_prefix: ${LOCAL_EMQ_TEST_TOPIC_PREFIX}
```

Create additional `config/environments/<environment>.yaml` files only for real named environments such as `sit` or `staging`, using the same shape and environment-prefixed placeholders. Do not generate empty environment files. Capability `enabled` switches remain only in `runtime.yaml.defaults`; environment files provide connection values and cannot redefine them. Each service contains `base_url`, explicit `auth`, and `headers`; `headers` may be empty only when source confirms no custom headers. Supported `auth.type` values are source-confirmed `none`, `ak_sk`, `bearer`, `basic`, or `custom`. `ak_sk` requires non-blank `ak`, `sk`, and protocol Header mappings.

Scenario definitions contain only HTTP service names and explicit component booleans:

```yaml
# 用途：声明本场景实际依赖的客户端和组件；禁止保存连接参数或敏感值。
# 依赖清单：HTTP 值关联客户端名；布尔值表示是否实例化并 preflight 对应组件。
integrations:
  http:
    - mno-traffic
    - mno-operator
  kafka: true
  mysql: true
  redis: false
  emq: false
```

Do not repeat endpoints, credentials, authentication, custom headers, topic prefixes, polling defaults, or shared `enabled` switches in `场景定义.yaml`. Keep environment-keyed business inputs in `业务数据.json`.

Configuration and data selection use one deterministic chain:

1. Parse `runtime.yaml` and validate `active_environment` against `[a-z][a-z0-9_-]*`.
2. Require exactly one matching `environments/<active_environment>.yaml`; never fall back to `local` or another environment.
3. Recursively deep-merge `runtime.yaml.defaults` with that environment mapping. Mapping values merge by key; a later non-mapping value replaces only its matching leaf.
4. Load each scenario's `业务数据.json`, select its exact `<active_environment>` root object, and resolve `data_ref` inside that object. Never merge business values across environments.
5. During preflight, recursively walk the selected configuration and business-data mappings/sequences and resolve only strings that exactly match `${ENV_NAME}`.

Treat an unset variable or a value whose trimmed text is empty as an explicit configuration failure, list the configuration/data key path without revealing the value, and report `pending_environment`. Do not interpolate partial strings or guess defaults. Convert values such as ports and booleans only under their schema-defined format.

## Collection before environment availability

Configuration and data modules may parse files at import time only if unresolved placeholders remain inert. They must not require environment variables, instantiate runtime clients, open sockets, or run health checks during module import, marker registration, or test collection.

Generate complete clients, builders, repositories, assertions, fixtures, scenario steps, and cleanup from source-confirmed contracts even when runtime values are missing. Use delayed imports for optional integration packages when importing them would otherwise break collection.

Resolve and validate runtime values in a preflight fixture or explicit preflight call before subscriptions, business requests, seed writes, or mutable configuration changes. Preflight:

- validates the selected named environment and approved isolation;
- checks values required by the scenario's `integrations` declaration;
- validates service and component reachability without producing business data;
- lists missing configuration keys without printing values or secrets;
- reports `pending_environment` and fails clearly when requirements are unmet.

Never call `pytest.skip`, `xfail`, or return a passing no-op for missing environment configuration. Use `contract_blocked` only for missing source contracts.

## Isolation and cleanup

- Never fall back to a developer URL or implicit shared environment.
- Use run- and scenario-derived namespaces, Kafka consumer groups, EMQ client IDs, cache prefixes, and object paths where contracts permit.
- Register idempotent cleanup immediately after acquiring each owned resource.
- Prefer business API cleanup. Direct database cleanup requires an approved test boundary and exact correlated keys.
- Redis cleanup is permitted only for exact scenario-owned keys under the configured test prefix.
- Snapshot and restore mutable settings under the project's approved lock; mark the scenario serial when isolation is impossible.
- Guarantee cleanup through `finally`, `yield` fixture teardown, `request.addfinalizer`, `ExitStack`, or a context manager. A cleanup call placed only after assertions is insufficient.
- When both the test body and cleanup fail, retain the test-body exception as the primary failure and record the cleanup exception through logging, notes, or another non-masking diagnostic. Raise a cleanup exception normally only when no earlier failure exists.

Cover deep merge, active-environment selection, authentication validation, isolated JSON data lookup, recursive placeholder resolution, missing/blank variables, and original-exception preservation with the focused shared-logic tests specified in [e2e-workflow.md](e2e-workflow.md). Use the project's existing test dependency.

## Headers and signing

Transport helpers expose per-request headers. Unless source says otherwise, merge ordinary headers in this order:

```text
runtime defaults < active environment < scenario < request
```

Apply signing after the merge. The signer removes stale reserved headers, then owns their final values. Signing settings and key references come from the active environment file, never from scenario definitions or hardcoded constants.

For the Seres contract, use [request-signing-template.md](request-signing-template.md). When disabled, send no `sign`, `timestamp`, or `accesskey`. When enabled, require the source-confirmed raw-body SHA-256 contract and never log the secret, access key, canonical string, authorization headers, or complete sensitive payload.
