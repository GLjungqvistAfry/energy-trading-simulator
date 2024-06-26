import datetime
from unittest import TestCase

import pytz

from tradingplatformpoc.price.heating_price import HeatingPrice, calculate_jan_feb_avg_heating_sold, \
    calculate_peak_day_avg_cons_kw


class Test(TestCase):
    # Rough prices:
    # Jan - Feb: 0.5 + 0.66 + 0.093 = 1.253 SEK / kWh
    # Mar - Apr: 0.5 + 0.093 = 0.593 SEK / kWh
    # May - Sep: 0.3 + 0.093 = 0.393 SEK / kWh
    # Oct - Dec: 0.5 + 0.093 = 0.593 SEK / kWh
    dhp = HeatingPrice(0)

    def test_calculate_jan_feb_avg_heating_sold(self):
        """Test basic functionality of calculate_jan_feb_avg_heating_sold"""
        dhp = HeatingPrice(0)
        dhp.add_external_sell(datetime.datetime(2019, 2, 1, 1, tzinfo=pytz.utc), 50)
        march_1st = datetime.datetime(2019, 3, 1, 1, tzinfo=pytz.utc)
        dhp.add_external_sell(march_1st, 100)
        self.assertAlmostEqual(50.0, calculate_jan_feb_avg_heating_sold(dhp.all_external_sells, march_1st))

    def test_calculate_jan_feb_avg_heating_sold_when_no_data(self):
        """Test that calculate_jan_feb_avg_heating_sold 'cheats' and uses future data, when there is no data to do the
        calculation properly."""
        dhp = HeatingPrice(0)
        feb_1st = datetime.datetime(2019, 2, 1, 1, tzinfo=pytz.utc)
        dhp.add_external_sell(feb_1st, 50)
        dhp.add_external_sell(datetime.datetime(2019, 3, 1, 1, tzinfo=pytz.utc), 100)
        self.assertAlmostEqual(50.0, calculate_jan_feb_avg_heating_sold(dhp.all_external_sells, feb_1st))

    def test_calculate_peak_day_avg_cons_kw(self):
        """Test basic functionality of calculate_peak_day_avg_cons_kw"""
        dhp = HeatingPrice(0)
        dhp.add_external_sell(datetime.datetime(2019, 3, 1, 1, tzinfo=pytz.utc), 100)
        dhp.add_external_sell(datetime.datetime(2019, 3, 1, 2, tzinfo=pytz.utc), 140)
        dhp.add_external_sell(datetime.datetime(2019, 3, 2, 1, tzinfo=pytz.utc), 50)
        dhp.add_external_sell(datetime.datetime(2019, 3, 2, 2, tzinfo=pytz.utc), 50)
        dhp.add_external_sell(datetime.datetime(2019, 3, 2, 3, tzinfo=pytz.utc), 50)
        self.assertAlmostEqual(10.0, calculate_peak_day_avg_cons_kw(dhp.all_external_sells, 2019, 3))

    def test_get_base_marginal_price(self):
        """Test marginal price for summer/winter periods: summer should be lower than winter."""
        self.assertTrue(self.dhp.get_base_marginal_price(5) < self.dhp.get_base_marginal_price(2))

    def test_get_grid_fee_for_month(self):
        self.assertAlmostEqual(571.758904109589, self.dhp.get_grid_fee_for_month(5, 2019, 10))

    def test_exact_effect_fee(self):
        self.assertAlmostEqual(170.0, self.dhp.exact_effect_fee(2.5))

    def test_exact_district_heating_price_for_month(self):
        self.assertAlmostEqual(776.758904109589, self.dhp.exact_district_heating_price_for_month(
            10, 2019, 70, 5, 2.5))
