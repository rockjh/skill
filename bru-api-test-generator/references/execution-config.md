# Bruno Execution Configuration

`qa/execution/config.yaml` is the only shared runtime configuration. Do not
recreate `request-auth.yaml`, `request-context.yaml`, or per-request
authentication scripts.

## Layout

```text
qa/
|-- bruno/
|   |-- bruno.json
|   |-- collection.bru
|   `-- <module>/
|-- contracts/
|   `-- ...
|-- execution/
|   |-- config.yaml
|   |-- environments/
|   |   `-- local.bru
|   |-- run.bat
|   |-- run.sh
|   `-- README.md
`-- scripts/
    |-- execution_config.py
    `-- run_bruno.py
```

Move existing environments from `bruno/environments/` as one directory. Do
not leave a copy or invent `dev.bru`, `test.bru`, or other environments.

## Schema

```yaml
# 当前激活环境；不包含 .bru 后缀。
active_environment: local

auth:
  # none | seres-sign | bearer | api-key | cookie
  mode: none

  # seres-sign:
  # access_key_env: ACCESS_KEY
  # secret_key_env: SECRET_KEY

  # bearer:
  # token_env: ACCESS_TOKEN

  # api-key:
  # key_env: API_KEY

  # cookie:
  # cookie_env: SESSION_COOKIE

custom_headers:
  operatorInfo:
    # env 是 Bruno 环境变量名；值为空时不发送。
    env: OPERATOR_INFO
    # 匹配请求路径，忽略查询参数。
    paths:
      - "/v*/admin/**"

  # 非敏感固定值使用 value；value 和 env 不能同时出现。
  # X-Gray-Traffic:
  #   value: "true"
  #   paths:
  #     - "/v*/internal/**"
```

Unknown fields are errors. The configuration intentionally has no `version`:
Git history, strict validation, and regression tests define the format until a
real incompatible migration exists.

`seres-sign` fixes SHA-256, URL/body/query inputs, millisecond timestamps, and
the `sign`, `timestamp`, and `accesskey` Header names in `collection.bru`.
OAuth2 acquisition is an external bootstrap concern; configure the resulting
token as `bearer`.

## Precedence And Exclusions

The collection-level pre-request script runs before request-level scripts. It
does not replace a Header already declared by the request. To verify that a
configured Header is absent, declare:

```yaml
request:
  omit_common_headers:
    - operatorInfo
```

The materializer emits a request-level `req.deleteHeaders(...)` block. Changing
authentication or common Headers never requires regenerating all request
files.

## Execution

Use `qa/execution/run.bat` on Windows and `qa/execution/run.sh` on Linux or
macOS. Both default to all modules and accept `--module <id-or-name>`. The
runner loads the active environment with Bruno `--env-file`, injects only the
validated runtime structure, stores the raw Bruno report in a temporary
directory, and normalizes evidence without request/response Headers or bodies.

A module run reports only that module and never updates
`contracts/version-lock.yaml`. Only a verified full run can advance the lock.
Bruno GUI does not discover environments outside the collection or read
`config.yaml`; import an environment manually for GUI debugging.
