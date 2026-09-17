# Incremental Generation

Run:

```bash
dev-ai api-test generate --openapi qa/contracts/openapi.json --incremental
```

`qa/contracts/generation-state.yaml` records the OpenAPI SHA, stable endpoint
IDs, endpoint fingerprints, module contract fingerprints, case structural
fingerprints, last generation time, generator version, deleted endpoints, and
manual-review cases. `qa-lock.yaml` independently seals the current state.

The generator follows these rules:

- An unchanged OpenAPI/module/endpoint is not rewritten.
- A new endpoint adds only its module contract and draft cases.
- A deleted endpoint is retained as an explicit cleanup notice; remove its
  registered `.bru` only after review.
- Existing case content is fingerprinted without generated path fields. A
  manual change is preserved and marked `manual_review: true`.
- Writes use content comparison, so identical regeneration does not change
  timestamps or create meaningless workspace changes.

The stable endpoint ID and stable English case ID are identities; business
titles and filenames are presentation. A title change must reconcile
`cases.yaml`, `<NN>-<中文标题>.bru`, `meta.name` mapping, and `CASES.md` without
changing the stable ID.

Do not clear `manual_review` automatically. A human must compare the changed
contract/source evidence, update the request and assertions, execute the
affected module, and then confirm the case. Successful unchanged cases retain
their execution evidence and are not reset.
