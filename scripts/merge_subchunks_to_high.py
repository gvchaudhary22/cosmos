"""
merge_subchunks_to_high.py — Merge high/params.yaml + high/response.yaml + high/examples.yaml
into high.yaml for all Pillar 3 API directories.

Problem:
  621 enriched APIs store request_schema + response_fields in separate sub-chunk files
  (high/params.yaml, high/response.yaml) instead of in high.yaml directly.
  This forces runtime merge in kb_ingestor.py and makes change detection complex.

Fix:
  Write the sub-chunk content into high.yaml on disk so:
    1. high.yaml is the single source of truth for what gets embedded
    2. Change detection is simple: MD5(high.yaml) = the embedding content hash
    3. No runtime _merge_sub_chunks() call needed in kb_ingestor.py

Rules:
  - Only fills keys that are MISSING or EMPTY in high.yaml (never overwrites existing content)
  - Idempotent: safe to run multiple times
  - Works for both enriched and non-enriched APIs (non-enriched already have the keys → skipped)

Usage:
  python scripts/merge_subchunks_to_high.py                          # dry-run
  python scripts/merge_subchunks_to_high.py --apply                  # live
  python scripts/merge_subchunks_to_high.py --apply --repo MultiChannel_API
"""

import argparse
import os
import sys
from pathlib import Path

import yaml


# Keys to pull from each sub-chunk file
# Format: { filename_stem: [(source_key_in_file, dest_key_in_high), ...] }
MERGE_MAP = {
    "params":   [("request_schema",  "request_schema")],
    "response": [("response_fields", "response_fields"),
                 ("side_effects",    "side_effects")],
    "examples": [("examples",        "examples")],
}


def load_yaml(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"  [WARN] Could not read {path}: {e}")
        return None


def dump_yaml(data, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def process_api_dir(api_dir: Path, apply: bool) -> str:
    """
    Merge sub-chunks into high.yaml for one API directory.

    Returns:
        "merged"   — changes were made (or would be in dry-run)
        "skipped"  — high.yaml already has all keys, nothing to do
        "no_high"  — no high.yaml found
        "error"    — read/write error
    """
    high_yaml = api_dir / "high.yaml"
    high_dir  = api_dir / "high"

    if not high_yaml.exists():
        return "no_high"

    if not high_dir.is_dir():
        return "skipped"

    high = load_yaml(high_yaml)
    if not high or not isinstance(high, dict):
        return "error"

    changes: dict = {}

    for stem, key_pairs in MERGE_MAP.items():
        chunk_path = high_dir / f"{stem}.yaml"
        if not chunk_path.exists():
            continue
        data = load_yaml(chunk_path)
        if not data or not isinstance(data, dict):
            continue

        for src_key, dest_key in key_pairs:
            value = data.get(src_key)
            if value is None:
                continue
            # Only fill if missing or empty in high.yaml — never overwrite existing content
            if dest_key not in high or not high[dest_key]:
                changes[dest_key] = value

    if not changes:
        return "skipped"

    if apply:
        high.update(changes)
        try:
            dump_yaml(high, high_yaml)
        except Exception as e:
            print(f"  [ERROR] Could not write {high_yaml}: {e}")
            return "error"

    return "merged"


def main():
    parser = argparse.ArgumentParser(description="Merge high/ sub-chunks into high.yaml")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--repo", default=None, help="Filter to one repo (e.g. MultiChannel_API)")
    parser.add_argument(
        "--kb-path",
        default="/Users/gauravchaudhary/Documents/project/marsproject/mars/knowledge_base/shiprocket",
        help="Root KB path",
    )
    args = parser.parse_args()

    kb_root = Path(args.kb_path)
    if not kb_root.exists():
        print(f"[ERROR] KB path not found: {kb_root}")
        sys.exit(1)

    mode = "LIVE" if args.apply else "DRY-RUN"
    print(f"\n{'='*60}")
    print(f"merge_subchunks_to_high.py  [{mode}]")
    print(f"KB root: {kb_root}")
    if args.repo:
        print(f"Repo filter: {args.repo}")
    print(f"{'='*60}\n")

    counts = {"merged": 0, "skipped": 0, "no_high": 0, "error": 0}

    repos = [args.repo] if args.repo else sorted(os.listdir(kb_root))
    for repo_name in repos:
        repo_dir = kb_root / repo_name
        if not repo_dir.is_dir():
            continue

        apis_dir = repo_dir / "pillar_3_api_mcp_tools" / "apis"
        if not apis_dir.is_dir():
            continue

        repo_merged = 0
        api_dirs = sorted(apis_dir.iterdir())
        total = len([d for d in api_dirs if d.is_dir()])

        for api_dir in api_dirs:
            if not api_dir.is_dir():
                continue
            result = process_api_dir(api_dir, apply=args.apply)
            counts[result] += 1
            if result == "merged":
                repo_merged += 1

        if repo_merged > 0 or True:
            print(f"  {repo_name}: {repo_merged} APIs updated, {total - repo_merged} already complete")

    print(f"\n{'='*60}")
    print(f"Results [{mode}]:")
    print(f"  Updated (merged):    {counts['merged']}")
    print(f"  Already complete:    {counts['skipped']}")
    print(f"  No high.yaml:        {counts['no_high']}")
    print(f"  Errors:              {counts['error']}")
    print(f"  Total processed:     {sum(counts.values())}")
    print(f"{'='*60}")

    if not args.apply and counts["merged"] > 0:
        print(f"\n  Run with --apply to write {counts['merged']} changes to disk.")
    elif args.apply and counts["merged"] > 0:
        print(f"\n  Done. {counts['merged']} high.yaml files updated.")
        print("  Next: re-run training pipeline — changed files will be detected and re-embedded.")


if __name__ == "__main__":
    main()
