"""
Base Signal Interface for AEGIS System
ALL signals must inherit from this class for integration
"""
from abc import ABC, abstractmethod
import pandas as pd
from typing import Dict, Any, Optional

class BaseSignal(ABC):
    """
    Abstract base class for all AEGIS signals
    
    Integration Contract:
    - All signals return standardized DataFrame columns
    - All signals provide score in range [-1, 0, 1]
    - All signals can be combined in the scoring engine
    """
    
    def __init__(self, name: str, family: str, timeframe: str, weight: float = 1.0):
        """
        Initialize signal
        
        Args:
            name: Signal identifier (e.g., 'oi_delta')
            family: 'A' for derivatives or 'B' for microstructure
            timeframe: '4h' or '15m'
            weight: Signal weight in final scoring (default 1.0)
        """
        self.name = name
        self.family = family
        self.timeframe = timeframe
        self.weight = weight
        self._is_fitted = False
    
    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate signal values and add columns to DataFrame
        
        Required output columns:
        - {name}_value: Raw signal value
        - {name}_signal: Classification (BULLISH/BEARISH/NEUTRAL/etc)
        - {name}_score: Numeric score (-1, 0, or 1)
        
        Args:
            df: Input DataFrame with OHLCV data
        
        Returns:
            DataFrame with signal columns added
        """
        pass
    
    @abstractmethod
    def get_required_columns(self) -> list:
        """
        Return list of required input columns
        
        Returns:
            List of column names needed for calculation
        """
        pass
    
    def validate_input(self, df: pd.DataFrame) -> bool:
        """
        Validate input DataFrame has required columns
        
        Args:
            df: Input DataFrame
        
        Returns:
            True if valid
        
        Raises:
            ValueError if validation fails
        """
        required = self.get_required_columns()
        missing = set(required) - set(df.columns)
        
        if missing:
            raise ValueError(
                f"Signal '{self.name}' missing required columns: {missing}"
            )
        
        return True
    
    def get_output_columns(self) -> list:
        """
        Return list of output column names this signal produces
        
        Returns:
            List of output column names
        """
        return [
            f"{self.name}_value",
            f"{self.name}_signal",
            f"{self.name}_score"
        ]
    
    def get_metadata(self) -> Dict[str, Any]:
        """
        Return signal metadata for documentation
        
        Returns:
            Dictionary with signal information
        """
        return {
            'name': self.name,
            'family': self.family,
            'timeframe': self.timeframe,
            'weight': self.weight,
            'required_columns': self.get_required_columns(),
            'output_columns': self.get_output_columns()
        }