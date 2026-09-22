#!/usr/bin/env node

const { cpSync, existsSync, mkdirSync, readdirSync } = require("node:fs");
const { homedir } = require("node:os");
const { join } = require("node:path");
const { spawnSync } = require("node:child_process");

const root = join(__dirname, "..");

function pipxCommand() {
  return process.platform === "win32" ? "pipx.exe" : "pipx";
}

function resolveDltk() {
  const configured = process.env.DLTK_EXECUTABLE;
  if (configured) return configured;
  let binDir = process.env.PIPX_BIN_DIR;
  if (!binDir) {
    const result = spawnSync(pipxCommand(), ["environment", "--value", "PIPX_BIN_DIR"], { encoding: "utf8" });
    if (!result.error && result.status === 0) binDir = result.stdout.trim();
  }
  binDir ||= join(homedir(), ".local", "bin");
  const executable = join(binDir, process.platform === "win32" ? "dltk.exe" : "dltk");
  if (!existsSync(executable)) throw new Error(`pipx-installed dltk is missing: ${executable}`);
  return executable;
}

function install() {
  const dist = join(root, "dist");
  const wheel = existsSync(dist) ? readdirSync(dist).find((name) => name.endsWith(".whl")) : undefined;
  if (!wheel) throw new Error("dltk wheel is missing from npm/dist; run python scripts/release.py first");

  const args = ["install", "--force"];
  if (process.env.DLTK_PYTHON) args.push("--python", process.env.DLTK_PYTHON);
  args.push(join(dist, wheel));
  const pipIndex = process.env.DLTK_PIP_INDEX_URL || process.env.PIP_INDEX_URL;
  if (pipIndex) args.push("--pip-args", `--index-url ${pipIndex}`);
  const extraIndex = process.env.DLTK_EXTRA_INDEX_URL || process.env.PIP_EXTRA_INDEX_URL;
  if (extraIndex) args.push("--pip-args", `--extra-index-url ${extraIndex}`);
  const result = spawnSync(pipxCommand(), args, { stdio: "inherit" });
  if (result.error || result.status !== 0) throw result.error || new Error(`pipx install failed with exit code ${result.status}`);

  const skillHome = process.env.DLTK_SKILL_HOME || join(homedir(), ".agents", "skills");
  const destination = join(skillHome, "get-my-dev-lifecycle-toolkit");
  mkdirSync(destination, { recursive: true });
  cpSync(join(root, "skills", "get-my-dev-lifecycle-toolkit"), destination, { recursive: true, force: true });

  const doctor = spawnSync(resolveDltk(), ["doctor", "--json"], { stdio: "inherit" });
  if (doctor.error || doctor.status !== 0) throw doctor.error || new Error(`dltk doctor failed with exit code ${doctor.status}`);
}

if (require.main === module) {
  try {
    install();
  } catch (error) {
    console.error(error.message);
    console.error("Run `npx seres-dltk install` after fixing Python, pipx, or the configured repository.");
    process.exit(1);
  }
}

module.exports = { install, pipxCommand, resolveDltk };
