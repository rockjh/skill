const { existsSync } = require("node:fs");
const { homedir } = require("node:os");
const { join } = require("node:path");
const { spawnSync } = require("node:child_process");

function pipxCommand() {
  return process.platform === "win32" ? "pipx.exe" : "pipx";
}

function resolveDevAI() {
  const configured = process.env.SERES_DEV_AI_EXECUTABLE;
  if (configured) return configured;

  let binDir = process.env.PIPX_BIN_DIR;
  if (!binDir) {
    const result = spawnSync(pipxCommand(), ["environment", "--value", "PIPX_BIN_DIR"], {
      encoding: "utf8",
    });
    if (!result.error && result.status === 0) binDir = result.stdout.trim();
  }
  binDir ||= join(homedir(), ".local", "bin");
  const executable = join(binDir, process.platform === "win32" ? "dev-ai.exe" : "dev-ai");
  if (!existsSync(executable)) {
    throw new Error(`pipx-installed dev-ai is missing: ${executable}`);
  }
  return executable;
}

module.exports = { pipxCommand, resolveDevAI };
