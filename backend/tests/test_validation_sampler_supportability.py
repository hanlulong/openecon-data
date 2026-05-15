from __future__ import annotations

from scripts.validation.common import (
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    selection_supportability_reason_for_row,
)
from scripts.validation.materialize_next_review_batch import select_quality_screened_direct_records
from scripts.validation.sample_direct_cert_set import _select_quality_screened_records


UNSUPPORTED_IMF_REASON = "imf_non_weo_public_surface_unsupported"


def _imf_record(
    *,
    row_id: str,
    code: str,
    name: str,
    category: str = "INDICATOR",
    selection_supportability_reason: str | None = None,
    anchor_reason: str | None = None,
) -> dict:
    provenance = {
        "certification_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
        "query_quality_risk": "low",
        "query_quality_reasons": [],
        "selection_quality_reasons": [],
    }
    if selection_supportability_reason:
        provenance["selection_supportability_reason"] = selection_supportability_reason
    if anchor_reason:
        provenance["user_answerability_sampling_anchor"] = anchor_reason
    return {
        "id": row_id,
        "evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY,
        "provider_stratum": "IMF",
        "query": f"Brazil {name} from IMF",
        "origin": {
            "source_provider": "IMF",
            "source_indicator_code": code,
            "name": name,
            "category": category,
            "popularity": 1,
        },
        "provenance": provenance,
        "gold": {"evaluation_target": CERTIFICATION_TARGET_USER_ANSWERABILITY},
    }


def test_selection_supportability_reason_uses_exact_imf_metadata_only() -> None:
    unsupported_hs = _imf_record(
        row_id="unsupported-hs",
        code="NXG_H5_XII_FOB_USD",
        name="National Accounts, External Sector, Exports of Goods, HS 2017 Section XII",
    )
    supported_cpi = _imf_record(
        row_id="supported-cpi",
        code="PCPI_CP_01_BY2015M12_IX",
        name="Prices, Consumer Prices, Food and non-alcoholic beverages, BY2015, Index",
    )
    weo_anchor = _imf_record(
        row_id="weo-anchor",
        code="NGDPD",
        name="Gross domestic product, current prices, U.S. dollars",
        category="WEO",
    )
    non_imf = {
        "id": "fred",
        "provider_stratum": "FRED",
        "origin": {
            "source_provider": "FRED",
            "source_indicator_code": "GDP",
            "name": "Gross Domestic Product",
        },
    }

    assert selection_supportability_reason_for_row(unsupported_hs) == UNSUPPORTED_IMF_REASON
    assert selection_supportability_reason_for_row(supported_cpi) is None
    assert selection_supportability_reason_for_row(weo_anchor) is None
    assert selection_supportability_reason_for_row(non_imf) is None


def test_next_review_selection_demotes_unsupported_imf_surfaces() -> None:
    supported_cpi = _imf_record(
        row_id="supported-cpi",
        code="PCPI_CP_01_BY2015M12_IX",
        name="Prices, Consumer Prices, Food and non-alcoholic beverages, BY2015, Index",
        anchor_reason="imf_provider_native_sdmx_cpi_aggregate",
    )
    unsupported_hs = _imf_record(
        row_id="unsupported-hs",
        code="NXG_H5_XII_FOB_USD",
        name="National Accounts, External Sector, Exports of Goods, HS 2017 Section XII",
        selection_supportability_reason=UNSUPPORTED_IMF_REASON,
    )
    unsupported_bop = _imf_record(
        row_id="unsupported-bop",
        code="BXISOPT_BP6_USD",
        name="Balance of Payments, Current Account, Secondary Income, Personal transfers, Credit [BPM6]",
        selection_supportability_reason=UNSUPPORTED_IMF_REASON,
    )

    selected = select_quality_screened_direct_records(
        [unsupported_hs, unsupported_bop, supported_cpi],
        1,
    )

    assert [row["id"] for row in selected] == ["supported-cpi"]


def test_direct_sampler_selection_demotes_unsupported_imf_surfaces() -> None:
    weo_anchor = _imf_record(
        row_id="weo-anchor",
        code="NGDPD",
        name="Gross domestic product, current prices, U.S. dollars",
        category="WEO",
        anchor_reason="imf_provider_native_weo_surface",
    )
    unsupported_hs = _imf_record(
        row_id="unsupported-hs",
        code="NXG_H5_XII_FOB_USD",
        name="National Accounts, External Sector, Exports of Goods, HS 2017 Section XII",
        selection_supportability_reason=UNSUPPORTED_IMF_REASON,
    )
    unsupported_bop = _imf_record(
        row_id="unsupported-bop",
        code="BXISOPT_BP6_USD",
        name="Balance of Payments, Current Account, Secondary Income, Personal transfers, Credit [BPM6]",
        selection_supportability_reason=UNSUPPORTED_IMF_REASON,
    )

    selected = _select_quality_screened_records(
        [unsupported_hs, unsupported_bop, weo_anchor],
        1,
    )

    assert [row["id"] for row in selected] == ["weo-anchor"]
