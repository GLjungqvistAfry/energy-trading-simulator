import datetime
from typing import Dict, Tuple

import pandas as pd

from pkg_resources import resource_filename

import polars as pl

from tradingplatformpoc.generate_data.generation_functions.common import constants, get_noise, \
    is_day_before_major_holiday_sweden, is_major_holiday_sweden, scale_energy_consumption


def simulate_office_based_on_hourly(atemp_m2: float, std_dev: float, scale_factor: float, col_name: str,
                                    random_seed: int, df_inputs: pl.LazyFrame, n_rows: int) -> pl.LazyFrame:
    """
    Similarly to simulate_residential_total_heating, this method uses a column in df_inputs and adds some noise.
    """
    if atemp_m2 == 0:
        return constants(df_inputs, 0)

    noise = get_noise(n_rows, random_seed, std_dev)

    energy_unscaled = df_inputs.select([pl.col('datetime'), pl.col(col_name).alias('value') * noise])

    # Scale
    return scale_energy_consumption(energy_unscaled, atemp_m2, scale_factor, n_rows)


def simulate_office_based_on_daily(atemp_m2: float, std_dev: float, scale_factor: float, random_seed: int,
                                   lazy_inputs: pl.LazyFrame, n_rows: int,
                                   per_month_and_hour: Dict[Tuple[int, int], float]) -> pl.LazyFrame:
    # First, add in the raw values from BDAB
    lf = lazy_inputs.with_column(
        pl.col('datetime').apply(lambda d: get_value_for_month_and_hour(d, per_month_and_hour)).alias('raw')
    )
    return simulate_office_based_on_hourly(atemp_m2, std_dev, scale_factor, 'raw', random_seed, lf, n_rows)


def get_value_for_month_and_hour(d: datetime.datetime, per_month_and_hour: Dict[Tuple[int, int], float]):
    """
    The values in the dict are for Mon-Fri. We need to check if it is a weekend, or a public holiday - if so, we use the
    "inactive" value, taken to be the value for hour = 0, for the whole day.
    """
    if is_major_holiday_sweden(d) or is_day_before_major_holiday_sweden(d) or d.weekday() >= 5:
        return per_month_and_hour[(d.month, 0)]
    return per_month_and_hour[(d.month, d.hour)]


def read_office_dicts() -> Tuple[Dict[Tuple[int, int], float], Dict[Tuple[int, int], float]]:
    """
    From the 'office_days.csv' file, this function constructs dicts (the first for electricity, the second for hot
    water), with a value for energy consumption based on the month and hour of day.
    """
    df = pd.read_csv(resource_filename('tradingplatformpoc.data', 'office_days.csv'), sep=';',
                     names=['month', 'hour', 'elec_1', 'elec_2', 'hot_water'], skiprows=1)
    df['month'] = df['month'].fillna(method='ffill').astype(int)
    df = df.set_index(['month', 'hour'])
    df['elec'] = df['elec_1'] + df['elec_2']
    df.drop(['elec_1', 'elec_2'], axis=1, inplace=True)
    return df['elec'].to_dict(), df['hot_water'].to_dict()
