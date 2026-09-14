# API Case Matrix

Generate endpoint decisions from actual contract, configuration, and source
evidence. Every decision has `status: inferred` until a human changes it to
`confirmed`. An applicable decision links cases; a non-applicable decision
contains a concrete reason.

| Category | Inference source | Generated coverage |
| --- | --- | --- |
| `success` | every reachable operation | one distinct success case |
| `validation` | `required`, `enum`, `min/max`, `pattern`, request schema | missing/invalid/boundary cases |
| `query` | pagination, filter, sort query parameters | pageNum/pageSize and filter boundaries |
| `file` | `multipart/form-data`, binary schema | missing file and declared file constraints |
| `authentication` | OpenAPI security, admin/internal paths, interceptor and audit Header evidence | missing/invalid credential or required Header |
| `authorization` | permission annotations/extensions, Controller path, role/tenant/data-scope model | authenticated but forbidden cases |
| `business_error` | service/domain exceptions and project error-code definitions | one case per observable business code |
| `safety` | idempotency, concurrency, repeat-submission semantics | the declared safety behavior |

Example:

```yaml
scenario_matrix:
  success:
    applicable: true
    status: inferred
    reason: 所有可达接口默认覆盖成功路径
  authentication:
    applicable: true
    status: inferred
    reason: 管理端路径且源码读取必需的 operatorInfo Header
  business_error:
    applicable: true
    status: confirmed
    reason: DeleteAcGroupService 抛出业务码 143000
```

Contract generation may seed only observable values defined by OpenAPI. It
must cover required Header/body fields, illegal enum and pattern values,
min/max and pagination boundaries, and required upload files when a matching
error response is declared. It marks all seeds `review_required: true` and all
contract logic `draft`.

Source enhancement scans controllers, orchestration/domain services,
validation, permissions, exception handlers, state and uniqueness checks,
external-call failures, and error-code definitions. A source candidate is not
coverage until a reviewed `logic.yaml` entry links a real case. Do not invent
fixtures, HTTP status, or business envelopes that source/contract evidence does
not establish; leave the gap explicit.

Before copying authentication expectations across a module, execute one
representative missing-credential and one invalid-credential probe. Preserve
the application's real transport status and response envelope, including
systems that return HTTP 200 with a business error code.

For object-valued query parameters, verify framework binding and emit flattened
fields or a documented supported serialization. Never send a whole object as
an unexplained scalar variable.

## Module Flow

Only a module or endpoint explicitly marked `flow_required: true` or with a
non-empty `flow_kind` needs an ordered flow. Capture and reuse the same record
through create/update/query/delete/post-delete query. If the API lacks a step,
record that capability gap; do not invent an endpoint. HTTP method alone never
implies CRUD flow coverage.
