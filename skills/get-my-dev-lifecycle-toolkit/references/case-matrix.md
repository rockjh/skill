# API Case Matrix

Generate protocol decisions from OpenAPI and business decisions from reviewed design rules. Source and probe evidence can only establish execution support or report implementation drift. Every endpoint has all eight categories. Applicable decisions link dedicated cases; confirmed false decisions contain a concrete reason.

| Category | Evidence | Required coverage |
| --- | --- | --- |
| `success` | reviewed design rule for every reachable operation | at least one distinct success case |
| `authentication` | reviewed design rule; OpenAPI security only describes request shape | missing and invalid credentials with the design-defined status/envelope |
| `authorization` | reviewed design permissions and conditions; OpenAPI security is protocol context | authenticated but forbidden cases defined by design |
| `validation` | required, enum, pattern, min/max, length, format, media type | each declared invalid or boundary class |
| `query` | reviewed design behavior plus OpenAPI parameter structure | boundaries, filters, sorting, combinations, empty results, invalid page/pageSize |
| `file` | multipart/binary schema and explicit contract constraints | missing/empty file, extension, MIME, and size cases where proven |
| `business_error` | design rules and documented business error semantics | one case per documented business result |
| `safety` | design rules for idempotency, concurrency, repeat submission | declared design behavior |

## Coverage Profiles

`contract-draft` generates only cases whose request shape and expected transport behavior are directly provable from OpenAPI plus the project constraint library. Any `manual_confirmation` is a generation blocker; resolve the design ambiguity instead of executing unrelated business cases from a partial model.

`full-matrix` generates every applicable scenario supported by OpenAPI protocol constraints or reviewed design rules. Security configuration prepares requests; probe evidence reports drift. A true scenario without enough design evidence for a precise business case remains a blocking gap.

`verified` is not a generation profile. It is available only after the default all-module scope passes strict reconciliation and execution.

## Authentication And Required Headers

Do not infer authentication or authorization from an `/admin` path. Distinguish these profiles:

```yaml
required-tenant-context:
  type: required-header
  header: X-Tenant-Id
  probe_result:
    missing_header_status: 400
    reason: The application rejects a missing tenant context Header

auth-token:
  type: authentication
  header: Authorization
  probe_result:
    no_token_status: 401
    invalid_token_status: 401
```

Run one representative missing-token probe and one invalid-token probe only to report implementation drift. Preserve the actual HTTP status and response envelope in evidence; do not copy observed values into expectations unless the reviewed design declares them.

A required application Header is not automatically authentication evidence. Use OpenAPI and execution configuration to prepare the request, and use reviewed design rules for authentication behavior; record confirmed non-applicability separately.

Authorization behavior requires a reviewed design rule. OpenAPI security extensions and `security-profile.yaml` can prepare credentials, while source annotations and probes can report drift. Otherwise use a confirmed false decision such as:

```yaml
authorization:
  applicable: false
  status: confirmed
  reason: reviewed design declares no authorization branch for this operation
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

For query endpoints cover page/pageSize boundaries, filter values, sort values, combinations, empty results, and invalid pagination inputs. Use real parameter names from OpenAPI; do not fabricate a generic pagination API.

For multipart or binary inputs always cover a missing required file. Cover empty files, invalid extension, invalid MIME, and oversized files only when OpenAPI or the reviewed design declares the corresponding constraint. Fixtures belong in the execution environment or fixture directory, not in config.

## Business Errors And Safety

Generate business errors only from reviewed design rules. Source exception handlers and runtime observations may report implementation drift, but they must never create business expectations.

Generate safety cases only where idempotency, concurrency, or duplicate-submission behavior is declared. HTTP method alone is not evidence.

## Design Rule Markers

Each `METHOD /path` section in a reviewed Markdown design document may contain one or more explicit rules. Use one `Rule ID` per rule; repeat the marker block for multiple outcomes on the same endpoint:

```markdown
## POST /jobs
Rule ID: JOBS_ACCEPTED
Scenario: success
Condition: request is valid
Request: {body: {jobType: reconcile}}
Async: true
HTTP status: 202
Acceptance status: accepted
Final status: completed
State: completed
Business code: 0
Assert: $.status = completed
```

For an ordered multi-request rule, declare the complete executable flow once.
Each step references a reviewed rule ID; captures map names to response JSON
paths, and every `uses` name must occur in that rule's request:

```markdown
Test Flow: {id: JOB_FLOW, steps: [{rule_id: JOB_ACCEPTED, operation: submit, capture: {job_id: "$.jobId"}}, {rule_id: JOB_COMPLETED, operation: poll, uses: [job_id]}]}
```

Use distinct rule IDs for repeated submissions or retries. A retry step uses
`operation: retry`. An external-failure rule is blocked unless the project
supplies an authorized fault-injection operation and a verified
restoration/cleanup operation. A step named `fault-inject`, `inject-failure`,
`mock-failure`, or `dependency-failure` is only a design label; it is not
execution evidence. All referenced operations still require OpenAPI endpoints
and design rules.

Supported markers are `Rule ID`, `Scenario`, `Condition`, `Request`, `Async`, `HTTP status`, `Business code`, `State`, `State transition`, `Acceptance status`, `Final status`, `Side effect`, `Idempotency`, `Retry`, `Concurrency`, `External failure`, and `Assert`. Marker values use YAML scalar/container types. Markers are an acceleration format, not a design requirement: prose, tables, code/curl blocks, ordered steps, and acceptance checklists are read into the design-understanding matrix first. Each extracted fact is tagged `explicit`, `derived`, or `unknown`, with a source quote and derivation. Every non-success branch and every endpoint with multiple rules needs an executable request mapping; if prose does not establish one, keep the candidate in pending confirmation instead of borrowing values from source or runtime.

An asynchronous rule must declare both acceptance and final status and a
bounded polling/reconciliation mechanism with an explicit termination policy.
A sequential submit-plus-single-query flow is not polling evidence and remains
blocked. Async and safety rules are blocked while they describe only
single-request metadata. A sequential flow never proves concurrency, and
cross-module or cross-service flows belong to the E2E domain. The generator
rejects design HTTP statuses absent from OpenAPI and records ambiguous rules in
`manual_confirmations` without materializing business tests.

## Completion

Every case keeps its request, expected HTTP/business result, design or OpenAPI evidence, and precise assertions. `review-*` values and unresolved confirmations block generation. Every success case requires at least one exact business-result or state-change assertion; when a business code is declared, it must be a success value and be accompanied by a concrete result assertion. Every design-backed logic entry links real case IDs, and every declared flow has ordered execution evidence.
