"""Serialization utilities for JSON encoding of complex types.

This module provides shared JSON encoders for:
- NumPy arrays and scalar types
- Pandas DataFrames, Series, and Timestamps
- Other common data science types
"""
from __future__ import annotations

import json
from datetime import datetime, date
from typing import Any


class NumpyPandasEncoder(json.JSONEncoder):
    """
    Custom JSON encoder for numpy and pandas types.

    This encoder handles common data science types that are not
    natively JSON serializable, including:
    - numpy arrays, integers, and floats
    - pandas DataFrames, Series, and Timestamps
    - datetime objects
    - NaN/NaT values (converted to None)

    Example:
        >>> import json
        >>> import numpy as np
        >>> data = {"values": np.array([1, 2, 3])}
        >>> json.dumps(data, cls=NumpyPandasEncoder)
        '{"values": [1, 2, 3]}'
    """

    def default(self, obj: Any) -> Any:
        """
        Convert non-serializable objects to JSON-serializable types.

        Args:
            obj: Object to serialize

        Returns:
            JSON-serializable representation of the object

        Raises:
            TypeError: If object cannot be serialized
        """
        # Import numpy and pandas lazily to avoid import errors if not installed
        try:
            import numpy as np

            # Handle numpy arrays
            if isinstance(obj, np.ndarray):
                return obj.tolist()

            # Handle numpy integer types
            if isinstance(obj, (np.integer, np.int64, np.int32, np.int16, np.int8)):
                return int(obj)

            # Handle numpy unsigned integer types
            if isinstance(obj, (np.unsignedinteger, np.uint64, np.uint32, np.uint16, np.uint8)):
                return int(obj)

            # Handle numpy float types
            if isinstance(obj, (np.floating, np.float64, np.float32, np.float16)):
                # Handle NaN values
                if np.isnan(obj):
                    return None
                return float(obj)

            # Handle numpy boolean
            if isinstance(obj, np.bool_):
                return bool(obj)

        except ImportError:
            pass

        try:
            import pandas as pd

            # Handle pandas DataFrame
            if isinstance(obj, pd.DataFrame):
                return obj.to_dict('records')

            # Handle pandas Series
            if isinstance(obj, pd.Series):
                return obj.to_list()

            # Handle pandas Timestamp
            if isinstance(obj, pd.Timestamp):
                if pd.isna(obj):
                    return None
                return obj.isoformat()

            # Handle pandas NaT (Not a Time)
            if pd.isna(obj):
                return None

        except ImportError:
            pass

        # Handle standard datetime types
        if isinstance(obj, datetime):
            return obj.isoformat()

        if isinstance(obj, date):
            return obj.isoformat()

        # Handle objects with custom serialization methods
        if hasattr(obj, 'to_dict'):
            return obj.to_dict()

        if hasattr(obj, 'tolist'):
            return obj.tolist()

        if hasattr(obj, 'item'):
            return obj.item()

        if hasattr(obj, 'isoformat'):
            return obj.isoformat()

        # Fall back to default behavior
        return super().default(obj)


def json_serialize(obj: Any) -> str:
    """
    Serialize an object to JSON string, handling numpy and pandas types.

    Args:
        obj: Object to serialize

    Returns:
        JSON string representation

    Example:
        >>> import numpy as np
        >>> json_serialize({"array": np.array([1, 2, 3])})
        '{"array": [1, 2, 3]}'
    """
    return json.dumps(obj, cls=NumpyPandasEncoder)


def json_serialize_for_execution(obj: Any) -> Any:
    """
    Serialize an object to a JSON-compatible Python object.

    This is useful for preparing data that will be passed to
    sandboxed code execution environments.

    Args:
        obj: Object to serialize

    Returns:
        JSON-compatible Python object (dict, list, str, int, float, bool, None)

    Example:
        >>> import numpy as np
        >>> json_serialize_for_execution(np.array([1, 2, 3]))
        [1, 2, 3]
    """
    # Convert to JSON string and back to get a clean Python object
    return json.loads(json.dumps(obj, cls=NumpyPandasEncoder))
