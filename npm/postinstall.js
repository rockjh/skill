const { cpSync, existsSync, mkdirSync, readdirSync } = require("node:fs");
const { homedir } = require("node:os");
const { join } = require("node:path");
const { spawnSync } = require("node:child_process");
const { pipxCommand, resolveDevAI } = require("./runtime");

const root = __dirname;
const vendor = join(root, "vendor");
const wheel = existsSync(vendor)
  ? readdirSync(vendor).find((name) => name.endsWith(".whl"))
  : undefined;
if (!wheel) {
  throw new Error("dev-ai wheel is missing from npm/vendor; run scripts/release.py before publishing");
}

const install = spawnSync(pipxCommand(), ["install", "--force", join(vendor, wheel)], { stdio: "inherit" });
if (install.error || install.status !== 0) {
  throw install.error ?? new Error(`pipx install failed with exit code ${install.status}`);
}

const skillHome = process.env.CODEX_HOME || join(homedir(), ".codex");
const destination = join(skillHome, "skills");
mkdirSync(destination, { recursive: true });
for (const name of readdirSync(join(root, "skills"))) {
  cpSync(join(root, "skills", name), join(destination, name), { recursive: true, force: true });
}

const doctor = spawnSync(resolveDevAI(), ["doctor", "--json"], { stdio: "inherit" });
if (doctor.error || doctor.status !== 0) {
  throw doctor.error ?? new Error(`dev-ai doctor failed with exit code ${doctor.status}`);
}
