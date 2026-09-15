# Cross-Service Integration Policy

Read this reference when a scenario crosses a service boundary or uses Kafka, MySQL, Redis, EMQ/EMQX, XXL-JOB, or another observable component.

## Boundaries

Keep these concerns separate:

1. `common/clients/` calls public business interfaces.
2. `common/builders/` builds reusable source-confirmed payloads.
3. `common/repositories/` owns read-only SQL and maps rows into stable records.
4. `common/integrations/` provides generic component adapters.
5. `common/fixtures/` owns adapter lifecycle and scenario isolation.
6. `common/assertions/` compares protocol, message, and persistence evidence.
7. Scenario modules express business actions, mappings, scenario-only assertions, and cleanup.

Generic modules accept environment configuration and scenario mappings. Do not embed business topic names, table names, credentials, or environment addresses in them. Reuse an approved pinned dependency; do not add an integration client silently.

Shared integration capability is configured in `config/common.yaml`. Connection details live only in `config/environments/<profile>.yaml`. A scenario declares `true` or `false` for Kafka, MySQL, Redis, and EMQ under `integrations`. Instantiate and preflight only components declared `true`.

No adapter construction, connection, subscription, or health check occurs during module import or pytest collection. Missing required runtime access produces `pending_environment` and a preflight failure, never a skip or weaker fallback.

## Kafka and database evidence

For Kafka publication evidence:

1. Create a unique consumer group and subscribe to the exact source-confirmed topic before the business request.
2. Wait for partition assignment and capture starting offsets.
3. Invoke the public business action.
4. Consume only from the bounded starting-offset/time window.
5. Match with source-confirmed keys such as `orderNo`, `taskId`, `orderId`, atomic order ID, or `iccid`.
6. Assert topic, key, relevant headers, schema/version, and business payload fields.
7. Close the consumer even after a failure.

For database evidence, use a repository method with a parameterized, read-only query. Poll by the same correlation key with a bounded deadline and report the last observed row. Assert ownership, state, meaningful columns, and relationships.

Kafka proves publication; the database proves downstream processing and persistence. When both are declared, assert them independently and then compare every shared source-confirmed field. Define an explicit field map when names differ, normalize only source-confirmed representation differences, and report field name, Kafka value, and database value on mismatch. A broad topic match, a row found by time range alone, or equality of correlation IDs alone is insufficient.

## Redis

When any scenario declares `integrations.redis: true`, provide these reusable modules:

```text
common/integrations/redis.py
common/fixtures/redis_observer.py
```

Expose the minimum read-oriented contract:

```python
"""提供只读 Redis 业务证据观察能力。"""


class RedisObserver:
    """读取并等待场景声明的 Redis 证据。"""

    def get_json(self, key: str) -> dict | None:
        """读取并解码 JSON 值。"""
        ...

    def exists(self, key: str) -> bool:
        """判断指定键是否存在。"""
        ...

    def ttl(self, key: str) -> int:
        """读取指定键的剩余有效期。"""
        ...

    def hash_get_all(self, key: str) -> dict:
        """读取并解码哈希字段。"""
        ...

    def wait_for_key(self, key: str, timeout_seconds: int) -> dict | None:
        """在限定时间内等待指定键出现。"""
        ...
```

Requirements:

- Default to read-only operations and do not expose `flushdb`, wildcard deletion, or business-key cleanup.
- Redis evidence is secondary when API, Kafka, or database evidence is available.
- Validate exact key namespace, decoded value, and relevant TTL/version semantics from source.
- If cleanup of an owned key is required, expose it only through a fixture that enforces the configured test prefix and rejects every other key.
- Use a bounded monotonic deadline and useful last-state diagnostics for `wait_for_key`.
- Load endpoints, credentials, database index, TLS, and test-key prefix only from the selected environment profile.

Do not generate these modules as empty scaffolding when Redis is unused.

## EMQ/EMQX

When any scenario declares `integrations.emq: true`, provide:

```text
common/integrations/emq.py
common/fixtures/emq_observer.py
```

Expose these contracts:

```python
"""提供测试主题发布和 MQTT 业务消息观察能力。"""

from collections.abc import Callable


class EmqPublisher:
    """仅向测试专属主题发布场景消息。"""

    def publish(
        self,
        topic: str,
        payload: dict,
        qos: int = 1,
        retain: bool = False,
    ) -> None:
        """向通过测试前缀校验的主题发布消息。"""
        ...


class EmqObserver:
    """订阅并等待与场景关联的 MQTT 消息。"""

    def start(self, topics: list[str]) -> None:
        """订阅主题并等待订阅就绪。"""
        ...

    def wait_for_message(
        self,
        matcher: Callable[[dict], bool],
        timeout_seconds: int,
    ) -> dict | None:
        """在限定时间内等待匹配的业务消息。"""
        ...

    def close(self) -> None:
        """断开连接并释放客户端资源。"""
        ...
```

Requirements:

- Build a unique client ID from the run and scenario IDs to avoid session collisions.
- Subscribe and confirm readiness before the business action.
- Support configured TLS, account, QoS, retain behavior, and topic prefix.
- Load broker addresses, credentials, TLS, and topic prefixes only from the selected environment profile.
- Validate every publisher topic against the configured test-topic prefix. Never default to publishing into a business topic.
- Match messages with a source-confirmed correlation key and bounded deadline.
- Disconnect deterministically through a fixture finalizer or context manager.

Do not generate these modules as empty scaffolding when EMQ is unused.

## Other observers

- **XXL-JOB:** use an approved trigger, correlate arguments and execution records, assert both job outcome and downstream state, and restore owned configuration.
- **Other stores or brokers:** preserve the same rules: preflight, pre-observation setup, exact correlation, bounded wait, useful diagnostics, and deterministic cleanup.

Each observer reports its non-secret endpoint identifier, correlation key, deadline, and last observed state with sensitive values redacted.
