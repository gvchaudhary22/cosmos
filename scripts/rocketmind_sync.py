"""
rocketmind_sync.py — Sync RocketMind agents, skills, and workflows into COSMOS.

What this does:
  1. Reads rocketmind.registry.json (self-contained — no external repo needed)
  2. Inserts agents as Neo4j nodes (:CosmosAgent) with domain + trigger edges
  3. Inserts skills as KB documents (P6 pillar) → Qdrant via training pipeline
  4. Inserts workflows into MySQL rocketmind_workflows table
  5. Generates .claude/commands/cosmos.md (slash commands for Claude Code)

Usage:
  python scripts/rocketmind_sync.py                  # sync everything
  python scripts/rocketmind_sync.py --target agents  # agents only
  python scripts/rocketmind_sync.py --target skills  # skills only
  python scripts/rocketmind_sync.py --target workflows
  python scripts/rocketmind_sync.py --target kb      # kb docs only
  python scripts/rocketmind_sync.py --target graph   # neo4j only
  python scripts/rocketmind_sync.py --dry-run        # print what would be synced
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger()

ROOT = Path(__file__).parent.parent
REGISTRY_PATH = ROOT / "rocketmind.registry.json"
COSMOS_CONFIG  = ROOT / "cosmos.config.json"
COMMANDS_DIR   = ROOT / ".claude" / "commands"
STATE_DIR      = ROOT / ".cosmos" / "state"
SKILLS_DIR     = ROOT / ".claude" / "skills"
AGENTS_DIR     = ROOT / ".claude" / "agents"


# ─── Load registry ─────────────────────────────────────────────────────────────

def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        logger.error("rocketmind.registry.json not found", path=str(REGISTRY_PATH))
        return {}
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def read_skill_md(skill_name: str) -> str:
    """Read skill markdown from .claude/skills/."""
    p = SKILLS_DIR / f"{skill_name}.md"
    if p.exists():
        return p.read_text()
    return ""


def read_agent_md(agent_name: str) -> str:
    """Read agent markdown from .claude/agents/."""
    p = AGENTS_DIR / f"{agent_name}.md"
    if p.exists():
        return p.read_text()
    return ""


# ─── Neo4j: sync agents ────────────────────────────────────────────────────────

async def sync_agents_to_neo4j(agents: list, dry_run: bool = False):
    """Create :CosmosAgent nodes in Neo4j for each RocketMind agent."""
    try:
        from neo4j import AsyncGraphDatabase
        from app.config import settings

        driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        async with driver.session() as session:
            for agent in agents:
                name     = agent["name"]
                domains  = agent.get("domains", [])
                triggers = agent.get("triggers", [])
                skills   = agent.get("skills", [])

                if dry_run:
                    logger.info("dry_run.agent", name=name, domains=domains)
                    continue

                await session.run(
                    """
                    MERGE (a:CosmosAgent {name: $name})
                    SET a.domains = $domains,
                        a.triggers = $triggers,
                        a.skills = $skills,
                        a.source = 'rocketmind',
                        a.synced_at = datetime()
                    """,
                    name=name, domains=domains, triggers=triggers, skills=skills,
                )
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


# ─── MySQL: sync workflows ─────────────────────────────────────────────────────

async def sync_workflows_to_mysql(workflows: list, dry_run: bool = False):
    """Insert/update RocketMind workflows into rocketmind_workflows MySQL table."""
    try:
        import hashlib
        import aiomysql
        from app.config import settings

        conn = await aiomysql.connect(
            host=settings.MARS_DB_HOST, port=int(settings.MARS_DB_PORT),
            user=settings.MARS_DB_USER, password=settings.MARS_DB_PASSWORD,
            db=settings.MARS_DB_NAME, autocommit=True,
        )

        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS rocketmind_workflows (
                    id         VARCHAR(64)  PRIMARY KEY,
                    name       VARCHAR(128) NOT NULL,
                    command    VARCHAR(128) NOT NULL,
                    mode       VARCHAR(32)  DEFAULT 'collaborative',
                    agents     JSON,
                    inputs     JSON,
                    outputs    JSON,
                    source     VARCHAR(32)  DEFAULT 'rocketmind',
                    synced_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_name (name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        if not dry_run:
            async with conn.cursor() as cur:
                for wf in workflows:
                    wf_id = hashlib.md5(wf["name"].encode()).hexdigest()
                    await cur.execute("""
                        INSERT INTO rocketmind_workflows
                            (id, name, command, mode, agents, inputs, outputs, source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'rocketmind')
                        ON DUPLICATE KEY UPDATE
                            command=VALUES(command), mode=VALUES(mode),
                            agents=VALUES(agents), inputs=VALUES(inputs),
                            outputs=VALUES(outputs), synced_at=CURRENT_TIMESTAMP
                    """, (
                        wf_id, wf["name"], wf.get("command", f"/cosmos:{wf['name']}"),
                        wf.get("mode", "collaborative"),
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
    """Insert RocketMind agents into rocketmind_agents MySQL table."""
    try:
        import hashlib
        import aiomysql
        from app.config import settings

        conn = await aiomysql.connect(
            host=settings.MARS_DB_HOST, port=int(settings.MARS_DB_PORT),
            user=settings.MARS_DB_USER, password=settings.MARS_DB_PASSWORD,
            db=settings.MARS_DB_NAME, autocommit=True,
        )

        if not dry_run:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS rocketmind_agents (
                        id        VARCHAR(64)  PRIMARY KEY,
                        name      VARCHAR(128) NOT NULL,
                        domains   JSON,
                        triggers  JSON,
                        skills    JSON,
                        outputs   JSON,
                        source    VARCHAR(32)  DEFAULT 'rocketmind',
                        synced_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uq_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                for agent in agents:
                    agent_id = hashlib.md5(agent["name"].encode()).hexdigest()
                    await cur.execute("""
                        INSERT INTO rocketmind_agents
                            (id, name, domains, triggers, skills, outputs, source)
                        VALUES (%s, %s, %s, %s, %s, %s, 'rocketmind')
                        ON DUPLICATE KEY UPDATE
                            domains=VALUES(domains), triggers=VALUES(triggers),
                            skills=VALUES(skills), outputs=VALUES(outputs),
                            synced_at=CURRENT_TIMESTAMP
                    """, (
                        agent_id, agent["name"],
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
    """Ingest RocketMind skills as P6 KB documents into Qdrant + Neo4j."""
    try:
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
            docs.append({
                "pillar": "p6_action_contracts",
                "entity_id": f"rocketmind_skill_{skill_name}",
                "entity_type": "skill",
                "title": f"COSMOS Skill: {skill_name}",
                "content": content,
                "metadata": {
                    "source": "rocketmind",
                    "skill_name": skill_name,
                    "purpose": skill.get("purpose", ""),
                    "loaded_by": skill.get("loaded_by", []),
                    "query_mode": "act",
                    "trust_score": 0.9,
                    "training_ready": True,
                },
            })

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
        "> RocketMind orchestration commands — self-contained in COSMOS.\n",
        "> Use in Claude Code session: `/cosmos:<command>`\n\n",
        "## Workflow Commands\n\n",
    ]
    for wf in workflows:
        lines.append(f"### `/cosmos:{wf['name']}`\n")
        lines.append(f"> Mode: `{wf.get('mode', 'collaborative')}`\n\n")
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
        domains  = ", ".join(a.get("domains", []))
        lines.append(f"| `{a['name']}` | {domains} | {triggers} |\n")

    out = COMMANDS_DIR / "cosmos.md"
    out.write_text("".join(lines))
    logger.info("commands_md.generated", path=str(out))


def ensure_state_md():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / "STATE.md"
    if state_path.exists():
        return
    state_path.write_text("""# COSMOS — Project State
> Managed by /cosmos:* commands (RocketMind). Updated automatically.

## Active Project
_None. Run `/cosmos:new` to start._

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

## Blockers
_None._
""")
    logger.info("state_md.created", path=str(state_path))


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main(target: str, dry_run: bool):
    reg = load_registry()
    if not reg:
        logger.error("rocketmind_registry.empty")
        sys.exit(1)

    agents    = reg.get("agents", [])
    skills    = reg.get("skills", [])
    workflows = reg.get("workflows", [])

    logger.info("rocketmind_sync.start",
                agents=len(agents), skills=len(skills),
                workflows=len(workflows), target=target, dry_run=dry_run)

    generate_commands_md(workflows, agents)
    ensure_state_md()

    if target in ("all", "agents", "graph"):
        await sync_agents_to_neo4j(agents, dry_run=dry_run)
        await sync_agents_to_mysql(agents, dry_run=dry_run)

    if target in ("all", "workflows"):
        await sync_workflows_to_mysql(workflows, dry_run=dry_run)

    if target in ("all", "skills", "kb"):
        await sync_skills_to_kb(skills, dry_run=dry_run)

    logger.info("rocketmind_sync.complete", target=target)
    print(f"\n✓ RocketMind sync complete (target={target}, dry_run={dry_run})")
    print(f"  Agents:    {len(agents)}")
    print(f"  Skills:    {len(skills)}")
    print(f"  Workflows: {len(workflows)}")
    print(f"\n  Registry:  rocketmind.registry.json")
    print(f"  Commands:  .claude/commands/cosmos.md")
    print(f"  State:     .cosmos/state/STATE.md")
    print(f"\n  Start:     npm start")
    print(f"  Command:   npm run cosmos:plan\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync RocketMind into COSMOS")
    parser.add_argument("--target", default="all",
                        choices=["all", "agents", "skills", "workflows", "kb", "graph"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.target, args.dry_run))
