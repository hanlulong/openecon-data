# Query correctness follow-up

The September 13 reliability check also exercised the broader query suite. Both the original code and the first repair candidate passed 438 tests and failed the same seven. Investigation distinguished obsolete fixtures from concrete behavior defects. The reviewed follow-up plan addresses those defects without introducing provider preferences or keyword-based final indicator decisions.

## Requested time windows

An unwindowed diagnostic may discover observations that a provider failed to return with date parameters. Finding a 2023 observation does not establish absence of data within 2021–2024. Consolidate the duplicate diagnostic at the resolved dispatch boundary, preserve the original provider/series/country/filter identity, and make at most one diagnostic per failed attempt.

Recover only usable, dated, finite observations inside the original bounds through normal validation and caching under the original bounded request identity. Work on copied observations. Null values are missing observations and are ignored. Unknown or malformed dates and raw nonfinite values cannot establish recovery or prove every observation lies outside the window. The diagnostic uses existing period normalization and validates ISO calendar dates before retaining the existing year-level comparisons. If the evidence shows only older/newer observations, retain the non-retriable window marker with neutral latest-available wording. Preserve the existing year-granularity convention for annual/quarterly data and implicit/default sparse-history behavior.

## Country-correct clarification

The clarification collector must apply the selector's existing country constraint for a single requested and resolved country. A China request should not offer explicitly United States or Germany series. Preserve matching and country-neutral candidates, their associated scores, existing missing-score defaults, and normal executability checks. Do not add country maps, provider preferences, or thresholds. Unknown and multiple-country behavior remains outside this narrow correction.

Formatting tests use deterministic options and verify actionable identities and conversation state. Retrieval correctness is covered separately with the actual country helper. Provider appearance in a live ranking is not a correctness criterion.

## Geographic scope

Europe and the European Union are different geographic scopes. Remove aliases that silently replace the former with the latter, including the corresponding rewrite shortcut. Preserve explicit EU/European Union and euro-area aggregates and member comparisons. The semantic parser must preserve the requested geographic entity and ask when materially different interpretations remain unresolved. A valid parser clarification must not be overwritten by an incorrect regional expansion.

Do not replace the removed shortcut with a forced keyword menu or a new continent membership list. Verify actual Europe, explicit union/euro-area, and member-comparison requests before claiming production correctness; resolver-only tests are insufficient.

Provider normalization must preserve the same distinction. World Bank's EUU, ECS and EMU codes identify the European Union, Europe and Central Asia, and euro area respectively. Remove false continent aliases and incomplete fallback member lists; use the central resolver for explicit member groups. Complete phrase matching must not read `EU` inside `Europe` or expand an incomplete group name into a different scope. Normalize spaces and underscores consistently for established provider aliases. Test the real normalization and fetch paths, including explicit aggregate codes and ordinary country controls.

Live acceptance also exposed a separate override after parsing: correct scalar union and euro-area labels were replaced with all their members. Preserve the parser's requested entities and scope questions. A group aggregate remains one entity, including when compared with individual countries; an explicit member comparison retains its member list. Normalize exact established aggregate labels and codes using existing provider reference data after routing and before execution plans and cache requests are built. Requested and returned geography checks must recognize the same identity while continuing to reject a different aggregate or member set. Do not recover an unavailable aggregate by silently serving members or infer aggregate intent from matching membership lists.

## Fixtures and acceptance

Positive IMF, StatsCan and oil fixtures must provide coherent selector decisions and realistic returned data while preserving real selector helpers and dispatch checks. Test selected, fetched and served identities, country/dimensions, units and values. Preserve separate ambiguous/unsupported cases. Fallback fixtures must preserve the economic concept and requested country without fixing incidental query wording.

Record failing regressions before behavior changes. Require focused query/window/country/region/prompt checks, the complete query suite, retained website/MCP/index tests, three independent final reviews, and sequential service deployment with live acceptance checks. Keep the proven metadata/vector repair and ingress protections intact. Exact server evidence, staged hashes and rollback snapshots live in the protected recovery directory; they are not part of the public repository.
