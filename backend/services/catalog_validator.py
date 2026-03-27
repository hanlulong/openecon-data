"""Catalog Indicator Validation Service.

Validates that all indicator codes referenced in catalog concept YAML files
actually exist in the indicator database. Prevents silent failures where
a catalog maps to a non-existent or archived indicator code.

This was added after discovering that:
- GOLDAMGBD228NLBM (gold price) did not exist in FRED
- PE.USG.LNDN (London gold price) was archived from WorldBank
- 7 Eurostat codes had case mismatches (lowercase vs uppercase)
- 10+ OECD/IMF/StatsCan codes were stale

Run at startup or via CLI:
    python -m backend.services.catalog_validator
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Codes that are intentionally dynamic (resolved at query time)
DYNAMIC_CODES = {"dynamic", "DYNAMIC", ""}


def validate_catalog_against_db(
    catalog_dir: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, List[Tuple[str, str, str, str]]]:
    """Validate all catalog concept codes against the indicator database.

    Returns a dict with:
        "valid": list of (concept_file, provider, code, name) that exist in DB
        "stale": list of (concept_file, provider, code, name) NOT in DB
        "dynamic": list of (concept_file, provider, code, name) intentionally dynamic
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed — skipping catalog validation")
        return {"valid": [], "stale": [], "dynamic": []}

    if catalog_dir is None:
        catalog_dir = str(Path(__file__).parent.parent / "catalog" / "concepts")
    if db_path is None:
        db_path = str(Path(__file__).parent.parent / "data" / "indicators.db")

    if not os.path.exists(catalog_dir):
        logger.warning(f"Catalog directory not found: {catalog_dir}")
        return {"valid": [], "stale": [], "dynamic": []}

    if not os.path.exists(db_path):
        logger.warning(f"Indicator database not found: {db_path}")
        return {"valid": [], "stale": [], "dynamic": []}

    db = sqlite3.connect(db_path)
    results: Dict[str, List[Tuple[str, str, str, str]]] = {
        "valid": [], "stale": [], "dynamic": []
    }

    for fname in sorted(os.listdir(catalog_dir)):
        if not fname.endswith(".yaml"):
            continue
        try:
            with open(os.path.join(catalog_dir, fname)) as f:
                data = yaml.safe_load(f)
            if not data or not isinstance(data, dict):
                continue

            providers = data.get("providers", {})
            for prov_name, prov_data in providers.items():
                if not isinstance(prov_data, dict):
                    continue
                primary = prov_data.get("primary", {})
                if not isinstance(primary, dict):
                    continue
                code = primary.get("code", "")
                name = primary.get("name", "")
                if not code:
                    continue

                entry = (fname, prov_name, code, name)

                if code in DYNAMIC_CODES:
                    results["dynamic"].append(entry)
                    continue

                # Check if code exists in DB (case-sensitive first, then insensitive)
                row = db.execute(
                    "SELECT code FROM indicators WHERE code = ? LIMIT 1", (code,)
                ).fetchone()
                if row:
                    results["valid"].append(entry)
                else:
                    # Try case-insensitive
                    row = db.execute(
                        "SELECT code FROM indicators WHERE UPPER(code) = UPPER(?) LIMIT 1",
                        (code,),
                    ).fetchone()
                    if row:
                        results["valid"].append(entry)
                        logger.warning(
                            f"Catalog case mismatch: {fname} has '{code}', DB has '{row[0]}'"
                        )
                    else:
                        results["stale"].append(entry)

        except Exception as e:
            logger.warning(f"Error reading catalog {fname}: {e}")

    db.close()
    return results


def log_validation_summary(results: Dict[str, List]) -> None:
    """Log a summary of catalog validation results."""
    valid = len(results.get("valid", []))
    stale = len(results.get("stale", []))
    dynamic = len(results.get("dynamic", []))
    total = valid + stale + dynamic

    if stale == 0:
        logger.info(
            f"✅ Catalog validation: {valid}/{total} codes valid, "
            f"{dynamic} dynamic, 0 stale"
        )
    else:
        logger.warning(
            f"⚠️ Catalog validation: {valid}/{total} codes valid, "
            f"{dynamic} dynamic, {stale} STALE"
        )
        for fname, prov, code, name in results["stale"]:
            logger.warning(f"   STALE: {fname} → {prov}/{code} ({name[:50]})")


def run_validation() -> None:
    """Run catalog validation and log results. Called at startup."""
    results = validate_catalog_against_db()
    log_validation_summary(results)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_validation()
