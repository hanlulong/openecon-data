from __future__ import annotations

from scripts.validation.common import (
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    apply_selection_supportability_probe_query,
    selection_supportability_reason_for_row,
)
from backend.utils.imf_supportability import imf_exact_provider_surface_supportability_reason
from scripts.validation.materialize_next_review_batch import select_quality_screened_direct_records
from scripts.validation.sample_direct_cert_set import _select_quality_screened_records


UNSUPPORTED_IMF_REASON = "imf_non_weo_public_surface_unsupported"
UNSUPPORTED_IMF_DATAMAPPER_CATEGORY_REASON = "imf_public_datamapper_v1_category_not_served"
UNSUPPORTED_IMF_DATAFLOW_DIRECT_SERIES_REASON = "imf_catalog_dataflow_not_single_series"


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


def test_selection_supportability_demotes_only_evidence_backed_imf_datamapper_category() -> None:
    alt_fiscal = _imf_record(
        row_id="alt-fiscal",
        code="LS_NFA_09",
        name=(
            "Fiscal, Central Government, Net Worth and its Changes, "
            "Nonfinancial assets, Fixed Assets"
        ),
        category="ALT_FISCAL",
    )
    aipi = _imf_record(
        row_id="aipi",
        code="DI",
        name="Digital Infrastructure",
        category="AIPI",
    )
    fpp = _imf_record(
        row_id="fpp",
        code="prim_exp",
        name="Government primary expenditure, percent of GDP",
        category="FPP",
    )
    cf = _imf_record(
        row_id="cf",
        code="PrivInexDIGDP",
        name="Private Inflows excluding Direct Investment (% of GDP)",
        category="CF",
    )
    sprlu = _imf_record(
        row_id="sprlu",
        code="SITC1_2",
        name="Crude materials, inedible, except fuels",
        category="SPRLU",
    )

    assert selection_supportability_reason_for_row(alt_fiscal) == UNSUPPORTED_IMF_DATAMAPPER_CATEGORY_REASON
    assert selection_supportability_reason_for_row(aipi) is None
    assert selection_supportability_reason_for_row(fpp) is None
    assert selection_supportability_reason_for_row(cf) is None
    assert selection_supportability_reason_for_row(sprlu) is None


def test_selection_supportability_demotes_imf_dataflow_descriptors_only() -> None:
    dataflow = _imf_record(
        row_id="dataflow",
        code="DF:IMTS_M",
        name="Dataset: IMTS Monthly",
        category="Dataflow",
    )
    fpp = _imf_record(
        row_id="fpp",
        code="prim_exp",
        name="Government primary expenditure, percent of GDP",
        category="FPP",
    )

    assert selection_supportability_reason_for_row(dataflow) == UNSUPPORTED_IMF_DATAFLOW_DIRECT_SERIES_REASON
    assert selection_supportability_reason_for_row(fpp) is None


def test_alt_fiscal_sampler_prior_is_not_runtime_supportability_block() -> None:
    assert imf_exact_provider_surface_supportability_reason(
        "LS_NFA_09 from IMF",
        params={
            "__semantic_indicator_label": (
                "Fiscal, Central Government, Net Worth and its Changes, "
                "Nonfinancial assets, Fixed Assets"
            ),
            "__semantic_indicator_category": "ALT_FISCAL",
        },
    ) is None


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


def test_next_review_selection_excludes_supportability_rows_by_default() -> None:
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

    selected = select_quality_screened_direct_records(
        [unsupported_hs, supported_cpi],
        2,
    )

    assert [row["id"] for row in selected] == ["supported-cpi"]


def test_next_review_selection_can_include_supportability_probe_rows_explicitly() -> None:
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

    selected = select_quality_screened_direct_records(
        [unsupported_hs, supported_cpi],
        2,
        include_supportability_probes=True,
    )

    assert [row["id"] for row in selected] == ["supported-cpi", "unsupported-hs"]


def test_apply_selection_supportability_probe_query_preserves_original_prompt() -> None:
    unsupported_hs = _imf_record(
        row_id="unsupported-hs",
        code="NXG_H5_XII_FOB_USD",
        name="National Accounts, External Sector, Exports of Goods, HS 2017 Section XII",
        selection_supportability_reason=UNSUPPORTED_IMF_REASON,
    )

    apply_selection_supportability_probe_query(unsupported_hs)

    assert unsupported_hs["query"] == "NXG_H5_XII_FOB_USD from IMF"
    assert unsupported_hs["provenance"]["supportability_probe_query"] == "imf_exact_provider_code"
    assert unsupported_hs["provenance"]["original_user_answerability_query"].startswith(
        "Brazil National Accounts"
    )


def test_next_review_selection_keeps_datamapper_positive_controls_ahead_of_alt_fiscal() -> None:
    alt_fiscal = _imf_record(
        row_id="alt-fiscal",
        code="LS_NFA_09",
        name="Fiscal, Central Government, Net Worth and its Changes, Nonfinancial assets",
        category="ALT_FISCAL",
        selection_supportability_reason=UNSUPPORTED_IMF_DATAMAPPER_CATEGORY_REASON,
    )
    fpp = _imf_record(
        row_id="fpp",
        code="prim_exp",
        name="Government primary expenditure, percent of GDP",
        category="FPP",
    )

    selected = select_quality_screened_direct_records([alt_fiscal, fpp], 1)

    assert [row["id"] for row in selected] == ["fpp"]


def test_next_review_selection_keeps_single_series_controls_ahead_of_dataflow_descriptors() -> None:
    dataflow = _imf_record(
        row_id="dataflow",
        code="DF:IMTS_M",
        name="Dataset: IMTS Monthly",
        category="Dataflow",
        selection_supportability_reason=UNSUPPORTED_IMF_DATAFLOW_DIRECT_SERIES_REASON,
    )
    fpp = _imf_record(
        row_id="fpp",
        code="prim_exp",
        name="Government primary expenditure, percent of GDP",
        category="FPP",
    )

    selected = select_quality_screened_direct_records([dataflow, fpp], 1)

    assert [row["id"] for row in selected] == ["fpp"]


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


def test_direct_sampler_selection_excludes_supportability_rows_by_default() -> None:
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

    selected = _select_quality_screened_records(
        [unsupported_hs, weo_anchor],
        2,
    )

    assert [row["id"] for row in selected] == ["weo-anchor"]


def test_direct_sampler_selection_can_include_supportability_probe_rows_explicitly() -> None:
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

    selected = _select_quality_screened_records(
        [unsupported_hs, weo_anchor],
        2,
        include_supportability_probes=True,
    )

    assert [row["id"] for row in selected] == ["weo-anchor", "unsupported-hs"]
