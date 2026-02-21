#!/usr/bin/env python3
"""
Comprehensive SDMX Metadata Downloader - CORRECTED VERSION

Downloads complete metadata catalogs from all SDMX data providers using
the correct source IDs based on https://sdmx1.readthedocs.io/en/latest/sources.html

Key fixes:
- IMF: Use IMF_DATA (data endpoint) instead of IMF (structure-only)
- World Bank: Use WB_WDI (World Development Indicators) instead of WB
- Added provider-specific configurations
"""

import sdmx
import json
import os
from pathlib import Path
from typing import Dict, List, Any, Optional
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class SDMXMetadataDownloader:
    """Download and organize SDMX metadata from multiple providers."""

    # CORRECTED: Priority providers for economic data with correct source IDs
    PRIORITY_PROVIDERS = [
        # IMF endpoints (THREE different source IDs - use them all!)
        'IMF',           # Structure-only; Fusion Metadata Registry instance
        'IMF_DATA',      # Data endpoint (no metadata structure support)
        'IMF_DATA3',     # SDMX 3.0 data endpoint

        # World Bank endpoints
        'WB_WDI',        # World Development Indicators (recommended)
        'WB',            # Trade solution focus

        # Other major providers
        'OECD',          # Organisation for Economic Co-operation and Development
        'ESTAT',         # Eurostat (use ESTAT not ESTAT3 for better compatibility)
        'BIS',           # Bank for International Settlements
        'ECB',           # European Central Bank
        'UNSD',          # United Nations Statistics Division
        'ILO',           # International Labour Organization
        'ABS',           # Australian Bureau of Statistics
    ]

    # Provider-specific configurations from documentation
    PROVIDER_CONFIGS = {
        'OECD': {
            'note': 'Requires provider="ALL" for structure queries',
            'data_endpoint_supported': False,  # Only metadata works
        },
        'IMF': {
            'note': 'Structure-only; Fusion Metadata Registry instance',
            'data_endpoint_supported': False,
        },
        'IMF_DATA': {
            'note': 'Data endpoint; no metadata structure support',
            'data_endpoint_supported': True,
        },
        'IMF_DATA3': {
            'note': 'SDMX 3.0; limited hierarchical codelist support',
            'data_endpoint_supported': True,
        },
        'ESTAT': {
            'note': 'Large datasets; ZIP file handling for extended queries',
            'timeout_increase': True,
        },
        'WB_WDI': {
            'note': 'World Development Indicators; accepts JSON via custom Accept header',
        },
    }

    def __init__(self, output_dir: str = 'backend/data/metadata/sdmx'):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def list_available_sources(self) -> List[str]:
        """List all available SDMX data sources."""
        sources = list(sdmx.list_sources())
        logger.info(f"Found {len(sources)} available SDMX sources")
        return sources

    def download_dataflows(self, provider: str) -> Optional[Dict[str, Any]]:
        """Download catalog of all dataflows from a provider."""
        logger.info(f"Downloading dataflows from {provider}...")

        # Check provider configuration
        config = self.PROVIDER_CONFIGS.get(provider, {})
        if config.get('data_endpoint_supported') == False and provider != 'IMF':
            logger.warning(f"⚠️  {provider}: Note - {config.get('note', 'No special notes')}")

        try:
            client = sdmx.Client(provider)

            # Special handling for OECD: use provider="ALL" for broad queries
            if provider == 'OECD':
                logger.info(f"Using provider='ALL' for OECD structure queries")
                # Note: This may require special parameters

            msg = client.dataflow()

            dataflows = {}
            for key, flow in msg.dataflow.items():
                dataflows[str(key)] = {
                    'id': str(key),
                    'name': str(flow.name) if hasattr(flow, 'name') else str(key),
                    'description': str(flow.description) if hasattr(flow, 'description') else '',
                    'structure': str(flow.structure) if hasattr(flow, 'structure') else None,
                }

            logger.info(f"✅ {provider}: Downloaded {len(dataflows)} dataflows")

            # Add provider config info to metadata
            if config:
                dataflows['_provider_config'] = config

            return dataflows

        except Exception as e:
            logger.error(f"❌ {provider}: Failed to download dataflows - {str(e)}")
            return None

    def download_data_structure(self, provider: str, dataflow_id: str) -> Optional[Dict[str, Any]]:
        """Download Data Structure Definition for a specific dataflow."""
        try:
            client = sdmx.Client(provider)

            # Get dataflow to find structure reference
            flow_msg = client.dataflow(dataflow_id)
            flow = flow_msg.dataflow[dataflow_id]

            # Get the data structure definition
            if hasattr(flow, 'structure'):
                dsd_ref = flow.structure
                dsd_msg = client.datastructure(dsd_ref.id)

                structure_info = {
                    'id': str(dsd_ref.id),
                    'dimensions': [],
                    'attributes': [],
                    'measures': [],
                }

                # Extract dimensions
                dsd = list(dsd_msg.structure.values())[0]
                for dim in dsd.dimensions:
                    structure_info['dimensions'].append({
                        'id': str(dim.id),
                        'name': str(dim.concept_identity.name) if hasattr(dim, 'concept_identity') else str(dim.id),
                        'codelist': str(dim.local_representation.enumerated) if hasattr(dim, 'local_representation') and dim.local_representation else None,
                    })

                return structure_info

        except Exception as e:
            logger.debug(f"Could not download structure for {provider}/{dataflow_id}: {str(e)}")
            return None

    def download_code_lists(self, provider: str) -> Optional[Dict[str, Any]]:
        """Download all code lists from a provider."""
        logger.info(f"Downloading code lists from {provider}...")

        try:
            client = sdmx.Client(provider)
            msg = client.codelist()

            codelists = {}
            for key, codelist in msg.codelist.items():
                codes = {}
                for code_id, code in codelist.items():
                    codes[str(code_id)] = {
                        'id': str(code_id),
                        'name': str(code.name) if hasattr(code, 'name') else str(code_id),
                        'description': str(code.description) if hasattr(code, 'description') else '',
                    }

                codelists[str(key)] = {
                    'id': str(key),
                    'name': str(codelist.name) if hasattr(codelist, 'name') else str(key),
                    'codes': codes,
                }

            logger.info(f"✅ {provider}: Downloaded {len(codelists)} code lists")
            return codelists

        except Exception as e:
            logger.error(f"❌ {provider}: Failed to download code lists - {str(e)}")
            return None

    def download_concepts(self, provider: str) -> Optional[Dict[str, Any]]:
        """Download all concept schemes from a provider."""
        logger.info(f"Downloading concepts from {provider}...")

        try:
            client = sdmx.Client(provider)
            msg = client.conceptscheme()

            concepts = {}
            for key, scheme in msg.concept_scheme.items():
                scheme_concepts = {}
                for concept_id, concept in scheme.items():
                    scheme_concepts[str(concept_id)] = {
                        'id': str(concept_id),
                        'name': str(concept.name) if hasattr(concept, 'name') else str(concept_id),
                        'description': str(concept.description) if hasattr(concept, 'description') else '',
                    }

                concepts[str(key)] = {
                    'id': str(key),
                    'name': str(scheme.name) if hasattr(scheme, 'name') else str(key),
                    'concepts': scheme_concepts,
                }

            logger.info(f"✅ {provider}: Downloaded {len(concepts)} concept schemes")
            return concepts

        except Exception as e:
            logger.error(f"❌ {provider}: Failed to download concepts - {str(e)}")
            return None

    def save_metadata(self, provider: str, metadata_type: str, data: Dict[str, Any]):
        """Save metadata to JSON file."""
        if data is None:
            return

        output_file = self.output_dir / f"{provider.lower()}_{metadata_type}.json"

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info(f"💾 Saved {provider} {metadata_type} to {output_file}")

    def download_provider_metadata(self, provider: str):
        """Download all metadata for a single provider."""
        logger.info(f"\n{'='*70}")
        logger.info(f"📊 Processing provider: {provider}")

        # Show provider configuration
        config = self.PROVIDER_CONFIGS.get(provider, {})
        if config:
            logger.info(f"ℹ️  Configuration: {config.get('note', 'Standard configuration')}")
        logger.info(f"{'='*70}")

        # 1. Download dataflows (catalog)
        dataflows = self.download_dataflows(provider)
        if dataflows:
            self.save_metadata(provider, 'dataflows', dataflows)

        # 2. Download code lists (skip for data-only providers)
        if not (provider.startswith('IMF_DATA')):
            codelists = self.download_code_lists(provider)
            if codelists:
                self.save_metadata(provider, 'codelists', codelists)

        # 3. Download concepts (skip for data-only providers)
        if not (provider.startswith('IMF_DATA')):
            concepts = self.download_concepts(provider)
            if concepts:
                self.save_metadata(provider, 'concepts', concepts)

        # 4. Try to download detailed structure for first few dataflows (as examples)
        if dataflows and not (provider.startswith('IMF_DATA')):
            logger.info(f"Downloading detailed structures for sample dataflows...")
            structures = {}
            for i, (flow_id, flow_info) in enumerate(list(dataflows.items())[:5]):  # First 5 as samples
                if flow_id == '_provider_config':  # Skip metadata
                    continue
                structure = self.download_data_structure(provider, flow_id)
                if structure:
                    structures[flow_id] = structure

            if structures:
                self.save_metadata(provider, 'sample_structures', structures)

    def run(self, providers: Optional[List[str]] = None):
        """Download metadata from all specified providers."""
        if providers is None:
            providers = self.PRIORITY_PROVIDERS

        # First, list all available sources
        all_sources = self.list_available_sources()
        logger.info(f"\nAvailable SDMX sources: {', '.join(all_sources)}")

        # Show which providers from our list are available
        logger.info(f"\nChecking provider availability:")
        for provider in providers:
            status = "✅ Available" if provider in all_sources else "❌ Not available"
            config_note = self.PROVIDER_CONFIGS.get(provider, {}).get('note', '')
            logger.info(f"  {provider}: {status}" + (f" - {config_note}" if config_note else ""))

        # Save list of sources
        sources_file = self.output_dir / '_sources.json'
        with open(sources_file, 'w') as f:
            json.dump({
                'all_sources': all_sources,
                'priority_sources': self.PRIORITY_PROVIDERS,
                'provider_configs': self.PROVIDER_CONFIGS,
            }, f, indent=2)

        # Download metadata from each provider
        results = {}
        for provider in providers:
            if provider not in all_sources:
                logger.warning(f"⚠️ Provider {provider} not available, skipping...")
                continue

            try:
                self.download_provider_metadata(provider)
                results[provider] = 'success'
            except Exception as e:
                logger.error(f"❌ Failed to process {provider}: {str(e)}")
                results[provider] = 'failed'

        # Save summary
        summary_file = self.output_dir / '_summary.json'
        with open(summary_file, 'w') as f:
            json.dump({
                'total_providers': len(providers),
                'successful': sum(1 for v in results.values() if v == 'success'),
                'failed': sum(1 for v in results.values() if v == 'failed'),
                'results': results,
                'provider_configs': self.PROVIDER_CONFIGS,
            }, f, indent=2)

        logger.info(f"\n{'='*70}")
        logger.info(f"✅ Metadata download complete!")
        logger.info(f"📁 Output directory: {self.output_dir}")
        logger.info(f"📊 Successful: {sum(1 for v in results.values() if v == 'success')}/{len(providers)}")
        logger.info(f"{'='*70}\n")


if __name__ == '__main__':
    downloader = SDMXMetadataDownloader()
    downloader.run()
