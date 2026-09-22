# Offline Swagger/OpenAPI Acquisition and Parsing

The generator always parses a local specification. A checked-in file is preferred. When it is missing, an already-running target application may be queried on loopback once to create that local file; this is not permission to use a remote contract or to keep the application as a live source of truth.

## Acquisition Order

1. Use the user-provided local path.
2. Search the target repository for the checked-in names below.
3. If neither exists, let `dltk api-test generate` perform its loopback recovery attempt:

   ```bash
   dltk api-test generate --qa-root qa --design-root docs/design
   ```

   The helper detects local TCP listeners and probes only `127.0.0.1`, `localhost`, or `::1`, trying IPv4 before IPv6 on the same port. Use `--base-url http://127.0.0.1:8080` or `--port 8080` when the listener cannot be discovered automatically, and add `--path /your/openapi.json` when the application uses a non-standard documentation path. It tries common JSON/YAML documentation paths, validates the response, and writes the result atomically. Contract identity ignores deployment-only `servers`, Swagger `host`/`schemes`, and collection provenance while retaining paths, components, security, and `basePath`. If different local services still expose different contracts, it blocks instead of silently selecting one; rerun with an explicit base URL/path.
4. Continue with the saved file as an ordinary offline contract. Record the source URL and downloaded file path in the generated manifest so the acquisition is auditable.
5. After successful generation and materialization, a loopback `provenance.source_url` requires an immediate default execution attempt. Environment or request failures are reported per case and do not cancel later cases.

Do not stop at step 2 with a "provide or check in an offline specification" message. The loopback helper is the required recovery attempt when the repository has no usable local contract.

The helper does not start the application, send authentication credentials, follow redirects away from loopback, or contact a remote host. Exit code `2` means no local listener was found without an explicit candidate; exit code `3` means no valid document could be fetched or written at the supplied/discovered candidates. Either result is a blocker and must include the helper output in the handoff.

## File Discovery

Use the user-provided path first. If none is supplied, look for checked-in files with these names or extensions:

```text
swagger.json
openapi.json
swagger.yaml
openapi.yaml
*.swagger.json
*.openapi.json
```

If multiple files exist, list them and ask which one is the contract for the target application or select the one identified by the repository's build/configuration. Do not merge unrelated specifications silently. A file produced by the loopback helper is preferred over an unrelated checked-in specification only when its URL and target application are clear.

## Minimal Parse Contract

Support OpenAPI 2.0 and OpenAPI 3.x in JSON. Support YAML only when an existing YAML parser such as `PyYAML` is available. Extract, for every operation under `paths`:

- stable endpoint ID, HTTP method, and path template;
- `operationId`, tags, summary, and controller/domain hint when available;
- path/query/header/cookie parameters and required flags;
- OpenAPI 3 `requestBody`, or OpenAPI 2 body/form parameters;
- declared response status codes and content/schema references;
- operation-level security, falling back to top-level security definitions.

Full schema generation and exhaustive `$ref` dereferencing are not required for the initial inventory. Preserve unresolved references in the manifest and inspect the referenced definitions when concrete request/response assertions need them. A malformed or unsupported document is a blocker, not a reason to guess.

The shared runtime resolves local `#/...` references for parameter, request-body,
and response metadata while retaining the original `$ref`. The generated source
records the document SHA. A loopback-acquired document retains its `provenance`
block; missing application build metadata is reported as
`contract_provenance_unverified` and must be resolved before a collection is
called verified. The parser is an inventory aid. Use reviewed design documents
for normal and error logic; source inspection is limited to execution
preparation and request data support.

## Module Partition

Provide a `module-map.yaml` file with one stable ASCII `id`, an optional
human-readable `directory`, and at most one `swagger_tags` value per original
OpenAPI Tag, then use the parser's `--module-map` and `--output-dir` options.
For untagged operations, declare `operation_ids`, `path_prefixes`, one
`default: true` module, or a single-module map. The parser writes one isolated
`endpoints.yaml`, `parameters.yaml`, `definitions.yaml`, and `responses.yaml`
per module, plus a generated `index.yaml`. An operation with multiple Tags
needs an explicit `primary_tags` override. A missing fallback owner remains a
blocker; URL prefixes are only fallback configuration for operations that have
no Tag.
