import unittest
import json
from datetime import datetime
from unittest import TestCase

from tradingplatformpoc import data_store, agent
from tradingplatformpoc.bid import Resource, Action
from tradingplatformpoc.digitaltwin.static_digital_twin import StaticDigitalTwin
from tradingplatformpoc.digitaltwin.storage_digital_twin import StorageDigitalTwin
from tradingplatformpoc.trade import Trade, Market

import tradingplatformpoc.agent.building_agent
import tradingplatformpoc.agent.grid_agent
import tradingplatformpoc.agent.grocery_store_agent
import tradingplatformpoc.agent.pv_agent
import tradingplatformpoc.agent.storage_agent

data_store_entity = data_store.DataStore(config_area_info={
    "ParkPVArea": 24324.3,
    "StorePVArea": 320,
    "PVEfficiency": 0.165
})


class TestGridAgent(unittest.TestCase):
    grid_agent = tradingplatformpoc.agent.grid_agent.ElectricityGridAgent(data_store_entity)

    def test_make_bids(self):
        bids = self.grid_agent.make_bids(datetime(2019, 2, 1, 1, 0, 0))
        self.assertEqual(1, len(bids))
        self.assertEqual(Resource.ELECTRICITY, bids[0].resource)
        self.assertEqual(Action.SELL, bids[0].action)
        self.assertTrue(bids[0].quantity > 0)

    def test_calculate_trades_1(self):
        retail_price = 0.99871
        trades_excl_external = [
            Trade(Action.BUY, Resource.ELECTRICITY, 100, retail_price, "BuildingAgent", False, Market.LOCAL,
                  datetime(2019, 2, 1, 1, 0, 0))
        ]
        external_trades = self.grid_agent.calculate_external_trades(trades_excl_external, retail_price)
        self.assertEqual(1, len(external_trades))
        self.assertEqual(Action.SELL, external_trades[0].action)
        self.assertEqual(Resource.ELECTRICITY, external_trades[0].resource)
        self.assertEqual(trades_excl_external[0].quantity, external_trades[0].quantity)
        self.assertEqual(retail_price, external_trades[0].price)
        self.assertEqual("ElectricityGridAgent", external_trades[0].source)
        self.assertEqual(Market.LOCAL, external_trades[0].market)
        self.assertEqual(datetime(2019, 2, 1, 1, 0, 0), external_trades[0].period)

    def test_calculate_trades_local_equilibrium(self):
        retail_price = 0.99871
        trades_excl_external = [
            Trade(Action.BUY, Resource.ELECTRICITY, 100, retail_price, "BuildingAgent", False, Market.LOCAL,
                  datetime(2019, 2, 1, 1, 0, 0)),
            Trade(Action.SELL, Resource.ELECTRICITY, 100, retail_price, "PVAgent", False, Market.LOCAL,
                  datetime(2019, 2, 1, 1, 0, 0))
        ]
        external_trades = self.grid_agent.calculate_external_trades(trades_excl_external, retail_price)
        self.assertEqual(0, len(external_trades))

    def test_calculate_trades_price_not_matching(self):
        local_price = 1
        trades_excl_external = [
            Trade(Action.BUY, Resource.ELECTRICITY, 100, local_price, "BuildingAgent", False, Market.LOCAL,
                  datetime(2019, 2, 1, 1, 0, 0))
        ]
        with self.assertRaises(RuntimeError):
            external_trades = self.grid_agent.calculate_external_trades(trades_excl_external, local_price)

    def test_calculate_trades_price_not_matching_2(self):
        local_price = 0.5
        trades_excl_external = [
            Trade(Action.BUY, Resource.ELECTRICITY, 100, local_price, "BuildingAgent", False, Market.LOCAL,
                  datetime(2019, 2, 1, 1, 0, 0))
        ]
        # Should log a line about external grid and market clearing price being different
        external_trades = self.grid_agent.calculate_external_trades(trades_excl_external, local_price)
        self.assertEqual(1, len(external_trades))

    def test_calculate_trades_2(self):
        wholesale_price = 0.56871
        period = datetime(2019, 2, 1, 1, 0, 0)
        trades_excl_external = [
            Trade(Action.BUY, Resource.ELECTRICITY, 100, wholesale_price, "BuildingAgent", False, Market.LOCAL, period),
            Trade(Action.BUY, Resource.ELECTRICITY, 200, wholesale_price, "GSAgent", False, Market.LOCAL, period),
            Trade(Action.SELL, Resource.ELECTRICITY, 400, wholesale_price, "PvAgent", False, Market.LOCAL, period)
        ]
        external_trades = self.grid_agent.calculate_external_trades(trades_excl_external, wholesale_price)
        self.assertEqual(1, len(external_trades))
        self.assertEqual(Action.BUY, external_trades[0].action)
        self.assertEqual(Resource.ELECTRICITY, external_trades[0].resource)
        self.assertEqual(100, external_trades[0].quantity)
        self.assertEqual(wholesale_price, external_trades[0].price)
        self.assertEqual("ElectricityGridAgent", external_trades[0].source)
        self.assertEqual(Market.LOCAL, external_trades[0].market)
        self.assertEqual(period, external_trades[0].period)


class TestBatteryStorageAgent(unittest.TestCase):
    twin = StorageDigitalTwin(max_capacity_kwh=1000, max_charge_rate_fraction=0.1, max_discharge_rate_fraction=0.1)
    battery_agent = tradingplatformpoc.agent.storage_agent.StorageAgent(data_store_entity, twin, 168, 20, 80)

    def test_make_bids(self):
        bids = self.battery_agent.make_bids(datetime(2019, 2, 1, 1, 0, 0), {})
        self.assertEqual(Resource.ELECTRICITY, bids[0].resource)
        self.assertEqual(Action.BUY, bids[0].action)
        self.assertTrue(bids[0].quantity > 0)
        self.assertTrue(bids[0].quantity <= 1000)
        self.assertTrue(bids[0].price > 0)


class TestBuildingAgent(TestCase):
    building_digital_twin = StaticDigitalTwin(electricity_usage=data_store_entity.tornet_household_elec_cons,
                                              heating_usage=data_store_entity.tornet_heat_cons)
    building_agent = agent.building_agent.BuildingAgent(data_store_entity, building_digital_twin)

    def test_make_bids(self):
        bids = self.building_agent.make_bids(datetime(2019, 2, 1, 1, 0, 0))
        self.assertEqual(bids[0].resource, Resource.ELECTRICITY)
        self.assertEqual(bids[0].action, Action.BUY)
        self.assertTrue(bids[0].quantity > 0)
        self.assertTrue(bids[0].price > 0)


class TestGroceryStoreAgent(TestCase):
    grocery_store_digital_twin = StaticDigitalTwin(electricity_usage=data_store_entity.coop_elec_cons,
                                                   heating_usage=data_store_entity.coop_heat_cons,
                                                   electricity_production=data_store_entity.coop_pv_prod)
    grocery_store_agent = tradingplatformpoc.agent.grocery_store_agent.GroceryStoreAgent(data_store_entity,
                                                                                         grocery_store_digital_twin)

    def test_make_bids(self):
        bids = self.grocery_store_agent.make_bids(datetime(2019, 7, 7, 11, 0, 0))
        self.assertEqual(Resource.ELECTRICITY, bids[0].resource)
        self.assertEqual(Action.BUY, bids[0].action)
        self.assertAlmostEqual(193.7625279202484, bids[0].quantity)
        self.assertTrue(bids[0].price > 1000)


class TestPVAgent(TestCase):
    pv_digital_twin = StaticDigitalTwin(electricity_production=data_store_entity.tornet_park_pv_prod)
    tornet_pv_agent = tradingplatformpoc.agent.pv_agent.PVAgent(data_store_entity, pv_digital_twin)

    def test_make_bids(self):
        bids = self.tornet_pv_agent.make_bids(datetime(2019, 7, 7, 11, 0, 0))
        self.assertEqual(Resource.ELECTRICITY, bids[0].resource)
        self.assertEqual(Action.SELL, bids[0].action)
        self.assertAlmostEqual(3215.22246045, bids[0].quantity)
        self.assertAlmostEqual(0.34389, bids[0].price)
