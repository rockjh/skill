#!/usr/bin/env node

const { spawnSync } = require("node:child_process");
const { resolveDevAI } = require("../runtime");

let result;
try {
  result = spawnSync(resolveDevAI(), process.argv.slice(2), { stdio: "inherit" });
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
if (result.error) {
  console.error(`dev-ai is not available: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
