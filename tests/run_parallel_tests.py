"""
Parallel Testing Framework for Prompt Comparison

Uses concurrent execution to test queries faster.
Tracks errors, fixes, and manual verification requirements.
"""
import asyncio
import json
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
import concurrent.futures

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.services.query import QueryService
from backend.config import get_settings

# Test queries from PROMPT_COMPARISON_TESTING.md
BATCH_SIZE = 20
MAX_CONCURRENT = 5  # Max parallel queries to avoid rate limits


class ParallelTester:
    """Runs tests in parallel and tracks results"""

    def __init__(self):
        self.settings = get_settings()
        self.results = []
        self.errors = []
        self.manual_verification_needed = []

    async def test_single_query(
        self,
        query_id: int,
        query: str,
        provider_hint: str,
        batch_num: int
    ) -> Dict[str, Any]:
        """Test a single query"""
        print(f"[Batch {batch_num}] Query {query_id}: {query[:60]}...")

        try:
            query_service = QueryService(
                openrouter_key=self.settings.openrouter_api_key,
                fred_key=self.settings.fred_api_key,
                comtrade_key=self.settings.comtrade_api_key,
                coingecko_key=self.settings.coingecko_api_key,
                settings=self.settings
            )

            start_time = asyncio.get_event_loop().time()
            response = await query_service.process_query(query)
            elapsed = asyncio.get_event_loop().time() - start_time

            result = {
                "query_id": query_id,
                "batch": batch_num,
                "query": query,
                "provider_hint": provider_hint,
                "success": True,
                "elapsed_time": elapsed,
                "provider_selected": response.intent.apiProvider if response.intent else None,
                "data_count": len(response.data) if response.data else 0,
                "error": None,
                "needs_manual_verification": self._needs_verification(query, provider_hint),
            }

            # Check if provider routing is correct
            if response.intent:
                expected_upper = provider_hint.upper()
                actual_upper = response.intent.apiProvider.upper()
                result["provider_correct"] = actual_upper == expected_upper

                if not result["provider_correct"]:
                    print(f"  ⚠️  Provider mismatch: Expected {provider_hint}, got {response.intent.apiProvider}")
                    self.errors.append({
                        "query_id": query_id,
                        "query": query,
                        "error_type": "provider_mismatch",
                        "expected": provider_hint,
                        "actual": response.intent.apiProvider,
                    })

            # Track queries needing manual verification
            if result["needs_manual_verification"]:
                self.manual_verification_needed.append({
                    "query_id": query_id,
                    "query": query,
                    "provider": provider_hint,
                    "data_count": result["data_count"],
                })

            print(f"  ✅ Success (Provider: {result['provider_selected']}, {result['data_count']} datasets, {elapsed:.2f}s)")
            return result

        except Exception as e:
            print(f"  ❌ Error: {str(e)}")
            error_result = {
                "query_id": query_id,
                "batch": batch_num,
                "query": query,
                "provider_hint": provider_hint,
                "success": False,
                "elapsed_time": 0,
                "provider_selected": None,
                "data_count": 0,
                "error": str(e),
                "needs_manual_verification": False,
            }

            self.errors.append({
                "query_id": query_id,
                "query": query,
                "error_type": "exception",
                "error_message": str(e),
            })

            return error_result

    def _needs_verification(self, query: str, provider: str) -> bool:
        """Determine if query needs manual data verification"""
        # Trade flows always need verification
        if provider.upper() == "COMTRADE":
            return True

        # GDP figures should be verified
        if any(term in query.lower() for term in ["gdp", "gross domestic"]):
            return True

        # Housing prices should be verified
        if any(term in query.lower() for term in ["house price", "housing", "property price"]):
            return True

        # Government debt should be verified
        if any(term in query.lower() for term in ["debt", "deficit", "fiscal"]):
            return True

        return False

    async def run_batch(
        self,
        queries: List[tuple],
        batch_num: int
    ) -> List[Dict[str, Any]]:
        """Run a batch of queries with limited concurrency"""
        print(f"\n{'='*80}")
        print(f"BATCH {batch_num}: Testing {len(queries)} queries")
        print(f"{'='*80}\n")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT)

        async def limited_test(query_id, query, provider):
            async with semaphore:
                return await self.test_single_query(query_id, query, provider, batch_num)

        tasks = [
            limited_test(query_id, query, provider)
            for query_id, (query, provider) in queries
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle exceptions
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                query_id, (query, provider) = queries[i]
                processed_results.append({
                    "query_id": query_id,
                    "batch": batch_num,
                    "query": query,
                    "provider_hint": provider,
                    "success": False,
                    "error": str(result),
                })
            else:
                processed_results.append(result)

        # Batch summary
        success_count = sum(1 for r in processed_results if r.get("success", False))
        avg_time = sum(r.get("elapsed_time", 0) for r in processed_results) / len(processed_results)

        print(f"\n{'='*80}")
        print(f"Batch {batch_num} Summary:")
        print(f"  ✅ Success: {success_count}/{len(processed_results)}")
        print(f"  ❌ Errors: {len(processed_results) - success_count}")
        print(f"  ⏱️  Avg Time: {avg_time:.2f}s")
        print(f"{'='*80}\n")

        return processed_results

    def save_results(self, output_file: str):
        """Save results to JSON file"""
        output_data = {
            "timestamp": datetime.now().isoformat(),
            "total_queries": len(self.results),
            "successful": sum(1 for r in self.results if r.get("success", False)),
            "errors": len(self.errors),
            "manual_verification_needed": len(self.manual_verification_needed),
            "results": self.results,
            "error_details": self.errors,
            "manual_verification_list": self.manual_verification_needed,
        }

        with open(output_file, 'w') as f:
            json.dump(output_data, f, indent=2)

        print(f"✅ Results saved to: {output_file}")

    def save_tracking_markdown(self):
        """Update PROMPT_COMPARISON_TESTING.md with results"""
        tracking_file = Path(__file__).parent.parent / "PROMPT_COMPARISON_TESTING.md"

        error_table_rows = []
        for error in self.errors:
            error_table_rows.append(
                f"| {error['query_id']} | {error['query'][:50]}... | "
                f"{error.get('error_type', 'unknown')} | 🔴 Open | - | ❌ |"
            )

        manual_verification_rows = []
        for item in self.manual_verification_needed:
            manual_verification_rows.append(
                f"| {item['query_id']} | {item['query'][:40]}... | TBD | TBD | TBD | "
                f"[Source needed] | ❌ |"
            )

        # Read current content
        if tracking_file.exists():
            with open(tracking_file, 'r') as f:
                content = f.read()

            # Update error tracking section
            if error_table_rows:
                error_section = "\n".join(error_table_rows)
                content = content.replace(
                    "| | | | | | |",
                    error_section,
                    1  # Replace only first occurrence in Critical Errors section
                )

            # Update manual verification section
            if manual_verification_rows:
                verification_section = "\n".join(manual_verification_rows)
                # Find and replace in manual verification table
                # This is a simplified approach - you may need to adjust based on actual structure

            # Write back
            with open(tracking_file, 'w') as f:
                f.write(content)

            print(f"✅ Updated tracking document: {tracking_file}")


async def main():
    """Main test runner"""
    print(f"\n{'='*80}")
    print("PARALLEL PROMPT COMPARISON TESTING")
    print(f"{'='*80}\n")

    # Load test queries
    from comprehensive_test_suite_100 import TEST_QUERIES

    # Flatten queries
    all_queries = []
    query_id = 1
    for provider, queries in TEST_QUERIES.items():
        for query in queries:
            all_queries.append((query_id, (query, provider)))
            query_id += 1

    print(f"Total queries to test: {len(all_queries)}")
    print(f"Batch size: {BATCH_SIZE}")
    print(f"Max concurrent requests: {MAX_CONCURRENT}")
    print(f"Estimated batches: {(len(all_queries) + BATCH_SIZE - 1) // BATCH_SIZE}\n")

    tester = ParallelTester()

    # Run in batches
    batch_num = 1
    for i in range(0, len(all_queries), BATCH_SIZE):
        batch = all_queries[i:i+BATCH_SIZE]
        batch_results = await tester.run_batch(batch, batch_num)
        tester.results.extend(batch_results)
        batch_num += 1

        # Small delay between batches
        if i + BATCH_SIZE < len(all_queries):
            print("⏸️  Pausing 3 seconds before next batch...")
            await asyncio.sleep(3)

    # Save results
    output_file = "test_results.json"
    tester.save_results(output_file)

    # Update tracking document
    tester.save_tracking_markdown()

    # Final summary
    print(f"\n{'='*80}")
    print("FINAL SUMMARY")
    print(f"{'='*80}")
    print(f"Total Queries: {len(tester.results)}")
    print(f"Successful: {sum(1 for r in tester.results if r.get('success', False))}")
    print(f"Errors: {len(tester.errors)}")
    print(f"Manual Verification Needed: {len(tester.manual_verification_needed)}")
    print(f"{'='*80}\n")

    if tester.errors:
        print("❌ ERRORS FOUND - Review error_details in output file")
        return 1
    else:
        print("✅ ALL TESTS PASSED")
        return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
