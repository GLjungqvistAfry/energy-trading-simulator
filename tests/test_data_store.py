from datetime import datetime
from unittest import TestCase

import numpy as np

import pandas as pd
from pandas import DatetimeIndex

from pkg_resources import resource_filename

from tests import utility_test_objects

from tradingplatformpoc import data_store
from tradingplatformpoc.bid import Resource
from tradingplatformpoc.trading_platform_utils import datetime_array_between

FEB_1_1_AM = datetime(2019, 2, 1, 1, 0, 0)

DATETIME_ARRAY = datetime_array_between(datetime(2018, 12, 31, 23), datetime(2020, 1, 31, 22))
CONSTANT_NORDPOOL_PRICE = 0.6  # Doesn't matter what this is
ONES_SERIES = pd.Series(np.ones(shape=len(DATETIME_ARRAY)), index=DATETIME_ARRAY)


class TestDataStore(TestCase):
    data_store_entity = data_store.DataStore(config_area_info=utility_test_objects.AREA_INFO,
                                             nordpool_data=ONES_SERIES * CONSTANT_NORDPOOL_PRICE,
                                             irradiation_data=ONES_SERIES)

    def test_get_nordpool_price_for_period(self):
        """Test that what we put into data_store is the same as we get out"""
        self.assertEqual(CONSTANT_NORDPOOL_PRICE,
                         self.data_store_entity.get_nordpool_price_for_period(FEB_1_1_AM))

    def test_estimated_retail_price_greater_than_wholesale_price(self):
        """Test that the retail price is always greater than the wholesale price"""
        # May want to test for other resources than ELECTRICITY
        for dt in DATETIME_ARRAY:
            retail_price = self.data_store_entity.get_estimated_retail_price(dt, Resource.ELECTRICITY)
            wholesale_price = self.data_store_entity.get_estimated_wholesale_price(dt, Resource.ELECTRICITY)
            self.assertTrue(retail_price > wholesale_price)

    def test_get_estimated_price_for_non_implemented_resource(self):
        with self.assertRaises(RuntimeError):
            self.data_store_entity.get_estimated_retail_price(FEB_1_1_AM, Resource.COOLING)

    def test_read_school_csv(self):
        """Test that the CSV file with school energy data reads correctly."""
        file_path = resource_filename("tradingplatformpoc.data", "school_electricity_consumption.csv")
        school_energy_data = data_store.read_school_energy_consumption_csv(file_path)
        self.assertTrue(school_energy_data.shape[0] > 0)
        self.assertIsInstance(school_energy_data.index, DatetimeIndex)

    def test_add_external_heating_sell(self):
        ds = data_store.DataStore(config_area_info=utility_test_objects.AREA_INFO,
                                  nordpool_data=ONES_SERIES * CONSTANT_NORDPOOL_PRICE,
                                  irradiation_data=ONES_SERIES)
        self.assertEqual(0, len(ds.all_external_heating_sells))
        ds.add_external_heating_sell(FEB_1_1_AM, 50.0)
        self.assertEqual(1, len(ds.all_external_heating_sells))

    def test_add_external_heating_sell_where_already_exists(self):
        ds = data_store.DataStore(config_area_info=utility_test_objects.AREA_INFO,
                                  nordpool_data=ONES_SERIES * CONSTANT_NORDPOOL_PRICE,
                                  irradiation_data=ONES_SERIES)
        self.assertEqual(0, len(ds.all_external_heating_sells))
        ds.add_external_heating_sell(FEB_1_1_AM, 50.0)
        self.assertEqual(1, len(ds.all_external_heating_sells))
        self.assertAlmostEqual(50.0, ds.all_external_heating_sells[FEB_1_1_AM])
        # Now add again for the same period, with different value
        # First test that this logs as expectec
        with self.assertLogs() as captured:
            ds.add_external_heating_sell(FEB_1_1_AM, 70.0)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelname, 'WARNING')
        # Then test that the result of the operation is expected
        self.assertEqual(1, len(ds.all_external_heating_sells))
        self.assertAlmostEqual(70.0, ds.all_external_heating_sells[FEB_1_1_AM])
