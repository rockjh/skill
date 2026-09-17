# Direct Database Access

Use direct MySQL, Elasticsearch, MongoDB, or other datastore statements only in these two cases:

1. `missing_prerequisite_api`: the module exposes no API or approved test control that can create data required by its API cases.
2. `missing_response_state`: an API response does not expose the state needed to prove that the operation succeeded.

Prefer a public or approved test API whenever it exists. Database setup is test preparation, never the business action under test. Database assertions complement the HTTP status and available business response assertions; they do not replace observable response checks that the service does provide.

## Case Format

Declare case-owned steps in `cases.yaml`. The runner gathers every selected `setup` step into one run-level plan and materializes only a prerequisite guard in the case. Bruno retains `assertion` steps in `script:post-response`, and the runner enables its developer sandbox only when the selected scope contains database steps.

```yaml
database_steps:
  - phase: setup
    reason: missing_prerequisite_api
    engine: mysql
    evidence:
      - src/main/java/example/ThingRepository.java:42
    id: thing-ready
    data_source: primary
    estimated_records: 1
    idempotent: true
    ownership:
      namespace_env: DEV_AI_DATA_NAMESPACE
      resource: thing
      selector: id = DEV_AI_DATA_NAMESPACE
    precheck: |
      const mysql = require("mysql2/promise");
      const connection = await mysql.createConnection({
        host: bru.getEnvVar("MYSQL_HOST"),
        port: Number(bru.getEnvVar("MYSQL_PORT")),
        user: bru.getEnvVar("MYSQL_USER"),
        password: bru.getEnvVar("MYSQL_PASSWORD"),
        database: bru.getEnvVar("MYSQL_DATABASE")
      });
      const [rows] = await connection.execute(
        "SELECT id FROM thing WHERE id = ?",
        [bru.getEnvVar("DEV_AI_DATA_NAMESPACE")]
      );
      await connection.end();
      bru.setVar("DEV_AI_STEP_EXISTS", rows.length ? "true" : "false");
    script: |
      const mysql = require("mysql2/promise");
      const connection = await mysql.createConnection({
        host: bru.getEnvVar("MYSQL_HOST"),
        port: Number(bru.getEnvVar("MYSQL_PORT")),
        user: bru.getEnvVar("MYSQL_USER"),
        password: bru.getEnvVar("MYSQL_PASSWORD"),
        database: bru.getEnvVar("MYSQL_DATABASE")
      });
      const thingId = bru.getEnvVar("DEV_AI_DATA_NAMESPACE");
      await connection.execute(
        "INSERT IGNORE INTO thing(id, status) VALUES (?, ?)",
        [thingId, "READY"]
      );
      await connection.end();
    setup_verification: |
      const mysql = require("mysql2/promise");
      const connection = await mysql.createConnection({
        host: bru.getEnvVar("MYSQL_HOST"),
        port: Number(bru.getEnvVar("MYSQL_PORT")),
        user: bru.getEnvVar("MYSQL_USER"),
        password: bru.getEnvVar("MYSQL_PASSWORD"),
        database: bru.getEnvVar("MYSQL_DATABASE")
      });
      const [rows] = await connection.execute(
        "SELECT id FROM thing WHERE id = ?",
        [bru.getEnvVar("DEV_AI_DATA_NAMESPACE")]
      );
      await connection.end();
      if (rows.length !== 1) throw new Error("owned fixture is missing");
    cleanup: |
      const mysql = require("mysql2/promise");
      const connection = await mysql.createConnection({
        host: bru.getEnvVar("MYSQL_HOST"),
        port: Number(bru.getEnvVar("MYSQL_PORT")),
        user: bru.getEnvVar("MYSQL_USER"),
        password: bru.getEnvVar("MYSQL_PASSWORD"),
        database: bru.getEnvVar("MYSQL_DATABASE")
      });
      await connection.execute("DELETE FROM thing WHERE id = ?", [bru.getEnvVar("DEV_AI_DATA_NAMESPACE")]);
      await connection.end();
    cleanup_verification: |
      const mysql = require("mysql2/promise");
      const connection = await mysql.createConnection({
        host: bru.getEnvVar("MYSQL_HOST"),
        port: Number(bru.getEnvVar("MYSQL_PORT")),
        user: bru.getEnvVar("MYSQL_USER"),
        password: bru.getEnvVar("MYSQL_PASSWORD"),
        database: bru.getEnvVar("MYSQL_DATABASE")
      });
      const [rows] = await connection.execute(
        "SELECT id FROM thing WHERE id = ?",
        [bru.getEnvVar("DEV_AI_DATA_NAMESPACE")]
      );
      await connection.end();
      if (rows.length) throw new Error("owned fixture still exists after cleanup");

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

`evidence` identifies the schema, entity, repository, migration, or index mapping that proves the statement and expected value. An assertion step must declare its exact `expected` result. A setup step must declare its data source, record estimate, idempotence, read-only ownership precheck, run-owned selector, setup verification, bounded cleanup, and cleanup absence verification. Setup and cleanup scripts use `DEV_AI_DATA_NAMESPACE`; they are executed once at run scope rather than once per Bruno case. A true precheck reuses the row without running setup and never schedules that row for cleanup.

## Other Engines

- MongoDB uses the official `mongodb` client, an exact document selector, and `$setOnInsert` with `upsert`; `$set` is rejected because it can modify existing data.
- Elasticsearch and OpenSearch use their official clients with exact index/document IDs and create semantics; overwrite-style `index` calls are rejected.
- Redis uses the official client with a run-owned exact key and `NX` creation semantics.
- MySQL/MariaDB, PostgreSQL, Oracle, and SQL Server use parameterized, insert-if-absent statements and exact-key deletes.

Declare required Node clients in `qa/bruno/package.json` and pin their versions. Do not add a generic database abstraction for a one-off step.

## Runtime And Safety

Connection addresses, database/index names, users, passwords, TLS settings, and fixture identifiers come only from the active Bruno environment or its process environment. Never commit resolved credentials or credential-bearing URLs.

Statements should use parameter binding or exact document IDs and be idempotent where possible.

Each possible creation is journaled before setup begins, then promoted to `created` only after setup verification. This allows a partially failed write to be cleaned without claiming that creation succeeded. Cleanup runs in reverse dependency order after the suite or later through `dev-ai api-test mock-data-clean`; interrupted runs remain recoverable by run ID. A connection, query, expectation, cleanup, or absence-verification error is a failure, not a manual pass.
