import datetime
from unittest import TestCase, mock

import pandas as pd

import pytz

from tradingplatformpoc.config.access_config import read_config
from tradingplatformpoc.data.preprocessing import read_and_process_input_data
from tradingplatformpoc.generate_data.mock_data_utils import get_elec_cons_key, \
    get_hot_tap_water_cons_key, get_space_heat_cons_key
from tradingplatformpoc.price.heating_price import HeatingPrice
from tradingplatformpoc.simulation_runner.trading_simulator import TradingSimulator
from tradingplatformpoc.sql.job.models import uuid_as_str_generator
from tradingplatformpoc.trading_platform_utils import get_external_prices


class Test(TestCase):

    fake_job_id = "111111111111"
    config = read_config()
    heat_pricing: HeatingPrice = HeatingPrice(
        heating_wholesale_price_fraction=config['AreaInfo']['ExternalHeatingWholesalePriceFraction'])

    def test_initialize_agents(self):
        """Test that an error is thrown if no GridAgents are initialized."""
        fake_config = {'Agents': [agent for agent in self.config['Agents'] if agent['Type'] != 'GridAgent'],
                       'AreaInfo': self.config['AreaInfo'],
                       'MockDataConstants': self.config['MockDataConstants']}
        agent_specs = {agent['Name']: uuid_as_str_generator() for agent in fake_config['Agents'][:]
                       if agent['Type'] == 'BlockAgent'}
        mock_data_columns = [[get_elec_cons_key(agent_id),
                              get_space_heat_cons_key(agent_id),
                              get_hot_tap_water_cons_key(agent_id)] for agent_id in agent_specs.values()]
        input_data = read_and_process_input_data()[[
            'datetime', 'irradiation', 'coop_electricity_consumed', 'coop_hot_tap_water_consumed',
            'coop_space_heating_consumed', 'coop_space_heating_produced']].rename(columns={'datetime': 'period'})

        with (mock.patch('tradingplatformpoc.simulation_runner.trading_simulator.get_config_id_for_job_id',
                         return_value='fake_config_id'),
              mock.patch('tradingplatformpoc.simulation_runner.trading_simulator.read_config',
                         return_value=fake_config),
              mock.patch('tradingplatformpoc.simulation_runner.trading_simulator.get_all_agent_name_id_pairs_in_config',
                         return_value=agent_specs),
              mock.patch('tradingplatformpoc.simulation_runner.trading_simulator.get_periods_from_db',
                         return_value=pd.DatetimeIndex(input_data.period)),
              mock.patch('tradingplatformpoc.simulation_runner.trading_simulator.get_generated_mock_data',
                         return_value=pd.DataFrame(columns=[bid for sublist in mock_data_columns for bid in sublist])),
              mock.patch('tradingplatformpoc.simulation_runner.trading_simulator.read_inputs_df_for_agent_creation',
                         return_value=input_data),
              mock.patch('tradingplatformpoc.simulation_runner.trading_simulator.get_nordpool_data',
                         return_value=pd.Series())):
            simulator = TradingSimulator('fake_job_id')
            simulator.initialize_data()
            with self.assertRaises(RuntimeError):
                simulator.initialize_agents()

    def test_get_external_heating_prices_from_empty_data_store(self):
        """
        When trying to calculate external heating prices using an empty DataStore, NaNs should be returned for all
        prices.
        """
        datetime_index = pd.DatetimeIndex([datetime.datetime(2019, 2, 1, tzinfo=pytz.UTC),
                                           datetime.datetime(2019, 2, 2, tzinfo=pytz.UTC)])
        heating_price_list = get_external_prices(self.heat_pricing, self.fake_job_id,
                                                 datetime_index, ['abc'], True)
        heating_prices = pd.DataFrame.from_records(heating_price_list)
        self.assertTrue(heating_prices.exact_retail_price.isna().all())
        self.assertTrue(heating_prices.exact_wholesale_price.isna().all())
        self.assertTrue(heating_prices.estimated_retail_price.isna().all())
        self.assertTrue(heating_prices.estimated_wholesale_price.isna().all())
