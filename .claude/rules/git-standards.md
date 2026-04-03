# Cosmos Git Standards

Enforced conventions for all branches and commits. Adopted from Orbit v2.8.1.

## Branch Naming

```
fix/NNN-short-description      # Bug fix (issue #NNN)
feat/NNN-short-description     # New feature (issue #NNN)
arch/NNN-short-description     # Architecture change (issue #NNN)
chore/NNN-short-description    # Maintenance, deps, tooling
docs/NNN-short-description     # Documentation only
test/NNN-short-description     # Tests only
refactor/NNN-short-description # Refactor (no behavior change)
```

**Rules:**
- Always include issue number
- kebab-case description
- Cut branch from latest `develop` — run `git pull origin develop` first
- **Never commit directly to `main` or `develop`**

**Examples:**
```bash
git checkout -b feat/42-bert-baseline-training
git checkout -b fix/56-inference-latency-timeout
git checkout -b arch/70-knowledge-graph-refactor
```

## Commit Message Format

```
<type>(<scope>): <what was done> (#NNN)
```

**Types:** `feat`, `fix`, `arch`, `refactor`, `test`, `docs`, `chore`, `perf`, `security`

**Scopes (Cosmos):** `brain`, `engine`, `graph`, `learning`, `api`, `grpc`, `guardrails`, `db`, `monitoring`, `ci`, `hooks`

**Examples:**
```
feat(brain): add multi-model routing with confidence scoring (#42)
fix(engine): handle timeout on inference call with retry (#56)
arch(graph): migrate knowledge graph to Neo4j (#70)
test(api): add integration tests for routing endpoint (#83)
chore(deps): upgrade anthropic sdk to 0.37 (#99)
```

## Pull Request Rules

- One PR per issue
- All CI gates must pass before review request
- Squash merge only
- Rebase onto develop before PR (never merge develop into branch)
