"""ENTSO-E API service for fetching CO2 intensity data."""
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# CO2 emission factors (g/kWh) by fuel type
CO2_FACTORS = {
    'Biomass': 230,
    'Fossil Brown coal/Lignite': 1050,
    'Fossil Coal-derived gas': 350,
    'Fossil Gas': 490,
    'Fossil Hard coal': 900,
    'Fossil Oil': 650,
    'Fossil Oil shale': 800,
    'Fossil Peat': 1100,
    'Geothermal': 38,
    'Hydro Pumped Storage': 0,
    'Hydro Run-of-river and poundage': 0,
    'Hydro Water Reservoir': 0,
    'Marine': 0,
    'Nuclear': 12,
    'Other': 300,
    'Other renewable': 0,
    'Solar': 0,
    'Waste': 330,
    'Wind Offshore': 0,
    'Wind Onshore': 0,
}


def get_co2_intensity(api_key: str, target_date: datetime, hour: int = None, country_code: str = 'DE') -> Optional[int]:
    """
    Fetch CO2 intensity (g/kWh) for a given date and country from ENTSO-E.
    If hour is provided (0-23), returns intensity for that specific hour.
    Otherwise returns daily average.
    """
    if not api_key:
        logger.warning("No ENTSO-E API key configured")
        return None

    try:
        from entsoe import EntsoePandasClient
        import pandas as pd

        client = EntsoePandasClient(api_key=api_key)

        start = pd.Timestamp(target_date.strftime('%Y-%m-%d'), tz='Europe/Berlin')
        end = start + pd.Timedelta(days=1)

        generation = client.query_generation(country_code, start=start, end=end, psr_type=None)

        if generation is None or generation.empty:
            logger.warning(f"No generation data for {target_date}")
            return None

        # Handle MultiIndex columns (actual vs forecast)
        if isinstance(generation.columns, pd.MultiIndex):
            actual = generation.xs('Actual Aggregated', axis=1, level=1, drop_level=True)
        else:
            actual = generation

        # Filter to specific hour if provided
        if hour is not None and 0 <= hour <= 23:
            hour_data = actual[actual.index.hour == hour]
            avg_gen = hour_data.mean() if not hour_data.empty else actual.mean()
        else:
            avg_gen = actual.mean()

        total_gen = 0
        total_co2 = 0

        for fuel_type, gen_mw in avg_gen.items():
            if gen_mw > 0:
                co2_factor = CO2_FACTORS.get(fuel_type, 300)
                total_gen += gen_mw
                total_co2 += gen_mw * co2_factor

        if total_gen > 0:
            intensity = int(round(total_co2 / total_gen))
            label = f"{target_date.date()} {hour}:00" if hour is not None else f"{target_date.date()}"
            logger.info(f"CO2 intensity for {label}: {intensity} g/kWh")
            return intensity

        return None

    except ImportError:
        logger.error("entsoe-py not installed. Run: pip install entsoe-py")
        return None
    except Exception as e:
        logger.error(f"ENTSO-E API error: {e}")
        return None


def test_api_key(api_key: str) -> bool:
    """Test if an ENTSO-E API key is valid."""
    try:
        from entsoe import EntsoePandasClient
        import pandas as pd

        client = EntsoePandasClient(api_key=api_key)
        yesterday = pd.Timestamp.now(tz='Europe/Berlin') - pd.Timedelta(days=2)
        end = yesterday + pd.Timedelta(days=1)
        result = client.query_generation('DE', start=yesterday, end=end)
        return result is not None and not result.empty
    except Exception as e:
        logger.error(f"API key test failed: {e}")
        return False
