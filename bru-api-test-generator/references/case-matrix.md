# API Case Matrix

Generate decisions from OpenAPI, source, security configuration, and probe evidence. Every endpoint has all eight categories. Applicable decisions link dedicated cases; confirmed false decisions contain a concrete reason.

| Category | Evidence | Required coverage |
| --- | --- | --- |
| `success` | every reachable operation | at least one distinct success case |
| `authentication` | OpenAPI security, authentication configuration, probes | missing and invalid credentials with observed status/envelope |
| `authorization` | `x-permissions`, `x-roles`, source permission annotations, security profile, probes | authenticated but forbidden cases |
| `validation` | required, enum, pattern, min/max, length, format, media type | each declared invalid or boundary class |
| `query` | pagination, filter, sort parameters | boundaries, filters, sorting, combinations, empty results, invalid page/pageSize |
| `file` | multipart/binary schema and declared source constraints | missing/empty file, extension, MIME, and size cases where proven |
| `business_error` | API-reachable exceptions and real error codes | one observable case per reachable business result |
| `safety` | idempotency, concurrency, repeat-submission evidence | declared safety behavior |

## Coverage Profiles

`contract-draft` generates only cases whose request shape and expected transport behavior are directly provable from OpenAPI. Generated cases stay `status: draft` and `review_required: true` until their exact assertions and fixtures are confirmed.

`full-matrix` generates every applicable scenario supported by contract, source, security profile, or probe evidence. It does not invent missing evidence. A true scenario without enough evidence to build a precise case remains a blocking gap.

`verified` is not a generation profile. It is available only after the default all-module scope passes strict reconciliation and execution.

## Authentication And Audit Context

Do not infer authentication or authorization from an `/admin` path. Distinguish these profiles:

```yaml
admin-operator-context:
  type: audit-context
  header: operatorInfo
  probe_result:
    missing_header_status: 200
    reason: 本地 project.headerVerify=false

auth-token:
  type: authentication
  header: Authorization
  probe_result:
    no_token_status: 401
    invalid_token_status: 401
```

Run one representative missing-token probe and one invalid-token probe before copying authentication expectations. Preserve the actual HTTP status and response envelope, including applications that return HTTP 200 with a business error code.

An audit Header that is optional under the active configuration is not authentication evidence. Record it as confirmed non-applicable for an authentication case, with the probe result and reason.

Authorization requires at least one of OpenAPI security/permission extensions, `x-permissions`, `x-roles`, source permission annotations, `security-profile.yaml`, or a probe result. Otherwise use a confirmed false decision such as:

```yaml
authorization:
  applicable: false
  status: confirmed
  reason: OpenAPI 未声明权限模型，源码未发现权限校验；operatorInfo 仅为审计上下文
```

## Validation

Generate distinct cases for applicable constraints:

- missing required query, path, Header, and body fields;
- illegal enum values and pattern mismatches;
- values immediately outside minimum/maximum and minLength/maxLength;
- invalid declared formats;
- wrong Content-Type when the endpoint declares a rejection response.

For object-valued query parameters, verify framework binding and emit flattened fields or documented serialization. Never send the object as an unexplained scalar.

## Query And File Cases

For query endpoints cover page/pageSize boundaries, filter values, sort values, combinations, empty results, and invalid pagination inputs. Use real parameter names from the contract or source; do not fabricate a generic pagination API.

For multipart or binary inputs always cover a missing required file. Cover empty files, invalid extension, invalid MIME, and oversized files only when the contract or API-reachable source declares the corresponding constraint. Fixtures belong in the execution environment or fixture directory, not in config.

## Business Errors And Safety

Generate business errors only from API-reachable implementation evidence. Discover the exception/error-code family from `ControllerAdvice`, constructor types, or one unique name-matched pair; block ambiguous families until explicitly configured.

Scanners exclude tests, architecture checks, Javadoc, error-enum definitions, constant-only classes, build output, and branches that are not reachable from a controller mapping. A candidate that maps to zero or multiple endpoints is a blocker.

Generate safety cases only where idempotency, concurrency, or duplicate-submission behavior is declared. HTTP method alone is not evidence.

## Completion

Every case keeps its `risk`, request, expected HTTP/business result, and precise assertions. Draft existence-only assertions aid review but cannot satisfy verified completion. Every source-backed logic entry links real case IDs, and every declared flow has ordered execution evidence.
