import datetime
import math
from unittest import TestCase

import numpy as np

from tradingplatformpoc.bid import Action, Bid, Resource
from tradingplatformpoc.market_solver import no_bids_accepted, resolve_bids
from tradingplatformpoc.trading_platform_utils import ALL_IMPLEMENTED_RESOURCES

SOME_DATETIME = datetime.datetime(2019, 1, 2)


class TestMarketSolver(TestCase):

    def test_resolve_bids_1(self):
        """Test the clearing price calculation in a very simple example with one seller and one buyer."""
        bids = [Bid(Action.SELL, Resource.ELECTRICITY, 100, 1, "Seller1", False),
                Bid(Action.BUY, Resource.ELECTRICITY, 100, 1.5, "Buyer1", False)]
        # Someone willing to sell 100 kWh at 1 SEK/kWh,
        # someone willing to buy 100 kWh at 1.5 SEK/kWh.
        # Clearing price should be 1 SEK/kWh
        clearing_prices, bids_with_acceptance_status = resolve_bids(SOME_DATETIME, bids)
        self.assertEqual(len(ALL_IMPLEMENTED_RESOURCES), len(clearing_prices))
        self.assertEqual(1, clearing_prices[Resource.ELECTRICITY])
        for bid in bids_with_acceptance_status:
            self.assertIsNotNone(bid.was_accepted)
            self.assertTrue(bid.was_accepted)

    def test_resolve_bids_2(self):
        """Test the clearing price calculation when there are no accepted bids on one side (buy-side)."""
        bids = [Bid(Action.SELL, Resource.ELECTRICITY, 100, 1, "Seller1", False),
                Bid(Action.BUY, Resource.ELECTRICITY, 100, 0.5, "Buyer1", False)]
        # Someone willing to sell 100 kWh at 1 SEK/kWh,
        # someone willing to buy 100 kWh at 0.5 SEK/kWh.
        # Clearing price should be 1 SEK/kWh
        clearing_prices, bids_with_acceptance_status = resolve_bids(SOME_DATETIME, bids)
        self.assertEqual(len(ALL_IMPLEMENTED_RESOURCES), len(clearing_prices))
        self.assertEqual(1, clearing_prices[Resource.ELECTRICITY])
        for bid in bids_with_acceptance_status:
            self.assertIsNotNone(bid.was_accepted)
            if bid.source == 'Buyer1':
                self.assertFalse(bid.was_accepted)
            else:
                self.assertTrue(bid.was_accepted)

    def test_resolve_bids_3(self):
        """Test the clearing price calculation when there are 4 different bids, one of them from an external grid."""
        bids = [Bid(Action.SELL, Resource.ELECTRICITY, 100, 0, "Seller1", False),
                Bid(Action.SELL, Resource.ELECTRICITY, 100, 1.5, "Seller2", False),
                Bid(Action.SELL, Resource.ELECTRICITY, 10000, 10, "Grid", True),
                Bid(Action.BUY, Resource.ELECTRICITY, 300, math.inf, "Buyer1", False)]
        # Someone willing to sell 100 kWh at 0 SEK/kWh,
        # someone willing to sell 100 kWh at 1.5 SEK/kWh,
        # someone willing to sell 10000 kWh at 10 SEK/kWh,
        # someone willing to buy 300 kWh at Inf SEK/kWh.
        # Clearing price should be 10 SEK/kWh
        clearing_prices, bids_with_acceptance_status = resolve_bids(SOME_DATETIME, bids)
        self.assertEqual(len(ALL_IMPLEMENTED_RESOURCES), len(clearing_prices))
        self.assertEqual(10, clearing_prices[Resource.ELECTRICITY])
        for bid in bids_with_acceptance_status:
            self.assertTrue(bid.was_accepted)

    def test_resolve_bids_4(self):
        """Test the clearing price calculation when there are 4 different bids, and the locally produced energy covers
        the demand."""
        bids = [Bid(Action.SELL, Resource.ELECTRICITY, 10000, 2, "Grid", True),
                Bid(Action.SELL, Resource.ELECTRICITY, 100, 0.75, "Seller1", False),
                Bid(Action.SELL, Resource.ELECTRICITY, 100, 1, "Seller2", False),
                Bid(Action.BUY, Resource.ELECTRICITY, 200, math.inf, "Buyer1", False)]
        # Top bid being typical for external grid
        # Someone willing to sell 100 kWh at 0.75 SEK/kWh,
        # someone willing to sell 100 kWh at 1 SEK/kWh,
        # someone willing to buy 200 kWh at Inf SEK/kWh.
        # Clearing price should be 1 SEK/kWh
        clearing_prices, bids_with_acceptance_status = resolve_bids(SOME_DATETIME, bids)
        self.assertEqual(len(ALL_IMPLEMENTED_RESOURCES), len(clearing_prices))
        self.assertEqual(1, clearing_prices[Resource.ELECTRICITY])
        for bid in bids_with_acceptance_status:
            self.assertIsNotNone(bid.was_accepted)
            if bid.source == 'Grid':
                self.assertFalse(bid.was_accepted)
            else:
                self.assertTrue(bid.was_accepted)

    def test_resolve_bids_5(self):
        """
        Test that no bids are deemed 'accepted' and that the clearing price is np.nan when there isn't enough energy to
        satisfy the local demand.
        """
        bids = [Bid(Action.SELL, Resource.ELECTRICITY, 100, 0.75, "Seller1", False),
                Bid(Action.BUY, Resource.ELECTRICITY, 200, math.inf, "Buyer1", False)]
        clearing_prices, bids_with_acceptance_status = resolve_bids(SOME_DATETIME, bids)
        self.assertEqual(len(ALL_IMPLEMENTED_RESOURCES), len(clearing_prices))
        self.assertTrue(np.isnan(clearing_prices[Resource.ELECTRICITY]))
        for bid in bids_with_acceptance_status:
            self.assertFalse(bid.was_accepted)

    def test_resolve_bids_with_local_surplus(self):
        """Test that the clearing price is calculated correctly when there is a local surplus."""
        bids = [Bid(Action.BUY, Resource.ELECTRICITY, 192.76354849517332, math.inf, 'BuildingAgent', False),
                Bid(Action.SELL, Resource.ELECTRICITY, 100, 0.46069, 'BatteryStorageAgent', False),
                Bid(Action.SELL, Resource.ELECTRICITY, 275.3113968, 0.46069, 'PVAgent', False),
                Bid(Action.BUY, Resource.ELECTRICITY, 100.8875027389364, math.inf, 'CommercialBuildingAgent', False),
                Bid(Action.SELL, Resource.ELECTRICITY, 10000, 0.89069, 'ElectricityGridAgent', True)]
        # Local surplus
        # Clearing price should be 0.46069 SEK/kWh
        clearing_prices, bids_with_acceptance_status = resolve_bids(SOME_DATETIME, bids)
        self.assertEqual(len(ALL_IMPLEMENTED_RESOURCES), len(clearing_prices))
        self.assertEqual(0.46069, clearing_prices[Resource.ELECTRICITY])
        for bid in bids_with_acceptance_status:
            self.assertIsNotNone(bid.was_accepted)
            if bid.source == 'ElectricityGridAgent':
                self.assertFalse(bid.was_accepted)
            else:
                self.assertTrue(bid.was_accepted)

    def test_resolve_bids_with_only_sell_bid_for_heating(self):
        """Test that if for a resource, there are only sell bids and no buy bids, the clearing price is nan. Also, it
        shouldn't break anything for other resources."""
        bids = [Bid(Action.BUY, Resource.ELECTRICITY, 200, math.inf, 'BuildingAgent', False),
                Bid(Action.SELL, Resource.ELECTRICITY, 10000, 0.8, 'ElectricityGridAgent', True),
                Bid(Action.SELL, Resource.HEATING, 10000, 2.0, 'HeatingGridAgent', True)]
        # Local surplus
        # Clearing price for electricity should be 0.46069 SEK/kWh
        clearing_prices, bids_with_acceptance_status = resolve_bids(SOME_DATETIME, bids)
        self.assertEqual(len(ALL_IMPLEMENTED_RESOURCES), len(clearing_prices))
        self.assertEqual(0.8, clearing_prices[Resource.ELECTRICITY])
        self.assertTrue(np.isnan(clearing_prices[Resource.HEATING]))
        for bid in bids_with_acceptance_status:
            self.assertIsNotNone(bid.was_accepted)
            if bid.source == 'HeatingGridAgent':
                self.assertFalse(bid.was_accepted)
            else:
                self.assertTrue(bid.was_accepted)

    def test_resolve_bids_with_two_resources(self):
        """Test that clearing prices are calculated correctly for two resources."""
        bids = [Bid(Action.BUY, Resource.ELECTRICITY, 200, math.inf, 'BuildingAgent', False),
                Bid(Action.SELL, Resource.ELECTRICITY, 10000, 0.8, 'ElectricityGridAgent', True),
                Bid(Action.BUY, Resource.HEATING, 400, math.inf, 'BuildingAgent', False),
                Bid(Action.SELL, Resource.HEATING, 10000, 2.0, 'HeatingGridAgent', True)]
        clearing_prices, bids_with_acceptance_status = resolve_bids(SOME_DATETIME, bids)
        self.assertEqual(len(ALL_IMPLEMENTED_RESOURCES), len(clearing_prices))
        self.assertEqual(0.8, clearing_prices[Resource.ELECTRICITY])
        self.assertEqual(2, clearing_prices[Resource.HEATING])
        for bid in bids_with_acceptance_status:
            self.assertIsNotNone(bid.was_accepted)
            self.assertTrue(bid.was_accepted)

    def test_no_bids_accepted(self):
        """Test that the no_bids_accepted method doesn't throw a fit when an empty list is passed in."""
        with_acceptance_status = no_bids_accepted([])
        self.assertEqual(0, len(with_acceptance_status))
