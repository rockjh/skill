# Seres Request Signing Template

Use this template only when discovery confirms the service uses the supplied
Seres contract. The switch is the project configuration key `seres.sign`:

- `true`: compute and send `sign`, `timestamp`, and `accesskey`;
- `false`: do not compute a signature and do not send any signature-derived
  headers or fields;
- missing or malformed: fail preflight instead of guessing a default.

The algorithm is CryptoJS `SHA256`, rendered as lowercase hexadecimal. The
secret is appended to the canonical string as a suffix; this is not HMAC. Do
not URL-encode, JSON-normalize, or otherwise reorder values unless the service
contract explicitly changes.

## Configuration template

```yaml
# 是否启用 Seres 签名；只能明确填写 true 或 false，不能省略。
seres.sign: true
# 以下值必须由环境变量、密钥管理服务或项目批准的配置提供方注入。
SECRET_KEY: ${SECRET_KEY}
ACCESS_KEY: ${ACCESS_KEY}
```

在 Python/pytest 中把 `seres.sign` 解析成严格的布尔值后再传给
`apply_seres_headers(sign_enabled=...)`；不要把非空字符串直接当成 true。

## Canonicalization

1. Build `url` as `/` plus the request path segments joined by `/`.
2. Copy query parameters and add the current millisecond `timestamp`.
3. Remove values that are `null`, `undefined`, or the empty string. Convert an
   array to `[` + its default-sorted values joined by `,` + `]`.
4. Sort remaining query keys and join `key=value` pairs with `&`.
5. Use the raw request body unchanged. Build
   `url&body&sorted_query&secretKey` when a body exists, otherwise
   `url&sorted_query&secretKey`.
6. Hash that exact string with SHA-256 and upsert the three output headers.

Never print the canonical string, secret, access key, or complete signed
request in logs. The examples use Chinese comments so they can be copied into
generated artifacts without losing the rationale for each security-sensitive
step.

## Postman / CryptoJS pre-request script

```javascript
const CryptoJS = require('crypto-js');

// 签名开关必须来自项目配置；Postman 环境变量通常以字符串返回，因此同时兼容布尔值。
const signSwitch = pm.environment.get('seres.sign');
const signSwitchText = signSwitch === true || signSwitch === false
    ? String(signSwitch).toLowerCase()
    : String(signSwitch || '').toLowerCase();
if (signSwitchText !== 'true' && signSwitchText !== 'false') {
    throw new Error('seres.sign 必须明确配置为 true 或 false');
}
const signEnabled = signSwitchText === 'true';

// 关闭签名时先移除可能残留的签名派生 Header，确保本次请求不会携带旧签名。
if (!signEnabled) {
    pm.request.headers.remove('sign');
    pm.request.headers.remove('timestamp');
    pm.request.headers.remove('accesskey');
} else {
    // 密钥只能从批准的密钥提供方读取，禁止把真实密钥写进脚本或测试数据。
    const secretKey = pm.environment.get('SECRET_KEY');
    const accessKey = pm.environment.get('ACCESS_KEY');
    if (!secretKey || !accessKey) {
        throw new Error('seres.sign=true 时必须配置 SECRET_KEY 和 ACCESS_KEY');
    }

    // 时间戳既参与签名串，也必须与最终请求 Header 使用同一个值。
    const timestamp = Date.now().toString();

    // 严格复现服务端的路径拼接规则，不额外做 URL 编码或大小写转换。
    const url = `/${pm.request.url.path.join('/')}`;
    const params = pm.request.url.query.toObject();
    params.timestamp = timestamp;

    // 删除空参数；数组按 JavaScript 默认排序后用逗号连接，并保留方括号。
    Object.entries(params).forEach(([key, value]) => {
        if (value === null || value === undefined || value === '') {
            delete params[key];
        } else if (Array.isArray(value)) {
            params[key] = `[${value.sort().join(',')}]`;
        }
    });

    // 参数名按字典序排序，不能改成请求出现顺序，也不能省略 timestamp。
    const signParams = Object.keys(params)
        .sort()
        .map(key => `${key}=${params[key]}`)
        .join('&');

    // Seres 只定义 raw Body 的拼接规则；其他 Body 类型若继续执行会产生错误签名。
    const bodyMode = pm.request.body && pm.request.body.mode;
    if (bodyMode && bodyMode !== 'raw') {
        throw new Error('seres.sign=true 时 Body 类型必须为 raw');
    }

    // 使用原始 Body，不做 JSON 重排或压缩；空 Body 走无 body 的拼接分支。
    const body = pm.request.body && pm.request.body.raw;
    const signStr = body
        ? `${url}&${body}&${signParams}&${secretKey}`
        : `${url}&${signParams}&${secretKey}`;

    // 只保留签名结果；禁止输出 signStr、secretKey 或完整请求到日志。
    const sign = CryptoJS.SHA256(signStr).toString();

    // Header 名称是 Seres 契约的一部分，使用 upsert 避免重复 Header。
    pm.request.headers.upsert({ key: 'sign', value: sign });
    pm.request.headers.upsert({ key: 'timestamp', value: timestamp });
    pm.request.headers.upsert({ key: 'accesskey', value: accessKey });
}
```

## Python / pytest adapter

```python
from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence


def _js_query_value(value: object) -> str:
    """把常见 Python 值转换成与 JavaScript 模板字符串一致的文本。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _js_array_value(value: object) -> str:
    """模拟 JavaScript Array.join(',') 对 null 数组元素的空文本转换。"""
    if value is None:
        return ""
    return _js_query_value(value)


def _validate_query_value(key: str, value: object) -> None:
    """限制查询值为 URL 解析器常见的字符串形态，避免跨语言转换漂移。"""
    if isinstance(value, (list, tuple)):
        if any(item is not None and not isinstance(item, str) for item in value):
            raise TypeError(f"查询参数 {key} 的数组元素必须是字符串或 None")
    elif value is not None and not isinstance(value, str):
        raise TypeError(f"查询参数 {key} 必须是字符串、字符串数组或 None")


def build_seres_signature(
    *,
    path_segments: Sequence[str],
    query: Mapping[str, object],
    raw_body: str | None,
    secret_key: str,
    timestamp: str | None = None,
) -> tuple[str, str]:
    """按 Seres 契约返回 (签名, 时间戳)，保持与 Postman 模板同一拼接顺序。"""
    # 时间戳必须只生成一次，签名串和最终 Header 不能各自取值。
    request_timestamp = timestamp or str(time.time_ns() // 1_000_000)
    url = "/" + "/".join(path_segments)

    # 复制查询参数，避免修改调用方对象；timestamp 覆盖同名请求参数。
    params: dict[str, object] = dict(query)
    params["timestamp"] = request_timestamp
    normalized: dict[str, str] = {}
    for key, value in params.items():
        _validate_query_value(key, value)
        # Python 没有 JavaScript 的 undefined；None 对应 null，均按契约删除。
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            # 先按 JavaScript 的字符串值排序，再模拟 join(',') 的 null 空文本转换。
            items = sorted(value, key=_js_query_value)
            normalized[key] = "[" + ",".join(_js_array_value(item) for item in items) + "]"
        else:
            normalized[key] = _js_query_value(value)

    # 参数名按字典序排序；不要在这里做 URL 编码或 JSON 序列化。
    sign_params = "&".join(f"{key}={normalized[key]}" for key in sorted(normalized))
    if raw_body:
        sign_str = f"{url}&{raw_body}&{sign_params}&{secret_key}"
    else:
        sign_str = f"{url}&{sign_params}&{secret_key}"

    # 只返回 SHA-256 小写十六进制结果，绝不记录 sign_str 或密钥。
    signature = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()
    return signature, request_timestamp


def apply_seres_headers(
    headers: Mapping[str, str],
    *,
    sign_enabled: bool,
    path_segments: Sequence[str],
    query: Mapping[str, object],
    raw_body: str | None,
    secret_key: str | None,
    access_key: str | None,
    body_mode: str | None,  # None 表示无 Body，raw 表示 raw Body
) -> dict[str, str]:
    """根据 seres.sign 开关返回请求 Header，关闭时不产生签名派生字段。"""
    # 开关必须是配置层解析后的布尔值，禁止把任意非空字符串当成 true。
    if not isinstance(sign_enabled, bool):
        raise ValueError("seres.sign 必须明确配置为 true 或 false")
    if sign_enabled and (body_mode not in (None, "raw") or (raw_body is not None and body_mode != "raw")):
        raise ValueError("seres.sign=true 时 Body 类型必须为 raw；无 Body 时传 None")

    # 先复制调用方 Header，确保场景级自定义 Header 不被原地修改。
    result = dict(headers)
    # 关闭签名时移除残留值，不能仅仅跳过新增，否则旧签名仍会随请求发送。
    for key in list(result):
        if key.lower() in {"sign", "timestamp", "accesskey"}:
            result.pop(key)
    if not sign_enabled:
        return result
    if not secret_key or not access_key:
        raise ValueError("seres.sign=true 时必须配置 SECRET_KEY 和 ACCESS_KEY")

    signature, timestamp = build_seres_signature(
        path_segments=path_segments,
        query=query,
        raw_body=raw_body,
        secret_key=secret_key,
    )
    # Seres Header 名称固定；赋值会覆盖同名旧值，避免签名和时间戳不一致。
    result.update({"sign": signature, "timestamp": timestamp, "accesskey": access_key})
    return result
```

Keep these functions in the shared signing client only after a second scenario
uses the same contract; otherwise keep the implementation in the owning
scenario directory. Add a focused test that fixes the timestamp and asserts
the exact signature string/result, without ever printing the secret.
