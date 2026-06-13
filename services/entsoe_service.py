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


def get_co2_intensity_window(api_key: str, start_dt: datetime, end_dt: datetime,
                             country_code: str = 'DE') -> Optional[int]:
    """Time-weighted average CO2 intensity (g/kWh) across [start_dt, end_dt].

    v3.0.64: Replaces the single-hour ``get_co2_intensity`` for charges
    that span multiple hours. ENTSO-E returns generation in 15-min or
    1-h buckets. For each bucket overlapping [start_dt, end_dt] we
    compute the bucket's CO2 intensity (per-fuel weighted) and weight
    it by the seconds the charge actually overlapped that bucket. The
    final number is the bucket-weighted average — i.e. the grid mix
    the EV actually drew from over the full charge window.

    If the window straddles midnight, falls back to a single ENTSO-E
    query covering both days (we add a one-day buffer at both ends so
    the broker returns the full set). Returns None on any failure or
    if ENTSO-E has no data yet (typical for the current hour during
    an active charge — the backfill thread will retry later).
    """
    if not api_key:
        logger.warning("No ENTSO-E API key configured")
        return None
    if start_dt >= end_dt:
        # Degenerate window — fall back to the single-hour path.
        return get_co2_intensity(api_key, start_dt, hour=start_dt.hour,
                                 country_code=country_code)

    try:
        from entsoe import EntsoePandasClient
        import pandas as pd

        client = EntsoePandasClient(api_key=api_key)
        # One-day buffer at both ends covers TZ slop and bucket
        # alignment so the window's edges always land inside the
        # returned series.
        start = pd.Timestamp(start_dt.strftime('%Y-%m-%d'), tz='Europe/Berlin')
        end = (pd.Timestamp(end_dt.strftime('%Y-%m-%d'), tz='Europe/Berlin')
               + pd.Timedelta(days=1))

        generation = client.query_generation(country_code, start=start,
                                             end=end, psr_type=None)
        if generation is None or generation.empty:
            logger.warning(f"No generation data for window {start_dt}..{end_dt}")
            return None

        if isinstance(generation.columns, pd.MultiIndex):
            actual = generation.xs('Actual Aggregated', axis=1,
                                   level=1, drop_level=True)
        else:
            actual = generation

        win_start = pd.Timestamp(start_dt, tz='Europe/Berlin')
        win_end = pd.Timestamp(end_dt, tz='Europe/Berlin')

        # Bucket width (15 min for granular ENTSO-E feeds, 60 min for
        # older ones). pandas index is sorted, so the diff gives us a
        # robust per-bucket duration without hard-coding 15.
        if len(actual.index) >= 2:
            bucket = actual.index[1] - actual.index[0]
        else:
            bucket = pd.Timedelta(hours=1)

        total_weight_s = 0.0
        total_co2_weighted = 0.0
        for ts, row in actual.iterrows():
            bs = ts
            be = ts + bucket
            # Overlap of bucket [bs, be] with window [win_start, win_end]
            ov_start = max(bs, win_start)
            ov_end = min(be, win_end)
            if ov_end <= ov_start:
                continue
            ov_s = (ov_end - ov_start).total_seconds()

            # Per-bucket intensity (per-fuel weighted)
            tot_gen = 0.0
            tot_co2 = 0.0
            for fuel_type, gen_mw in row.items():
                if pd.isna(gen_mw) or gen_mw <= 0:
                    continue
                factor = CO2_FACTORS.get(fuel_type, 300)
                tot_gen += float(gen_mw)
                tot_co2 += float(gen_mw) * factor
            if tot_gen <= 0:
                continue
            bucket_intensity = tot_co2 / tot_gen

            total_weight_s += ov_s
            total_co2_weighted += bucket_intensity * ov_s

        if total_weight_s <= 0:
            logger.warning(
                f"No ENTSO-E buckets covered window {start_dt}..{end_dt}"
            )
            return None
        intensity = int(round(total_co2_weighted / total_weight_s))
        logger.info(
            f"CO2 intensity for window {start_dt.isoformat(timespec='minutes')}"
            f"..{end_dt.isoformat(timespec='minutes')}: "
            f"{intensity} g/kWh (time-weighted)"
        )
        return intensity

    except ImportError:
        logger.error("entsoe-py not installed. Run: pip install entsoe-py")
        return None
    except Exception as e:
        logger.error(f"ENTSO-E window query error: {e}")
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
