"""
Measurement Aggregator - Converts individual measurements to daily aggregates.

This module optimizes storage by aggregating measurements by date, reducing
27MB+ raw data to <1MB with min/max/avg statistics per day.
"""

from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from collections import defaultdict
import statistics

try:
    import h3  # type: ignore
    HAS_H3 = True
except ImportError:
    HAS_H3 = False

try:
    from geopy.geocoders import Nominatim  # type: ignore
    HAS_GEOPY = True
except ImportError:
    HAS_GEOPY = False


class MeasurementAggregator:
    """Aggregates individual measurements into daily min/max/avg statistics."""
    
    def __init__(self):
        self._country_cache: Dict[str, str] = {}
        self._geolocator = None
        
    def get_country_from_hex(self, hex_id: str) -> str:
        """Get country name from H3 hex ID with caching."""
        if hex_id in self._country_cache:
            return self._country_cache[hex_id]
        
        if not HAS_H3 or not HAS_GEOPY:
            country = 'Unknown'
        else:
            try:
                # Convert hex to coordinates
                lat, lon = h3.cell_to_latlng(hex_id)  # type: ignore
                
                # Initialize geolocator on first use
                if self._geolocator is None:
                    self._geolocator = Nominatim(user_agent="hardware_poc_measurements")  # type: ignore
                
                # Reverse geocode
                location = self._geolocator.reverse(f"{lat}, {lon}", language='en', timeout=10)
                
                if location and location.raw.get('address'):
                    country = location.raw['address'].get('country', 'Unknown')
                else:
                    country = 'Unknown'
            except Exception:
                country = 'Unknown'
        
        self._country_cache[hex_id] = country
        return country
    
    def parse_timestamp(self, ts_str: str) -> Optional[datetime]:
        """Parse various timestamp formats."""
        if not ts_str:
            return None
        try:
            if '+' in ts_str or ts_str.endswith('Z'):
                return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            return datetime.fromisoformat(ts_str)
        except:
            return None
    
    def aggregate_measurement(
        self,
        existing_aggregates: Dict[str, Dict[str, Any]],
        timestamp: str,
        value: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Add a new measurement to existing aggregates.
        
        Args:
            existing_aggregates: Dict mapping date strings to aggregate data
            timestamp: ISO timestamp string
            value: Measurement value dict
            
        Returns:
            Updated aggregates dict
        """
        ts = self.parse_timestamp(timestamp)
        if not ts:
            return existing_aggregates
        
        date_key = ts.strftime('%Y-%m-%d')
        
        # Initialize aggregate for this date if needed
        if date_key not in existing_aggregates:
            existing_aggregates[date_key] = {
                'count': 0,
                '_values': defaultdict(list)  # Temp storage for calculating stats
            }
        
        agg = existing_aggregates[date_key]
        agg['count'] += 1
        
        # Collect values for aggregation
        for key, val in value.items():
            if isinstance(val, (int, float)):
                agg['_values'][key].append(val)
            elif key not in agg:
                # For non-numeric, store the first/most common value
                agg[key] = val
        
        return existing_aggregates
    
    def finalize_aggregates(
        self,
        aggregates: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Calculate final min/max/avg from collected values.
        
        Args:
            aggregates: Dict with '_values' temp storage
            
        Returns:
            Finalized aggregates with min/max/avg
        """
        result = {}
        
        for date_key, agg in aggregates.items():
            final_agg = {'count': agg['count']}
            
            # Process numeric values
            values_dict = agg.get('_values', {})
            for key, values in values_dict.items():
                if values:
                    final_agg[key] = {
                        'min': round(min(values), 2),
                        'max': round(max(values), 2),
                        'avg': round(statistics.mean(values), 2)
                    }
            
            # Keep non-numeric values
            for key, val in agg.items():
                if key not in ('count', '_values') and not isinstance(val, dict):
                    final_agg[key] = val
            
            result[date_key] = final_agg
        
        return result
    
    def process_measurement_upload(
        self,
        hex_id: str,
        timestamp: str,
        measurement_type: str,
        value: Dict[str, Any],
        existing_doc: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Process a single measurement upload and return updated aggregate document.
        
        Args:
            hex_id: H3 hex cell ID
            timestamp: ISO timestamp
            measurement_type: Type of measurement (Bandwidth, Satellite, etc.)
            value: Measurement value dict
            existing_doc: Existing aggregate document (if any)
            
        Returns:
            Updated aggregate document ready for storage
        """
        # Initialize or use existing document
        if existing_doc is None:
            doc: Dict[str, Any] = {
                'hex_id': hex_id,
                'country': self.get_country_from_hex(hex_id) if HAS_H3 and HAS_GEOPY else 'Unknown'
            }
        else:
            doc = dict(existing_doc)
            doc.setdefault('hex_id', hex_id)
            doc.setdefault('country', 'Unknown')
        
        # Get existing aggregates for this measurement type (already finalized)
        type_aggregates: Dict[str, Dict[str, Any]] = doc.get(measurement_type, {})
        if not isinstance(type_aggregates, dict):
            type_aggregates = {}
        
        # Parse the new measurement timestamp
        ts = self.parse_timestamp(timestamp)
        if not ts:
            return doc
        
        date_key = ts.strftime('%Y-%m-%d')
        
        # Get or create aggregate for this date
        if date_key not in type_aggregates:
            type_aggregates[date_key] = {'count': 0}
        
        agg: Dict[str, Any] = type_aggregates[date_key]
        count = agg.get('count', 0)
        
        # Update count
        new_count = count + 1
        agg['count'] = new_count
        
        # Update each numeric field with incremental aggregation
        for key, val in value.items():
            if isinstance(val, (int, float)):
                if key not in agg:
                    # First value for this field
                    agg[key] = {
                        'min': round(val, 2),
                        'max': round(val, 2),
                        'avg': round(val, 2)
                    }
                else:
                    # Update existing stats
                    stats: Dict[str, Any] = agg[key]
                    old_avg = stats.get('avg', val)
                    old_min = stats.get('min', val)
                    old_max = stats.get('max', val)
                    
                    # Update min/max
                    stats['min'] = round(min(old_min, val), 2)
                    stats['max'] = round(max(old_max, val), 2)
                    
                    # Update average incrementally
                    # new_avg = (old_avg * old_count + new_value) / new_count
                    stats['avg'] = round((old_avg * count + val) / new_count, 2)
            elif key not in agg or agg.get(key) is None:
                # For non-numeric, store the first value
                agg[key] = val
        
        # Update document
        doc[measurement_type] = type_aggregates
        
        return doc


# Singleton instance for reuse
_aggregator = MeasurementAggregator()


def get_aggregator() -> MeasurementAggregator:
    """Get the singleton aggregator instance."""
    return _aggregator
