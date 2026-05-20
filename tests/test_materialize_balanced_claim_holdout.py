from __future__ import annotations

import json
from pathlib import Path
import urllib.error

import pytest

from scripts.validation import materialize_balanced_claim_holdout as module


def _row(row_id: int, code: str) -> dict:
    return {
        "id": row_id,
        "provider": "FRED",
        "code": code,
        "name": f"{code} test indicator",
        "category": None,
        "subcategory": None,
        "description": "",
        "unit": None,
        "frequency": None,
        "coverage": None,
        "keywords": None,
        "synonyms": None,
        "raw_metadata": None,
        "popularity": row_id,
        "last_updated": None,
    }


def _record(row: dict, seq: int, **_: object) -> dict:
    return {
        "id": f"direct-fred-{seq:06d}",
        "provider_stratum": "FRED",
        "query": f"{row['code']} from FRED",
        "origin": {
            "source_provider": "FRED",
            "source_indicator_code": row["code"],
            "name": row["name"],
            "popularity": row.get("popularity"),
        },
        "provenance": {},
    }


def test_build_direct_rows_excludes_high_risk_queries_before_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "provider_catalog_count", lambda provider, db_path: 3)
    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: [_row(1, "BAD"), _row(2, "OK1"), _row(3, "OK2")])
    monkeypatch.setattr(module, "build_direct_record", _record)
    monkeypatch.setattr(
        module,
        "audit_direct_query_shape",
        lambda record: {
            "risk_level": "high" if (record["origin"]["source_indicator_code"] == "BAD") else "low",
            "reasons": ["synthetic_high"] if (record["origin"]["source_indicator_code"] == "BAD") else [],
        },
    )

    records, supportability_excluded, quality_excluded, inventory = module.build_direct_rows(
        provider="FRED",
        count=2,
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc123", "indicator_count": 330050},
        db_path=Path("unused.db"),
        seed=20260516,
        dataset_tier="cert_holdout",
        holdout_split="quality-test",
        session_id_prefix="quality",
        allow_provider_cap_truncation=False,
    )

    assert supportability_excluded == 0
    assert inventory == []
    assert quality_excluded == 1
    assert [record["origin"]["source_indicator_code"] for record in records] == ["OK1", "OK2"]
    assert all(record["provenance"]["query_quality_risk"] == "low" for record in records)
    assert all(record["provenance"]["balanced_claim_holdout_high_risk_excluded_before_fill"] == 1 for record in records)


def test_build_direct_rows_raises_on_quality_screen_underfill_without_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "provider_catalog_count", lambda provider, db_path: 2)
    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: [_row(1, "BAD"), _row(2, "OK")])
    monkeypatch.setattr(module, "build_direct_record", _record)
    monkeypatch.setattr(
        module,
        "audit_direct_query_shape",
        lambda record: {
            "risk_level": "high" if (record["origin"]["source_indicator_code"] == "BAD") else "low",
            "reasons": ["synthetic_high"] if (record["origin"]["source_indicator_code"] == "BAD") else [],
        },
    )

    with pytest.raises(RuntimeError, match="could not fill FRED target 2 after high-risk query exclusion"):
        module.build_direct_rows(
            provider="FRED",
            count=2,
            snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc123", "indicator_count": 330050},
            db_path=Path("unused.db"),
            seed=20260516,
            dataset_tier="cert_holdout",
            holdout_split="quality-test",
            session_id_prefix="quality",
            allow_provider_cap_truncation=False,
        )


def test_selection_supportability_reason_marks_unimplemented_bis_dataflows() -> None:
    from scripts.validation.common import selection_supportability_reason_for_row

    newly_supported_cpmi = {
        "provider_stratum": "BIS",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "BIS",
            "source_indicator_code": "BIS_WS_CPMI_CASHLESS",
            "name": "CPMI cashless payments",
            "category": "BIS Dataflow",
        },
    }
    supported = {
        "provider_stratum": "BIS",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "BIS",
            "source_indicator_code": "BIS_WS_CBPOL",
            "name": "Central bank policy rates",
            "category": "BIS Dataflow",
        },
    }
    exact_code_only_cpmi_systems = {
        "provider_stratum": "BIS",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "BIS",
            "source_indicator_code": "BIS_WS_CPMI_SYSTEMS",
            "name": "CPMI systems",
            "category": "BIS Dataflow",
        },
    }
    unsupported = {
        "provider_stratum": "BIS",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "BIS",
            "source_indicator_code": "BIS_WS_CBS_PUB",
            "name": "Consolidated banking",
            "category": "BIS Dataflow",
        },
    }
    release_calendar = {
        "provider_stratum": "BIS",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "BIS",
            "source_indicator_code": "BIS_REL_CAL",
            "name": "BIS_RELEASE_CALENDAR",
            "category": "BIS Statistics",
        },
    }

    assert selection_supportability_reason_for_row(supported) is None
    assert selection_supportability_reason_for_row(newly_supported_cpmi) is None
    assert selection_supportability_reason_for_row(exact_code_only_cpmi_systems) == "bis_runtime_dataflow_not_implemented"
    assert selection_supportability_reason_for_row(unsupported) == "bis_runtime_dataflow_not_implemented"
    assert selection_supportability_reason_for_row(release_calendar) == "bis_release_calendar_not_runtime_supported"


def test_bis_supportability_allowlist_remains_per_dataflow_evidence_gated() -> None:
    from scripts.validation.common import selection_supportability_reason_for_row

    newly_allowed = [
        ("BIS_WS_CPMI_CASHLESS", "CPMI cashless payments"),
        ("BIS_WS_CPMI_CT1", "CPMI comparative tables type 1"),
        ("BIS_WS_CPMI_CT2", "CPMI comparative tables type 2"),
        ("BIS_WS_CPMI_DEVICES", "CPMI payment devices"),
        ("BIS_WS_CPMI_INSTITUT", "CPMI institutions"),
        ("BIS_WS_CPMI_MACRO", "CPMI macro"),
        ("BIS_WS_CPMI_PARTICIP", "CPMI participants"),
        ("BIS_WS_DER_OTC_TOV", "OTC derivatives turnover"),
        ("BIS_WS_NA_SEC_DSS", "Debt securities statistics"),
    ]
    still_excluded = [
        ("BIS_REL_CAL", "BIS_RELEASE_CALENDAR", "bis_release_calendar_not_runtime_supported"),
        ("BIS_WS_CBS_PUB", "Consolidated banking", "bis_runtime_dataflow_not_implemented"),
        ("BIS_WS_CPMI_SYSTEMS", "CPMI systems", "bis_runtime_dataflow_not_implemented"),
        ("BIS_WS_LBS_D_PUB", "Locational banking", "bis_runtime_dataflow_not_implemented"),
        ("BIS_WS_NA_SEC_C3", "Debt securities C3", "bis_runtime_dataflow_not_implemented"),
        ("BIS_WS_OTC_DERIV2", "OTC derivatives", "bis_runtime_dataflow_not_implemented"),
        ("BIS_WS_XTD_DERIV", "Exchange-traded derivatives", "bis_runtime_dataflow_not_implemented"),
        ("CBS", "Consolidated Banking Statistics", "bis_runtime_dataflow_not_implemented"),
        ("LBSN", "Locational Banking Statistics by Nationality", "bis_runtime_dataflow_not_implemented"),
        ("LBSR", "Locational Banking Statistics by Residence", "bis_runtime_dataflow_not_implemented"),
    ]

    for code, name in newly_allowed:
        record = {
            "provider_stratum": "BIS",
            "evaluation_target": "user_answerability",
            "origin": {
                "source_provider": "BIS",
                "source_indicator_code": code,
                "name": name,
                "category": "BIS Dataflow",
            },
        }
        assert selection_supportability_reason_for_row(record) is None

    for code, name, reason in still_excluded:
        record = {
            "provider_stratum": "BIS",
            "evaluation_target": "user_answerability",
            "origin": {
                "source_provider": "BIS",
                "source_indicator_code": code,
                "name": name,
                "category": "BIS Dataflow",
            },
        }
        assert selection_supportability_reason_for_row(record) == reason


def test_selection_supportability_reason_marks_oecd_runtime_alias_dataflows() -> None:
    from scripts.validation.common import selection_supportability_reason_for_row

    stale_aliases = [
        "OECD_SEEA_AEA_A",
        "OECD_DF_SDG_GLC",
        "OECD_DF_SDG_GLH",
    ]
    supported_exact_dataflows = [
        "DSD_AEA@DF_AEA",
        "OECD_DSD_AEA@DF_AEA",
        "DSD_SDG@DF_SDG",
        "OECD_DSD_EAG_EARNINGS@AV_AN_WAGE",
        "OECD.SDD.NAD,DSD_NASEC10@DF_QNA_EXPENDITURE_INST",
    ]

    for code in stale_aliases:
        record = {
            "provider_stratum": "OECD",
            "evaluation_target": "user_answerability",
            "origin": {
                "source_provider": "OECD",
                "source_indicator_code": code,
                "name": "OECD catalog dataflow",
                "category": "OECD Dataflow",
                "raw_metadata": json.dumps({"type": "oecd_dataset"}),
            },
        }
        assert selection_supportability_reason_for_row(record) == "oecd_runtime_dataflow_alias_not_supported"

    for code in supported_exact_dataflows:
        record = {
            "provider_stratum": "OECD",
            "evaluation_target": "user_answerability",
            "origin": {
                "source_provider": "OECD",
                "source_indicator_code": code,
                "name": "OECD runtime-shaped dataflow",
                "category": "OECD Dataflow",
                "raw_metadata": json.dumps({"type": "oecd_dataset"}),
            },
        }
        assert selection_supportability_reason_for_row(record) is None


def test_selection_supportability_reason_marks_non_executable_coingecko_assets() -> None:
    from scripts.validation.common import selection_supportability_reason_for_row

    unsupported = {
        "provider_stratum": "CoinGecko",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "CoinGecko",
            "source_indicator_code": "_",
            "name": "༼ つ ◕_◕ ༽つ",
            "category": "Cryptocurrency",
            "raw_metadata": json.dumps({"id": "_", "symbol": "gib", "name": "༼ つ ◕_◕ ༽つ"}),
        },
    }
    supported_numeric_slug = {
        "provider_stratum": "CoinGecko",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "CoinGecko",
            "source_indicator_code": "01-token",
            "name": "01",
            "category": "Cryptocurrency",
            "raw_metadata": json.dumps({"id": "01-token", "symbol": "01", "name": "01"}),
        },
    }
    supported_display_name = {
        "provider_stratum": "CoinGecko",
        "evaluation_target": "user_answerability",
        "origin": {
            "source_provider": "CoinGecko",
            "source_indicator_code": "cat-4",
            "name": "#cat",
            "category": "Cryptocurrency",
            "raw_metadata": json.dumps({"id": "cat-4", "symbol": "#cat", "name": "#cat"}),
        },
    }

    assert selection_supportability_reason_for_row(unsupported) == "coingecko_non_executable_asset_slug"
    assert selection_supportability_reason_for_row(supported_numeric_slug) is None
    assert selection_supportability_reason_for_row(supported_display_name) is None


def test_coingecko_price_supportability_reason_marks_missing_usd_metric() -> None:
    from backend.utils.coingecko_supportability import coingecko_catalog_price_supportability_reason

    assert (
        coingecko_catalog_price_supportability_reason(
            code="0vix-protocol",
            simple_price_payload={"0vix-protocol": {}},
            vs_currency="usd",
        )
        == "coingecko_current_price_unavailable"
    )
    assert (
        coingecko_catalog_price_supportability_reason(
            code="000-capital",
            simple_price_payload={"000-capital": {"usd": 1.23}},
            vs_currency="usd",
        )
        is None
    )


def test_build_direct_rows_excludes_bis_supportability_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    bis_rows = [
        {
            **_row(1, "BIS_REL_CAL"),
            "provider": "BIS",
            "name": "BIS_RELEASE_CALENDAR",
            "category": "BIS Statistics",
        },
        {
            **_row(2, "BIS_WS_CBPOL"),
            "provider": "BIS",
            "name": "Central bank policy rates",
            "category": "BIS Dataflow",
        },
    ]
    monkeypatch.setattr(module, "provider_catalog_count", lambda provider, db_path: 2)
    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: bis_rows)
    monkeypatch.setattr(module, "audit_direct_query_shape", lambda record: {"risk_level": "low", "reasons": []})

    records, supportability_excluded, quality_excluded, inventory = module.build_direct_rows(
        provider="BIS",
        count=1,
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc123", "indicator_count": 330050},
        db_path=Path("unused.db"),
        seed=20260516,
        dataset_tier="cert_holdout",
        holdout_split="supportability-test",
        session_id_prefix="supportability",
        allow_provider_cap_truncation=False,
    )

    assert supportability_excluded == 1
    assert quality_excluded == 0
    assert len(inventory) == 1
    assert inventory[0]["supportability_reason"] == "bis_release_calendar_not_runtime_supported"
    assert [record["origin"]["source_indicator_code"] for record in records] == ["BIS_WS_CBPOL"]
    assert records[0]["provenance"]["balanced_claim_holdout_supportability_excluded_before_fill"] == 1


def test_build_direct_rows_excludes_coingecko_supportability_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    coingecko_rows = [
        {
            **_row(1, "_"),
            "provider": "CoinGecko",
            "name": "༼ つ ◕_◕ ༽つ",
            "category": "Cryptocurrency",
            "raw_metadata": json.dumps({"id": "_", "symbol": "gib", "name": "༼ つ ◕_◕ ༽つ"}),
        },
        {
            **_row(2, "01-token"),
            "provider": "CoinGecko",
            "name": "01",
            "category": "Cryptocurrency",
            "raw_metadata": json.dumps({"id": "01-token", "symbol": "01", "name": "01"}),
        },
    ]
    monkeypatch.setattr(module, "provider_catalog_count", lambda provider, db_path: 2)
    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: coingecko_rows)
    monkeypatch.setattr(module, "audit_direct_query_shape", lambda record: {"risk_level": "low", "reasons": []})

    records, supportability_excluded, quality_excluded, inventory = module.build_direct_rows(
        provider="CoinGecko",
        count=1,
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc123", "indicator_count": 330050},
        db_path=Path("unused.db"),
        seed=20260516,
        dataset_tier="cert_holdout",
        holdout_split="supportability-test",
        session_id_prefix="supportability",
        allow_provider_cap_truncation=False,
    )

    assert supportability_excluded == 1
    assert quality_excluded == 0
    assert len(inventory) == 1
    assert inventory[0]["supportability_reason"] == "coingecko_non_executable_asset_slug"
    assert [record["origin"]["source_indicator_code"] for record in records] == ["01-token"]
    assert records[0]["provenance"]["balanced_claim_holdout_supportability_excluded_before_fill"] == 1


def test_build_direct_rows_excludes_coingecko_current_price_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    coingecko_rows = [
        {
            **_row(1, "0vix-protocol"),
            "provider": "CoinGecko",
            "name": "0VIX Protocol",
            "category": "Cryptocurrency",
            "raw_metadata": json.dumps({"id": "0vix-protocol", "symbol": "vix", "name": "0VIX Protocol"}),
        },
        {
            **_row(2, "000-capital"),
            "provider": "CoinGecko",
            "name": "000 Capital",
            "category": "Cryptocurrency",
            "raw_metadata": json.dumps({"id": "000-capital", "symbol": "000", "name": "000 Capital"}),
        },
    ]
    monkeypatch.setattr(module, "provider_catalog_count", lambda provider, db_path: 2)
    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: coingecko_rows)
    monkeypatch.setattr(module, "audit_direct_query_shape", lambda record: {"risk_level": "low", "reasons": []})
    monkeypatch.setattr(
        module,
        "coingecko_price_unavailability_by_code",
        lambda rows, **_kwargs: {"0vix-protocol": "coingecko_current_price_unavailable"},
    )

    records, supportability_excluded, quality_excluded, inventory = module.build_direct_rows(
        provider="CoinGecko",
        count=1,
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc123", "indicator_count": 330050},
        db_path=Path("unused.db"),
        seed=20260516,
        dataset_tier="cert_holdout",
        holdout_split="supportability-test",
        session_id_prefix="supportability",
        allow_provider_cap_truncation=False,
    )

    assert supportability_excluded == 1
    assert quality_excluded == 0
    assert len(inventory) == 1
    assert inventory[0]["supportability_reason"] == "coingecko_current_price_unavailable"
    assert [record["origin"]["source_indicator_code"] for record in records] == ["000-capital"]


def test_comtrade_export_unavailability_probe_marks_exact_zero_observation_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            **_row(1, "010119"),
            "provider": "Comtrade",
            "name": "010119 - Horses; live, other than pure-bred breeding animals",
            "category": "HS Subheading",
        },
        {
            **_row(2, "010514"),
            "provider": "Comtrade",
            "name": "010514 - Poultry; live, geese, weighing not more than 185g",
            "category": "HS Subheading",
        },
    ]

    monkeypatch.setattr(module, "_comtrade_period_chunks", lambda: ["2022,2023"])

    def fake_available(*, reporter_code: str, cmd_code: str, api_key: str, period_chunks: list[str]) -> bool:
        assert period_chunks == ["2022,2023"]
        assert reporter_code
        assert api_key == "test-key"
        return cmd_code == "010514"

    monkeypatch.setattr(module, "_comtrade_export_observations_available", fake_available)

    unavailable = module.comtrade_export_unavailability_by_key(rows, api_key="test-key")

    assert module.COMTRADE_SUPPORTABILITY_REASON in unavailable.values()
    assert len(unavailable) == 1
    assert next(iter(unavailable)).endswith(":010119")


def test_comtrade_export_unavailability_reuses_completed_exact_artifact_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        {
            **_row(1, "010119"),
            "provider": "Comtrade",
            "name": "010119 - Horses; live, other than pure-bred breeding animals",
            "category": "HS Subheading",
        },
        {
            **_row(2, "010514"),
            "provider": "Comtrade",
            "name": "010514 - Poultry; live, geese, weighing not more than 185g",
            "category": "HS Subheading",
        },
    ]
    period_chunks = ["2022,2023"]
    monkeypatch.setattr(module, "_comtrade_period_chunks", lambda: period_chunks)
    artifact_path = tmp_path / "comtrade_export_supportability_probe.json"
    cached_key = module._comtrade_probe_key(rows[0])
    reporter_code, cmd_code = cached_key.split(":", 1)
    artifact_path.write_text(
        json.dumps(
            module._comtrade_supportability_base_artifact(
                requested_pair_count=1,
                period_chunks=period_chunks,
                rows=[
                    module._comtrade_artifact_row(
                        key=cached_key,
                        reporter_code=reporter_code,
                        cmd_code=cmd_code,
                        row=rows[0],
                        period_chunks=period_chunks,
                        status="completed",
                        available=False,
                    )
                ],
            )
        )
        + "\n",
        encoding="utf-8",
    )

    probed_codes: list[str] = []

    def fake_available(*, reporter_code: str, cmd_code: str, api_key: str, period_chunks: list[str]) -> bool:
        probed_codes.append(cmd_code)
        return True

    monkeypatch.setattr(module, "_comtrade_export_observations_available", fake_available)

    unavailable = module.comtrade_export_unavailability_by_key(
        rows,
        artifact_path=artifact_path,
        api_key="test-key",
    )

    assert unavailable == {cached_key: module.COMTRADE_SUPPORTABILITY_REASON}
    assert probed_codes == ["010514"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["completed_pair_count"] == 2
    assert artifact["unavailable_pair_count"] == 1
    assert artifact["unprobed_pair_count"] == 0
    assert artifact["reused_pair_count"] == 1
    assert artifact["rows"][0]["reused_from_artifact"] is True


def test_comtrade_export_unavailability_rejects_mismatched_cache_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        {
            **_row(1, "010119"),
            "provider": "Comtrade",
            "name": "010119 - Horses; live, other than pure-bred breeding animals",
            "category": "HS Subheading",
        }
    ]
    current_period_chunks = ["2022,2023"]
    monkeypatch.setattr(module, "_comtrade_period_chunks", lambda: current_period_chunks)
    artifact_path = tmp_path / "comtrade_export_supportability_probe.json"
    cached_key = module._comtrade_probe_key(rows[0])
    reporter_code, cmd_code = cached_key.split(":", 1)
    artifact_path.write_text(
        json.dumps(
            module._comtrade_supportability_base_artifact(
                requested_pair_count=1,
                period_chunks=["2010,2011"],
                rows=[
                    module._comtrade_artifact_row(
                        key=cached_key,
                        reporter_code=reporter_code,
                        cmd_code=cmd_code,
                        row=rows[0],
                        period_chunks=["2010,2011"],
                        status="completed",
                        available=False,
                    )
                ],
            )
        )
        + "\n",
        encoding="utf-8",
    )

    probed_codes: list[str] = []

    def fake_available(*, reporter_code: str, cmd_code: str, api_key: str, period_chunks: list[str]) -> bool:
        probed_codes.append(cmd_code)
        assert period_chunks == current_period_chunks
        return True

    monkeypatch.setattr(module, "_comtrade_export_observations_available", fake_available)

    unavailable = module.comtrade_export_unavailability_by_key(
        rows,
        artifact_path=artifact_path,
        api_key="test-key",
    )

    assert unavailable == {}
    assert probed_codes == ["010119"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["reused_pair_count"] == 0
    assert artifact["available_pair_count"] == 1
    assert artifact["unprobed_pair_count"] == 0
    assert artifact["period_chunks"] == current_period_chunks


def test_comtrade_export_unavailability_quota_writes_partial_artifact_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        {
            **_row(1, "010119"),
            "provider": "Comtrade",
            "name": "010119 - Horses; live, other than pure-bred breeding animals",
            "category": "HS Subheading",
        },
        {
            **_row(2, "010514"),
            "provider": "Comtrade",
            "name": "010514 - Poultry; live, geese, weighing not more than 185g",
            "category": "HS Subheading",
        },
    ]
    monkeypatch.setattr(module, "_comtrade_period_chunks", lambda: ["2022,2023"])
    artifact_path = tmp_path / "comtrade_export_supportability_probe.json"
    calls = 0

    def fake_available(*, reporter_code: str, cmd_code: str, api_key: str, period_chunks: list[str]) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        raise urllib.error.HTTPError(
            url="https://comtradeapi.un.org/data/v1/get/C/A/HS",
            code=403,
            msg="Quota Exceeded",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(module, "_comtrade_export_observations_available", fake_available)

    with pytest.raises(RuntimeError, match="Comtrade export supportability probe failed"):
        module.comtrade_export_unavailability_by_key(
            rows,
            artifact_path=artifact_path,
            api_key="test-key",
        )

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "blocked"
    assert artifact["completed_pair_count"] == 1
    assert artifact["unavailable_pair_count"] == 1
    assert artifact["blocked_pair_count"] == 1
    assert artifact["unprobed_pair_count"] == 0
    assert artifact["rows"][0]["status"] == "completed"
    assert artifact["rows"][0]["supportability_reason"] == module.COMTRADE_SUPPORTABILITY_REASON
    assert artifact["rows"][1]["status"] == "blocked"
    assert artifact["rows"][1]["available"] is None
    assert artifact["rows"][1]["supportability_reason"] is None
    assert artifact["rows"][1]["error_class"] == "HTTPError"


def test_comtrade_probe_manifest_metadata_discloses_resume_counts(tmp_path: Path) -> None:
    rows = [
        {
            **_row(1, "010119"),
            "provider": "Comtrade",
            "name": "010119 - Horses; live, other than pure-bred breeding animals",
            "category": "HS Subheading",
        }
    ]
    key = module._comtrade_probe_key(rows[0])
    reporter_code, cmd_code = key.split(":", 1)
    artifact_path = tmp_path / "comtrade_export_supportability_probe.json"
    module._write_json_atomic(
        artifact_path,
        module._comtrade_supportability_base_artifact(
            requested_pair_count=1,
            period_chunks=["2022,2023"],
            reused_pair_count=1,
            rows=[
                module._comtrade_artifact_row(
                    key=key,
                    reporter_code=reporter_code,
                    cmd_code=cmd_code,
                    row=rows[0],
                    period_chunks=["2022,2023"],
                    status="completed",
                    available=False,
                    reused_from_artifact=True,
                )
            ],
        ),
    )

    metadata = module.comtrade_probe_manifest_metadata(artifact_path)

    assert metadata is not None
    assert metadata["artifact_path"] == str(artifact_path)
    assert metadata["artifact_sha256"] == module.file_sha256(artifact_path)
    assert metadata["artifact_version"] == module.COMTRADE_SUPPORTABILITY_ARTIFACT_VERSION
    assert metadata["status"] == "complete"
    assert metadata["requested_pair_count"] == 1
    assert metadata["completed_pair_count"] == 1
    assert metadata["unavailable_pair_count"] == 1
    assert metadata["blocked_pair_count"] == 0
    assert metadata["unprobed_pair_count"] == 0
    assert metadata["reused_pair_count"] == 1


def test_build_direct_rows_excludes_comtrade_supportability_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    comtrade_rows = [
        {
            **_row(1, "010119"),
            "provider": "Comtrade",
            "name": "010119 - Horses; live, other than pure-bred breeding animals",
            "category": "HS Subheading",
        },
        {
            **_row(2, "010514"),
            "provider": "Comtrade",
            "name": "010514 - Poultry; live, geese, weighing not more than 185g",
            "category": "HS Subheading",
        },
    ]
    monkeypatch.setattr(module, "provider_catalog_count", lambda provider, db_path: 2)
    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: comtrade_rows)
    monkeypatch.setattr(module, "audit_direct_query_shape", lambda record: {"risk_level": "low", "reasons": []})
    monkeypatch.setattr(
        module,
        "comtrade_export_unavailability_by_key",
        lambda rows, **_kwargs: {
            module._comtrade_probe_key(rows[0]): module.COMTRADE_SUPPORTABILITY_REASON
        },
    )

    records, supportability_excluded, quality_excluded, inventory = module.build_direct_rows(
        provider="Comtrade",
        count=1,
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc123", "indicator_count": 330050},
        db_path=Path("unused.db"),
        seed=20260516,
        dataset_tier="cert_holdout",
        holdout_split="supportability-test",
        session_id_prefix="supportability",
        allow_provider_cap_truncation=False,
        probe_comtrade_supportability=True,
    )

    assert supportability_excluded == 1
    assert quality_excluded == 0
    assert len(inventory) == 1
    assert inventory[0]["supportability_reason"] == module.COMTRADE_SUPPORTABILITY_REASON
    assert [record["origin"]["source_indicator_code"] for record in records] == ["010514"]
    assert records[0]["provenance"]["balanced_claim_holdout_supportability_excluded_before_fill"] == 1


def test_build_direct_rows_fails_closed_when_comtrade_probe_scope_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    comtrade_rows = [
        {
            **_row(1, "010119"),
            "provider": "Comtrade",
            "name": "010119 - Horses; live, other than pure-bred breeding animals",
            "category": "HS Subheading",
        },
        {
            **_row(2, "010514"),
            "provider": "Comtrade",
            "name": "010514 - Poultry; live, geese, weighing not more than 185g",
            "category": "HS Subheading",
        },
    ]
    monkeypatch.setattr(module, "provider_catalog_count", lambda provider, db_path: 2)
    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: comtrade_rows)
    monkeypatch.setattr(module, "audit_direct_query_shape", lambda record: {"risk_level": "low", "reasons": []})
    monkeypatch.setattr(module, "comtrade_export_unavailability_by_key", lambda rows, **_kwargs: {})

    with pytest.raises(RuntimeError, match="Comtrade supportability probe scope exhausted"):
        module.build_direct_rows(
            provider="Comtrade",
            count=2,
            snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc123", "indicator_count": 330050},
            db_path=Path("unused.db"),
            seed=20260516,
            dataset_tier="cert_holdout",
            holdout_split="supportability-test",
            session_id_prefix="supportability",
            allow_provider_cap_truncation=False,
            probe_comtrade_supportability=True,
            max_comtrade_probe_rows=1,
        )


def test_build_direct_rows_excludes_oecd_supportability_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    oecd_rows = [
        {
            **_row(1, "OECD_DF_SDG_GLC"),
            "provider": "OECD",
            "name": "SDG Country Global Dataflow",
            "category": "OECD Dataflow",
            "raw_metadata": json.dumps({"type": "oecd_dataset"}),
        },
        {
            **_row(2, "DSD_SDG@DF_SDG"),
            "provider": "OECD",
            "name": "Sustainable Development Goals",
            "category": "OECD Dataflow",
            "raw_metadata": json.dumps({"type": "oecd_dataset"}),
        },
    ]
    monkeypatch.setattr(module, "provider_catalog_count", lambda provider, db_path: 2)
    monkeypatch.setattr(module, "sample_indicator_rows", lambda *args, **kwargs: oecd_rows)
    monkeypatch.setattr(module, "audit_direct_query_shape", lambda record: {"risk_level": "low", "reasons": []})

    records, supportability_excluded, quality_excluded, inventory = module.build_direct_rows(
        provider="OECD",
        count=1,
        snapshot_meta={"snapshot_date": "2026-04-14", "git_sha": "abc123", "indicator_count": 330050},
        db_path=Path("unused.db"),
        seed=20260516,
        dataset_tier="cert_holdout",
        holdout_split="supportability-test",
        session_id_prefix="supportability",
        allow_provider_cap_truncation=False,
    )

    assert supportability_excluded == 1
    assert quality_excluded == 0
    assert len(inventory) == 1
    assert inventory[0]["supportability_reason"] == "oecd_runtime_dataflow_alias_not_supported"
    assert [record["origin"]["source_indicator_code"] for record in records] == ["DSD_SDG@DF_SDG"]
    assert records[0]["provenance"]["balanced_claim_holdout_supportability_excluded_before_fill"] == 1
