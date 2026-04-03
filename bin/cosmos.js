#!/usr/bin/env node
/**
 * COSMOS CLI — Orbit-powered orchestration for Shiprocket ICRM AI Brain
 *
 * Usage:
 *   node bin/cosmos.js <command> [options]
 *   npm run cosmos:<command>
 *
 * Commands map to /cosmos:* slash commands which delegate to Orbit's
 * workflow engine, running inside COSMOS's wave execution pipeline.
 */

"use strict";

const { execSync, spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");

const ROOT = path.resolve(__dirname, "..");
const CONFIG_PATH = path.join(ROOT, "cosmos.config.json");
const STATE_PATH = path.join(ROOT, ".cosmos", "state", "STATE.md");

// ─── Load config ──────────────────────────────────────────────────────────────
let config = {};
try {
  config = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
} catch (e) {
  // Config not found — continue with defaults
}

const COSMOS_PORT = (config.cosmos && config.cosmos.port) || 10001;
const API_BASE = `http://localhost:${COSMOS_PORT}${(config.cosmos && config.cosmos.api_prefix) || "/cosmos/api/v1"}`;
const CMD_ENDPOINT = `${API_BASE}/cmd/execute`;

// ─── Colours ──────────────────────────────────────────────────────────────────
const C = {
  bold:   (s) => `\x1b[1m${s}\x1b[0m`,
  green:  (s) => `\x1b[32m${s}\x1b[0m`,
  blue:   (s) => `\x1b[34m${s}\x1b[0m`,
  yellow: (s) => `\x1b[33m${s}\x1b[0m`,
  red:    (s) => `\x1b[31m${s}\x1b[0m`,
  cyan:   (s) => `\x1b[36m${s}\x1b[0m`,
  dim:    (s) => `\x1b[2m${s}\x1b[0m`,
};

// ─── Banner ───────────────────────────────────────────────────────────────────
function banner(cmd) {
  console.log(C.bold("\n╔══════════════════════════════════════════╗"));
  console.log(C.bold(`║   COSMOS  ·  RocketMind  ·  v1.0.0       ║`));
  console.log(C.bold(`║   Command: /cosmos:${(cmd || "").padEnd(22)}║`));
  console.log(C.bold("╚══════════════════════════════════════════╝\n"));
}

// ─── POST to COSMOS API ───────────────────────────────────────────────────────
function postCmd(command, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ command, args: args || {}, ...opts });
    const url = new URL(CMD_ENDPOINT);
    const req = http.request(
      {
        hostname: url.hostname,
        port: url.port,
        path: url.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          try { resolve(JSON.parse(data)); }
          catch (e) { resolve({ raw: data }); }
        });
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// ─── GET from COSMOS API ──────────────────────────────────────────────────────
function getApi(path) {
  return new Promise((resolve, reject) => {
    http.get(`http://localhost:${COSMOS_PORT}${path}`, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { resolve({ raw: data }); }
      });
    }).on("error", reject);
  });
}

// ─── Read STATE.md ────────────────────────────────────────────────────────────
function readState() {
  if (fs.existsSync(STATE_PATH)) return fs.readFileSync(STATE_PATH, "utf8");
  // Try COSMOS API
  return null;
}

// ─── Write STATE.md ───────────────────────────────────────────────────────────
function writeState(content) {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
  fs.writeFileSync(STATE_PATH, content, "utf8");
}

// ─── Ensure STATE.md exists ───────────────────────────────────────────────────
function ensureState() {
  if (!fs.existsSync(STATE_PATH)) {
    const template = `# COSMOS — Project State
> Managed by /cosmos:* commands. Updated automatically by COSMOS orchestration.

## Active Project
_No project started. Run \`npm run cosmos:new\` to begin._

## Current Phase
_None_

## Active Wave
_None_

## Last 5 Completed Tasks
_None yet._

## Decisions Log
| Date | Command | Decision | Rationale |
|------|---------|----------|-----------|

## Blockers
_None._

## Clarification Requests
_None._

## Agent Sessions
| Agent | Status | Wave | Output |
|-------|--------|------|--------|
`;
    writeState(template);
    console.log(C.green("  ✓ STATE.md created at .cosmos/state/STATE.md"));
  }
}

// ─── Run RocketMind command ────────────────────────────────────────────────────
function runCosmosCmd(cmd, extraArgs) {
  // Map /cosmos:X → COSMOS API endpoint
  const mapped = (config.commands && config.commands.map && config.commands.map[`/cosmos:${cmd}`]) || `POST /cosmos/api/v1/cmd/execute`;
  const isHttpCmd = mapped.startsWith("GET ") || mapped.startsWith("POST ");

  if (isHttpCmd) {
    const [method, apiPath] = mapped.split(" ");
    return method === "GET"
      ? getApi(apiPath).then((r) => { console.log(JSON.stringify(r, null, 2)); })
      : postCmd(cmd, extraArgs).then((r) => { console.log(JSON.stringify(r, null, 2)); });
  }

  console.log(C.dim(`  Delegating to: ${mapped}`));
  console.log(C.yellow(`  Note: Open Claude Code and run: ${mapped}\n`));
  console.log(C.dim(`  Or use: npm run cosmos:${cmd} -- [args]\n`));
  

  // Attempt to call COSMOS API if it's running
  return postCmd(cmd, extraArgs).catch(() => {
    console.log(C.yellow(`  COSMOS not running on :${COSMOS_PORT}. Start with: npm start`));
  });
}

// ─── Subcommands ──────────────────────────────────────────────────────────────

const commands = {
  // ── cosmos cmd <name> ──────────────────────────────────────────────────────
  cmd: async (args) => {
    const [name, ...rest] = args;
    if (!name) { commands.help([]); return; }
    banner(name);
    ensureState();

    if (name === "state") {
      const state = readState();
      if (state) { console.log(state); return; }
      try {
        const r = await getApi("/cosmos/api/v1/cmd/state");
        console.log(JSON.stringify(r, null, 2));
      } catch { console.log(C.yellow("  COSMOS not running. STATE.md not found.")); }
      return;
    }

    if (name === "help") { commands.help([]); return; }
    if (name === "agents") { commands.agents([]); return; }
    if (name === "skills") { commands.skills([]); return; }

    await runCosmosCmd(name, rest);
  },

  // ── cosmos build ──────────────────────────────────────────────────────────
  build: async (args) => {
    banner("build");
    console.log(C.cyan("  Building COSMOS knowledge base and agent registry...\n"));

    const target = args.find(a => a.startsWith("--target="))?.split("=")[1] || "all";
    console.log(C.dim(`  Target: ${target}`));

    try {
      execSync(`python scripts/rocketmind_sync.py --target ${target}`, {
        cwd: ROOT, stdio: "inherit",
      });
      console.log(C.green("\n  ✓ COSMOS build complete"));
    } catch (e) {
      console.error(C.red("  ✗ Build failed. Run: python scripts/rocketmind_sync.py"));
    }
  },

  // ── cosmos train ──────────────────────────────────────────────────────────
  train: async (args) => {
    banner("train");
    const source = args.find(a => a.startsWith("--source="))?.split("=")[1] || "kb";
    console.log(C.cyan(`  Triggering COSMOS training pipeline (source: ${source})...\n`));

    try {
      const r = await postCmd("train", { source });
      console.log(C.green("  ✓ Training triggered"));
      console.log(JSON.stringify(r, null, 2));
    } catch {
      console.log(C.yellow(`  POST to COSMOS API failed. Run directly:\n  curl -X POST http://localhost:${COSMOS_PORT}/cosmos/api/v1/pipeline/run`));
    }
  },

  // ── cosmos eval ───────────────────────────────────────────────────────────
  eval: async () => {
    banner("eval");
    console.log(C.cyan("  Running COSMOS eval benchmark (201 ICRM seeds)...\n"));
    try {
      execSync("python scripts/run_eval.py", { cwd: ROOT, stdio: "inherit" });
    } catch {
      console.log(C.yellow("  scripts/run_eval.py not found. Use: POST /cosmos/api/v1/cmd/eval"));
    }
  },

  // ── cosmos agents ─────────────────────────────────────────────────────────
  agents: async () => {
    console.log(C.bold("\n  COSMOS Agent Registry\n"));
    try {
      const r = await getApi("/cosmos/api/v1/cmd/agents");
      if (r.agents) {
        r.agents.forEach(a => {
          console.log(`  ${C.green("•")} ${C.bold(a.name.padEnd(22))} ${C.dim(a.domains?.join(", ") || "")}`);
          if (a.triggers) console.log(C.dim(`    triggers: ${a.triggers.slice(0, 3).join(", ")}`));
        });
      } else console.log(JSON.stringify(r, null, 2));
    } catch {
      // Print from registry file
      const registry = path.join(ROOT, "rocketmind.registry.json");
      if (fs.existsSync(registry)) {
        const reg = JSON.parse(fs.readFileSync(registry, "utf8"));
        reg.agents.forEach(a => {
          console.log(`  ${C.green("•")} ${C.bold(a.name.padEnd(22))} ${C.dim(a.domains?.join(", ") || "")}`);
        });
      } else console.log(C.yellow("  Start COSMOS or run: npm run sync"));
    }
  },

  // ── cosmos skills ─────────────────────────────────────────────────────────
  skills: async () => {
    console.log(C.bold("\n  COSMOS Skills Library\n"));
    const skillsDir = path.join(ROOT, ".claude", "skills");
    if (fs.existsSync(skillsDir)) {
      fs.readdirSync(skillsDir).filter(f => f.endsWith(".md")).forEach(f => {
        const name = f.replace(".md", "");
        console.log(`  ${C.cyan("◆")} ${name}`);
      });
    } else {
      try {
        const r = await getApi("/cosmos/api/v1/cmd/skills");
        console.log(JSON.stringify(r, null, 2));
      } catch { console.log(C.yellow("  Start COSMOS or run: npm run sync")); }
    }
  },

  // ── cosmos generate ───────────────────────────────────────────────────────
  generate: async () => {
    banner("generate");
    console.log(C.cyan("  Generating COSMOS command surface from Orbit registry...\n"));

    const registry = path.join(ROOT, "rocketmind.registry.json");
    if (!fs.existsSync(registry)) {
      console.error(C.red("  rocketmind.registry.json not found at project root"));
      process.exit(1);
    }
    const reg = JSON.parse(fs.readFileSync(registry, "utf8"));

    // Generate .claude/commands/cosmos.md
    const commandsDir = path.join(ROOT, ".claude", "commands");
    fs.mkdirSync(commandsDir, { recursive: true });

    let md = `# COSMOS Commands\n\n`;
    md += `> Orbit-powered slash commands for COSMOS. All commands route through COSMOS wave execution.\n\n`;
    md += `## Available Commands\n\n`;

    reg.workflows.forEach(w => {
      const cosmosCmd = `/cosmos:${w.name}`;
      
      md += `### \`${cosmosCmd}\`\n`;
      md += `> Command: \`${w.command}\` | Mode: \`${w.mode || "collaborative"}\`\n\n`;
      if (w.inputs?.length)  md += `**Inputs:** ${w.inputs.join(", ")}\n\n`;
      if (w.outputs?.length) md += `**Outputs:** ${w.outputs.join(", ")}\n\n`;
      if (w.agents?.length)  md += `**Agents:** ${w.agents.join(", ")}\n\n`;
    });

    fs.writeFileSync(path.join(commandsDir, "cosmos.md"), md);
    console.log(C.green("  ✓ .claude/commands/cosmos.md generated"));

    // Generate .cosmos/state/STATE.md template
    ensureState();
    console.log(C.green("  ✓ .cosmos/state/STATE.md ensured"));

    console.log(C.bold("\n  Done. COSMOS commands ready.\n"));
  },

  // ── cosmos help ───────────────────────────────────────────────────────────
  help: async () => {
    banner("help");
    console.log(C.bold("  COSMOS — Orbit-Powered AI Brain\n"));
    console.log(C.bold("  Lifecycle"));
    console.log(`  ${C.green("npm start")}              Start COSMOS FastAPI server (:10001)`);
    console.log(`  ${C.green("npm run build")}          Sync Orbit agents/skills → COSMOS KB + Neo4j`);
    console.log(`  ${C.green("npm run train")}          Run KB ingestion + embedding pipeline`);
    console.log(`  ${C.green("npm run eval")}           Run 201-seed eval benchmark`);
    console.log(`  ${C.green("npm test")}               Run pytest suite`);
    console.log();
    console.log(C.bold("  Workflow Commands (Orbit-powered)"));
    console.log(`  ${C.cyan("npm run cosmos:new")}      Start a new project`);
    console.log(`  ${C.cyan("npm run cosmos:plan")}     Plan current phase`);
    console.log(`  ${C.cyan("npm run cosmos:build")}    Execute phase with wave architecture`);
    console.log(`  ${C.cyan("npm run cosmos:verify")}   Test + review phase`);
    console.log(`  ${C.cyan("npm run cosmos:ship")}     PR + deploy + release`);
    console.log(`  ${C.cyan("npm run cosmos:next")}     Auto-detect next step`);
    console.log(`  ${C.cyan("npm run cosmos:quick")}    Ad-hoc task`);
    console.log(`  ${C.cyan("npm run cosmos:riper")}    Research→Innovate→Plan→Execute→Review`);
    console.log(`  ${C.cyan("npm run cosmos:forge")}    Build a new specialized agent`);
    console.log(`  ${C.cyan("npm run cosmos:review")}   Code + architecture review`);
    console.log(`  ${C.cyan("npm run cosmos:audit")}    Security audit`);
    console.log(`  ${C.cyan("npm run cosmos:debug")}    Root-cause debugging`);
    console.log(`  ${C.cyan("npm run cosmos:state")}    Show STATE.md`);
    console.log(`  ${C.cyan("npm run cosmos:resume")}   Resume after compaction`);
    console.log(`  ${C.cyan("npm run cosmos:progress")} Project status`);
    console.log();
    console.log(C.bold("  Introspection"));
    console.log(`  ${C.dim("npm run agents")}         List all registered agents`);
    console.log(`  ${C.dim("npm run skills")}         List all available skills`);
    console.log(`  ${C.dim("npm run health")}         COSMOS health check`);
    console.log(`  ${C.dim("npm run state")}          Current workflow state`);
    console.log();
    console.log(C.bold("  In Claude Code, use: /cosmos:<command>"));
    console.log(C.dim("  All /cosmos:* commands run inside COSMOS (RocketMind — self-contained).\n"));
  },
};

// ─── Entry point ──────────────────────────────────────────────────────────────
const [, , subcommand, ...restArgs] = process.argv;

if (!subcommand || subcommand === "help") {
  commands.help([]);
} else if (commands[subcommand]) {
  commands[subcommand](restArgs).catch(e => {
    console.error(C.red(`  Error: ${e.message}`));
    process.exit(1);
  });
} else {
  // Unknown subcommand — treat as cosmos cmd <subcommand>
  commands.cmd([subcommand, ...restArgs]).catch(e => {
    console.error(C.red(`  Error: ${e.message}`));
    process.exit(1);
  });
}
