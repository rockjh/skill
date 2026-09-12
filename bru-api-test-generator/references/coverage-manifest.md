# Modular Coverage Manifest

Use module manifests as the source of truth. The global index is generated from them and is used only for cross-module reconciliation.

## Version Lock

Keep version-lock.yaml at the contracts root. It records the business Git commit against which the Bruno collection was reviewed and executed. A stale lock is not a passing state; classify the changed files, adapt affected modules when needed, then advance the lock.

    version: 1
    business:
      repo: /path/to/business-repository
      commit: abc123...
      ref: main
      impact_review: non-api

## module-map.yaml

Map Swagger tags, path prefixes, and controllers to one primary business module:

    modules:
      - id: system-user
        name: 用户管理
        swagger_tags:
          - sys-user-controller
        path_prefixes:
          - /system/user
        controllers:
          - SysUserController
      - id: system-role
        name: 角色管理
        swagger_tags:
          - sys-role-controller
        path_prefixes:
          - /system/role
        controllers:
          - SysRoleController

Every operation must match exactly one primary module. Unmatched operations must be excluded with a reason; multiply matched operations need an explicit primary module.

## Module endpoints.yaml

Each module owns its endpoint inventory:

    version: 1
    module: system-user
    source:
      openapi_file: contracts/openapi.json
    endpoints:
      - id: USER_CREATE
        method: POST
        path: /system/user
        operation_id: addUser
        case_ids:
          - USER_CREATE_OK
          - USER_CREATE_DUPLICATE
          - USER_CREATE_NO_TOKEN

## Module cases.yaml

Cases link back to an endpoint and optionally to source logic. Assertions must include a concrete field or relation beyond status and business code:

    version: 1
    module: system-user
    cases:
      - id: USER_CREATE_OK
        endpoint_id: USER_CREATE
        scenarios:
          success: {applicable: true}
          authentication: {applicable: true}
          validation: {applicable: true}
          authorization: {applicable: false, reason: "endpoint has no role or tenant guard"}
        logic_ids:
          - USER_CREATE_NORMAL_LOGIC
        bru: create-success.bru
        expected:
          http_status: 200
          business_code: 0
        assertions:
          - path: $.data.userName
            equals: "{{user_name}}"
          - path: $.data.userId
            exists: true

      - id: USER_CREATE_DUPLICATE
        endpoint_id: USER_CREATE
        logic_ids:
          - USER_CREATE_DUPLICATE_LOGIC
        bru: create-duplicate.bru
        expected:
          http_status: 200
          business_code: 601
        assertions:
          - path: $.msg
            contains: "已存在"

If decisions are shared by multiple cases, put the matrix on the endpoint
instead of repeating it on every case:

```yaml
      - id: USER_CREATE
        method: POST
        path: /system/user
        scenario_matrix:
          success: {applicable: true}
          authentication: {applicable: true}
          authorization: {applicable: false, reason: "no role/tenant guard"}
          validation: {applicable: true}
          business_error: {applicable: true}
          query: {applicable: false, reason: "not a query operation"}
          safety: {applicable: false, reason: "no idempotency contract"}
          file: {applicable: false, reason: "not a file operation"}
```

Run the coverage checker with `--require-scenarios` at the completion gate;
it accepts either this endpoint-level form or per-case `scenarios` entries.

## Module logic.yaml

    version: 1
    module: system-user
    logic:
      - id: USER_CREATE_NORMAL_LOGIC
        source: SysUserService.insertUser
        condition: username is unique and required data is valid
        case_ids:
          - USER_CREATE_OK
      - id: USER_CREATE_DUPLICATE_LOGIC
        source: SysUserService.insertUser
        condition: username already exists
        case_ids:
          - USER_CREATE_DUPLICATE

## Module flows.yaml

    version: 1
    module: system-user
    flows:
      - id: USER_CRUD_FLOW
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
            uses: user_id
            assert_absent: user_id

If a module does not expose one CRUD operation, omit that step and record the missing capability in the module exclusions file.

## Global index.yaml

Generate this file; do not hand-edit it:

    version: 1
    source:
      openapi_file: contracts/openapi.json
      swagger_sha256: ...
    modules:
      - id: system-user
        endpoints_file: modules/system-user/endpoints.yaml
        cases_file: modules/system-user/cases.yaml
        endpoint_count: 18
        case_count: 72
      - id: system-role
        endpoints_file: modules/system-role/endpoints.yaml
        cases_file: modules/system-role/cases.yaml
        endpoint_count: 12
        case_count: 48

The global checker compares the union of module endpoints with the offline Swagger inventory and verifies that every endpoint, logic path, case, and flow is accounted for.

The checker should also reject duplicate IDs, duplicate case-to-file mappings, unregistered Bruno files, unknown case endpoint references, and missing scenario decisions when strict matrix validation is enabled. Pass the saved offline document explicitly (`--openapi contracts/openapi.json`) so a manifest cannot validate against itself.
