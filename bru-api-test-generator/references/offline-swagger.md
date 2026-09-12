# Offline Swagger/OpenAPI Acquisition and Parsing

The generator always parses a local specification. A checked-in file is preferred. When it is missing, an already-running target application may be queried on loopback once to create that local file; this is not permission to use a remote contract or to keep the application as a live source of truth.

## Acquisition Order

1. Use the user-provided local path.
2. Search the target repository for the checked-in names below.
3. If neither exists, run the bundled helper from the target repository:

   ```bash
   python /path/to/bru-api-test-generator/scripts/fetch_local_openapi.py \
     --project-root . --output qa/contracts/openapi.json
   ```

   The helper detects local TCP listeners and probes only `127.0.0.1`, `localhost`, or `::1`. Use `--base-url http://127.0.0.1:8080` or `--port 8080` when the listener cannot be discovered automatically, and add `--path /your/openapi.json` when the application uses a non-standard documentation path. It tries common JSON/YAML documentation paths, validates the response, and writes the result atomically. If different local services expose different valid contracts, it blocks instead of silently selecting one; rerun with an explicit base URL/path.
4. Continue with the saved file as an ordinary offline contract. Record the source URL and downloaded file path in the generated manifest so the acquisition is auditable.

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

The repository may use the bundled `scripts/parse_openapi.py` to produce a JSON or YAML manifest based on the output extension. The parser is an inventory aid; source-code inspection remains mandatory for normal and error logic.

## Module Partition

When the application has more than one business domain, provide a module-map.yaml file and use the parser's module-map and output-dir options. The parser should write one endpoints.yaml per module and a generated index.yaml. Use Swagger tags first, then path prefixes, then reviewed controller mappings. Do not silently put unmatched operations into an arbitrary module.
