import datetime
import logging
from calendar import isleap
from typing import Optional

import numpy as np

import pandas as pd

from tradingplatformpoc.market.trade import Resource
from tradingplatformpoc.price.iprice import IPrice, get_days_in_month

logger = logging.getLogger(__name__)


def handle_no_consumption_when_calculating_heating_price(period):
    logger.debug("Tried to calculate exact external heating price, in SEK/kWh, for {:%B %Y}, but had no "
                 "consumption for this month, so returned np.nan.".format(period))
    return np.nan


class HeatingPrice(IPrice):
    """
    Class for calculating exact and estimated price of heating.
    For a more thorough explanation of the district heating pricing mechanism, see
    https://www.varbergenergi.se/foretag/tjanster/fjarrvarme/fjarrvarme-priser/
    """

    heating_wholesale_price_fraction: float

    GRID_FEE_MARGINAL_SUB_50: int = 1116
    GRID_FEE_FIXED_SUB_50: int = 1152
    GRID_FEE_MARGINAL_50_100: int = 1068
    GRID_FEE_FIXED_50_100: int = 3060
    GRID_FEE_MARGINAL_100_200: int = 1020
    GRID_FEE_FIXED_100_200: int = 8148
    GRID_FEE_MARGINAL_200_400: int = 972
    GRID_FEE_FIXED_200_400: int = 18348
    GRID_FEE_MARGINAL_400_PLUS: int = 936
    GRID_FEE_FIXED_400_PLUS: int = 33696
    MARGINAL_PRICE_WINTER: float = 0.5
    MARGINAL_PRICE_SUMMER: float = 0.3

    def __init__(self, heating_wholesale_price_fraction: float, effect_fee: float = 68.0):
        super().__init__(Resource.HIGH_TEMP_HEAT)
        self.heating_wholesale_price_fraction = heating_wholesale_price_fraction
        self.effect_fee = effect_fee
    
    def marginal_grid_fee_assuming_top_bracket(self, year: int) -> float:
        """
        The grid fee is based on the average consumption in kW during January and February.
        Using 1 kWh during January and February, increases the average Jan-Feb consumption by 1 / hours_in_jan_feb.
        The marginal cost depends on what "bracket" one falls into, but we'll assume
        we always end up in the top bracket.
        More info at https://www.varbergenergi.se/foretag/tjanster/fjarrvarme/fjarrvarme-priser/
        """
        hours_in_jan_feb = 1416 + (24 if isleap(year) else 0)
        return self.GRID_FEE_MARGINAL_400_PLUS / hours_in_jan_feb
    
    def get_base_marginal_price(self, month_of_year: int) -> float:
        """'Summer price' during May-September, 'Winter price' other months."""
        if 5 <= month_of_year <= 9:
            return self.MARGINAL_PRICE_SUMMER  # Cheaper in summer
        else:
            return self.MARGINAL_PRICE_WINTER

    def get_retail_price_excl_effect_fee(self, period: datetime.datetime) -> float:
        """
        Returns the price at which the external grid operator is believed to be willing to sell energy, in SEK/kWh,
        while excluding the part which depends on peak usage. No tax included (district heating is not taxed).
        """
        jan_feb_extra = self.marginal_grid_fee_assuming_top_bracket(period.year)
        base_marginal_price = self.get_base_marginal_price(period.month)
        return base_marginal_price + (jan_feb_extra * (period.month <= 2))
    
    def exact_effect_fee(self, monthly_peak_day_avg_consumption_kw: float) -> float:
        """
        @param monthly_peak_day_avg_consumption_kw Calculated by taking the day during the month which has the highest
            heating energy use, and taking the average hourly heating use that day.
        """
        return self.effect_fee * monthly_peak_day_avg_consumption_kw
    
    def get_yearly_grid_fee(self, jan_feb_hourly_avg_consumption_kw: float) -> float:
        """Based on Jan-Feb average hourly heating use."""
        if jan_feb_hourly_avg_consumption_kw < 50:
            return self.GRID_FEE_FIXED_SUB_50 + self.GRID_FEE_MARGINAL_SUB_50 * jan_feb_hourly_avg_consumption_kw
        elif jan_feb_hourly_avg_consumption_kw < 100:
            return self.GRID_FEE_FIXED_50_100 + self.GRID_FEE_MARGINAL_50_100 * jan_feb_hourly_avg_consumption_kw
        elif jan_feb_hourly_avg_consumption_kw < 200:
            return self.GRID_FEE_FIXED_100_200 + self.GRID_FEE_MARGINAL_100_200 * jan_feb_hourly_avg_consumption_kw
        elif jan_feb_hourly_avg_consumption_kw < 400:
            return self.GRID_FEE_FIXED_200_400 + self.GRID_FEE_MARGINAL_200_400 * jan_feb_hourly_avg_consumption_kw
        else:
            return self.GRID_FEE_FIXED_400_PLUS + self.GRID_FEE_MARGINAL_400_PLUS * jan_feb_hourly_avg_consumption_kw

    def get_grid_fee_for_month(self, jan_feb_hourly_avg_consumption_kw: float, year: int, month_of_year: int) -> float:
        """
        The grid fee is based on the average consumption in kW during January and February.
        This fee is then spread out evenly during the year.
        """
        days_in_month = get_days_in_month(month_of_year, year)
        days_in_year = 366 if isleap(year) else 365
        fraction_of_year = days_in_month / days_in_year
        yearly_fee = self.get_yearly_grid_fee(jan_feb_hourly_avg_consumption_kw)
        return yearly_fee * fraction_of_year

    def exact_district_heating_price_for_month(self, month: int, year: int, consumption_this_month_kwh: float,
                                               jan_feb_avg_consumption_kw: float,
                                               prev_month_peak_day_avg_consumption_kw: float) -> float:
        """
        Three price components:
        * "Energy price" or "base marginal price"
        * "Grid fee" based on consumption in January+February
        * "Effect fee" based on the "peak day" of the month
        @param month                                    The month one wants the price for.
        @param year                                     The year one wants the price for.
        @param consumption_this_month_kwh               The total amount of heating bought, in kWh, this month.
        @param jan_feb_avg_consumption_kw               The average heating effect bought, in kW, during the previous
                                                            January-February period. This is used to calculate the "grid
                                                            fee" price component.
        @param prev_month_peak_day_avg_consumption_kw   The average heating effect bought, in kW, during the day of the
                                                            previous month when it was the highest. This is used to
                                                            calculate the "effect fee" price component.
        """
        effect_fee = self.exact_effect_fee(prev_month_peak_day_avg_consumption_kw)
        grid_fee = self.get_grid_fee_for_month(jan_feb_avg_consumption_kw, year, month)
        base_marginal_price = self.get_base_marginal_price(month)
        return base_marginal_price * consumption_this_month_kwh + effect_fee + grid_fee

    def get_exact_retail_price(self, period: datetime.datetime, include_tax: bool, agent: Optional[str] = None) \
            -> float:
        """Returns the price at which the external grid operator is willing to sell energy, in SEK/kWh"""
        # District heating is not taxed
        sells_series = self.get_sells(agent)
        consumption_this_month_kwh = calculate_consumption_this_month(sells_series, period.year, period.month)
        if consumption_this_month_kwh == 0:
            return handle_no_consumption_when_calculating_heating_price(period)
        jan_feb_avg_consumption_kw = calculate_jan_feb_avg_heating_sold(sells_series, period)
        prev_month_peak_day_avg_consumption_kw = calculate_peak_day_avg_cons_kw(sells_series, period.year, period.month)
        total_cost_for_month = self.exact_district_heating_price_for_month(
            period.month, period.year, consumption_this_month_kwh, jan_feb_avg_consumption_kw,
            prev_month_peak_day_avg_consumption_kw)
        return total_cost_for_month / consumption_this_month_kwh
    
    def get_exact_wholesale_price(self, period: datetime.datetime, agent: Optional[str] = None) -> float:
        """Returns the price at which the external grid operator is willing to buy energy, in SEK/kWh"""
        return self.get_exact_retail_price(period, False, agent) * self.heating_wholesale_price_fraction

    def get_avg_peak_for_month(self, period: datetime.datetime, agent: Optional[str] = None) -> float:
        """
        This method will fetch the average outtake of the month's peak day - but if it is early in the month, it will
        also look at the previous month's value.
        """
        sells_series = self.get_sells(agent)
        peak_this_month = calculate_peak_day_avg_cons_kw(sells_series, period.year, period.month)
        at_least_n_days = 5
        if period.day < at_least_n_days:
            # Early in the month, we'll also use last month's value, so that we don't underestimate.
            # We will scale that value a bit though, so that we don't overestimate.
            scale_factor_for_last_month = 0.8
            prev_month = period - datetime.timedelta(days=at_least_n_days + 1)
            peak_last_month = calculate_peak_day_avg_cons_kw(sells_series, prev_month.year, prev_month.month)
            scaled_last_month = peak_last_month * scale_factor_for_last_month
            # Return the maximum of this month's peak, and the (scaled) last month's peak
            avg_peak = max(peak_this_month, scaled_last_month)
        else:
            avg_peak = peak_this_month
        if np.isnan(avg_peak):
            return 0.0
        return avg_peak

    def get_effect_fee_per_day(self, date_time: datetime.datetime) -> float:
        return self.effect_fee / get_days_in_month(date_time.month, date_time.year)


def calculate_consumption_this_month(dt_series: pd.Series, year: int, month: int) -> float:
    """
    Calculate the sum of all external heating sells for the specified year-month combination.
    Returns a float with the unit kWh.
    """
    subset = (dt_series.index.year == year) & (dt_series.index.month == month)
    return sum(dt_series[subset])


def calculate_jan_feb_avg_heating_sold(dt_series: pd.Series, period: datetime.datetime) -> float:
    """
    Calculates the average effect (in kW) of heating sold in the previous January-February.
    """
    year_we_are_interested_in = period.year - 1 if period.month <= 2 else period.year
    subset = (dt_series.index.year == year_we_are_interested_in) & (dt_series.index.month <= 2)
    if not any(subset):
        logger.debug("No data to base grid fee on, will 'cheat' and use future data")
        subset = (dt_series.index.month <= 2)
    return dt_series[subset].mean()


def calculate_peak_day_avg_cons_kw(dt_series: pd.Series, year: int, month: int) -> float:
    subset = (dt_series.index.year == year) & (dt_series.index.month == month)
    heating_sells_this_month = dt_series[subset].copy()
    sold_by_day = heating_sells_this_month.groupby(heating_sells_this_month.index.day).sum()
    peak_day_avg_consumption = sold_by_day.max() / 24
    return peak_day_avg_consumption
