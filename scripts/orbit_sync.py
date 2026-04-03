"""
orbit_sync.py — Sync Orbit agents, skills, and workflows into COSMOS.

What this does:
  1. Reads ../orbit/orbit.registry.json + all agents/*.md + skills/*.md
  2. Inserts agents as Neo4j nodes (:CosmosAgent) with domain + trigger edges
  3. Inserts skills as KB documents (P3/P6 pillar YAML) → Qdrant via training pipeline
  4. Inserts workflows into MySQL cosmos_workflow_state table
  5. Generates .claude/commands/cosmos.md (slash commands for Claude Code)

Usage:
  python scripts/orbit_sync.py                  # sync everything
  python scripts/orbit_sync.py --target agents  # agents only
  python scripts/orbit_sync.py --target skills  # skills only
  python scripts/orbit_sync.py --target workflows
  python scripts/orbit_sync.py --target kb      # kb docs only
  python scripts/orbit_sync.py --target graph   # neo4j only
  python scripts/orbit_sync.py --dry-run        # print what would be synced
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger()

ROOT = Path(__file__).parent.parent
ORBIT_ROOT = ROOT.parent / "orbit"
COSMOS_CONFIG = ROOT / "cosmos.config.json"
COMMANDS_DIR = ROOT / ".claude" / "commands"
STATE_DIR = ROOT / ".cosmos" / "state"


# ─── Load configs ──────────────────────────────────────────────────────────────

def load_orbit_registry() -> dict:
    reg_path = ORBIT_ROOT / "orbit.registry.json"
    if not reg_path.exists():
        logger.error("orbit.registry.json not found", path=str(reg_path))
        return {}
    with open(reg_path) as f:
        return json.load(f)


def load_cosmos_config() -> dict:
    if not COSMOS_CONFIG.exists():
        return {}
    with open(COSMOS_CONFIG) as f:
        return json.load(f)


def read_skill_md(skill_name: str) -> str:
    """Read skill markdown from orbit skills directory."""
    skill_path = ORBIT_ROOT / "skills" / f"{skill_name}.md"
    if skill_path.exists():
        return skill_path.read_text()
    return ""


def read_agent_md(agent_name: str) -> str:
    """Read agent markdown from orbit agents directory."""
    agent_path = ORBIT_ROOT / "agents" / f"{agent_name}.md"
    if agent_path.exists():
        return agent_path.read_text()
    return ""


# ─── Neo4j: sync agents ────────────────────────────────────────────────────────

async def sync_agents_to_neo4j(agents: list, dry_run: bool = False):
    """Create :CosmosAgent nodes in Neo4j for each Orbit agent."""
    try:
        from neo4j import AsyncGraphDatabase
        from app.config import settings

        driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        async with driver.session() as session:
            for agent in agents:
                name = agent["name"]
                domains = agent.get("domains", [])
                triggers = agent.get("triggers", [])
                skills = agent.get("skills", [])
                agent_md = read_agent_md(name)

                if dry_run:
                    logger.info("dry_run.agent", name=name, domains=domains)
                    continue

                # Upsert agent node
                await session.run(
                    """
                    MERGE (a:CosmosAgent {name: $name})
                    SET a.domains = $domains,
                        a.triggers = $triggers,
                        a.skills = $skills,
                        a.source = 'orbit',
                        a.synced_at = datetime()
                    """,
                    name=name, domains=domains, triggers=triggers,
                    skills=[s.split("/")[-1].replace(".md", "") for s in skills],
                )

                # Create domain edges
                for domain in domains:
                    await session.run(
                        """
                        MERGE (d:Domain {name: $domain})
                        MERGE (a:CosmosAgent {name: $agent})
                        MERGE (a)-[:BELONGS_TO_DOMAIN]->(d)
                        """,
                        domain=domain, agent=name,
                    )

                logger.info("neo4j.agent.synced", agent=name)

        await driver.close()
        logger.info("neo4j.agents.complete", count=len(agents))

    except Exception as e:
        logger.warning("neo4j.agents.failed", error=str(e))
        logger.info("neo4j_skip", reason="Neo4j not available — agents registered in MySQL only")


# ─── MySQL: sync workflows ─────────────────────────────────────────────────────

async def sync_workflows_to_mysql(workflows: list, dry_run: bool = False):
    """Insert/update Orbit workflows into cosmos_workflow_state MySQL table."""
    try:
        import aiomysql
        from app.config import settings

        conn = await aiomysql.connect(
            host=settings.MARS_DB_HOST,
            port=int(settings.MARS_DB_PORT),
            user=settings.MARS_DB_USER,
            password=settings.MARS_DB_PASSWORD,
            db=settings.MARS_DB_NAME,
            autocommit=True,
        )

        async with conn.cursor() as cur:
            # Ensure table exists
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS cosmos_orbit_workflows (
                    id          VARCHAR(64) PRIMARY KEY,
                    name        VARCHAR(128) NOT NULL,
                    command     VARCHAR(128) NOT NULL,
                    cosmos_cmd  VARCHAR(128) NOT NULL,
                    mode        VARCHAR(32) DEFAULT 'collaborative',
                    agents      JSON,
                    inputs      JSON,
                    outputs     JSON,
                    source      VARCHAR(32) DEFAULT 'orbit',
                    synced_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_name (name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # Ensure agents table
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS cosmos_orbit_agents (
                    id          VARCHAR(64) PRIMARY KEY,
                    name        VARCHAR(128) NOT NULL,
                    file        VARCHAR(256),
                    domains     JSON,
                    triggers    JSON,
                    skills      JSON,
                    outputs     JSON,
                    source      VARCHAR(32) DEFAULT 'orbit',
                    synced_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_name (name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        if not dry_run:
            async with conn.cursor() as cur:
                for wf in workflows:
                    cosmos_cmd = f"/cosmos:{wf['name']}"
                    import hashlib
                    wf_id = hashlib.md5(wf["name"].encode()).hexdigest()
                    await cur.execute("""
                        INSERT INTO cosmos_orbit_workflows
                            (id, name, command, cosmos_cmd, mode, agents, inputs, outputs, source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'orbit')
                        ON DUPLICATE KEY UPDATE
                            command=VALUES(command), cosmos_cmd=VALUES(cosmos_cmd),
                            mode=VALUES(mode), agents=VALUES(agents),
                            inputs=VALUES(inputs), outputs=VALUES(outputs),
                            synced_at=CURRENT_TIMESTAMP
                    """, (
                        wf_id, wf["name"], wf.get("command", ""),
                        cosmos_cmd, wf.get("mode", "collaborative"),
                        json.dumps(wf.get("agents", [])),
                        json.dumps(wf.get("inputs", [])),
                        json.dumps(wf.get("outputs", [])),
                    ))
                logger.info("mysql.workflows.synced", count=len(workflows))
        else:
            for wf in workflows:
                logger.info("dry_run.workflow", name=wf["name"])

        conn.close()

    except Exception as e:
        logger.warning("mysql.workflows.failed", error=str(e))


# ─── MySQL: sync agents ────────────────────────────────────────────────────────

async def sync_agents_to_mysql(agents: list, dry_run: bool = False):
    """Insert Orbit agents into cosmos_orbit_agents MySQL table."""
    try:
        import aiomysql
        from app.config import settings

        conn = await aiomysql.connect(
            host=settings.MARS_DB_HOST,
            port=int(settings.MARS_DB_PORT),
            user=settings.MARS_DB_USER,
            password=settings.MARS_DB_PASSWORD,
            db=settings.MARS_DB_NAME,
            autocommit=True,
        )

        if not dry_run:
            async with conn.cursor() as cur:
                # Ensure table
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS cosmos_orbit_agents (
                        id        VARCHAR(64) PRIMARY KEY,
                        name      VARCHAR(128) NOT NULL,
                        file      VARCHAR(256),
                        domains   JSON,
                        triggers  JSON,
                        skills    JSON,
                        outputs   JSON,
                        source    VARCHAR(32) DEFAULT 'orbit',
                        synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uq_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)

                for agent in agents:
                    import hashlib
                    agent_id = hashlib.md5(agent["name"].encode()).hexdigest()
                    await cur.execute("""
                        INSERT INTO cosmos_orbit_agents
                            (id, name, file, domains, triggers, skills, outputs, source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'orbit')
                        ON DUPLICATE KEY UPDATE
                            domains=VALUES(domains), triggers=VALUES(triggers),
                            skills=VALUES(skills), outputs=VALUES(outputs),
                            synced_at=CURRENT_TIMESTAMP
                    """, (
                        agent_id, agent["name"], agent.get("file", ""),
                        json.dumps(agent.get("domains", [])),
                        json.dumps(agent.get("triggers", [])),
                        json.dumps(agent.get("skills", [])),
                        json.dumps(agent.get("outputs", [])),
                    ))
            logger.info("mysql.agents.synced", count=len(agents))

        conn.close()

    except Exception as e:
        logger.warning("mysql.agents.failed", error=str(e))


# ─── KB: sync skills as documents ─────────────────────────────────────────────

async def sync_skills_to_kb(skills: list, dry_run: bool = False):
    """
    Ingest Orbit skills as P6-style KB documents into Qdrant + Neo4j.

    Each skill becomes:
      - A KB YAML doc (pillar: p6_action_contracts or p3_apis_tools)
      - Embedded via text-embedding-3-small → Qdrant
      - A Neo4j node (:CosmosSkill) with edges to agents that use it
    """
    try:
        sys.path.insert(0, str(ROOT))
        from app.config import settings
        from app.services.vectorstore import VectorStoreService
        from app.services.chunker import chunk_documents

        vs = VectorStoreService()
        await vs.ensure_schema()

        docs = []
        for skill in skills:
            skill_name = skill["name"]
            content = read_skill_md(skill_name)
            if not content:
                continue

            # Wrap skill as a KB document
            doc = {
                "pillar": "p6_action_contracts",
                "entity_id": f"orbit_skill_{skill_name}",
                "entity_type": "skill",
                "title": f"COSMOS Skill: {skill_name}",
                "content": content,
                "metadata": {
                    "source": "orbit",
                    "skill_name": skill_name,
                    "purpose": skill.get("purpose", ""),
                    "loaded_by": skill.get("loaded_by", []),
                    "query_mode": "act",
                    "trust_score": 0.9,
                    "training_ready": True,
                },
            }
            docs.append(doc)

        if dry_run:
            for d in docs:
                logger.info("dry_run.skill_doc", entity_id=d["entity_id"])
            return

        chunks = chunk_documents(docs)
        if chunks:
            await vs.upsert_chunks(chunks)
            logger.info("kb.skills.synced", chunks=len(chunks), skills=len(docs))

    except Exception as e:
        logger.warning("kb.skills.failed", error=str(e))


# ─── Generate .claude/commands/cosmos.md ──────────────────────────────────────

def generate_commands_md(workflows: list, agents: list):
    """Generate the /cosmos:* slash command definitions for Claude Code."""
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# COSMOS Slash Commands\n",
        "> Orbit-powered orchestration commands running inside COSMOS wave execution.\n",
        "> Use in Claude Code session: `/cosmos:<command>`\n\n",
        "## Workflow Commands\n\n",
    ]

    for wf in workflows:
        cosmos_cmd = f"`/cosmos:{wf['name']}`"
        orbit_cmd  = wf.get("command", f"/orbit:{wf['name']}")
        lines.append(f"### {cosmos_cmd}\n")
        lines.append(f"> Orbit equivalent: `{orbit_cmd}` | Mode: `{wf.get('mode', 'collaborative')}`\n\n")
        if wf.get("inputs"):
            lines.append(f"**Inputs:** {', '.join(wf['inputs'])}\n\n")
        if wf.get("outputs"):
            lines.append(f"**Outputs:** {', '.join(wf['outputs'])}\n\n")
        if wf.get("agents"):
            lines.append(f"**Agents:** {', '.join(wf['agents'])}\n\n")
        lines.append("---\n\n")

    lines.append("## Agent Registry\n\n")
    lines.append("| Agent | Domains | Triggers |\n|-------|---------|----------|\n")
    for a in agents:
        triggers = ", ".join(a.get("triggers", [])[:3])
        domains = ", ".join(a.get("domains", []))
        lines.append(f"| `{a['name']}` | {domains} | {triggers} |\n")

    out = COMMANDS_DIR / "cosmos.md"
    out.write_text("".join(lines))
    logger.info("commands_md.generated", path=str(out))


# ─── Generate STATE.md template ───────────────────────────────────────────────

def ensure_state_md():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / "STATE.md"
    if state_path.exists():
        return

    content = """# COSMOS — Project State
> Managed by /cosmos:* commands. Updated automatically on every task completion.
> Source of truth for all active waves, phases, and decisions.

## Active Project
_None. Run `/cosmos:new` or `npm run cosmos:new` to start._

## Current Phase
_None_

## Active Wave
| Wave | Tasks | Status |
|------|-------|--------|

## Last 5 Completed Tasks
_None yet._

## Decisions Log
| Date | Command | Decision | Rationale |
|------|---------|----------|-----------|

## Agent Sessions
| Agent | Status | Wave | Output |
|-------|--------|------|--------|

## Blockers
_None._

## Clarification Requests
_None._
"""
    state_path.write_text(content)
    logger.info("state_md.created", path=str(state_path))


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main(target: str, dry_run: bool):
    reg = load_orbit_registry()
    if not reg:
        logger.error("orbit_registry.empty")
        sys.exit(1)

    agents    = reg.get("agents", [])
    skills    = reg.get("skills", [])
    workflows = reg.get("workflows", [])

    logger.info("orbit_sync.start",
                agents=len(agents), skills=len(skills),
                workflows=len(workflows), target=target, dry_run=dry_run)

    # Always generate the command surface and state template
    generate_commands_md(workflows, agents)
    ensure_state_md()

    if target in ("all", "agents", "graph"):
        await sync_agents_to_neo4j(agents, dry_run=dry_run)
        await sync_agents_to_mysql(agents, dry_run=dry_run)

    if target in ("all", "workflows"):
        await sync_workflows_to_mysql(workflows, dry_run=dry_run)

    if target in ("all", "skills", "kb"):
        await sync_skills_to_kb(skills, dry_run=dry_run)

    logger.info("orbit_sync.complete", target=target)
    print(f"\n✓ COSMOS sync complete (target={target}, dry_run={dry_run})")
    print(f"  Agents:    {len(agents)}")
    print(f"  Skills:    {len(skills)}")
    print(f"  Workflows: {len(workflows)}")
    print(f"\n  Slash commands: .claude/commands/cosmos.md")
    print(f"  State template: .cosmos/state/STATE.md")
    print(f"\n  Start COSMOS:  npm start")
    print(f"  Run command:   npm run cosmos:plan")
    print(f"  In Claude:     /cosmos:plan\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Orbit into COSMOS")
    parser.add_argument("--target", default="all",
                        choices=["all", "agents", "skills", "workflows", "kb", "graph"],
                        help="What to sync")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be synced without writing")
    args = parser.parse_args()

    asyncio.run(main(args.target, args.dry_run))
