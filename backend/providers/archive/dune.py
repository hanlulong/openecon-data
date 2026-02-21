from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime
from collections import defaultdict

import httpx

from ..models import DataPoint, Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError


class DuneProvider:
    """
    Dune Analytics SIM API Provider

    Provides comprehensive blockchain data across 60+ chains:

    **Wallet-Level Data:**
    - Token balances (ERC20, native tokens) with USD values
    - Transaction history and activity feed
    - NFT holdings (ERC721/ERC1155)

    **Token-Level Data:**
    - Token metadata (name, symbol, decimals, logo)
    - Real-time pricing and market cap
    - Token holder distribution

    **Supported Chains:**
    - EVM: Ethereum, Polygon, Arbitrum, Optimism, Base, Avalanche, BSC, and 50+ more
    - SVM: Solana
    """

    BASE_URL_EVM = "https://api.sim.dune.com/v1/evm"
    BASE_URL_SVM = "https://api.sim.dune.com/beta/svm"

    # Chain ID mapping (common chains)
    CHAIN_IDS = {
        "ethereum": 1,
        "eth": 1,
        "optimism": 10,
        "bsc": 56,
        "binance": 56,
        "gnosis": 100,
        "polygon": 137,
        "matic": 137,
        "fantom": 250,
        "base": 8453,
        "arbitrum": 42161,
        "arb": 42161,
        "avalanche": 43114,
        "avax": 43114,
        "linea": 59144,
        "zksync": 324,
    }

    def __init__(self, api_key: Optional[str]) -> None:
        self.api_key = api_key
        if not self.api_key:
            print("⚠️  Dune API key not provided. Blockchain data queries will fail.")

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers with API key"""
        return {
            "X-Sim-Api-Key": self.api_key,
            "Accept": "application/json",
        }

    def _normalize_chain(self, chain: Optional[str]) -> str:
        """Normalize chain name to standard format"""
        if not chain:
            return "ethereum"

        chain_lower = chain.lower().strip()
        # Return the key name from CHAIN_IDS or the original value
        for name, chain_id in self.CHAIN_IDS.items():
            if chain_lower == name:
                return name
        return chain_lower

    def _get_chain_id(self, chain: str) -> int:
        """Get numeric chain ID from chain name"""
        chain_normalized = self._normalize_chain(chain)
        return self.CHAIN_IDS.get(chain_normalized, 1)  # Default to Ethereum

    def _validate_address(self, address: str, chain_type: str = "evm") -> str:
        """Basic address validation"""
        address = address.strip()

        if chain_type == "evm":
            # EVM addresses should start with 0x and be 42 characters
            if not address.startswith("0x"):
                address = "0x" + address
            if len(address) != 42:
                raise ValueError(
                    f"Invalid EVM address: {address}. Must be 40 hex characters (with or without 0x prefix)"
                )
        elif chain_type == "svm":
            # Solana addresses are base58 encoded, typically 32-44 characters
            if len(address) < 32 or len(address) > 44:
                raise ValueError(
                    f"Invalid Solana address: {address}. Must be 32-44 base58 characters"
                )

        return address

    async def fetch_balances(self, params: Dict[str, Any]) -> NormalizedData:
        """
        Fetch token balances for a wallet address

        Parameters:
        - address: Wallet address (required)
        - chain: Blockchain name (optional, defaults to 'ethereum')
        - chainType: 'evm' or 'svm' (optional, auto-detected)
        """
        address = params.get("address")
        if not address:
            raise ValueError("Wallet address is required for balance queries")

        chain = self._normalize_chain(params.get("chain"))
        chain_type = params.get("chainType", "evm" if chain != "solana" else "svm")
        address = self._validate_address(address, chain_type)

        # Build endpoint
        if chain_type == "svm":
            endpoint = f"{self.BASE_URL_SVM}/balances/{address}"
        else:
            endpoint = f"{self.BASE_URL_EVM}/balances/{address}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(endpoint, headers=self._get_headers())
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise DataNotAvailableError(
                        f"Address {address} not found or has no balance data"
                    )
                elif e.response.status_code == 401:
                    raise DataNotAvailableError(
                        "Invalid Dune API key. Please check your configuration."
                    )
                elif e.response.status_code == 429:
                    raise DataNotAvailableError(
                        "Rate limit exceeded. Please try again later."
                    )
                raise DataNotAvailableError(
                    f"Dune API error: {e.response.status_code} - {e.response.text}"
                )

        balances = data.get("balances", [])
        if not balances:
            raise DataNotAvailableError(f"No balance data found for address {address}")

        # Calculate total portfolio value
        current_date = datetime.utcnow().date().isoformat()
        total_usd_value = 0.0

        for balance in balances:
            amount_raw = balance.get("amount", 0)
            decimals = balance.get("decimals", 18)
            usd_value = balance.get("price_usd", 0) or 0

            try:
                amount = float(amount_raw) / (10**decimals) if amount_raw else 0
            except (ValueError, TypeError, ZeroDivisionError):
                amount = 0

            if usd_value and amount:
                token_value = amount * float(usd_value)

                # Filter out suspicious tokens (likely spam/scam tokens)
                # Skip tokens with individual values > $100M (unrealistic for single token holdings)
                # This filters out meme coins with fake valuations
                if token_value > 100_000_000:
                    continue

                total_usd_value += token_value

        data_points = [DataPoint(date=current_date, value=total_usd_value)]

        metadata = Metadata(
            source="Dune Analytics",
            indicator=f"Portfolio Value - {address[:6]}...{address[-4:]}",
            country=None,
            frequency="snapshot",
            unit="USD",
            lastUpdated=current_date,
            seriesId=address,
            apiUrl=endpoint.replace(self.api_key, "***"),
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_activity(self, params: Dict[str, Any]) -> NormalizedData:
        """
        Fetch transaction activity feed for a wallet

        Shows decoded activity: receives, sends, swaps, NFT transfers
        More human-readable than raw transactions

        Parameters:
        - address: Wallet address (required)
        - limit: Max results (default 100)
        """
        address = params.get("address")
        if not address:
            raise ValueError("Wallet address is required for activity queries")

        address = self._validate_address(address, "evm")
        limit = params.get("limit", 100)

        endpoint = f"{self.BASE_URL_EVM}/activity/{address}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    endpoint,
                    params={"limit": limit},
                    headers=self._get_headers()
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise DataNotAvailableError(
                        f"No activity found for address {address}"
                    )
                elif e.response.status_code == 401:
                    raise DataNotAvailableError("Invalid Dune API key.")
                raise DataNotAvailableError(f"Dune API error: {e.response.status_code}")

        activities = data.get("activity", [])
        if not activities:
            raise DataNotAvailableError(f"No activity data found for address {address}")

        # Group activities by date
        activity_by_date = defaultdict(int)
        for activity in activities:
            block_time = activity.get("block_time")
            if block_time:
                try:
                    date = datetime.fromisoformat(block_time.replace("Z", "+00:00")).date().isoformat()
                    activity_by_date[date] += 1
                except (ValueError, TypeError):
                    continue

        data_points = [
            DataPoint(date=date, value=float(count))
            for date, count in sorted(activity_by_date.items())
        ]

        if not data_points:
            raise DataNotAvailableError(
                f"Could not parse activity dates for address {address}"
            )

        metadata = Metadata(
            source="Dune Analytics",
            indicator=f"Activity - {address[:6]}...{address[-4:]}",
            country=None,
            frequency="daily",
            unit="transactions",
            lastUpdated=datetime.utcnow().date().isoformat(),
            seriesId=address,
            apiUrl=endpoint.replace(self.api_key, "***"),
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_transactions(self, params: Dict[str, Any]) -> NormalizedData:
        """
        Fetch raw transaction data for a wallet

        Shows granular blockchain data: gas, nonce, block info

        Parameters:
        - address: Wallet address (required)
        - limit: Max results (default 100)
        """
        address = params.get("address")
        if not address:
            raise ValueError("Wallet address is required for transaction queries")

        address = self._validate_address(address, "evm")
        limit = params.get("limit", 100)

        endpoint = f"{self.BASE_URL_EVM}/transactions/{address}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    endpoint,
                    params={"limit": limit},
                    headers=self._get_headers()
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise DataNotAvailableError(
                        f"No transactions found for address {address}"
                    )
                elif e.response.status_code == 401:
                    raise DataNotAvailableError("Invalid Dune API key.")
                raise DataNotAvailableError(f"Dune API error: {e.response.status_code}")

        transactions = data.get("transactions", [])
        if not transactions:
            raise DataNotAvailableError(
                f"No transaction data found for address {address}"
            )

        # Group by date and count
        tx_by_date = defaultdict(int)
        for tx in transactions:
            block_time = tx.get("block_time")
            if block_time:
                try:
                    date = datetime.fromisoformat(block_time.replace("Z", "+00:00")).date().isoformat()
                    tx_by_date[date] += 1
                except (ValueError, TypeError):
                    continue

        data_points = [
            DataPoint(date=date, value=float(count))
            for date, count in sorted(tx_by_date.items())
        ]

        if not data_points:
            raise DataNotAvailableError(
                f"Could not parse transaction dates for address {address}"
            )

        metadata = Metadata(
            source="Dune Analytics",
            indicator=f"Transactions - {address[:6]}...{address[-4:]}",
            country=None,
            frequency="daily",
            unit="transactions",
            lastUpdated=datetime.utcnow().date().isoformat(),
            seriesId=address,
            apiUrl=endpoint.replace(self.api_key, "***"),
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_nft_holdings(self, params: Dict[str, Any]) -> NormalizedData:
        """
        Fetch NFT holdings (collectibles) for a wallet

        Parameters:
        - address: Wallet address (required)
        """
        address = params.get("address")
        if not address:
            raise ValueError("Wallet address is required for NFT queries")

        address = self._validate_address(address, "evm")
        endpoint = f"{self.BASE_URL_EVM}/collectibles/{address}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(endpoint, headers=self._get_headers())
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise DataNotAvailableError(
                        f"No NFT holdings found for address {address}"
                    )
                elif e.response.status_code == 401:
                    raise DataNotAvailableError("Invalid Dune API key.")
                raise DataNotAvailableError(f"Dune API error: {e.response.status_code}")

        collectibles = data.get("collectibles", [])
        if not collectibles:
            raise DataNotAvailableError(f"No NFT data found for address {address}")

        # Count total NFTs
        total_nfts = len(collectibles)
        current_date = datetime.utcnow().date().isoformat()

        data_points = [DataPoint(date=current_date, value=float(total_nfts))]

        metadata = Metadata(
            source="Dune Analytics",
            indicator=f"NFT Holdings - {address[:6]}...{address[-4:]}",
            country=None,
            frequency="snapshot",
            unit="NFTs",
            lastUpdated=current_date,
            seriesId=address,
            apiUrl=endpoint.replace(self.api_key, "***"),
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_token_info(self, params: Dict[str, Any]) -> NormalizedData:
        """
        Fetch token metadata and pricing information

        Parameters:
        - tokenAddress: Token contract address (required)
        - chain: Chain name (optional, defaults to 'ethereum')
        - chains: List of chains for multi-chain lookup (optional)
        """
        token_address = params.get("tokenAddress") or params.get("address")
        if not token_address:
            raise ValueError("Token address is required for token info queries")

        token_address = self._validate_address(token_address, "evm")

        # Determine chain IDs
        chains = params.get("chains", [params.get("chain", "ethereum")])
        chain_ids = [str(self._get_chain_id(chain)) for chain in chains]
        chain_ids_param = ",".join(chain_ids)

        endpoint = f"{self.BASE_URL_EVM}/token-info/{token_address}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    endpoint,
                    params={"chain_ids": chain_ids_param},
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise DataNotAvailableError(
                        f"Token {token_address} not found on specified chains"
                    )
                elif e.response.status_code == 401:
                    raise DataNotAvailableError("Invalid Dune API key.")
                raise DataNotAvailableError(f"Dune API error: {e.response.status_code}")

        tokens = data.get("tokens", [])
        if not tokens:
            raise DataNotAvailableError(
                f"No token info found for {token_address} on chains {chain_ids_param}"
            )

        # Use first token's price (or aggregate if multiple chains)
        token = tokens[0]
        price_usd = token.get("price_usd", 0)
        symbol = token.get("symbol", "Unknown")
        name = token.get("name", "Unknown Token")

        current_date = datetime.utcnow().date().isoformat()
        data_points = [DataPoint(date=current_date, value=float(price_usd or 0))]

        metadata = Metadata(
            source="Dune Analytics",
            indicator=f"{name} ({symbol}) Price",
            country=None,
            frequency="snapshot",
            unit="USD",
            lastUpdated=current_date,
            seriesId=token_address,
            apiUrl=endpoint.replace(self.api_key, "***"),
        )

        return NormalizedData(metadata=metadata, data=data_points)

    async def fetch_token_holders(self, params: Dict[str, Any]) -> NormalizedData:
        """
        Fetch top token holders distribution

        Parameters:
        - tokenAddress: Token contract address (required)
        - chain: Chain name (required)
        - limit: Max holders to return (default 10)
        """
        token_address = params.get("tokenAddress") or params.get("address")
        if not token_address:
            raise ValueError("Token address is required for holder queries")

        chain = params.get("chain", "ethereum")
        chain_id = self._get_chain_id(chain)
        token_address = self._validate_address(token_address, "evm")
        limit = params.get("limit", 10)

        endpoint = f"{self.BASE_URL_EVM}/token-holders/{chain_id}/{token_address}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(
                    endpoint,
                    params={"limit": limit},
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise DataNotAvailableError(
                        f"No holder data found for token {token_address} on chain {chain}"
                    )
                elif e.response.status_code == 401:
                    raise DataNotAvailableError("Invalid Dune API key.")
                raise DataNotAvailableError(f"Dune API error: {e.response.status_code}")

        holders = data.get("holders", [])
        if not holders:
            raise DataNotAvailableError(
                f"No holders found for token {token_address}"
            )

        # Return holder count as a simple metric
        current_date = datetime.utcnow().date().isoformat()
        data_points = [DataPoint(date=current_date, value=float(len(holders)))]

        metadata = Metadata(
            source="Dune Analytics",
            indicator=f"Top Holders Count - {token_address[:6]}...{token_address[-4:]}",
            country=None,
            frequency="snapshot",
            unit="holders",
            lastUpdated=current_date,
            seriesId=token_address,
            apiUrl=endpoint.replace(self.api_key, "***"),
        )

        return NormalizedData(metadata=metadata, data=data_points)
