# Analysis Policy

Use this reference only for the business-flow document workflow. The CLI schemas and generated index/report remain authoritative for fields and counts.

## Entry discovery

Inspect framework configuration, annotations/decorators, route registration, service registration, scheduler/job declarations, message listener bindings, event subscriptions, file watchers/importers, command registration, webhook routes, and workflow/state-machine triggers. Search both source and configuration; a controller directory is not a complete inventory. Health, metrics, management, and static-resource handlers are excluded only when their non-business purpose is evidenced.

For each entry retain: type, method/topic/job/event/file identifier, handler, source location, owning module, and the document file. A route may have only one primary owner. A cross-module call is a dependency in the owner document.

`business-flow-modules.json` is the review boundary. Resolve every discovery finding with code locations, use `entry_overrides` when static analysis found the entry but not its exact metadata or evidence, add scanner misses through `additional_entries`, and exclude non-business candidates only with reason and source evidence. Re-run discovery after source/config changes; confirmation from a different source fingerprint is invalid.

## Call-chain and error review

Follow calls until the business result is produced. Check validation, state guards, persistence, transaction boundaries, locks, idempotency, concurrency, cache/files, external calls, message production, retries, and async scheduling. Search called helpers and exception adapters, not just the entry file. Only an active exception that can propagate to the entry is an entry error. Do not count framework-generated validation errors, unreachable branches, or caught exceptions that cannot escape.

When the code uses an exception handler or adapter to convert an internal exception, retain both the throwing evidence and the mapping evidence. If the error code is assembled dynamically or the path crosses an unresolved boundary, record `代码中未确认` instead of inventing a code.

## Incremental review

Compare the old recorded revision with the target using Git, including shared utilities and configuration. Re-analyze a module when an entry, call, rule, error mapping, state transition, data operation, transaction/lock/async behavior, external integration, or shared dependency changes. If the old revision cannot be resolved, perform a full current-version scan and report that comparison as unavailable. If no business file changed, preserve document structure and only update the effective version metadata.

## Completion report

Report effective commit and dirty status, module/document counts, URL, scheduled, message, and other entry counts, active error-code count, added/updated/deleted entries, version-only documents, business-changed documents, unresolved evidence, and both directions of entry/error-code coverage. A zero-entry repository is valid only when the scan evidence and excluded framework endpoints are reported.
