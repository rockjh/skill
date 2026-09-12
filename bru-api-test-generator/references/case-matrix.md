# API Case Matrix

Use this matrix to select cases per endpoint. A case is mandatory only when its category applies; record `applicable: false` and a short reason when it does not.

| Category | Typical cases | Applies when |
| --- | --- | --- |
| Success | valid create/read/update/delete, empty result | endpoint is reachable and authorized |
| Authentication | missing token, expired/malformed token | endpoint requires authentication |
| Authorization | authenticated but wrong role/tenant/data scope | endpoint has authorization or data permission |
| Validation | missing field, wrong type, invalid format, boundary value | request has parameters or a body |
| Business error | duplicate, forbidden state transition, missing domain object | implementation exposes a domain rule |
| Query behavior | pagination edges, sorting/filtering, no-result query | endpoint supports query options |
| Safety | repeat submission, idempotency, concurrent update | write or state-changing operation has these semantics |
| File behavior | empty, invalid type, size limit, download content | endpoint uploads or downloads files |

For every endpoint, record one decision per category in the module case manifest (or a companion scenario ledger): `applicable: true` must link to one or more case IDs, while `applicable: false` must include a reason. For every applicable case, assert the HTTP status, application/business code when present, and the important response or side effect. Use the actual application's contract; do not impose generic REST status expectations on a project that returns business errors in HTTP 200 responses.

Before templating authentication failures across a module, execute one
representative secured endpoint with no token and one with an invalid token.
Record the observed transport status and envelope code separately. Some
applications (including RuoYi-style Ajax responses) return HTTP 200 with a
business `code: 401`; copying an assumed HTTP 401 expectation creates a suite
that looks security-aware but fails every request.

The minimum contract-driven decisions are: success for every reachable operation; authentication for secured operations; validation for operations with parameters or request bodies; query behavior for list/search operations with filters, pagination, or sorting; and file behavior for multipart/upload/download operations. Authorization, business-error, safety, and boundary cases are required whenever source/configuration exposes those branches. A decision is not evidence by itself: the linked Bruno file must contain the corresponding request and assertions.

Do not manufacture cases that have no observable behavior. Explain exclusions in the manifest so the coverage checker can distinguish intentional scope from a missing test.

For object-valued query parameters, verify the target framework's binding
before writing the URL. For example, a Spring `@ModelAttribute UserQuery`
usually needs `?userName=admin&pageNum=1&pageSize=10`, not
`?user={{user}}`; the latter is one scalar string and commonly produces HTTP
400. Record the chosen serialization in the case or endpoint manifest.

## Module Flow

For a module that exposes CRUD operations, add a separate ordered flow in `flows.yaml`:

```yaml
- id: USER_CRUD_FLOW
  module: user
  steps:
    - operation: create
      case_id: USER_CREATE_OK
      capture: user_id
    - operation: update
      case_id: USER_UPDATE_OK
      uses: user_id
    - operation: query
      case_id: USER_QUERY_AFTER_UPDATE
      uses: user_id
    - operation: delete
      case_id: USER_DELETE_OK
      uses: user_id
    - operation: query
      case_id: USER_QUERY_AFTER_DELETE
      assert_absent: user_id
```

The default order is create -> update -> query -> delete -> query. If an operation is not exposed by the module, omit that step and record the missing capability and reason. The flow must use the same captured record and must remain deterministic even when other cases run before it.
