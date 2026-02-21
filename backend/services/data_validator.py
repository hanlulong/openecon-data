"""
Data Value Validation Service

This module provides validation for economic data values to catch suspicious
results before they reach users. It's a fundamental architectural component
that helps ensure data accuracy across all providers.

Key validation types:
1. Range validation - values should be within expected ranges for the indicator
2. Unit detection - detect when values might be in wrong units (thousands vs actual)
3. Sanity checks - flag impossible values (negative unemployment, >100% rates, etc.)
4. Comparison validation - when multiple series exist, check for consistency
"""

import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

from ..models import NormalizedData, DataPoint

logger = logging.getLogger(__name__)


class ValidationSeverity(Enum):
    """Severity levels for validation issues"""
    INFO = "info"           # FYI - might be fine
    WARNING = "warning"     # Suspicious but possible
    ERROR = "error"         # Likely wrong data
    CRITICAL = "critical"   # Definitely wrong, should reject


@dataclass
class ValidationIssue:
    """A single validation issue found in the data"""
    severity: ValidationSeverity
    field: str
    message: str
    value: Optional[Any] = None
    expected_range: Optional[Tuple[float, float]] = None
    suggestion: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of validating a data series"""
    valid: bool
    issues: List[ValidationIssue]
    confidence: float  # 0-1 confidence that data is correct
    corrected_data: Optional[NormalizedData] = None  # If auto-correction applied


class DataValidator:
    """
    Validates economic data values for accuracy and consistency.

    This validator applies indicator-specific rules to catch common data issues:
    - Values in wrong units (e.g., millions instead of actual)
    - Impossible values (negative rates where not allowed, >100% percentages)
    - Suspicious outliers (values far from typical ranges)
    """

    # Expected ranges for common indicators (value_min, value_max, unit)
    # These are sanity check ranges, not strict limits
    INDICATOR_RANGES = {
        # GDP and economic output
        "GDP": (1e9, 100e12, "USD"),  # $1B to $100T
        "GDP_GROWTH": (-50, 50, "percent"),
        "GDP_PER_CAPITA": (100, 200000, "USD"),

        # Employment
        "UNEMPLOYMENT": (0, 50, "percent"),
        "UNEMPLOYMENT_RATE": (0, 50, "percent"),
        "EMPLOYMENT_RATE": (20, 100, "percent"),
        "LABOR_FORCE_PARTICIPATION": (20, 100, "percent"),

        # Prices and inflation
        "INFLATION": (-20, 100, "percent"),
        "CPI": (0, 500, "index"),
        "PRICE_INDEX": (0, 1000, "index"),

        # Interest rates
        "INTEREST_RATE": (-5, 50, "percent"),
        "FEDERAL_FUNDS_RATE": (-5, 30, "percent"),
        "POLICY_RATE": (-5, 50, "percent"),
        "TREASURY_YIELD": (-2, 20, "percent"),

        # Trade data
        "EXPORTS": (1e6, 5e12, "USD"),
        "IMPORTS": (1e6, 5e12, "USD"),
        "TRADE_BALANCE": (-1e12, 1e12, "USD"),

        # Property prices
        "PROPERTY_PRICE_INDEX": (0, 500, "index"),
        "HOUSE_PRICE": (10000, 10e6, "USD"),

        # Exchange rates
        "EXCHANGE_RATE": (0.0001, 10000, "rate"),

        # Crypto
        "CRYPTO_PRICE": (0.000001, 1e6, "USD"),
        "MARKET_CAP": (1e6, 5e12, "USD"),

        # Demographics
        "POPULATION": (1000, 2e10, "persons"),
        "POPULATION_GROWTH": (-5, 10, "percent"),
        "LIFE_EXPECTANCY": (30, 100, "years"),
        "LITERACY_RATE": (0, 100, "percent"),

        # Government finance
        "DEBT_TO_GDP": (0, 300, "percent"),
        "DEFICIT_TO_GDP": (-30, 30, "percent"),
        "FOREIGN_RESERVES": (1e6, 5e12, "USD"),
    }

    # Pattern matching for indicator names to categories
    INDICATOR_PATTERNS = {
        "gdp": "GDP",
        "gross domestic product": "GDP",
        "economic output": "GDP",
        "unemployment": "UNEMPLOYMENT",
        "jobless": "UNEMPLOYMENT",
        "employment rate": "EMPLOYMENT_RATE",
        "inflation": "INFLATION",
        "cpi": "CPI",
        "consumer price": "CPI",
        "interest rate": "INTEREST_RATE",
        "federal funds": "FEDERAL_FUNDS_RATE",
        "policy rate": "POLICY_RATE",
        "treasury": "TREASURY_YIELD",
        "bond yield": "TREASURY_YIELD",
        "export": "EXPORTS",
        "import": "IMPORTS",
        "trade balance": "TRADE_BALANCE",
        "property price": "PROPERTY_PRICE_INDEX",
        "house price": "PROPERTY_PRICE_INDEX",
        "real estate": "PROPERTY_PRICE_INDEX",
        "exchange rate": "EXCHANGE_RATE",
        "forex": "EXCHANGE_RATE",
        "bitcoin": "CRYPTO_PRICE",
        "ethereum": "CRYPTO_PRICE",
        "crypto": "CRYPTO_PRICE",
        "market cap": "MARKET_CAP",
        "population": "POPULATION",
        "life expectancy": "LIFE_EXPECTANCY",
        "literacy": "LITERACY_RATE",
        "debt to gdp": "DEBT_TO_GDP",
        "deficit": "DEFICIT_TO_GDP",
        "reserves": "FOREIGN_RESERVES",
        "per capita": "GDP_PER_CAPITA",
        "growth": "GDP_GROWTH",
    }

    def __init__(self):
        self.strict_mode = False  # If True, treat warnings as errors

    def validate(self, data: NormalizedData) -> ValidationResult:
        """
        Validate a single data series.

        Args:
            data: The NormalizedData to validate

        Returns:
            ValidationResult with issues found and confidence score
        """
        issues = []

        # 1. Check for empty data
        if not data.data:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.ERROR,
                field="data",
                message="No data points in series",
            ))
            return ValidationResult(valid=False, issues=issues, confidence=0)

        # 2. Extract indicator type from metadata
        indicator_type = self._detect_indicator_type(data)

        # 3. Get expected range for this indicator
        expected_range = self.INDICATOR_RANGES.get(indicator_type)

        # 4. Validate each data point
        values = [p.value for p in data.data if p.value is not None]

        if not values:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.ERROR,
                field="data",
                message="All data points have null values",
            ))
            return ValidationResult(valid=False, issues=issues, confidence=0)

        # 5. Basic statistics
        min_val = min(values)
        max_val = max(values)
        avg_val = sum(values) / len(values)

        # 6. Range validation
        if expected_range:
            range_min, range_max, unit = expected_range

            # Check if values are within expected range
            if min_val < range_min * 0.01:  # 1% of minimum is suspicious
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    field="data.value",
                    message=f"Minimum value {min_val:,.2f} is suspiciously low for {indicator_type}",
                    value=min_val,
                    expected_range=(range_min, range_max),
                    suggestion=f"Expected values in range {range_min:,.0f} to {range_max:,.0f}",
                ))

            if max_val > range_max * 100:  # 100x maximum is suspicious
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    field="data.value",
                    message=f"Maximum value {max_val:,.2f} is suspiciously high for {indicator_type}",
                    value=max_val,
                    expected_range=(range_min, range_max),
                    suggestion=f"Expected values in range {range_min:,.0f} to {range_max:,.0f}",
                ))

            # Check for potential unit mismatch (e.g., thousands vs actual)
            if avg_val < range_min * 0.001:
                # Values might be in billions but expected in millions, etc.
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    field="data.unit",
                    message=f"Values might be in wrong units. Average {avg_val:,.2f} is much lower than expected {range_min:,.0f}",
                    value=avg_val,
                    expected_range=(range_min, range_max),
                    suggestion="Check if values need to be multiplied by 1000 or 1000000",
                ))

        # 7. Percentage sanity checks
        if indicator_type in ["UNEMPLOYMENT", "INFLATION", "INTEREST_RATE", "EMPLOYMENT_RATE",
                              "LITERACY_RATE", "DEBT_TO_GDP"]:
            if max_val > 100 and indicator_type not in ["DEBT_TO_GDP", "INFLATION"]:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    field="data.value",
                    message=f"Value {max_val:,.2f}% exceeds 100% for {indicator_type}",
                    value=max_val,
                    suggestion="Check if values are actually in percentage format",
                ))

        # 8. Negative value checks
        if indicator_type in ["UNEMPLOYMENT", "POPULATION", "EXPORTS", "IMPORTS",
                              "MARKET_CAP", "GDP"]:
            if min_val < 0:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    field="data.value",
                    message=f"Negative value {min_val:,.2f} found for {indicator_type} which cannot be negative",
                    value=min_val,
                ))

        # 9. Calculate confidence score
        confidence = self._calculate_confidence(issues)

        # 10. Determine if data is valid
        valid = not any(i.severity == ValidationSeverity.CRITICAL for i in issues)
        if self.strict_mode:
            valid = valid and not any(i.severity == ValidationSeverity.ERROR for i in issues)

        return ValidationResult(
            valid=valid,
            issues=issues,
            confidence=confidence,
        )

    def validate_multiple(self, data_list: List[NormalizedData]) -> Dict[int, ValidationResult]:
        """
        Validate multiple data series and check for consistency.

        Args:
            data_list: List of NormalizedData to validate

        Returns:
            Dictionary mapping index to ValidationResult
        """
        results = {}

        for i, data in enumerate(data_list):
            results[i] = self.validate(data)

        # TODO: Cross-series consistency checks
        # e.g., if two series claim to be the same indicator but have very different values

        return results

    def _detect_indicator_type(self, data: NormalizedData) -> Optional[str]:
        """Detect the indicator type from metadata"""
        if not data.metadata:
            return None

        # Check indicator name
        indicator = (data.metadata.indicator or "").lower()
        unit = (data.metadata.unit or "").lower()

        # Try pattern matching
        for pattern, indicator_type in self.INDICATOR_PATTERNS.items():
            if pattern in indicator:
                return indicator_type

        # Check unit hints
        if "percent" in unit or "%" in unit:
            if "gdp" in indicator:
                return "GDP_GROWTH"
            return "INFLATION"  # Default percentage indicator

        if "index" in unit:
            return "PRICE_INDEX"

        if "dollar" in unit or "usd" in unit or "$" in unit:
            if "per capita" in indicator:
                return "GDP_PER_CAPITA"
            return "GDP"

        return None

    def _calculate_confidence(self, issues: List[ValidationIssue]) -> float:
        """Calculate confidence score based on issues found"""
        confidence = 1.0

        for issue in issues:
            if issue.severity == ValidationSeverity.CRITICAL:
                confidence -= 0.5
            elif issue.severity == ValidationSeverity.ERROR:
                confidence -= 0.3
            elif issue.severity == ValidationSeverity.WARNING:
                confidence -= 0.1
            elif issue.severity == ValidationSeverity.INFO:
                confidence -= 0.02

        return max(0, confidence)

    def log_validation_results(self, data: NormalizedData, result: ValidationResult):
        """Log validation results for monitoring"""
        indicator = data.metadata.indicator if data.metadata else "UNKNOWN"

        if result.valid and result.confidence >= 0.8:
            logger.debug(f"Data validation passed for {indicator} (confidence: {result.confidence:.2f})")
        elif result.valid:
            logger.info(f"Data validation passed with warnings for {indicator} (confidence: {result.confidence:.2f})")
            for issue in result.issues:
                logger.info(f"  [{issue.severity.value}] {issue.message}")
        else:
            logger.warning(f"Data validation FAILED for {indicator} (confidence: {result.confidence:.2f})")
            for issue in result.issues:
                logger.warning(f"  [{issue.severity.value}] {issue.message}")


# Singleton instance
_validator: Optional[DataValidator] = None


def get_data_validator() -> DataValidator:
    """Get the singleton DataValidator instance"""
    global _validator
    if _validator is None:
        _validator = DataValidator()
    return _validator
