#!/usr/bin/env node

const { spawnSync } = require("node:child_process");
const { install, resolveDltk } = require("../scripts/install");

if (process.argv[2] === "install") {
  try {
    install();
    process.exit(0);
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}

let result;
try {
  result = spawnSync(resolveDltk(), process.argv.slice(2), { stdio: "inherit" });
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
if (result.error) {
  console.error(`dltk is not available: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
