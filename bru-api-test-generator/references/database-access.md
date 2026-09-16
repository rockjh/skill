# Direct Database Access

Use direct MySQL, Elasticsearch, MongoDB, or other datastore statements only in these two cases:

1. `missing_prerequisite_api`: the module exposes no API or approved test control that can create data required by its API cases.
2. `missing_response_state`: an API response does not expose the state needed to prove that the operation succeeded.

Prefer a public or approved test API whenever it exists. Database setup is test preparation, never the business action under test. Database assertions complement the HTTP status and available business response assertions; they do not replace observable response checks that the service does provide.

## Case Format

Declare case-owned steps in `cases.yaml`. The materializer writes `setup` into `script:pre-request`, writes `assertion` and cleanup into `script:post-response`, and the runner enables Bruno's developer sandbox only when the selected scope contains database steps.

```yaml
database_steps:
  - phase: setup
    reason: missing_prerequisite_api
    engine: mysql
    evidence:
      - src/main/java/example/ThingRepository.java:42
    script: |
      const mysql = require("mysql2/promise");
      const connection = await mysql.createConnection({
        host: bru.getEnvVar("MYSQL_HOST"),
        port: Number(bru.getEnvVar("MYSQL_PORT")),
        user: bru.getEnvVar("MYSQL_USER"),
        password: bru.getEnvVar("MYSQL_PASSWORD"),
        database: bru.getEnvVar("MYSQL_DATABASE")
      });
      const thingId = bru.getEnvVar("THING_ID");
      await connection.execute(
        "INSERT INTO thing(id, status) VALUES (?, ?) ON DUPLICATE KEY UPDATE status = VALUES(status)",
        [thingId, "READY"]
      );
      await connection.end();
    cleanup: |
      const mysql = require("mysql2/promise");
      const connection = await mysql.createConnection({
        host: bru.getEnvVar("MYSQL_HOST"),
        port: Number(bru.getEnvVar("MYSQL_PORT")),
        user: bru.getEnvVar("MYSQL_USER"),
        password: bru.getEnvVar("MYSQL_PASSWORD"),
        database: bru.getEnvVar("MYSQL_DATABASE")
      });
      await connection.execute("DELETE FROM thing WHERE id = ?", [bru.getEnvVar("THING_ID")]);
      await connection.end();

  - phase: assertion
    reason: missing_response_state
    engine: mysql
    evidence:
      - src/main/java/example/ThingRepository.java:68
    expected:
      row_count: 1
      status: DONE
    script: |
      const mysql = require("mysql2/promise");
      const connection = await mysql.createConnection({
        host: bru.getEnvVar("MYSQL_HOST"),
        port: Number(bru.getEnvVar("MYSQL_PORT")),
        user: bru.getEnvVar("MYSQL_USER"),
        password: bru.getEnvVar("MYSQL_PASSWORD"),
        database: bru.getEnvVar("MYSQL_DATABASE")
      });
      const [rows] = await connection.execute(
        "SELECT status FROM thing WHERE id = ?",
        [bru.getEnvVar("THING_ID")]
      );
      test("thing state was persisted", () => {
        expect(rows).to.have.lengthOf(1);
        expect(rows[0].status).to.equal("DONE");
      });
      await connection.end();
```

`evidence` identifies the schema, entity, repository, migration, or index mapping that proves the statement and expected value. An assertion step must declare its exact `expected` result. A setup step must provide bounded cleanup or a concrete `cleanup_not_required_reason` for a deliberately retained, namespaced fixture.

## Other Engines

- MongoDB scripts may use the official `mongodb` client and exact-key operations such as `updateOne({_id: id}, {$set: ...}, {upsert: true})` and `findOne({_id: id})`.
- Elasticsearch scripts may use `@elastic/elasticsearch` and an exact index/document ID for `index`, `get`, or a tightly filtered `search`.
- Other engines use the same contract: an already approved client, exact identifiers, source-backed fields, bounded results, and explicit cleanup for setup writes.

Declare required Node clients in `qa/bruno/package.json` and pin their versions. Do not add a generic database abstraction for a one-off step.

## Runtime And Safety

Connection addresses, database/index names, users, passwords, TLS settings, and fixture identifiers come only from the active Bruno environment or its process environment. Never commit resolved credentials or credential-bearing URLs.

Statements should use parameter binding or exact document IDs and be idempotent where possible.

Post-response cleanup cannot run when execution is terminated before Bruno reaches that phase. Keep setup idempotent and cleanup rerunnable so the next run can recover deterministically. A connection, query, expectation, or cleanup error is a failed case, not a manual pass.
