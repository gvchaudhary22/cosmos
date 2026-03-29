"""
Data Splitter — Merges eval files, deduplicates, and creates stratified splits.

Implements:
  - Milestone 2: train_set (70%) / dev_set (15%) / holdout_set (15%)
  - Section 13: Cold start bootstrap from grounded KB artifacts

Rules:
  - Dedup by normalized query
  - Stratify by intent + repo distribution
  - Fixed random seed for reproducibility
  - Store split provenance
  - holdout NEVER used for training
"""

import hashlib
import json
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

RANDOM_SEED = 42
TRAIN_RATIO = 0.70
DEV_RATIO = 0.15
HOLDOUT_RATIO = 0.15


class DataSplitter:
    """Merges eval sources, deduplicates, and produces train/dev/holdout splits."""

    def __init__(self, kb_path: str, output_dir: str):
        self.kb_path = Path(kb_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run_split(self) -> Dict:
        """Execute the full merge + dedup + stratified split pipeline."""
        # Step 1: Collect all eval examples from all sources
        all_examples = []
        all_examples.extend(self._load_global_eval_sets())
        all_examples.extend(self._load_training_seeds())
        all_examples.extend(self._load_pillar4_eval_cases())

        logger.info("splitter.collected", total=len(all_examples))

        # Step 2: Dedup by normalized query
        deduped = self._deduplicate(all_examples)
        logger.info("splitter.deduped", before=len(all_examples), after=len(deduped))

        # Step 3: Stratified split
        train, dev, holdout = self._stratified_split(deduped)

        # Step 4: Write to files with provenance
        train_path = self.output_dir / "train_set.jsonl"
        dev_path = self.output_dir / "dev_set.jsonl"
        holdout_path = self.output_dir / "holdout_set.jsonl"

        self._write_jsonl(train_path, train, "train")
        self._write_jsonl(dev_path, dev, "dev")
        self._write_jsonl(holdout_path, holdout, "holdout")

        # Step 5: Write split metadata
        meta = {
            "total_collected": len(all_examples),
            "after_dedup": len(deduped),
            "train_count": len(train),
            "dev_count": len(dev),
            "holdout_count": len(holdout),
            "random_seed": RANDOM_SEED,
            "split_ratios": {"train": TRAIN_RATIO, "dev": DEV_RATIO, "holdout": HOLDOUT_RATIO},
            "sources": self._count_sources(deduped),
        }

        meta_path = self.output_dir / "split_metadata.json"
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        logger.info(
            "splitter.complete",
            train=len(train),
            dev=len(dev),
            holdout=len(holdout),
        )

        return meta

    def _load_global_eval_sets(self) -> List[Dict]:
        """Load all global_eval_set.jsonl files across repos."""
        examples = []
        for eval_file in self.kb_path.glob("**/global_eval_set.jsonl"):
            repo = eval_file.parts[-4] if len(eval_file.parts) >= 4 else "unknown"
            try:
                for i, line in enumerate(open(eval_file)):
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    entry["_source"] = "global_eval_set"
                    entry["_source_file"] = str(eval_file)
                    entry["_source_line"] = i
                    entry["_repo"] = repo
                    examples.append(entry)
            except Exception as e:
                logger.warning("splitter.load_error", file=str(eval_file), error=str(e))
        return examples

    def _load_training_seeds(self) -> List[Dict]:
        """Load all training_seeds.jsonl files."""
        examples = []
        for seed_file in self.kb_path.glob("**/training_seeds.jsonl"):
            repo = seed_file.parts[-4] if len(seed_file.parts) >= 4 else "unknown"
            try:
                for i, line in enumerate(open(seed_file)):
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    entry["_source"] = "training_seeds"
                    entry["_source_file"] = str(seed_file)
                    entry["_source_line"] = i
                    entry["_repo"] = repo
                    examples.append(entry)
            except Exception as e:
                logger.warning("splitter.load_error", file=str(seed_file), error=str(e))
        return examples

    def _load_pillar4_eval_cases(self) -> List[Dict]:
        """Load eval_cases.yaml from Pillar 4 page directories."""
        examples = []
        for eval_file in self.kb_path.glob("**/pillar_4*/pages/*/eval_cases.yaml"):
            repo = eval_file.parts[-6] if len(eval_file.parts) >= 6 else "unknown"
            page_id = eval_file.parent.name
            try:
                import yaml
                with open(eval_file) as f:
                    data = yaml.safe_load(f) or {}
                cases = data.get("eval_cases", data.get("cases", []))
                if isinstance(cases, list):
                    for i, case in enumerate(cases):
                        query = case.get("query", case.get("input", ""))
                        expected = case.get("expected_output", case.get("expected_intent", ""))
                        if query:
                            examples.append({
                                "query": query,
                                "expected_output": expected,
                                "page_id": page_id,
                                "_source": "pillar4_eval_cases",
                                "_source_file": str(eval_file),
                                "_source_line": i,
                                "_repo": repo,
                            })
            except Exception as e:
                logger.warning("splitter.eval_yaml_error", file=str(eval_file), error=str(e))
        return examples

    def _deduplicate(self, examples: List[Dict]) -> List[Dict]:
        """Dedup by normalized query text."""
        seen = {}
        deduped = []
        for ex in examples:
            query = ex.get("query", "").strip().lower()
            if not query:
                continue
            key = hashlib.md5(query.encode()).hexdigest()
            if key not in seen:
                seen[key] = True
                deduped.append(ex)
        return deduped

    def _stratified_split(self, examples: List[Dict]) -> Tuple[List, List, List]:
        """Split maintaining intent/repo distribution across all splits."""
        # Group by intent (or source as fallback for stratification)
        groups = defaultdict(list)
        for ex in examples:
            # Use intent if available, else source, else "unknown"
            intent = ex.get("intent", ex.get("expected_tool", ex.get("_source", "unknown")))
            groups[str(intent)].append(ex)

        train, dev, holdout = [], [], []
        rng = random.Random(RANDOM_SEED)

        for intent, group in groups.items():
            rng.shuffle(group)
            n = len(group)
            t = int(n * TRAIN_RATIO)
            d = int(n * (TRAIN_RATIO + DEV_RATIO))
            train.extend(group[:t])
            dev.extend(group[t:d])
            holdout.extend(group[d:])

        # Final shuffle within each split
        rng.shuffle(train)
        rng.shuffle(dev)
        rng.shuffle(holdout)

        return train, dev, holdout

    def _write_jsonl(self, path: Path, data: List[Dict], split_name: str):
        """Write JSONL with split provenance."""
        with open(path, "w") as f:
            for ex in data:
                ex["_split"] = split_name
                ex["_split_version"] = "v1"
                f.write(json.dumps(ex, default=str) + "\n")

    def _count_sources(self, examples: List[Dict]) -> Dict[str, int]:
        """Count examples by source."""
        counts = defaultdict(int)
        for ex in examples:
            counts[ex.get("_source", "unknown")] += 1
        return dict(counts)
