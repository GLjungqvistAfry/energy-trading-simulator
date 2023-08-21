import datetime
import logging
import pickle
from typing import Any, Collection, Dict, List, Union

import numpy as np

import pandas as pd

from tradingplatformpoc.generate_data import generate_mock_data
from tradingplatformpoc.generate_data.mock_data_generation_functions import MockDataKey, get_all_building_agents
from tradingplatformpoc.market.bid import Action, GrossBid, NetBid, NetBidWithAcceptanceStatus, Resource
from tradingplatformpoc.market.trade import Trade, TradeMetadataKey
from tradingplatformpoc.price.electricity_price import ElectricityPrice
from tradingplatformpoc.price.heating_price import HeatingPrice
from tradingplatformpoc.trading_platform_utils import add_to_nested_dict

logger = logging.getLogger(__name__)


def net_bids_from_gross_bids(gross_bids: List[GrossBid], electricty_pricing: ElectricityPrice) -> List[NetBid]:
    """
    Add in internal tax and internal grid fee for internal SELL bids (for electricity, heating is not taxed).
    Note: External electricity bids already have grid fee
    """
    net_bids: List[NetBid] = []
    for gross_bid in gross_bids:
        if gross_bid.action == Action.SELL and gross_bid.resource == Resource.ELECTRICITY:
            if gross_bid.by_external:
                net_price = electricty_pricing.get_electricity_net_external_price(gross_bid.price)
                net_bids.append(NetBid.from_gross_bid(gross_bid, net_price))
            else:
                net_price = electricty_pricing.get_electricity_net_internal_price(gross_bid.price)
                net_bids.append(NetBid.from_gross_bid(gross_bid, net_price))
        else:
            net_bids.append(NetBid.from_gross_bid(gross_bid, gross_bid.price))
    return net_bids


def go_through_trades_metadata(metadata: Dict[TradeMetadataKey, Any], period: datetime.datetime, agent_guid: str,
                               heat_pump_levels_dict: Dict[str, Dict[datetime.datetime, float]],
                               storage_levels_dict: Dict[str, Dict[datetime.datetime, float]]):
    """
    The agent may want to send some metadata along with its trade, to the simulation runner. Any such metadata is dealt
    with here.
    """
    for metadata_key in metadata:
        if metadata_key == TradeMetadataKey.STORAGE_LEVEL:
            capacity_for_agent = metadata[metadata_key]
            add_to_nested_dict(storage_levels_dict, agent_guid, period, capacity_for_agent)
        elif metadata_key == TradeMetadataKey.HEAT_PUMP_WORKLOAD:
            current_heat_pump_level = metadata[metadata_key]
            add_to_nested_dict(heat_pump_levels_dict, agent_guid, period, current_heat_pump_level)
        else:
            logger.info('Encountered unexpected metadata! Key: {}, Value: {}'.
                        format(metadata_key, metadata[metadata_key]))


def get_external_heating_prices(heat_pricing: HeatingPrice,
                                trading_periods: Collection[datetime.datetime]) -> List[Dict[str, Any]]:
    heating_price_by_ym_lst: List[Dict[str, Any]] = []
    for (year, month) in set([(dt.year, dt.month) for dt in trading_periods]):
        first_day_of_month = datetime.datetime(year, month, 1)  # Which day it is doesn't matter
        heating_price_by_ym_lst.append({
            'year': year,
            'month': month,
            'exact_retail_price': heat_pricing.get_exact_retail_price(first_day_of_month, include_tax=True),
            'exact_wholesale_price': heat_pricing.get_exact_wholesale_price(first_day_of_month),
            'estimated_retail_price': heat_pricing.get_estimated_retail_price(first_day_of_month, include_tax=True),
            'estimated_wholesale_price': heat_pricing.get_estimated_wholesale_price(first_day_of_month)})
    return heating_price_by_ym_lst


def get_generated_mock_data(config_data: dict, mock_datas_pickle_path: str) -> pd.DataFrame:
    """
    Loads the dict stored in MOCK_DATAS_PICKLE, checks if it contains a key which is identical to the set of building
    agents specified in config_data. If it isn't, throws an error. If it is, it returns the value for that key in the
    dictionary.
    @param config_data: A dictionary specifying agents etc
    @param mock_datas_pickle_path: Path to pickle file where dict with mock data is saved
    @return: A pd.DataFrame containing mock data for building agents
    """
    with open(mock_datas_pickle_path, 'rb') as f:
        all_data_sets = pickle.load(f)
    building_agents, total_gross_floor_area = get_all_building_agents(config_data["Agents"])
    mock_data_key = MockDataKey(frozenset(building_agents), frozenset(config_data["MockDataConstants"].items()))
    if mock_data_key not in all_data_sets:
        logger.info("No mock data found for this configuration. Running mock data generation.")
        all_data_sets = generate_mock_data.run(config_data)
        logger.info("Finished mock data generation.")
    return all_data_sets[mock_data_key].to_pandas().set_index('datetime')


def get_quantity_heating_sold_by_external_grid(external_trades: List[Trade]) -> float:
    return sum([x.quantity_post_loss for x in external_trades if
                (x.resource == Resource.HEATING) & (x.action == Action.SELL)])


def construct_df_from_datetime_dict(some_dict: Union[Dict[datetime.datetime, Collection[NetBidWithAcceptanceStatus]],
                                                     Dict[datetime.datetime, Collection[Trade]]]) \
        -> pd.DataFrame:
    """
    Streamlit likes to deal with pd.DataFrames, so we'll save data in that format.
    """
    logger.info('Constructing dataframe from datetime dict')
    return pd.DataFrame([x.to_dict_with_period(period) for period, some_collection in some_dict.items()
                         for x in some_collection])


def get_local_price_if_exists_else_external_estimate(period: datetime.datetime, clearing_prices_historical:
                                                     Union[Dict[datetime.datetime, Dict[Resource, float]], None],
                                                     pricing: Collection[Union[ElectricityPrice, HeatingPrice]]
                                                     ) -> Dict[Resource, float]:
    to_return = {}

    if clearing_prices_historical is not None:
        clearing_prices = dict(clearing_prices_historical)
        if period in clearing_prices:
            to_return = clearing_prices[period].copy()  # Copy to avoid modifying clearing_prices_historical
    for pricing_for_resource in pricing:
        if (pricing_for_resource.resource not in to_return) or (to_return[pricing_for_resource.resource] is None)\
           or (np.isnan(to_return[pricing_for_resource.resource])):
            logger.debug('For period {}, resource {}, no historical clearing prices available, will use external '
                         'prices instead.'.format(period, pricing_for_resource.resource))
            to_return[pricing_for_resource.resource] = pricing_for_resource.get_estimated_retail_price(period,
                                                                                                       include_tax=True)
    return to_return