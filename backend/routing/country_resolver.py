"""
Country Resolver - Single Source of Truth for Country/Region Data

Consolidates country membership data from:
- provider_router.py (OECD_MEMBERS, EU_MEMBERS, NON_OECD_MAJOR, OECD_NON_EU)
- catalog_service.py (OECD_MEMBERS, EU_MEMBERS)

This module provides:
1. Country name normalization (aliases → ISO code)
2. Region membership checking (is_oecd, is_eu, etc.)
3. Provider coverage determination
"""

from __future__ import annotations

import logging
import re
from typing import Set, Optional, List, Dict, FrozenSet

logger = logging.getLogger(__name__)


class CountryResolver:
    """
    Resolves country names to ISO codes and checks region membership.

    Single source of truth for all country/region data used in routing.
    """

    # ==========================================================================
    # ISO Country Codes (Alpha-2)
    # ==========================================================================

    # Mapping of country aliases to ISO Alpha-2 codes
    # This is the definitive source for country name normalization
    COUNTRY_ALIASES: Dict[str, str] = {
        # United States
        "us": "US", "usa": "US", "united states": "US", "america": "US",
        "united states of america": "US", "u.s.": "US", "u.s.a.": "US",

        # United Kingdom
        "uk": "GB", "britain": "GB", "united kingdom": "GB", "great britain": "GB",
        "england": "GB", "scotland": "GB", "wales": "GB",

        # European Union members
        "austria": "AT", "at": "AT", "aut": "AT",
        "belgium": "BE", "be": "BE", "bel": "BE",
        "bulgaria": "BG", "bg": "BG", "bgr": "BG",
        "croatia": "HR", "hr": "HR", "hrv": "HR",
        "cyprus": "CY", "cy": "CY", "cyp": "CY",
        "czech republic": "CZ", "czechia": "CZ", "cz": "CZ", "cze": "CZ",
        "denmark": "DK", "dk": "DK", "dnk": "DK",
        "estonia": "EE", "ee": "EE", "est": "EE",
        "finland": "FI", "fi": "FI", "fin": "FI",
        "france": "FR", "fr": "FR", "fra": "FR",
        "germany": "DE", "de": "DE", "deu": "DE",
        "greece": "GR", "gr": "GR", "grc": "GR",
        "hungary": "HU", "hu": "HU", "hun": "HU",
        "ireland": "IE", "ie": "IE", "irl": "IE", "eire": "IE",
        "italy": "IT", "it": "IT", "ita": "IT",
        "latvia": "LV", "lv": "LV", "lva": "LV",
        "lithuania": "LT", "lt": "LT", "ltu": "LT",
        "luxembourg": "LU", "lu": "LU", "lux": "LU",
        "malta": "MT", "mt": "MT", "mlt": "MT",
        "netherlands": "NL", "nl": "NL", "nld": "NL", "holland": "NL",
        "poland": "PL", "pl": "PL", "pol": "PL",
        "portugal": "PT", "pt": "PT", "prt": "PT",
        "romania": "RO", "ro": "RO", "rou": "RO",
        "slovakia": "SK", "sk": "SK", "svk": "SK",
        "slovenia": "SI", "si": "SI", "svn": "SI",
        "spain": "ES", "es": "ES", "esp": "ES",
        "sweden": "SE", "se": "SE", "swe": "SE",

        # Other OECD members (non-EU)
        "australia": "AU", "au": "AU", "aus": "AU",
        "canada": "CA", "ca": "CA", "can": "CA",
        "chile": "CL", "cl": "CL", "chl": "CL",
        "colombia": "CO", "co": "CO", "col": "CO",
        "costa rica": "CR", "cr": "CR", "cri": "CR",
        "iceland": "IS", "is": "IS", "isl": "IS",
        "israel": "IL", "il": "IL", "isr": "IL",
        "japan": "JP", "jp": "JP", "jpn": "JP",
        "korea": "KR", "south korea": "KR", "kr": "KR", "kor": "KR",
        "mexico": "MX", "mx": "MX", "mex": "MX",
        "new zealand": "NZ", "nz": "NZ", "nzl": "NZ",
        "norway": "NO", "no": "NO", "nor": "NO",
        "switzerland": "CH", "ch": "CH", "che": "CH",
        "turkey": "TR", "türkiye": "TR", "turkiye": "TR", "tr": "TR", "tur": "TR",

        # Non-OECD major economies
        "china": "CN", "cn": "CN", "chn": "CN", "prc": "CN",
        "india": "IN", "in": "IN", "ind": "IN",
        "brazil": "BR", "br": "BR", "bra": "BR",
        "russia": "RU", "ru": "RU", "rus": "RU", "russian federation": "RU",
        "indonesia": "ID", "id": "ID", "idn": "ID",
        "saudi arabia": "SA", "sa": "SA", "sau": "SA",
        "argentina": "AR", "ar": "AR", "arg": "AR",
        "south africa": "ZA", "za": "ZA", "zaf": "ZA",
        "egypt": "EG", "eg": "EG", "egy": "EG",
        "thailand": "TH", "th": "TH", "tha": "TH",
        "vietnam": "VN", "vn": "VN", "vnm": "VN",
        "pakistan": "PK", "pk": "PK", "pak": "PK",
        "bangladesh": "BD", "bd": "BD", "bgd": "BD",
        "nigeria": "NG", "ng": "NG", "nga": "NG",

        # Other notable countries
        "singapore": "SG", "sg": "SG", "sgp": "SG",
        "hong kong": "HK", "hk": "HK", "hkg": "HK",
        "taiwan": "TW", "tw": "TW", "twn": "TW",
        "philippines": "PH", "ph": "PH", "phl": "PH",
        "malaysia": "MY", "my": "MY", "mys": "MY",
        "uae": "AE", "ae": "AE", "are": "AE", "united arab emirates": "AE",
        "iran": "IR", "ir": "IR", "irn": "IR", "islamic republic of iran": "IR",
        "ethiopia": "ET", "et": "ET", "eth": "ET",

        # Additional African countries
        "botswana": "BW", "bw": "BW", "bwa": "BW",
        "ghana": "GH", "gh": "GH", "gha": "GH",
        "kenya": "KE", "ke": "KE", "ken": "KE",
        "tanzania": "TZ", "tz": "TZ", "tza": "TZ",
        "uganda": "UG", "ug": "UG", "uga": "UG",
        "zambia": "ZM", "zm": "ZM", "zmb": "ZM",
        "zimbabwe": "ZW", "zw": "ZW", "zwe": "ZW",
        "angola": "AO", "ao": "AO", "ago": "AO",
        "cameroon": "CM", "cm": "CM", "cmr": "CM",
        "ivory coast": "CI", "cote d'ivoire": "CI", "ci": "CI", "civ": "CI",
        "senegal": "SN", "sn": "SN", "sen": "SN",
        "morocco": "MA", "ma": "MA", "mar": "MA",
        "algeria": "DZ", "dz": "DZ", "dza": "DZ",
        "tunisia": "TN", "tn": "TN", "tun": "TN",

        # Latin America
        "venezuela": "VE", "ve": "VE", "ven": "VE",
        "peru": "PE", "pe": "PE", "per": "PE",
        "ecuador": "EC", "ec": "EC", "ecu": "EC",
        "bolivia": "BO", "bo": "BO", "bol": "BO",
        "paraguay": "PY", "py": "PY", "pry": "PY",
        "uruguay": "UY", "uy": "UY", "ury": "UY",
        "guatemala": "GT", "gt": "GT", "gtm": "GT",
        "honduras": "HN", "hn": "HN", "hnd": "HN",
        "el salvador": "SV", "sv": "SV", "slv": "SV",
        "nicaragua": "NI", "ni": "NI", "nic": "NI",
        "panama": "PA", "pa": "PA", "pan": "PA",
        "cuba": "CU", "cu": "CU", "cub": "CU",
        "dominican republic": "DO", "do": "DO", "dom": "DO",

        # Middle East
        "qatar": "QA", "qa": "QA", "qat": "QA",
        "kuwait": "KW", "kw": "KW", "kwt": "KW",
        "oman": "OM", "om": "OM", "omn": "OM",
        "bahrain": "BH", "bh": "BH", "bhr": "BH",
        "jordan": "JO", "jo": "JO", "jor": "JO",
        "lebanon": "LB", "lb": "LB", "lbn": "LB",
        "iraq": "IQ", "iq": "IQ", "irq": "IQ",
        "syria": "SY", "sy": "SY", "syr": "SY",
        "yemen": "YE", "ye": "YE", "yem": "YE",

        # Caribbean
        "jamaica": "JM", "jm": "JM", "jam": "JM",
        "trinidad and tobago": "TT", "trinidad": "TT", "tt": "TT", "tto": "TT",
        "bahamas": "BS", "bs": "BS", "bhs": "BS",
        "barbados": "BB", "bb": "BB", "brb": "BB",
        "haiti": "HT", "ht": "HT", "hti": "HT",

        # Other Asian
        "cambodia": "KH", "kh": "KH", "khm": "KH",
        "laos": "LA", "la": "LA", "lao": "LA",
        "myanmar": "MM", "burma": "MM", "mm": "MM", "mmr": "MM",
        "brunei": "BN", "bn": "BN", "brn": "BN",
        "mongolia": "MN", "mn": "MN", "mng": "MN",
        "nepal": "NP", "np": "NP", "npl": "NP",
        "sri lanka": "LK", "lk": "LK", "lka": "LK",
        "afghanistan": "AF", "af": "AF", "afg": "AF",

        # Balkan countries (infrastructure addition)
        "albania": "AL", "al": "AL", "alb": "AL",
        "bosnia and herzegovina": "BA", "bosnia": "BA", "ba": "BA", "bih": "BA",
        "kosovo": "XK", "xk": "XK",  # XK is unofficial but widely used
        "montenegro": "ME", "me": "ME", "mne": "ME",
        "north macedonia": "MK", "macedonia": "MK", "mk": "MK", "mkd": "MK",
        "serbia": "RS", "rs": "RS", "srb": "RS",

        # West African countries for ECOWAS (infrastructure addition)
        "benin": "BJ", "bj": "BJ", "ben": "BJ",
        "burkina faso": "BF", "bf": "BF", "bfa": "BF",
        "cape verde": "CV", "cv": "CV", "cpv": "CV",
        "gambia": "GM", "gm": "GM", "gmb": "GM",
        "guinea": "GN", "gn": "GN", "gin": "GN",
        "guinea-bissau": "GW", "gw": "GW", "gnb": "GW",
        "liberia": "LR", "lr": "LR", "lbr": "LR",
        "mali": "ML", "ml": "ML", "mli": "ML",
        "niger": "NE", "ne": "NE", "ner": "NE",
        "sierra leone": "SL", "sl": "SL", "sle": "SL",
        "togo": "TG", "tg": "TG", "tgo": "TG",

        # Pacific Island nations (infrastructure addition)
        "fiji": "FJ", "fj": "FJ", "fji": "FJ",
        "papua new guinea": "PG", "pg": "PG", "png": "PG",
        "samoa": "WS", "ws": "WS", "wsm": "WS",
        "solomon islands": "SB", "sb": "SB", "slb": "SB",
        "vanuatu": "VU", "vu": "VU", "vut": "VU",
        "tonga": "TO", "to": "TO", "ton": "TO",
        "kiribati": "KI", "ki": "KI", "kir": "KI",
        "marshall islands": "MH", "mh": "MH", "mhl": "MH",
        "micronesia": "FM", "fm": "FM", "fsm": "FM",
        "palau": "PW", "pw": "PW", "plw": "PW",
        "nauru": "NR", "nr": "NR", "nru": "NR",
        "tuvalu": "TV", "tv": "TV", "tuv": "TV",

        # Additional countries for completeness
        "libya": "LY", "ly": "LY", "lby": "LY",
        "equatorial guinea": "GQ", "gq": "GQ", "gnq": "GQ",
        "gabon": "GA", "ga": "GA", "gab": "GA",
        "congo": "CG", "cg": "CG", "cog": "CG", "republic of congo": "CG",
        "democratic republic of congo": "CD", "drc": "CD", "cd": "CD", "cod": "CD",
        "madagascar": "MG", "mg": "MG", "mdg": "MG",
        "malawi": "MW", "mw": "MW", "mwi": "MW",
        "mozambique": "MZ", "mz": "MZ", "moz": "MZ",
        "namibia": "NA", "na": "NA", "nam": "NA",
        "rwanda": "RW", "rw": "RW", "rwa": "RW",
    }

    # ==========================================================================
    # Region Memberships (Frozen Sets for immutability)
    # ==========================================================================

    # OECD member countries (38 members as of 2024)
    OECD_MEMBERS: FrozenSet[str] = frozenset({
        "AU", "AT", "BE", "CA", "CL", "CO", "CR", "CZ", "DK", "EE",
        "FI", "FR", "DE", "GR", "HU", "IS", "IE", "IL", "IT", "JP",
        "KR", "LV", "LT", "LU", "MX", "NL", "NZ", "NO", "PL", "PT",
        "SK", "SI", "ES", "SE", "CH", "TR", "GB", "US"
    })

    # EU member countries (27 members as of 2024, UK left)
    EU_MEMBERS: FrozenSet[str] = frozenset({
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
        "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
        "PL", "PT", "RO", "SK", "SI", "ES", "SE"
    })

    # Eurozone countries (20 members using Euro)
    EUROZONE_MEMBERS: FrozenSet[str] = frozenset({
        "AT", "BE", "CY", "EE", "FI", "FR", "DE", "GR", "IE", "IT",
        "LV", "LT", "LU", "MT", "NL", "PT", "SK", "SI", "ES", "HR"
    })

    # OECD members NOT in EU (for routing decisions)
    OECD_NON_EU: FrozenSet[str] = OECD_MEMBERS - EU_MEMBERS

    # Non-OECD major economies (BRICS + others)
    NON_OECD_MAJOR: FrozenSet[str] = frozenset({
        "CN", "IN", "BR", "RU", "ID", "SA", "AR", "ZA", "EG",
        "TH", "VN", "PK", "BD", "NG", "PH", "MY"
    })

    # G7 countries
    G7_MEMBERS: FrozenSet[str] = frozenset({
        "CA", "FR", "DE", "IT", "JP", "GB", "US"
    })

    # G20 countries (19 countries + EU)
    G20_MEMBERS: FrozenSet[str] = frozenset({
        "AR", "AU", "BR", "CA", "CN", "FR", "DE", "IN", "ID", "IT",
        "JP", "KR", "MX", "RU", "SA", "ZA", "TR", "GB", "US"
    })

    # BRICS countries (original 5)
    BRICS_MEMBERS: FrozenSet[str] = frozenset({
        "BR", "RU", "IN", "CN", "ZA"
    })

    # BRICS+ countries (expanded in 2024)
    BRICS_PLUS_MEMBERS: FrozenSet[str] = frozenset({
        "BR", "RU", "IN", "CN", "ZA",  # Original BRICS
        "EG", "ET", "IR", "AE",  # 2024 expansion: Egypt, Ethiopia, Iran, UAE
    })

    # ASEAN countries
    ASEAN_MEMBERS: FrozenSet[str] = frozenset({
        "BN", "KH", "ID", "LA", "MY", "MM", "PH", "SG", "TH", "VN"
    })

    # Nordic countries
    NORDIC_MEMBERS: FrozenSet[str] = frozenset({
        "DK", "FI", "IS", "NO", "SE"
    })

    # MINT countries (Mexico, Indonesia, Nigeria, Turkey)
    MINT_MEMBERS: FrozenSet[str] = frozenset({
        "MX", "ID", "NG", "TR"
    })

    # CIVETS countries (Colombia, Indonesia, Vietnam, Egypt, Turkey, South Africa)
    CIVETS_MEMBERS: FrozenSet[str] = frozenset({
        "CO", "ID", "VN", "EG", "TR", "ZA"
    })

    # Next Eleven (N11) - Goldman Sachs designation
    N11_MEMBERS: FrozenSet[str] = frozenset({
        "BD", "EG", "ID", "IR", "MX", "NG", "PK", "PH", "KR", "TR", "VN"
    })

    # Latin America (major economies)
    LATAM_MEMBERS: FrozenSet[str] = frozenset({
        "AR", "BO", "BR", "CL", "CO", "CR", "CU", "DO", "EC", "SV",
        "GT", "HN", "MX", "NI", "PA", "PY", "PE", "UY", "VE"
    })

    # Middle East and North Africa (MENA)
    MENA_MEMBERS: FrozenSet[str] = frozenset({
        "DZ", "BH", "EG", "IR", "IQ", "IL", "JO", "KW", "LB", "LY",
        "MA", "OM", "QA", "SA", "SY", "TN", "AE", "YE"
    })

    # Gulf Cooperation Council (GCC)
    # Saudi Arabia, UAE, Kuwait, Qatar, Bahrain, Oman
    GCC_MEMBERS: FrozenSet[str] = frozenset({
        "SA", "AE", "KW", "QA", "BH", "OM"
    })

    # Sub-Saharan Africa (major economies)
    SSA_MEMBERS: FrozenSet[str] = frozenset({
        "AO", "BW", "CM", "CI", "CD", "ET", "GH", "KE", "MG", "MW",
        "ML", "MZ", "NA", "NE", "NG", "RW", "SN", "ZA", "TZ", "UG", "ZM", "ZW"
    })

    # Caribbean countries
    CARIBBEAN_MEMBERS: FrozenSet[str] = frozenset({
        "AG", "BB", "BS", "BZ", "CU", "DM", "DO", "GD", "GY", "HT",
        "JM", "KN", "LC", "SR", "TT", "VC"
    })

    # European sub-regional groupings (infrastructure addition for multi-country queries)
    # These are commonly used economic groupings that weren't previously supported

    # Benelux (Belgium, Netherlands, Luxembourg)
    BENELUX_MEMBERS: FrozenSet[str] = frozenset({"BE", "NL", "LU"})

    # Baltic states (Estonia, Latvia, Lithuania)
    BALTIC_MEMBERS: FrozenSet[str] = frozenset({"EE", "LV", "LT"})

    # DACH region (Germany, Austria, Switzerland)
    DACH_MEMBERS: FrozenSet[str] = frozenset({"DE", "AT", "CH"})

    # Visegrad Group / V4 (Czech Republic, Hungary, Poland, Slovakia)
    VISEGRAD_MEMBERS: FrozenSet[str] = frozenset({"CZ", "HU", "PL", "SK"})

    # Iberian Peninsula (Spain, Portugal)
    IBERIAN_MEMBERS: FrozenSet[str] = frozenset({"ES", "PT"})

    # East Asia
    EAST_ASIA_MEMBERS: FrozenSet[str] = frozenset({
        "CN", "JP", "KR", "TW", "HK", "MN", "KP"
    })

    # South Asia
    SOUTH_ASIA_MEMBERS: FrozenSet[str] = frozenset({
        "AF", "BD", "BT", "IN", "MV", "NP", "PK", "LK"
    })

    # Southeast Asia (same as ASEAN but with Timor-Leste)
    SOUTHEAST_ASIA_MEMBERS: FrozenSet[str] = frozenset({
        "BN", "KH", "ID", "LA", "MY", "MM", "PH", "SG", "TH", "TL", "VN"
    })

    # Balkan countries (Southeast Europe)
    BALKAN_MEMBERS: FrozenSet[str] = frozenset({
        "AL", "BA", "BG", "HR", "XK", "ME", "MK", "RO", "RS", "SI"
        # Albania, Bosnia-Herzegovina, Bulgaria, Croatia, Kosovo, Montenegro,
        # North Macedonia, Romania, Serbia, Slovenia
    })

    # ECOWAS - Economic Community of West African States (15 members)
    ECOWAS_MEMBERS: FrozenSet[str] = frozenset({
        "BJ", "BF", "CV", "CI", "GM", "GH", "GN", "GW", "LR", "ML",
        "NE", "NG", "SN", "SL", "TG"
        # Benin, Burkina Faso, Cape Verde, Côte d'Ivoire, Gambia, Ghana, Guinea,
        # Guinea-Bissau, Liberia, Mali, Niger, Nigeria, Senegal, Sierra Leone, Togo
    })

    # Pacific Island nations (Pacific Islands Forum members)
    PACIFIC_ISLAND_MEMBERS: FrozenSet[str] = frozenset({
        "AU", "FJ", "KI", "MH", "FM", "NR", "NZ", "PW", "PG", "WS",
        "SB", "TO", "TV", "VU"
        # Australia, Fiji, Kiribati, Marshall Islands, Micronesia, Nauru,
        # New Zealand, Palau, Papua New Guinea, Samoa, Solomon Islands, Tonga, Tuvalu, Vanuatu
    })

    # OPEC - Organization of Petroleum Exporting Countries
    OPEC_MEMBERS: FrozenSet[str] = frozenset({
        "DZ", "AO", "CG", "GQ", "GA", "IR", "IQ", "KW", "LY", "NG",
        "SA", "AE", "VE"
        # Algeria, Angola, Congo, Equatorial Guinea, Gabon, Iran, Iraq, Kuwait,
        # Libya, Nigeria, Saudi Arabia, UAE, Venezuela
    })

    # MERCOSUR - Southern Common Market (infrastructure fix for session 11)
    MERCOSUR_MEMBERS: FrozenSet[str] = frozenset({
        "AR", "BR", "PY", "UY",  # Full members: Argentina, Brazil, Paraguay, Uruguay
        "BO",  # Bolivia (accession in progress)
        # Associate members: Chile, Colombia, Ecuador, Guyana, Peru, Suriname
        # (not included by default - only full members)
    })

    # CARICOM - Caribbean Community (distinct from broader Caribbean)
    CARICOM_MEMBERS: FrozenSet[str] = frozenset({
        "AG", "BB", "BS", "BZ", "DM", "GD", "GY", "HT", "JM",
        "KN", "LC", "SR", "TT", "VC"
        # Antigua and Barbuda, Barbados, Bahamas, Belize, Dominica, Grenada,
        # Guyana, Haiti, Jamaica, St Kitts and Nevis, St Lucia, Suriname,
        # Trinidad and Tobago, St Vincent and the Grenadines
    })

    # Sahel Region - West African nations in the Sahel belt
    SAHEL_MEMBERS: FrozenSet[str] = frozenset({
        "ML", "NE", "BF", "MR", "TD", "SN", "GM"
        # Mali, Niger, Burkina Faso, Mauritania, Chad, Senegal, Gambia
    })

    # East African Community (EAC)
    EAC_MEMBERS: FrozenSet[str] = frozenset({
        "KE", "TZ", "UG", "RW", "BI", "SS", "CD"
        # Kenya, Tanzania, Uganda, Rwanda, Burundi, South Sudan, DR Congo
    })

    # ==========================================================================
    # Canadian Provinces (for StatsCan routing)
    # ==========================================================================

    CANADIAN_PROVINCES: FrozenSet[str] = frozenset({
        "ontario", "quebec", "british columbia", "bc", "alberta",
        "manitoba", "saskatchewan", "nova scotia", "new brunswick",
        "newfoundland", "prince edward island", "pei",
        "northwest territories", "yukon", "nunavut",
        # Major cities
        "toronto", "montreal", "vancouver", "calgary", "ottawa",
        "edmonton", "winnipeg", "quebec city"
    })

    # ==========================================================================
    # Public Methods
    # ==========================================================================

    @classmethod
    def normalize(cls, country: str) -> Optional[str]:
        """
        Normalize a country name or code to ISO Alpha-2 format.

        Args:
            country: Country name or code (e.g., "United States", "USA", "US")

        Returns:
            ISO Alpha-2 code (e.g., "US") or None if not found
        """
        if not country:
            return None

        country_lower = country.lower().strip()

        # Direct lookup in aliases
        if country_lower in cls.COUNTRY_ALIASES:
            return cls.COUNTRY_ALIASES[country_lower]

        # Check if already an ISO code (uppercase 2-letter)
        country_upper = country.upper().strip()
        if len(country_upper) == 2:
            # Check if it's a known ISO code
            if country_upper in cls.OECD_MEMBERS or country_upper in cls.NON_OECD_MAJOR:
                return country_upper
            # Check if it's in any alias value
            if country_upper in cls.COUNTRY_ALIASES.values():
                return country_upper

        return None

    @classmethod
    def is_oecd_member(cls, country: str) -> bool:
        """Check if country is an OECD member."""
        iso_code = cls.normalize(country)
        return iso_code in cls.OECD_MEMBERS if iso_code else False

    @classmethod
    def is_eu_member(cls, country: str) -> bool:
        """Check if country is an EU member."""
        iso_code = cls.normalize(country)
        return iso_code in cls.EU_MEMBERS if iso_code else False

    @classmethod
    def is_eurozone_member(cls, country: str) -> bool:
        """Check if country uses the Euro."""
        iso_code = cls.normalize(country)
        return iso_code in cls.EUROZONE_MEMBERS if iso_code else False

    @classmethod
    def is_non_oecd_major(cls, country: str) -> bool:
        """Check if country is a major non-OECD economy."""
        iso_code = cls.normalize(country)
        return iso_code in cls.NON_OECD_MAJOR if iso_code else False

    @classmethod
    def is_oecd_non_eu(cls, country: str) -> bool:
        """Check if country is OECD but NOT EU (for OECD vs Eurostat routing)."""
        iso_code = cls.normalize(country)
        return iso_code in cls.OECD_NON_EU if iso_code else False

    @classmethod
    def is_g7_member(cls, country: str) -> bool:
        """Check if country is a G7 member."""
        iso_code = cls.normalize(country)
        return iso_code in cls.G7_MEMBERS if iso_code else False

    @classmethod
    def is_g20_member(cls, country: str) -> bool:
        """Check if country is a G20 member."""
        iso_code = cls.normalize(country)
        return iso_code in cls.G20_MEMBERS if iso_code else False

    @classmethod
    def is_brics_member(cls, country: str) -> bool:
        """Check if country is a BRICS member (original 5)."""
        iso_code = cls.normalize(country)
        return iso_code in cls.BRICS_MEMBERS if iso_code else False

    @classmethod
    def is_brics_plus_member(cls, country: str) -> bool:
        """Check if country is a BRICS+ member (2024 expansion)."""
        iso_code = cls.normalize(country)
        return iso_code in cls.BRICS_PLUS_MEMBERS if iso_code else False

    @classmethod
    def is_asean_member(cls, country: str) -> bool:
        """Check if country is an ASEAN member."""
        iso_code = cls.normalize(country)
        return iso_code in cls.ASEAN_MEMBERS if iso_code else False

    @classmethod
    def is_canadian_region(cls, text: str) -> bool:
        """Check if text mentions Canada or a Canadian province/city."""
        if not text:
            return False
        text_lower = text.lower()
        if "canada" in text_lower or "canadian" in text_lower:
            return True
        return any(province in text_lower for province in cls.CANADIAN_PROVINCES)

    @classmethod
    def is_us(cls, country: str) -> bool:
        """Check if country is the United States."""
        iso_code = cls.normalize(country)
        return iso_code == "US"

    @classmethod
    def get_regions(cls, country: str) -> List[str]:
        """
        Get all region memberships for a country.

        Args:
            country: Country name or code

        Returns:
            List of region names (e.g., ["OECD", "EU", "G7", "G20"])
        """
        iso_code = cls.normalize(country)
        if not iso_code:
            return []

        regions = []
        if iso_code in cls.OECD_MEMBERS:
            regions.append("OECD")
        if iso_code in cls.EU_MEMBERS:
            regions.append("EU")
        if iso_code in cls.EUROZONE_MEMBERS:
            regions.append("Eurozone")
        if iso_code in cls.G7_MEMBERS:
            regions.append("G7")
        if iso_code in cls.G20_MEMBERS:
            regions.append("G20")
        if iso_code in cls.BRICS_MEMBERS:
            regions.append("BRICS")
        if iso_code in cls.BRICS_PLUS_MEMBERS and iso_code not in cls.BRICS_MEMBERS:
            regions.append("BRICS+")  # New members only
        if iso_code in cls.ASEAN_MEMBERS:
            regions.append("ASEAN")
        if iso_code in cls.NON_OECD_MAJOR:
            regions.append("Emerging")

        return regions

    @classmethod
    def get_preferred_provider(cls, country: str) -> Optional[str]:
        """
        Get the preferred data provider based on country.

        This is a simplified routing hint based solely on geography.
        The UnifiedRouter uses this along with indicator analysis.

        Args:
            country: Country name or code

        Returns:
            Preferred provider name or None
        """
        iso_code = cls.normalize(country)
        if not iso_code:
            return None

        # US → FRED
        if iso_code == "US":
            return "FRED"

        # Canada → StatsCan
        if iso_code == "CA":
            return "StatsCan"

        # EU members → Eurostat (with WorldBank fallback)
        if iso_code in cls.EU_MEMBERS:
            return "Eurostat"

        # OECD non-EU → OECD (for OECD-specific indicators)
        if iso_code in cls.OECD_NON_EU:
            return "OECD"

        # Non-OECD major → WorldBank
        if iso_code in cls.NON_OECD_MAJOR:
            return "WorldBank"

        # Default → WorldBank (broadest coverage)
        return "WorldBank"

    @classmethod
    def detect_country_in_query(cls, query: str) -> Optional[str]:
        """
        Detect country mentioned in a query string.

        Args:
            query: Natural language query

        Returns:
            ISO Alpha-2 code of first country found, or None
        """
        country_positions = cls._find_country_codes_with_positions(query)
        if not country_positions:
            return None
        return min(country_positions.items(), key=lambda item: item[1])[0]

    @classmethod
    def detect_all_countries_in_query(cls, query: str) -> List[str]:
        """
        Detect all countries mentioned in a query string.

        Uses word boundary matching to avoid partial matches
        (e.g., "india" should not match "indiana").

        Args:
            query: Natural language query

        Returns:
            List of ISO Alpha-2 codes for all countries found
        """
        country_positions = cls._find_country_codes_with_positions(query)
        if not country_positions:
            return []
        return [code for code, _ in sorted(country_positions.items(), key=lambda item: item[1])]

    @classmethod
    def _find_country_codes_with_positions(cls, query: str) -> Dict[str, int]:
        """
        Find all country aliases in query and return first-match position by ISO code.

        Implementation details:
        - Uses regex word boundaries to avoid partial matches.
        - Preserves query order (position sort) for deterministic multi-country extraction.
        - Avoids false positives from short aliases (e.g., "in", "no", "can"):
          short alphabetic aliases (<= 3 chars) require uppercase tokens unless allowlisted.
        """
        if not query:
            return {}

        query_lower = query.lower()
        country_positions: Dict[str, int] = {}

        # Track uppercase tokens from original query (for ISO code detection like "DE", "CAN")
        uppercase_tokens = set(re.findall(r"\b[A-Z]{2,3}\b", query))

        # Short aliases that are commonly used in lowercase and safe to match
        # (exclude ambiguous tokens like "in", "no", "can", "tur", etc.)
        short_allowlist = {"uk", "usa", "uae", "drc", "prc"}

        # Longest aliases first to prioritize specific names over shorter variants
        sorted_aliases = sorted(cls.COUNTRY_ALIASES.keys(), key=len, reverse=True)

        for alias in sorted_aliases:
            code = cls.COUNTRY_ALIASES[alias]
            alias_lower = alias.lower()

            # Special handling for US to avoid pronoun false positives ("show us ...")
            if alias_lower == "us":
                for match in re.finditer(r"\bUS\b|\bU\.S\.A?\.\b", query):
                    pos = match.start()
                    if code not in country_positions or pos < country_positions[code]:
                        country_positions[code] = pos
                continue

            # Short alphabetic aliases are often ambiguous in natural language.
            # Require explicit uppercase token unless allowlisted.
            if alias_lower.isalpha() and len(alias_lower) <= 3 and alias_lower not in short_allowlist:
                if alias.upper() not in uppercase_tokens:
                    continue

            pattern = rf"(?<!\w){re.escape(alias_lower)}(?!\w)"
            for match in re.finditer(pattern, query_lower):
                pos = match.start()
                if code not in country_positions or pos < country_positions[code]:
                    country_positions[code] = pos

        return country_positions

    # ==========================================================================
    # Region Expansion Methods (NEW - for multi-country query support)
    # ==========================================================================

    @classmethod
    def expand_region(cls, region: str) -> Optional[List[str]]:
        """
        Expand a region name to its member countries.

        This is the core method for multi-country query support.
        When a user queries "G7 GDP", this expands G7 to all 7 member countries.

        Args:
            region: Region name (G7, BRICS, EU, ASEAN, Nordic, etc.)

        Returns:
            List of ISO Alpha-2 codes, or None if not a recognized region
        """
        if not region:
            return None

        region_upper = region.upper().strip().replace(" ", "_").replace("-", "_")

        # Map region names to member sets
        REGION_MAPPINGS = {
            # G7
            "G7": cls.G7_MEMBERS,
            "G7_COUNTRIES": cls.G7_MEMBERS,
            "G_7": cls.G7_MEMBERS,
            # G20
            "G20": cls.G20_MEMBERS,
            "G20_COUNTRIES": cls.G20_MEMBERS,
            "G_20": cls.G20_MEMBERS,
            # BRICS
            "BRICS": cls.BRICS_MEMBERS,
            "BRICS_COUNTRIES": cls.BRICS_MEMBERS,
            "BRICS_NATIONS": cls.BRICS_MEMBERS,
            # BRICS+
            "BRICS_PLUS": cls.BRICS_PLUS_MEMBERS,
            "BRICS+": cls.BRICS_PLUS_MEMBERS,
            # EU
            "EU": cls.EU_MEMBERS,
            "EU_COUNTRIES": cls.EU_MEMBERS,
            "EU_MEMBERS": cls.EU_MEMBERS,
            "EUROPEAN_UNION": cls.EU_MEMBERS,
            "EU_MEMBER_STATES": cls.EU_MEMBERS,
            # Eurozone
            "EUROZONE": cls.EUROZONE_MEMBERS,
            "EURO_AREA": cls.EUROZONE_MEMBERS,
            "EURO_ZONE": cls.EUROZONE_MEMBERS,
            # ASEAN
            "ASEAN": cls.ASEAN_MEMBERS,
            "ASEAN_COUNTRIES": cls.ASEAN_MEMBERS,
            "ASEAN_NATIONS": cls.ASEAN_MEMBERS,
            # Nordic
            "NORDIC": cls.NORDIC_MEMBERS,
            "NORDIC_COUNTRIES": cls.NORDIC_MEMBERS,
            "SCANDINAVIAN": cls.NORDIC_MEMBERS,
            "SCANDINAVIA": cls.NORDIC_MEMBERS,
            # OECD
            "OECD": cls.OECD_MEMBERS,
            "OECD_COUNTRIES": cls.OECD_MEMBERS,
            "OECD_MEMBERS": cls.OECD_MEMBERS,
            # MINT
            "MINT": cls.MINT_MEMBERS,
            "MINT_COUNTRIES": cls.MINT_MEMBERS,
            # CIVETS
            "CIVETS": cls.CIVETS_MEMBERS,
            "CIVETS_COUNTRIES": cls.CIVETS_MEMBERS,
            # N11 / Next Eleven
            "N11": cls.N11_MEMBERS,
            "N_11": cls.N11_MEMBERS,
            "NEXT_11": cls.N11_MEMBERS,
            "NEXT_ELEVEN": cls.N11_MEMBERS,
            # Latin America
            "LATAM": cls.LATAM_MEMBERS,
            "LATIN_AMERICA": cls.LATAM_MEMBERS,
            "LATIN_AMERICAN": cls.LATAM_MEMBERS,
            "SOUTH_AMERICA": cls.LATAM_MEMBERS,
            # Middle East / MENA
            "MENA": cls.MENA_MEMBERS,
            "MIDDLE_EAST": cls.MENA_MEMBERS,
            "MIDEAST": cls.MENA_MEMBERS,
            # Gulf Cooperation Council (GCC)
            "GCC": cls.GCC_MEMBERS,
            "GCC_COUNTRIES": cls.GCC_MEMBERS,
            "GULF": cls.GCC_MEMBERS,
            "GULF_COUNTRIES": cls.GCC_MEMBERS,
            "GULF_STATES": cls.GCC_MEMBERS,
            "GULF_COOPERATION_COUNCIL": cls.GCC_MEMBERS,
            # Sub-Saharan Africa
            "SSA": cls.SSA_MEMBERS,
            "SUB_SAHARAN_AFRICA": cls.SSA_MEMBERS,
            "AFRICA": cls.SSA_MEMBERS,
            "AFRICAN": cls.SSA_MEMBERS,
            # Caribbean
            "CARIBBEAN": cls.CARIBBEAN_MEMBERS,
            "CARRIBEAN": cls.CARIBBEAN_MEMBERS,  # Common misspelling
            # East Asia
            "EAST_ASIA": cls.EAST_ASIA_MEMBERS,
            "EASTASIA": cls.EAST_ASIA_MEMBERS,
            # South Asia
            "SOUTH_ASIA": cls.SOUTH_ASIA_MEMBERS,
            "SOUTHASIA": cls.SOUTH_ASIA_MEMBERS,
            # Southeast Asia
            "SOUTHEAST_ASIA": cls.SOUTHEAST_ASIA_MEMBERS,
            "SOUTHEASTASIA": cls.SOUTHEAST_ASIA_MEMBERS,
            "SE_ASIA": cls.SOUTHEAST_ASIA_MEMBERS,
            # Asia general (combine all Asian regions)
            "ASIA": cls.EAST_ASIA_MEMBERS | cls.SOUTH_ASIA_MEMBERS | cls.SOUTHEAST_ASIA_MEMBERS,
            # European sub-regional groupings (infrastructure fix for multi-country queries)
            "BENELUX": cls.BENELUX_MEMBERS,
            "BENELUX_COUNTRIES": cls.BENELUX_MEMBERS,
            "BALTIC": cls.BALTIC_MEMBERS,
            "BALTIC_STATES": cls.BALTIC_MEMBERS,
            "BALTIC_COUNTRIES": cls.BALTIC_MEMBERS,
            "BALTICS": cls.BALTIC_MEMBERS,
            "DACH": cls.DACH_MEMBERS,
            "DACH_REGION": cls.DACH_MEMBERS,
            "DACH_COUNTRIES": cls.DACH_MEMBERS,
            "VISEGRAD": cls.VISEGRAD_MEMBERS,
            "VISEGRAD_GROUP": cls.VISEGRAD_MEMBERS,
            "VISEGRAD_COUNTRIES": cls.VISEGRAD_MEMBERS,
            "V4": cls.VISEGRAD_MEMBERS,
            "IBERIAN": cls.IBERIAN_MEMBERS,
            "IBERIA": cls.IBERIAN_MEMBERS,
            "IBERIAN_PENINSULA": cls.IBERIAN_MEMBERS,
            # Balkan region (infrastructure addition)
            "BALKAN": cls.BALKAN_MEMBERS,
            "BALKANS": cls.BALKAN_MEMBERS,
            "BALKAN_NATIONS": cls.BALKAN_MEMBERS,
            "BALKAN_COUNTRIES": cls.BALKAN_MEMBERS,
            "BALKAN_STATES": cls.BALKAN_MEMBERS,
            "SOUTHEAST_EUROPE": cls.BALKAN_MEMBERS,
            # ECOWAS - West African Economic Community (infrastructure addition)
            "ECOWAS": cls.ECOWAS_MEMBERS,
            "ECOWAS_COUNTRIES": cls.ECOWAS_MEMBERS,
            "ECOWAS_MEMBERS": cls.ECOWAS_MEMBERS,
            "WEST_AFRICA": cls.ECOWAS_MEMBERS,
            "WEST_AFRICAN": cls.ECOWAS_MEMBERS,
            # Pacific Islands (infrastructure addition)
            "PACIFIC_ISLANDS": cls.PACIFIC_ISLAND_MEMBERS,
            "PACIFIC_ISLAND": cls.PACIFIC_ISLAND_MEMBERS,
            "PACIFIC_ISLAND_NATIONS": cls.PACIFIC_ISLAND_MEMBERS,
            "PACIFIC_NATIONS": cls.PACIFIC_ISLAND_MEMBERS,
            "PACIFIC_FORUM": cls.PACIFIC_ISLAND_MEMBERS,
            "PACIFIC_ISLAND_FORUM": cls.PACIFIC_ISLAND_MEMBERS,
            # OPEC (infrastructure addition)
            "OPEC": cls.OPEC_MEMBERS,
            "OPEC_COUNTRIES": cls.OPEC_MEMBERS,
            "OPEC_MEMBERS": cls.OPEC_MEMBERS,
            "OPEC_NATIONS": cls.OPEC_MEMBERS,
            # MERCOSUR - Southern Common Market (infrastructure fix session 11)
            "MERCOSUR": cls.MERCOSUR_MEMBERS,
            "MERCOSUR_COUNTRIES": cls.MERCOSUR_MEMBERS,
            "MERCOSUR_NATIONS": cls.MERCOSUR_MEMBERS,
            "MERCOSUR_MEMBERS": cls.MERCOSUR_MEMBERS,
            "SOUTHERN_COMMON_MARKET": cls.MERCOSUR_MEMBERS,
            # CARICOM - Caribbean Community (infrastructure fix session 11)
            "CARICOM": cls.CARICOM_MEMBERS,
            "CARICOM_COUNTRIES": cls.CARICOM_MEMBERS,
            "CARICOM_NATIONS": cls.CARICOM_MEMBERS,
            "CARICOM_MEMBERS": cls.CARICOM_MEMBERS,
            "CARIBBEAN_COMMUNITY": cls.CARICOM_MEMBERS,
            # Sahel region (infrastructure fix session 11)
            "SAHEL": cls.SAHEL_MEMBERS,
            "SAHEL_REGION": cls.SAHEL_MEMBERS,
            "SAHEL_COUNTRIES": cls.SAHEL_MEMBERS,
            "SAHEL_NATIONS": cls.SAHEL_MEMBERS,
            # East African Community (infrastructure fix session 11)
            "EAC": cls.EAC_MEMBERS,
            "EAC_COUNTRIES": cls.EAC_MEMBERS,
            "EAC_MEMBERS": cls.EAC_MEMBERS,
            "EAST_AFRICAN_COMMUNITY": cls.EAC_MEMBERS,
            # Visegrad typo handling
            "VISEGARD": cls.VISEGRAD_MEMBERS,
            "VISEGARD_GROUP": cls.VISEGRAD_MEMBERS,
            "OIL_EXPORTERS": cls.OPEC_MEMBERS,
        }

        if region_upper in REGION_MAPPINGS:
            return list(REGION_MAPPINGS[region_upper])

        return None

    @classmethod
    def is_region(cls, text: str) -> bool:
        """
        Check if text is a recognized region name.

        Args:
            text: Text to check

        Returns:
            True if text is a region name that can be expanded
        """
        return cls.expand_region(text) is not None

    @classmethod
    def detect_regions_in_query(cls, query: str) -> List[str]:
        """
        Detect region names mentioned in a query.

        Args:
            query: Natural language query

        Returns:
            List of detected region names (normalized form)
        """
        if not query:
            return []

        query_lower = query.lower()
        detected = []

        # Region patterns to check (ordered by specificity)
        region_patterns = [
            # Multi-word first (more specific)
            ("european union", "EU"),
            ("euro area", "EUROZONE"),
            ("euro zone", "EUROZONE"),
            ("nordic countries", "NORDIC"),
            ("asean countries", "ASEAN"),
            ("asean nations", "ASEAN"),
            ("brics countries", "BRICS"),
            ("brics nations", "BRICS"),
            ("brics+", "BRICS_PLUS"),
            ("brics plus", "BRICS_PLUS"),
            ("g7 countries", "G7"),
            ("g7 nations", "G7"),
            ("g20 countries", "G20"),
            ("g20 nations", "G20"),
            ("oecd countries", "OECD"),
            ("oecd members", "OECD"),
            ("eu members", "EU"),
            ("eu countries", "EU"),
            # New regions - multi-word
            ("mint countries", "MINT"),
            ("civets countries", "CIVETS"),
            ("civets economies", "CIVETS"),
            ("next eleven", "N11"),
            ("next 11", "N11"),
            ("n-11", "N11"),
            ("latin america", "LATAM"),
            ("latin american", "LATAM"),
            ("south america", "LATAM"),
            ("south american", "LATAM"),
            ("middle east", "MENA"),
            ("middle eastern", "MENA"),
            ("sub-saharan africa", "SSA"),
            ("sub saharan africa", "SSA"),
            ("subsaharan africa", "SSA"),
            ("east asia", "EAST_ASIA"),
            ("east asian", "EAST_ASIA"),
            ("south asia", "SOUTH_ASIA"),
            ("south asian", "SOUTH_ASIA"),
            ("southeast asia", "SOUTHEAST_ASIA"),
            ("south east asia", "SOUTHEAST_ASIA"),
            ("southeast asian", "SOUTHEAST_ASIA"),
            # Single words (less specific, check last)
            ("eurozone", "EUROZONE"),
            ("scandinavia", "NORDIC"),
            ("scandinavian", "NORDIC"),
            ("nordic", "NORDIC"),
            ("asean", "ASEAN"),
            ("brics", "BRICS"),
            ("g7", "G7"),
            ("g-7", "G7"),
            ("g20", "G20"),
            ("g-20", "G20"),
            ("oecd", "OECD"),
            # New regions - single word
            ("mint", "MINT"),
            ("civets", "CIVETS"),
            ("n11", "N11"),
            ("latam", "LATAM"),
            ("mena", "MENA"),
            ("mideast", "MENA"),
            # Gulf Cooperation Council (GCC)
            ("gcc", "GCC"),
            ("gulf cooperation council", "GCC"),
            ("gulf countries", "GCC"),
            ("gulf states", "GCC"),
            ("gulf", "GCC"),
            ("caribbean", "CARIBBEAN"),
            ("carribean", "CARIBBEAN"),  # Common misspelling
            ("africa", "SSA"),
            ("african", "SSA"),
            # European sub-regional groupings (infrastructure fix)
            ("benelux", "BENELUX"),
            ("baltic states", "BALTIC"),
            ("baltic countries", "BALTIC"),
            ("baltics", "BALTIC"),
            ("dach region", "DACH"),
            ("dach countries", "DACH"),
            ("dach", "DACH"),
            ("visegrad", "VISEGRAD"),
            ("visegard", "VISEGRAD"),  # Common typo
            ("visegrad group", "VISEGRAD"),
            ("visegard group", "VISEGRAD"),  # Typo handling
            ("visegrad countries", "VISEGRAD"),
            ("v4", "VISEGRAD"),
            # MERCOSUR - Southern Common Market (infrastructure fix)
            ("mercosur", "MERCOSUR"),
            ("mercosur countries", "MERCOSUR"),
            ("mercosur nations", "MERCOSUR"),
            ("mercosur members", "MERCOSUR"),
            ("southern common market", "MERCOSUR"),
            # CARICOM - Caribbean Community (infrastructure fix)
            ("caricom", "CARICOM"),
            ("caricom nations", "CARICOM"),
            ("caricom countries", "CARICOM"),
            ("caricom members", "CARICOM"),
            ("caribbean community", "CARICOM"),
            # Sahel region (infrastructure fix)
            ("sahel", "SAHEL"),
            ("sahel region", "SAHEL"),
            ("sahel countries", "SAHEL"),
            ("sahel nations", "SAHEL"),
            # East African Community (infrastructure fix)
            ("eac", "EAC"),
            ("east african community", "EAC"),
            ("eac countries", "EAC"),
            ("eac members", "EAC"),
            ("iberian", "IBERIAN"),
            ("iberia", "IBERIAN"),
            ("iberian peninsula", "IBERIAN"),
            # Balkan region (infrastructure addition)
            ("balkan nations", "BALKAN"),
            ("balkan countries", "BALKAN"),
            ("balkan states", "BALKAN"),
            ("balkans", "BALKAN"),
            ("balkan", "BALKAN"),
            ("southeast europe", "BALKAN"),
            # ECOWAS - West African Economic Community (infrastructure addition)
            ("ecowas members", "ECOWAS"),
            ("ecowas countries", "ECOWAS"),
            ("ecowas", "ECOWAS"),
            ("west africa", "ECOWAS"),
            ("west african", "ECOWAS"),
            # Pacific Islands (infrastructure addition)
            ("pacific island nations", "PACIFIC_ISLANDS"),
            ("pacific island countries", "PACIFIC_ISLANDS"),
            ("pacific islands", "PACIFIC_ISLANDS"),
            ("pacific forum", "PACIFIC_ISLANDS"),
            ("pacific nations", "PACIFIC_ISLANDS"),
            # OPEC (infrastructure addition)
            ("opec countries", "OPEC"),
            ("opec nations", "OPEC"),
            ("opec members", "OPEC"),
            ("opec", "OPEC"),
            ("oil exporters", "OPEC"),
            # EU needs word boundary check to avoid matching "euro" or "neutral"
        ]

        # Special handling for "eu" - need word boundary to avoid false matches
        import re
        if re.search(r'\beu\b', query_lower) and "EU" not in detected:
            detected.append("EU")

        for pattern, region_name in region_patterns:
            if pattern in query_lower and region_name not in detected:
                detected.append(region_name)

        return detected

    @classmethod
    def expand_regions_in_query(cls, query: str) -> List[str]:
        """
        Detect regions in query and expand to all member countries.

        This is the main entry point for multi-country query processing.
        For "Compare G7 GDP", returns ["CA", "FR", "DE", "IT", "JP", "GB", "US"].

        Args:
            query: Natural language query

        Returns:
            List of all country ISO codes from detected regions (deduplicated)
        """
        regions = cls.detect_regions_in_query(query)
        all_countries: Set[str] = set()

        for region in regions:
            expanded = cls.expand_region(region)
            if expanded:
                all_countries.update(expanded)

        return list(all_countries)

    # ==========================================================================
    # ISO Code Conversion Utilities
    # ==========================================================================

    # ISO Alpha-2 to Alpha-3 mapping
    ISO2_TO_ISO3: Dict[str, str] = {
        # G7 countries
        "US": "USA", "GB": "GBR", "FR": "FRA", "DE": "DEU", "IT": "ITA", "CA": "CAN", "JP": "JPN",
        # EU members
        "AT": "AUT", "BE": "BEL", "BG": "BGR", "HR": "HRV", "CY": "CYP", "CZ": "CZE",
        "DK": "DNK", "EE": "EST", "FI": "FIN", "GR": "GRC", "HU": "HUN", "IE": "IRL",
        "LV": "LVA", "LT": "LTU", "LU": "LUX", "MT": "MLT", "NL": "NLD", "PL": "POL",
        "PT": "PRT", "RO": "ROU", "SK": "SVK", "SI": "SVN", "ES": "ESP", "SE": "SWE",
        # Other OECD
        "AU": "AUS", "CL": "CHL", "CO": "COL", "CR": "CRI", "IS": "ISL", "IL": "ISR",
        "KR": "KOR", "MX": "MEX", "NZ": "NZL", "NO": "NOR", "CH": "CHE", "TR": "TUR",
        # BRICS / Emerging
        "CN": "CHN", "IN": "IND", "BR": "BRA", "RU": "RUS", "ZA": "ZAF",
        "ID": "IDN", "SA": "SAU", "AR": "ARG", "EG": "EGY", "TH": "THA",
        "VN": "VNM", "PK": "PAK", "BD": "BGD", "NG": "NGA", "PH": "PHL",
        "MY": "MYS", "SG": "SGP", "AE": "ARE", "IR": "IRN", "ET": "ETH",
        # ASEAN
        "BN": "BRN", "KH": "KHM", "LA": "LAO", "MM": "MMR",
        # Nordic (IS already covered above)
        # Other notable
        "HK": "HKG", "TW": "TWN",
        # Caribbean
        "JM": "JAM", "TT": "TTO", "BB": "BRB", "BS": "BHS", "HT": "HTI",
        "DO": "DOM", "CU": "CUB", "GY": "GUY", "SR": "SUR", "BZ": "BLZ",
        "AG": "ATG", "DM": "DMA", "GD": "GRD", "KN": "KNA", "LC": "LCA", "VC": "VCT",
        # Latin America (additional)
        "PE": "PER", "EC": "ECU", "BO": "BOL", "PY": "PRY", "UY": "URY",
        "VE": "VEN", "GT": "GTM", "HN": "HND", "SV": "SLV", "NI": "NIC", "PA": "PAN",
        # Africa (additional)
        "GH": "GHA", "KE": "KEN", "TZ": "TZA", "UG": "UGA", "ZM": "ZMB", "ZW": "ZWE",
        "AO": "AGO", "CM": "CMR", "CI": "CIV", "SN": "SEN", "MA": "MAR", "DZ": "DZA",
        "TN": "TUN", "BW": "BWA", "NA": "NAM", "MW": "MWI", "MZ": "MOZ", "RW": "RWA",
        "CD": "COD", "CG": "COG", "MG": "MDG", "ML": "MLI", "NE": "NER",
        # Middle East (additional)
        "QA": "QAT", "KW": "KWT", "OM": "OMN", "BH": "BHR", "JO": "JOR",
        "LB": "LBN", "IQ": "IRQ", "SY": "SYR", "YE": "YEM",
        # South/Central Asia
        "NP": "NPL", "LK": "LKA", "AF": "AFG", "MN": "MNG",
        # Timor-Leste
        "TL": "TLS",
    }

    # Reverse mapping (ISO3 to ISO2)
    ISO3_TO_ISO2: Dict[str, str] = {v: k for k, v in ISO2_TO_ISO3.items()}

    # ISO Alpha-2 to UN Comtrade numeric codes
    ISO2_TO_UN_NUMERIC: Dict[str, int] = {
        # G7
        "US": 842, "GB": 826, "FR": 251, "DE": 276, "IT": 380, "CA": 124, "JP": 392,
        # EU major
        "ES": 724, "NL": 528, "BE": 56, "AT": 40, "PL": 616, "SE": 752, "DK": 208,
        "FI": 246, "PT": 620, "GR": 300, "IE": 372, "CZ": 203, "RO": 642, "HU": 348,
        "BG": 100, "HR": 191, "CY": 196, "EE": 233, "LV": 428, "LT": 440,
        "LU": 442, "MT": 470, "SK": 703, "SI": 705,
        # BRICS
        "CN": 156, "IN": 356, "BR": 76, "RU": 643, "ZA": 710,
        # ASEAN
        "ID": 360, "TH": 764, "MY": 458, "SG": 702, "PH": 608, "VN": 704,
        "MM": 104, "KH": 116, "LA": 418, "BN": 96,
        # Nordic
        "NO": 578, "IS": 352,
        # Others
        "AU": 36, "NZ": 554, "KR": 410, "MX": 484, "CH": 756, "TR": 792,
        "SA": 682, "AE": 784, "EG": 818, "AR": 32, "CL": 152, "CO": 170,
        # Caribbean
        "JM": 388, "TT": 780, "BB": 52, "BS": 44, "HT": 332, "DO": 214,
        "CU": 192, "GY": 328, "SR": 740, "BZ": 84,
        # Latin America (additional)
        "PE": 604, "EC": 218, "BO": 68, "PY": 600, "UY": 858, "VE": 862,
        "GT": 320, "HN": 340, "SV": 222, "NI": 558, "PA": 591, "CR": 188,
        # Africa
        "GH": 288, "KE": 404, "TZ": 834, "UG": 800, "ZM": 894, "ZW": 716,
        "AO": 24, "CM": 120, "CI": 384, "SN": 686, "MA": 504, "DZ": 12,
        "TN": 788, "NG": 566, "ET": 231,
        # Middle East
        "QA": 634, "KW": 414, "OM": 512, "BH": 48, "JO": 400, "LB": 422,
        "IQ": 368, "IL": 376, "IR": 364,
        # South Asia
        "PK": 586, "BD": 50, "NP": 524, "LK": 144, "AF": 4,
        # East Asia
        "HK": 344, "TW": 158, "MN": 496,
    }

    @classmethod
    def to_iso3(cls, iso2_code: str) -> Optional[str]:
        """
        Convert ISO Alpha-2 code to ISO Alpha-3 code.

        Args:
            iso2_code: ISO Alpha-2 country code (e.g., "US", "DE")

        Returns:
            ISO Alpha-3 code (e.g., "USA", "DEU") or None if not found
        """
        if not iso2_code:
            return None
        return cls.ISO2_TO_ISO3.get(iso2_code.upper())

    @classmethod
    def to_iso2(cls, iso3_code: str) -> Optional[str]:
        """
        Convert ISO Alpha-3 code to ISO Alpha-2 code.

        Args:
            iso3_code: ISO Alpha-3 country code (e.g., "USA", "DEU")

        Returns:
            ISO Alpha-2 code (e.g., "US", "DE") or None if not found
        """
        if not iso3_code:
            return None
        return cls.ISO3_TO_ISO2.get(iso3_code.upper())

    @classmethod
    def to_un_numeric(cls, iso2_code: str) -> Optional[int]:
        """
        Convert ISO Alpha-2 code to UN Comtrade numeric code.

        Args:
            iso2_code: ISO Alpha-2 country code (e.g., "US", "DE")

        Returns:
            UN numeric code (e.g., 842, 276) or None if not found
        """
        if not iso2_code:
            return None
        return cls.ISO2_TO_UN_NUMERIC.get(iso2_code.upper())

    @classmethod
    def expand_region_iso3(cls, region: str) -> Optional[List[str]]:
        """
        Expand a region name to its member countries in ISO Alpha-3 format.

        This is useful for providers that use ISO3 codes (WorldBank, IMF, OECD).

        Args:
            region: Region name (G7, BRICS, EU, etc.)

        Returns:
            List of ISO Alpha-3 codes, or None if not a recognized region
        """
        iso2_codes = cls.expand_region(region)
        if iso2_codes is None:
            return None

        iso3_codes = []
        for code in iso2_codes:
            iso3 = cls.to_iso3(code)
            if iso3:
                iso3_codes.append(iso3)
            else:
                # Fallback: try adding a letter (e.g., some edge cases)
                logger.warning(f"No ISO3 mapping for {code}, skipping")

        return iso3_codes if iso3_codes else None

    @classmethod
    def expand_region_un_numeric(cls, region: str) -> Optional[List[int]]:
        """
        Expand a region name to its member countries in UN Comtrade numeric format.

        This is useful for the Comtrade provider which uses numeric codes.

        Args:
            region: Region name (G7, BRICS, EU, etc.)

        Returns:
            List of UN numeric codes, or None if not a recognized region
        """
        iso2_codes = cls.expand_region(region)
        if iso2_codes is None:
            return None

        un_codes = []
        for code in iso2_codes:
            un_code = cls.to_un_numeric(code)
            if un_code:
                un_codes.append(un_code)
            else:
                logger.warning(f"No UN numeric mapping for {code}, skipping")

        return un_codes if un_codes else None

    @classmethod
    def get_region_expansion(
        cls,
        region: str,
        format: str = "iso2"
    ) -> Optional[List]:
        """
        Universal region expansion method supporting multiple output formats.

        This is the SINGLE ENTRY POINT for all providers to get region expansions.
        Providers should NOT maintain their own region mappings.

        Args:
            region: Region name (G7, BRICS, EU, ASEAN, Nordic, OECD, etc.)
            format: Output format - "iso2", "iso3", "un_numeric"

        Returns:
            List of country codes in the requested format, or None if not recognized

        Examples:
            >>> CountryResolver.get_region_expansion("G7", "iso2")
            ['CA', 'FR', 'DE', 'IT', 'JP', 'GB', 'US']

            >>> CountryResolver.get_region_expansion("G7", "iso3")
            ['CAN', 'FRA', 'DEU', 'ITA', 'JPN', 'GBR', 'USA']

            >>> CountryResolver.get_region_expansion("G7", "un_numeric")
            [124, 251, 276, 380, 392, 826, 842]
        """
        format_lower = format.lower().strip()

        if format_lower in ("iso2", "alpha2", "alpha-2"):
            return cls.expand_region(region)
        elif format_lower in ("iso3", "alpha3", "alpha-3"):
            return cls.expand_region_iso3(region)
        elif format_lower in ("un_numeric", "un", "numeric", "comtrade"):
            return cls.expand_region_un_numeric(region)
        else:
            logger.warning(f"Unknown format '{format}', defaulting to iso2")
            return cls.expand_region(region)
