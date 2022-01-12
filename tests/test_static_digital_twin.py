from datetime import datetime
from unittest import TestCase

import numpy as np
import pandas as pd

from tradingplatformpoc.bid import Resource
from tradingplatformpoc.digitaltwin.static_digital_twin import StaticDigitalTwin
from tradingplatformpoc.trading_platform_utils import datetime_array_between

FEB_1ST_1AM = datetime(2019, 2, 1, 1, 0, 0)


class TestStaticDigitalTwin(TestCase):
    datetime_array = datetime_array_between(datetime(2019, 2, 1, 1), datetime(2020, 1, 31, 23))
    ones_series = pd.Series(np.ones(shape=len(datetime_array)), index=datetime_array)

    building_digital_twin = StaticDigitalTwin(electricity_usage=ones_series * 150,
                                              heating_usage=ones_series * 250)

    def test_get_electricity_consumed(self):
        self.assertAlmostEqual(150, self.building_digital_twin.get_consumption(FEB_1ST_1AM, Resource.ELECTRICITY))

    def test_not_specified_series(self):
        self.assertAlmostEqual(0, self.building_digital_twin.get_production(FEB_1ST_1AM, Resource.ELECTRICITY))