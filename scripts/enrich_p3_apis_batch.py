"""
enrich_p3_apis_batch.py — Enrich non-enriched Pillar 3 API high.yaml files by reading
PHP controller source code and generating rich business_logic via Claude Opus.

Approach (matches COSMOS_KB_ENRICHMENT_STATE.md Phase 1-2):
  1. Find the PHP controller file for each API (from controller field in high.yaml)
  2. Extract the relevant method from the controller (~120 lines)
  3. Send to Claude Opus: "read this PHP code, generate enriched KB fields"
  4. Write back to high.yaml: business_logic.description, database_reads, side_effects,
     retrieval_hints.primary_use_case, operator_phrasing, negative_examples,
     examples.param_extraction_pairs, _source_lines

Processes batches of 5 APIs per Claude call (each includes PHP code → larger context).
Sets _enriched_by_claude: true and trust_score: 0.95 (source-code verified).

Checkpoint: .enrich_checkpoint.json — safe to re-run, skips completed APIs.

Usage:
  python scripts/enrich_p3_apis_batch.py --dry-run              # show what would run
  python scripts/enrich_p3_apis_batch.py --domain orders        # orders domain dry-run
  python scripts/enrich_p3_apis_batch.py --domain orders --apply  # live
  python scripts/enrich_p3_apis_batch.py --apply                # all non-enriched APIs
  python scripts/enrich_p3_apis_batch.py --apply --workers 2 --batch-size 3
"""

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

KB_ROOT = Path("/Users/gauravchaudhary/Documents/project/marsproject/mars/knowledge_base/shiprocket")
APIS_DIR = KB_ROOT / "MultiChannel_API" / "pillar_3_api_mcp_tools" / "apis"
PHP_ROOT = Path("/Users/gauravchaudhary/Documents/project/marsproject/mars/repos/shiprocket/MultiChannel_API")
CTRL_ROOT = PHP_ROOT / "app" / "Http" / "Controllers" / "v1"
CHECKPOINT_FILE = KB_ROOT / ".enrich_checkpoint.json"

BATCH_SIZE = 5            # full enrichment: PHP code dominates context
BATCH_SIZE_SOFT = 10      # soft-context-only: smaller output → larger batches
DEFAULT_WORKERS = 2

# Domain priority — highest ICRM operator traffic first
DOMAIN_PRIORITY = [
    "orders", "shipments", "ndr", "billing", "returns",
    "pickup", "settings", "courier", "support",
    "app", "admin", "channels", "webhook", "warehouse",
    "report", "cod", "account", "products", "auth",
    "hyperlocal", "external", "oneapp", "nugget",
    "pocx", "vas", "saral", "backdata", "srx",
]

# ---------------------------------------------------------------------------
# PHP source reader
# ---------------------------------------------------------------------------

def _find_controller_file(controller_str: str) -> Optional[Path]:
    """Map 'Orders\\OrderController@show' → Path to PHP file."""
    if not controller_str:
        return None
    ctrl = controller_str.split("@")[0].strip()
    # Strip full namespace prefix if present
    ctrl = ctrl.replace("\\MultiChannel\\Http\\Controllers\\v1\\", "")
    ctrl = ctrl.lstrip("\\").replace("\\", "/")
    php_name = ctrl + ".php" if not ctrl.endswith(".php") else ctrl
    # Direct path
    direct = CTRL_ROOT / php_name
    if direct.exists():
        return direct
    # Recursive search by filename
    fname = Path(php_name).name
    for f in CTRL_ROOT.rglob(fname):
        return f
    return None


def _extract_method(code: str, method_name: str, max_lines: int = 120) -> str:
    """Extract a PHP method body. Returns up to max_lines lines."""
    if not method_name:
        return ""
    pattern = rf'(?:public|protected|private)\s+function\s+{re.escape(method_name)}\s*\('
    m = re.search(pattern, code)
    if not m:
        return ""
    start_pos = m.start()
    brace_pos = code.find("{", m.end())
    if brace_pos == -1:
        return code[start_pos:start_pos + 3000]
    depth = 0
    end_pos = brace_pos
    for i in range(brace_pos, min(brace_pos + 30000, len(code))):
        c = code[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end_pos = i
                break
    method_code = code[start_pos:end_pos + 1]
    lines = method_code.splitlines()
    return "\n".join(lines[:max_lines])


def _get_php_source(controller_str: str) -> tuple[str, str]:
    """Return (php_code, source_lines_label) for a controller method."""
    if not controller_str:
        return "", ""
    parts = controller_str.split("@")
    ctrl_class = parts[0]
    method = parts[1] if len(parts) > 1 else ""

    php_file = _find_controller_file(ctrl_class)
    if not php_file or not php_file.exists():
        return "", ""

    try:
        code = php_file.read_text(errors="ignore")
    except Exception:
        return "", ""

    if method:
        method_code = _extract_method(code, method, max_lines=120)
        if method_code:
            # Find line numbers
            start_line = code[:code.find(method_code)].count("\n") + 1
            end_line = start_line + method_code.count("\n")
            label = f"{php_file.name}:{start_line}-{end_line}"
            return method_code, label
        # Method not found — return first 100 lines of file for context
        return "\n".join(code.splitlines()[:100]), f"{php_file.name}:1-100"

    # No method specified — return first 80 lines
    return "\n".join(code.splitlines()[:80]), f"{php_file.name}:1-80"


# ---------------------------------------------------------------------------
# API data extraction
# ---------------------------------------------------------------------------

def _needs_update(d: Dict, soft_only: bool = False) -> bool:
    """Return True if an already-enriched doc is missing fields we care about."""
    ov = d.get("overview", {}) or {}
    rh = ov.get("retrieval_hints", {}) or {}
    if soft_only:
        return "soft_required_context" not in rh
    return (
        not rh.get("primary_use_case")
        or not rh.get("operator_phrasing")
        or not rh.get("negative_examples")
        or not d.get("trust_score")
        or "soft_required_context" not in rh
    )


def _extract_api_data(api_dir: Path, force_update: bool = False, soft_only: bool = False, domain_filter: Optional[str] = None) -> Optional[Dict]:
    """Extract data from high.yaml + PHP source for enrichment prompt."""
    high_yaml = api_dir / "high.yaml"
    if not high_yaml.exists():
        return None

    try:
        d = yaml.safe_load(high_yaml.read_text())
    except Exception:
        return None

    if not d or not isinstance(d, dict):
        return None

    # Skip already enriched unless force_update and missing fields we care about
    if d.get("_enriched_by_claude"):
        if not force_update or not _needs_update(d, soft_only=soft_only):
            return None

    ov = d.get("overview", {}) or {}
    api_info = ov.get("api", {}) or {}
    cls = ov.get("classification", {}) or {}
    rh = ov.get("retrieval_hints", {}) or {}

    # Domain filter: use YAML classification.domain, not directory name
    # This catches APIs like mcapi.internal.report.hyperlocal_orders.get
    # whose directory name parses to "report" but YAML domain is "orders"
    if domain_filter and cls.get("domain", "") != domain_filter:
        return None

    method = api_info.get("method", "")
    path = api_info.get("path", "")
    controller = api_info.get("controller", "")

    if not method or not path:
        return None

    # Request schema fields
    rs = d.get("request_schema", {}) or {}
    contract = rs.get("contract", {}) or {}
    required_fields = contract.get("required", []) if isinstance(contract, dict) else []
    optional_fields = contract.get("optional", []) if isinstance(contract, dict) else []

    def _field_summary(fields):
        if not fields:
            return []
        out = []
        for f in fields[:10]:
            if isinstance(f, dict):
                name = f.get("name", "")
                ftype = f.get("type", "")
                validation = f.get("validation", "")[:60] if f.get("validation") else ""
                out.append(f"{name} ({ftype}){': ' + validation if validation else ''}")
            else:
                out.append(str(f))
        return out

    # Response fields
    rf = d.get("response_fields", {}) or {}
    resp_fields = []
    if isinstance(rf, dict):
        for section_val in rf.values():
            if isinstance(section_val, list):
                resp_fields.extend([
                    f.get("name", str(f)) if isinstance(f, dict) else str(f)
                    for f in section_val[:6]
                ])
            if len(resp_fields) >= 10:
                break

    # PHP source
    php_code, source_lines = _get_php_source(controller)

    return {
        "api_id": api_dir.name,
        "method": method,
        "path": path,
        "domain": cls.get("domain", ""),
        "subdomain": cls.get("subdomain", ""),
        "intent_primary": cls.get("intent_primary", ""),
        "canonical_summary": rh.get("canonical_summary", "")[:150],
        "controller": controller,
        "required_fields": _field_summary(required_fields),
        "optional_fields": _field_summary(optional_fields),
        "response_fields": resp_fields[:10],
        "php_code": php_code,
        "source_lines": source_lines,
    }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior Shiprocket backend engineer enriching knowledge base docs for COSMOS — Shiprocket's ICRM AI copilot.

You will receive PHP controller methods (or summaries when source isn't available) for Shiprocket's logistics platform APIs.

For each API, read the PHP code carefully and generate:

1. business_logic_description (50-150 words):
   - What this endpoint ACTUALLY does (from reading the code, not just the URL)
   - Who calls it (seller/operator/admin/internal)
   - Key validation rules (required fields, constraints)
   - Database tables read/written (from Eloquent models, DB::table(), or set_database calls)
   - Side effects: jobs dispatched, SMS sent, events fired, external services called
   - Auth/company_id scoping (multi-tenant isolation)
   - Any special business rules, config flags, or conditional paths

2. database_reads: list of {table, filter} objects extracted from code

3. side_effects: list of strings (SMS, jobs, events, external API calls seen in code)

4. primary_use_case: 1 sentence, operator-perspective: "Use this when..."

5. operator_phrasing: 3 phrases an ICRM operator or seller would type (include Hinglish where natural)
   Examples: "NDR reattempt karo", "cancel shipment 12345", "show me pickup history"

6. negative_examples: 2 things this is NOT (prevent mis-routing to wrong API)
   Format: "NOT: <description>"

7. param_extraction_pairs: 2 examples mapping operator query → actual API params
   Each: {query: "...", params: {field: value, ...}}

8. soft_required_context: list of params that are technically optional BUT the API returns
   garbage/empty/unfiltered results without them. Only include when you see in PHP code:
   - "returns ALL records if param missing" (massive unfiltered query)
   - company_id/client_id scoping that is skipped when param absent
   - empty result guards ("if no X provided, return []")
   - superadmin-only fallback when tenant filter is absent
   Leave as empty array [] if every optional param degrades gracefully.
   Each entry:
   {
     param: "client_id",
     alias: "company_id",              # alternate name operators use
     behavior_without_param: "...",    # what actually happens in the PHP code without it
     reason: "...",                    # 1 sentence why it matters
     ask_if_missing: "...",            # exact question COSMOS should ask the operator
     extract_from_context: [           # NL patterns → param value mappings
       "company {id} → client_id",
       "seller {id} → client_id"
     ],
     example_values: ["25149", "12345"],
     skip_if_present: ["awb"]         # other params that make this one unnecessary
   }

Output a JSON array, one object per API, SAME ORDER as input.
Keys: api_id, business_logic_description, database_reads, side_effects, primary_use_case, operator_phrasing, negative_examples, param_extraction_pairs, soft_required_context.

Shiprocket terms: AWB=tracking number, NDR=non-delivery report, RTO=return-to-origin,
COD=cash-on-delivery, ICRM=internal CRM for Shiprocket operators, company_id=seller account ID,
manifest=pickup collection document, shipment=physical package, order=seller's order record."""


SYSTEM_PROMPT_SOFT = """You are a senior Shiprocket backend engineer. Read each PHP controller method and identify ONLY soft_required_context — optional params that cause the API to return garbage/empty/unfiltered results when absent.

Rules for inclusion:
- Include ONLY when PHP code shows: returns ALL records without the param, skips company_id/client_id scoping, returns [] guard when param absent, or superadmin fallback when tenant filter missing.
- Leave as empty array [] if every optional param degrades gracefully.

Each entry schema:
{
  param: "client_id",
  alias: "company_id",
  behavior_without_param: "what the PHP code actually does without it",
  reason: "1 sentence why it matters for ICRM operators",
  ask_if_missing: "exact question COSMOS should ask the operator",
  extract_from_context: ["company {id} → client_id", "seller {id} → client_id"],
  example_values: ["25149", "12345"],
  skip_if_present: ["awb"]
}

Output a JSON array, one object per API, SAME ORDER as input.
Keys: api_id, soft_required_context (array, may be []).

Shiprocket terms: AWB=tracking number, NDR=non-delivery, RTO=return-to-origin,
company_id=seller account ID, ICRM=internal CRM for Shiprocket operators."""


def _build_soft_prompt(apis: List[Dict]) -> str:
    lines = [f"Find soft_required_context for these {len(apis)} APIs:\n"]
    for i, api in enumerate(apis, 1):
        lines.append(f"{'='*50}")
        lines.append(f"API {i}: {api['method']} {api['path']}")
        lines.append(f"api_id: {api['api_id']}")
        if api["optional_fields"]:
            lines.append(f"optional_params: {', '.join(api['optional_fields'][:8])}")
        if api["php_code"]:
            lines.append(f"\nPHP source ({api['source_lines']}):")
            lines.append("```php")
            lines.append(api["php_code"])
            lines.append("```")
        else:
            lines.append("\n[PHP source not available — use domain knowledge only]")
        lines.append("")
    lines.append(f"\nReturn a JSON array of exactly {len(apis)} objects.")
    return "\n".join(lines)


def _build_batch_prompt(apis: List[Dict]) -> str:
    lines = [f"Enrich these {len(apis)} APIs from Shiprocket's PHP codebase:\n"]
    for i, api in enumerate(apis, 1):
        lines.append(f"{'='*50}")
        lines.append(f"API {i}: {api['method']} {api['path']}")
        lines.append(f"api_id: {api['api_id']}")
        lines.append(f"domain: {api['domain']}/{api['subdomain']} | intent: {api['intent_primary']}")
        if api["controller"]:
            lines.append(f"controller: {api['controller']}")
        if api["source_lines"]:
            lines.append(f"source: {api['source_lines']}")
        if api["canonical_summary"]:
            lines.append(f"auto_summary: {api['canonical_summary']}")
        if api["required_fields"]:
            lines.append(f"required_params:")
            for f in api["required_fields"]:
                lines.append(f"  - {f}")
        if api["optional_fields"]:
            lines.append(f"optional_params: {', '.join(api['optional_fields'][:5])}")
        if api["response_fields"]:
            lines.append(f"response_fields: {', '.join(api['response_fields'])}")

        if api["php_code"]:
            lines.append(f"\nPHP source ({api['source_lines']}):")
            lines.append("```php")
            lines.append(api["php_code"])
            lines.append("```")
        else:
            lines.append("\n[PHP source not available — use URL, params, and domain knowledge]")
        lines.append("")

    lines.append(f"\nReturn a JSON array of exactly {len(apis)} objects.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude CLI caller
# ---------------------------------------------------------------------------

async def _call_claude_cli(prompt: str, model: str = "opus", soft_only: bool = False) -> str:
    """Call Claude via CLI subprocess. Uses Opus for source-code analysis."""
    sys_prompt = SYSTEM_PROMPT_SOFT if soft_only else SYSTEM_PROMPT
    full_prompt = f"[System: {sys_prompt}]\n\n{prompt}"
    cmd = [
        "claude", "-p", full_prompt,
        "--output-format", "json",
        "--model", model,
        "--max-turns", "1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"Claude CLI error (rc={proc.returncode}): {err}")
    raw = stdout.decode("utf-8", errors="ignore").strip()
    # CLI --output-format json wraps: {"type":"result","result":"..."}
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict) and "result" in outer:
            return outer["result"]
    except json.JSONDecodeError:
        pass
    return raw


# ---------------------------------------------------------------------------
# Enrichment writer
# ---------------------------------------------------------------------------

def _write_soft_context(api_dir: Path, enrichment: Dict) -> bool:
    """Write ONLY soft_required_context back into high.yaml. Never touches other fields."""
    high_yaml = api_dir / "high.yaml"
    try:
        d = yaml.safe_load(high_yaml.read_text())
        if not d or not isinstance(d, dict):
            return False
        rh = d.setdefault("overview", {}).setdefault("retrieval_hints", {})
        rh["soft_required_context"] = enrichment.get("soft_required_context", [])
        with open(high_yaml, "w", encoding="utf-8") as f:
            yaml.dump(d, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        print(f"  [WARN] Write failed for {api_dir.name}: {e}")
        return False


def _write_enrichment(api_dir: Path, enrichment: Dict, source_lines: str) -> bool:
    """Write enrichment fields back into high.yaml. Returns True on success."""
    high_yaml = api_dir / "high.yaml"
    try:
        d = yaml.safe_load(high_yaml.read_text())
        if not d or not isinstance(d, dict):
            return False

        # Business logic
        ov = d.setdefault("overview", {})
        bl = ov.setdefault("business_logic", {})
        if enrichment.get("business_logic_description"):
            bl["description"] = enrichment["business_logic_description"]
        if enrichment.get("database_reads"):
            bl["database_reads"] = enrichment["database_reads"]
        if enrichment.get("side_effects"):
            bl["side_effects"] = enrichment["side_effects"]

        # Retrieval hints
        rh = ov.setdefault("retrieval_hints", {})
        if enrichment.get("primary_use_case"):
            rh["primary_use_case"] = enrichment["primary_use_case"]
        if enrichment.get("operator_phrasing"):
            rh["operator_phrasing"] = enrichment["operator_phrasing"]
        if enrichment.get("negative_examples"):
            rh["negative_examples"] = enrichment["negative_examples"]
        if enrichment.get("soft_required_context"):
            rh["soft_required_context"] = enrichment["soft_required_context"]

        # Examples
        if enrichment.get("param_extraction_pairs"):
            ex = d.setdefault("examples", {})
            existing = ex.get("param_extraction_pairs", [])
            existing_queries = {p.get("query", "") for p in existing if isinstance(p, dict)}
            for pair in enrichment["param_extraction_pairs"]:
                if isinstance(pair, dict) and pair.get("query") not in existing_queries:
                    existing.append(pair)
            ex["param_extraction_pairs"] = existing

        # Mark enriched with source code provenance
        d["_enriched_by_claude"] = True
        d["_source_lines"] = source_lines
        d["trust_score"] = 0.95  # source-code verified → highest trust

        with open(high_yaml, "w", encoding="utf-8") as f:
            yaml.dump(d, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return True

    except Exception as e:
        print(f"  [WARN] Write failed for {api_dir.name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

def _load_checkpoint() -> set:
    if CHECKPOINT_FILE.exists():
        try:
            return set(json.loads(CHECKPOINT_FILE.read_text()).get("completed_api_ids", []))
        except Exception:
            return set()
    return set()


def _save_checkpoint(completed: set):
    try:
        CHECKPOINT_FILE.write_text(json.dumps({"completed_api_ids": list(completed)}, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Batch processor
# ---------------------------------------------------------------------------

async def _process_batch(
    batch_apis: List[Dict],
    api_dirs: Dict[str, Path],
    dry_run: bool,
    completed: set,
    model: str = "opus",
    soft_only: bool = False,
) -> int:
    if not batch_apis:
        return 0

    prompt = _build_soft_prompt(batch_apis) if soft_only else _build_batch_prompt(batch_apis)

    if dry_run:
        mode = "SOFT-CONTEXT-ONLY" if soft_only else "FULL"
        print(f"  [DRY-RUN] {len(batch_apis)} APIs | model={model} | mode={mode}")
        for a in batch_apis:
            src = f" [{a['source_lines']}]" if a["source_lines"] else " [no PHP source]"
            print(f"    {a['method']} {a['path']}{src}")
        return 0

    try:
        raw = await _call_claude_cli(prompt, model=model, soft_only=soft_only)

        start = raw.find("[")
        if start == -1:
            print(f"  [WARN] No JSON array in response. Raw: {raw[:200]}")
            return 0

        try:
            enrichments, _ = json.JSONDecoder().raw_decode(raw, start)
        except json.JSONDecodeError as e:
            print(f"  [WARN] JSON parse error: {e}")
            return 0
        if not isinstance(enrichments, list):
            return 0

        count = 0
        for i, enrichment in enumerate(enrichments):
            if i >= len(batch_apis):
                break
            api_id = batch_apis[i]["api_id"]
            api_dir = api_dirs.get(api_id)
            source_lines = batch_apis[i].get("source_lines", "")
            if not api_dir or not isinstance(enrichment, dict):
                continue

            if soft_only:
                # soft_required_context may legitimately be [] — treat presence of key as success
                if "soft_required_context" in enrichment:
                    if _write_soft_context(api_dir, enrichment):
                        completed.add(api_id)
                        count += 1
                else:
                    print(f"  [WARN] Missing soft_required_context key for {api_id}")
            else:
                if enrichment.get("business_logic_description"):
                    if _write_enrichment(api_dir, enrichment, source_lines):
                        completed.add(api_id)
                        count += 1
                else:
                    print(f"  [WARN] Empty enrichment for {api_id}")

        _save_checkpoint(completed)
        return count

    except asyncio.TimeoutError:
        print(f"  [TIMEOUT] Batch timed out after 180s")
        return 0
    except Exception as e:
        print(f"  [ERROR] {e}")
        return 0


# ---------------------------------------------------------------------------
# Domain key helpers
# ---------------------------------------------------------------------------

def _get_domain_key(api_dir_name: str) -> str:
    parts = api_dir_name.split(".")
    for i, p in enumerate(parts):
        if p in ("v1", "v2", "v3", "internal", "admin", "nugget", "channel"):
            if i + 1 < len(parts):
                return parts[i + 1]
    return "zzz_other"


def _domain_sort_key(api_dir_name: str) -> int:
    domain = _get_domain_key(api_dir_name)
    try:
        return DOMAIN_PRIORITY.index(domain)
    except ValueError:
        return 999


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(args):
    completed = _load_checkpoint()
    model = args.model
    soft_only = args.soft_context_only

    # Effective batch size: larger batches are safe in soft-only mode (smaller output)
    batch_size = args.batch_size if args.batch_size != BATCH_SIZE else (
        BATCH_SIZE_SOFT if soft_only else BATCH_SIZE
    )

    mode_label = "SOFT-CONTEXT-ONLY" if soft_only else "FULL"
    print(f"\n{'='*65}")
    print(f"enrich_p3_apis_batch.py  [{'DRY-RUN' if args.dry_run else 'LIVE'}] [{mode_label}]")
    print(f"Model: {model} | Batch: {batch_size} APIs/call | Workers: {args.workers}")
    print(f"PHP source: {PHP_ROOT.exists()} ({CTRL_ROOT})")
    if args.domain:
        print(f"Domain filter: {args.domain}")
    # Resolve --api-ids: comma-separated list OR path to a file with one ID per line
    api_id_filter: Optional[set] = None
    if args.api_ids:
        raw_ids = args.api_ids.strip()
        id_path = Path(raw_ids)
        if id_path.exists() and id_path.is_file():
            api_id_filter = {l.strip() for l in id_path.read_text().splitlines() if l.strip()}
            print(f"API ID filter: {len(api_id_filter)} IDs from file {id_path}")
        else:
            api_id_filter = {i.strip() for i in raw_ids.split(",") if i.strip()}
            print(f"API ID filter: {len(api_id_filter)} IDs")
    if soft_only:
        print("Mode: writing ONLY soft_required_context — all other fields preserved")
    print(f"Already completed (checkpoint): {len(completed)}")
    print(f"{'='*65}\n")

    # Collect APIs to enrich
    api_dirs: Dict[str, Path] = {}
    todo: List[Dict] = []

    all_dirs = sorted(APIS_DIR.iterdir(), key=lambda d: _domain_sort_key(d.name))
    no_php = 0

    for api_dir in all_dirs:
        if not api_dir.is_dir():
            continue
        # File-level filter: if --api-ids specified, only process those IDs
        if api_id_filter and api_dir.name not in api_id_filter:
            continue
        if not args.force_update and api_dir.name in completed:
            continue

        data = _extract_api_data(
            api_dir,
            force_update=args.force_update or bool(api_id_filter),  # --api-ids implies force
            soft_only=soft_only,
            domain_filter=args.domain if not api_id_filter else None,  # skip domain filter when IDs given
        )
        if data:
            api_dirs[api_dir.name] = api_dir
            todo.append(data)
            if not data["php_code"]:
                no_php += 1

    print(f"APIs to enrich: {len(todo)}")
    print(f"  with PHP source: {len(todo) - no_php}")
    print(f"  without PHP source (YAML-only): {no_php}")
    print(f"  batches: {(len(todo) + batch_size - 1) // batch_size}")
    print()

    if not todo:
        print("Nothing to do.")
        return

    if args.dry_run:
        await _process_batch(todo[:batch_size], api_dirs, dry_run=True, completed=completed, model=model, soft_only=soft_only)
        return

    sem = asyncio.Semaphore(args.workers)
    total_batches = (len(todo) + batch_size - 1) // batch_size

    async def _worker(batch_num: int, batch: List[Dict]):
        async with sem:
            count = await _process_batch(batch, api_dirs, dry_run=False, completed=completed, model=model, soft_only=soft_only)
            domain = _get_domain_key(batch[0]["api_id"]) if batch else "?"
            php_count = sum(1 for a in batch if a["php_code"])
            print(f"  [{batch_num:3d}/{total_batches}] domain={domain:<12} | enriched={count}/{len(batch)} | php={php_count}/{len(batch)}")
            return count

    tasks = [
        _worker(i // batch_size + 1, todo[i:i + batch_size])
        for i in range(0, len(todo), batch_size)
    ]

    t0 = time.monotonic()
    results = await asyncio.gather(*tasks)
    total = sum(results)
    elapsed = time.monotonic() - t0

    suffix = " (soft_required_context only)" if soft_only else " (trust_score=0.95 for source-verified)"
    print(f"\n{'='*65}")
    print(f"Done in {elapsed:.1f}s")
    print(f"Enriched: {total}/{len(todo)}{suffix}")
    print(f"Checkpoint: {CHECKPOINT_FILE}")
    print(f"{'='*65}")
    print(f"\nNext: POST /cosmos/api/v1/pipeline/schema  (re-embed updated APIs)")


def main():
    parser = argparse.ArgumentParser(description="Enrich P3 API high.yaml via PHP source + Claude Opus")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--domain", default=None, help="Domain filter (e.g. orders, shipments, ndr)")
    parser.add_argument("--model", default="opus", choices=["opus", "sonnet"], help="Claude model (default: opus)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--force-update", action="store_true",
        help="Re-enrich already-enriched APIs that are missing new fields "
             "(primary_use_case, operator_phrasing, negative_examples, trust_score)"
    )
    parser.add_argument(
        "--soft-context-only", action="store_true",
        help="Write ONLY soft_required_context — skip all other enrichment fields. "
             "~70%% fewer output tokens. Preserves existing business_logic, operator_phrasing, etc."
    )
    parser.add_argument(
        "--api-ids", default=None,
        help="Target specific APIs by ID. Accepts: "
             "(1) comma-separated: 'mcapi.v1.orders.get,mcapi.v1.orders.show.by_id.get' "
             "(2) path to a file with one API ID per line. "
             "Bypasses domain filter; implies --force-update for targeted IDs."
    )
    args = parser.parse_args()
    if args.apply:
        args.dry_run = False
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
