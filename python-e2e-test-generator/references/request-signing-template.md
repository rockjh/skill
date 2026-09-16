# Seres Request Signing Template

Use this contract only when source discovery confirms the service uses Seres signing. Read all runtime values from the service authentication block in the active environment file. Do not put signing switches or keys in `场景定义.yaml` or `业务数据.json`.

## Environment configuration

`config/environments/local.yaml` excerpt:

```yaml
# 用途：引用 Seres 请求签名所需的环境值并声明协议常量；禁止保存真实密钥或访问凭据。
# 服务认证：仅在源码确认 mno-traffic 使用 Seres 契约时使用此结构。
services:
  mno-traffic:
    auth:
      # 认证枚举：ak_sk 表示访问密钥加后缀密钥 SHA-256 签名。
      type: ak_sk
      # 严格布尔值 true/false，来源为 LOCAL_SERES_SIGN_ENABLED；缺失时不得猜测默认值。
      enabled: ${LOCAL_SERES_SIGN_ENABLED}
      # 协议枚举：SHA256 表示源码定义的后缀密钥摘要算法，不是 HMAC。
      algorithm: SHA256
      # Body 模式枚举：raw 表示签名使用未经重新序列化的原始请求体。
      body_mode: raw
      # 访问密钥文本，来源为 LOCAL_MNO_TRAFFIC_AK；真实值只允许存在于运行环境且不得记录。
      ak: ${LOCAL_MNO_TRAFFIC_AK}
      # 签名密钥文本，来源为 LOCAL_MNO_TRAFFIC_SK；真实值只允许存在于运行环境且不得记录。
      sk: ${LOCAL_MNO_TRAFFIC_SK}
      # 保留 Header 映射：键为内部语义，值必须保持服务端协议字段原名。
      headers:
        signature: sign
        timestamp: timestamp
        access_key: accesskey
```

The configuration loader must preserve unresolved placeholders during collection. Preflight parses `enabled` as a strict boolean and requires the key material only when enabled.

- `true`: compute and send `sign`, `timestamp`, and `accesskey`.
- `false`: remove and do not send any signature-derived headers.
- missing/malformed at execution: fail preflight with `pending_environment`; never guess a default.

## Canonicalization

1. Build `url` as `/` plus request path segments joined by `/`.
2. Copy query parameters and add the current millisecond `timestamp`.
3. Remove `null`, `undefined`, and empty-string values. Sort array values with JavaScript default string ordering and join them as `[a,b]`.
4. Sort remaining query keys and join `key=value` pairs with `&`.
5. Keep the raw request body unchanged. Build `url&body&sorted_query&secretKey` when a non-empty body exists; otherwise build `url&sorted_query&secretKey`.
6. Hash the exact UTF-8 string with SHA-256 and render lowercase hexadecimal.
7. Write reserved headers after all ordinary header merging.

This is suffix-secret SHA-256, not HMAC. Do not URL-encode or JSON-normalize the canonical input. Never log the canonical string, secret, access key, or complete signed request.

## Python adapter

```python
"""按 Seres 源码契约生成并应用请求签名。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence


RESERVED_HEADERS = {"sign", "timestamp", "accesskey"}


def _query_text(value: object) -> str:
    """将查询参数转换为签名契约要求的文本。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_seres_signature(
    *,
    path_segments: Sequence[str],
    query: Mapping[str, object],
    raw_body: str | None,
    sk: str,
    timestamp: str | None = None,
) -> tuple[str, str]:
    """按源码契约生成签名；固定 timestamp 可用于精确测试。"""
    request_timestamp = timestamp or str(time.time_ns() // 1_000_000)
    params = dict(query)
    params["timestamp"] = request_timestamp

    normalized: dict[str, str] = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            items = sorted(value, key=_query_text)
            normalized[key] = "[" + ",".join("" if item is None else _query_text(item) for item in items) + "]"
        elif isinstance(value, (str, bool, int, float)):
            normalized[key] = _query_text(value)
        else:
            raise TypeError(f"查询参数 {key} 使用了未确认的类型")

    sorted_query = "&".join(f"{key}={normalized[key]}" for key in sorted(normalized))
    url = "/" + "/".join(path_segments)
    canonical = (
        f"{url}&{raw_body}&{sorted_query}&{sk}"
        if raw_body
        else f"{url}&{sorted_query}&{sk}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), request_timestamp


def apply_seres_headers(
    headers: Mapping[str, str],
    *,
    enabled: bool,
    path_segments: Sequence[str],
    query: Mapping[str, object],
    raw_body: str | None,
    body_mode: str | None,
    sk: str | None,
    ak: str | None,
) -> dict[str, str]:
    """签名关闭时也移除旧的保留 Header，避免复用客户端时泄漏。"""
    if not isinstance(enabled, bool):
        raise ValueError("Seres 签名开关必须由 preflight 解析为布尔值")

    result = {key: value for key, value in headers.items() if key.lower() not in RESERVED_HEADERS}
    if not enabled:
        return result
    if body_mode not in (None, "raw") or (raw_body is not None and body_mode != "raw"):
        raise ValueError("Seres 签名只支持无 Body 或 raw Body")
    if not sk or not ak:
        raise ValueError("Seres 签名启用时必须提供 ak 和 sk")

    signature, timestamp = build_seres_signature(
        path_segments=path_segments,
        query=query,
        raw_body=raw_body,
        sk=sk,
    )
    result.update({"sign": signature, "timestamp": timestamp, "accesskey": ak})
    return result
```

Add one focused unit test with a fixed timestamp and exact expected digest. Keep the implementation scenario-owned until a second scenario genuinely reuses the same signing contract; then move it to `common/clients/`.
