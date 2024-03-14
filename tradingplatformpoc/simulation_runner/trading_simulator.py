import datetime
import logging
import math
import threading
from typing import Any, Dict, List, Tuple

import pandas as pd

from tradingplatformpoc.agent.block_agent import BlockAgent
from tradingplatformpoc.agent.grid_agent import GridAgent
from tradingplatformpoc.agent.iagent import IAgent
from tradingplatformpoc.app.app_threading import StoppableThread
from tradingplatformpoc.constants import LEC_CAN_SELL_HEAT_TO_EXTERNAL
from tradingplatformpoc.database import bulk_insert
from tradingplatformpoc.digitaltwin.battery import Battery
from tradingplatformpoc.digitaltwin.static_digital_twin import StaticDigitalTwin
from tradingplatformpoc.generate_data.generate_mock_data import get_generated_mock_data
from tradingplatformpoc.generate_data.mock_data_utils import get_cooling_cons_key, get_elec_cons_key, \
    get_hot_tap_water_cons_key, get_space_heat_cons_key
from tradingplatformpoc.market.balance_manager import correct_for_exact_heating_price
from tradingplatformpoc.market.extra_cost import ExtraCost
from tradingplatformpoc.market.trade import Resource, Trade, TradeMetadataKey
from tradingplatformpoc.price.electricity_price import ElectricityPrice
from tradingplatformpoc.price.heating_price import HeatingPrice
from tradingplatformpoc.simulation_runner.chalmers_interface import optimize
from tradingplatformpoc.simulation_runner.results_calculator import calculate_results_and_save
from tradingplatformpoc.sql.config.crud import get_all_agents_in_config, read_config
from tradingplatformpoc.sql.electricity_price.models import ElectricityPrice as TableElectricityPrice
from tradingplatformpoc.sql.extra_cost.crud import extra_costs_to_db_dict
from tradingplatformpoc.sql.extra_cost.models import ExtraCost as TableExtraCost
from tradingplatformpoc.sql.heating_price.models import HeatingPrice as TableHeatingPrice
from tradingplatformpoc.sql.input_data.crud import get_periods_from_db, read_inputs_df_for_agent_creation
from tradingplatformpoc.sql.input_electricity_price.crud import electricity_price_series_from_db
from tradingplatformpoc.sql.job.crud import delete_job, get_config_id_for_job_id, \
    update_job_with_time
from tradingplatformpoc.sql.level.crud import levels_to_db_dict
from tradingplatformpoc.sql.level.models import Level as TableLevel
from tradingplatformpoc.sql.trade.crud import trades_to_db_dict
from tradingplatformpoc.sql.trade.models import Trade as TableTrade
from tradingplatformpoc.trading_platform_utils import add_all_to_nested_dict, calculate_solar_prod, \
    get_external_heating_prices, get_glpk_solver

logger = logging.getLogger(__name__)


class TradingSimulator:
    def __init__(self, job_id: str):
        self.solver = get_glpk_solver()
        self.job_id = job_id
        self.config_id = get_config_id_for_job_id(self.job_id)
        self.config_data: Dict[str, Any] = read_config(self.config_id)
        self.agent_specs = get_all_agents_in_config(self.config_id)

    def __call__(self):
        if (self.job_id is not None) and (self.config_data is not None):
            try:
                update_job_with_time(self.job_id, 'start_time')
                self.initialize_data()
                self.agents, self.grid_agents = self.initialize_agents()
                self.run()
                self.extract_heating_price()
                update_job_with_time(self.job_id, 'end_time')

            except Exception as e:
                logger.exception(e)
                delete_job(self.job_id)

    def initialize_data(self):
        self.local_market_enabled = self.config_data['AreaInfo']['LocalMarketEnabled']

        self.heat_pricing: HeatingPrice = HeatingPrice(
            heating_wholesale_price_fraction=self.config_data['AreaInfo']['ExternalHeatingWholesalePriceFraction'],
            heat_transfer_loss=self.config_data['AreaInfo']["HeatTransferLoss"])
        self.electricity_pricing: ElectricityPrice = ElectricityPrice(
            elec_wholesale_offset=self.config_data['AreaInfo']['ExternalElectricityWholesalePriceOffset'],
            elec_tax=self.config_data['AreaInfo']["ElectricityTax"],
            elec_grid_fee=self.config_data['AreaInfo']["ElectricityGridFee"],
            elec_tax_internal=self.config_data['AreaInfo']["ElectricityTaxInternal"],
            elec_grid_fee_internal=self.config_data['AreaInfo']["ElectricityGridFeeInternal"],
            nordpool_data=electricity_price_series_from_db())

        self.trading_periods = get_periods_from_db().sort_values()
        self.trading_periods = self.trading_periods.take(list(range(48)) + list(range(4008, 4056)))  # FIXME: Remove
        self.trading_horizon = self.config_data['AreaInfo']['TradingHorizon']

    def initialize_agents(self) -> Tuple[List[IAgent], Dict[Resource, GridAgent]]:
        # Register all agents
        # Keep a list of all agents to iterate over later
        agents: List[IAgent] = []
        grid_agents: Dict[Resource, GridAgent] = {}

        # Read input data (irradiation and grocery store consumption) from database
        inputs_df = read_inputs_df_for_agent_creation()
        # Get mock data
        blocks_mock_data: pd.DataFrame = get_generated_mock_data(self.config_id)
        area_info = self.config_data['AreaInfo']

        for agent in self.config_data["Agents"]:
            agent_type = agent["Type"]
            agent_name = agent['Name']

            # Note - when making changes to StaticDigitalTwin creation here, you may also need to change the
            # "reconstruct_static_digital_twin"-related methods in app_data_display.py

            if agent_type == "BlockAgent":
                agent_id = self.agent_specs[agent['Name']]
                elec_cons_series = blocks_mock_data.get(get_elec_cons_key(agent_id))
                space_heat_cons_series = blocks_mock_data.get(get_space_heat_cons_key(agent_id))
                hot_tap_water_cons_series = blocks_mock_data.get(get_hot_tap_water_cons_key(agent_id))
                cool_cons_series = blocks_mock_data.get(get_cooling_cons_key(agent_id))
                pv_prod_series = calculate_solar_prod(inputs_df['irradiation'],
                                                      agent['PVArea'],
                                                      area_info['PVEfficiency'])

                block_digital_twin = StaticDigitalTwin(gross_floor_area=agent['GrossFloorArea'],
                                                       electricity_usage=elec_cons_series,
                                                       space_heating_usage=space_heat_cons_series,
                                                       hot_water_usage=hot_tap_water_cons_series,
                                                       cooling_usage=cool_cons_series,
                                                       electricity_production=pv_prod_series)

                storage_digital_twin = Battery(max_capacity_kwh=agent["BatteryCapacity"],
                                               max_charge_rate_fraction=area_info["BatteryChargeRate"],
                                               max_discharge_rate_fraction=area_info["BatteryDischargeRate"],
                                               discharging_efficiency=area_info["BatteryEfficiency"])

                agents.append(
                    BlockAgent(self.local_market_enabled, heat_pricing=self.heat_pricing,
                               electricity_pricing=self.electricity_pricing, digital_twin=block_digital_twin,
                               can_sell_heat_to_external=LEC_CAN_SELL_HEAT_TO_EXTERNAL,
                               heat_pump_max_input=agent["HeatPumpMaxInput"],
                               heat_pump_max_output=agent["HeatPumpMaxOutput"],
                               booster_pump_max_input=agent["BoosterPumpMaxInput"],
                               booster_pump_max_output=agent["BoosterPumpMaxOutput"],
                               acc_tank_volume=agent["AccumulatorTankVolume"],
                               battery=storage_digital_twin,
                               guid=agent_name))

            elif agent_type == "GroceryStoreAgent":
                pv_prod_series = calculate_solar_prod(inputs_df['irradiation'],
                                                      agent['PVArea'],
                                                      agent['PVEfficiency'])
                space_heat_prod = inputs_df['coop_space_heating_produced'] if agent["SellExcessHeat"] else None
                grocery_store_digital_twin = StaticDigitalTwin(gross_floor_area=agent['GrossFloorArea'],
                                                               electricity_usage=inputs_df['coop_electricity_consumed'],
                                                               space_heating_usage=inputs_df[
                                                                   'coop_space_heating_consumed'],
                                                               hot_water_usage=inputs_df['coop_hot_tap_water_consumed'],
                                                               electricity_production=pv_prod_series,
                                                               space_heating_production=space_heat_prod)
                agents.append(
                    BlockAgent(self.local_market_enabled, heat_pricing=self.heat_pricing,
                               electricity_pricing=self.electricity_pricing, digital_twin=grocery_store_digital_twin,
                               can_sell_heat_to_external=LEC_CAN_SELL_HEAT_TO_EXTERNAL,
                               heat_pump_max_input=agent["HeatPumpMaxInput"],
                               heat_pump_max_output=agent["HeatPumpMaxOutput"],
                               booster_pump_max_input=agent["BoosterPumpMaxInput"],
                               booster_pump_max_output=agent["BoosterPumpMaxOutput"],
                               acc_tank_volume=agent["AccumulatorTankVolume"],
                               guid=agent_name))
            elif agent_type == "GridAgent":
                if Resource[agent["Resource"]] == Resource.ELECTRICITY:
                    grid_agent = GridAgent(self.local_market_enabled, self.electricity_pricing,
                                           Resource[agent["Resource"]], can_buy=True,
                                           max_transfer_per_hour=agent["TransferRate"], guid=agent_name)
                elif Resource[agent["Resource"]] == Resource.HEATING:
                    grid_agent = GridAgent(self.local_market_enabled, self.heat_pricing, Resource[agent["Resource"]],
                                           can_buy=LEC_CAN_SELL_HEAT_TO_EXTERNAL,
                                           max_transfer_per_hour=agent["TransferRate"], guid=agent_name)
                agents.append(grid_agent)
                grid_agents[Resource[agent["Resource"]]] = grid_agent

        # Verify that we have a Grid Agent
        if not any(isinstance(agent, GridAgent) for agent in agents):
            raise RuntimeError("No grid agent initialized")

        return agents, grid_agents

    def run(self, number_of_batches: int = 5):
        """
        The core loop of the simulation, running through the desired time period and performing trades.
        """

        logger.info("Starting trading simulations")

        battery_levels_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        shallow_storage_rel_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        deep_storage_rel_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        shallow_storage_abs_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        deep_storage_abs_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        shallow_loss_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        deep_loss_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        shallow_charge_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        shallow_discharge_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        bites_flow_dict: Dict[str, Dict[datetime.datetime, float]] = {}
        hp_high_prod: Dict[str, Dict[datetime.datetime, float]] = {}
        hp_low_prod: Dict[str, Dict[datetime.datetime, float]] = {}

        number_of_trading_horizons = int(len(self.trading_periods) // self.trading_horizon)
        logger.info('Will run {} trading horizons'.format(number_of_trading_horizons))
        new_batch_size = math.ceil(number_of_trading_horizons / number_of_batches)

        # Loop over batches
        for batch_number in range(number_of_batches):
            current_thread = threading.current_thread()
            if isinstance(current_thread, StoppableThread):
                if current_thread.is_stopped():
                    logger.error('Simulation stopped by event.')
                    raise Exception("Simulation stopped by event.")
            logger.info("Simulating batch number {} of {}".format(batch_number + 1, number_of_batches))

            # Horizons in batch
            trading_horizon_start_points = self.trading_periods[::self.trading_horizon]
            thsps_in_this_batch = trading_horizon_start_points[
                batch_number * new_batch_size:min((batch_number + 1) * new_batch_size, number_of_trading_horizons)]
            all_trades_list_batch: List[List[Trade]] = []
            all_extra_costs_batch: List[ExtraCost] = []
            electricity_price_list_batch: List[dict] = []

            # ------- NEW --------
            for horizon_start in thsps_in_this_batch:
                logger.info("Simulating {:%Y-%m-%d}".format(horizon_start))
                # if horizon_start.day == horizon_start.hour == 1:
                #     info_string = "Simulations entering {:%B}".format(horizon_start)
                #     logger.info(info_string)
                # for i in range(10, len(self.agents) - len(self.grid_agents.keys())):
                #     print('Adding ' + self.agents[i-1].guid)
                #     chalmers_outputs = optimize(self.solver, self.agents[:i], self.grid_agents,
                #                                 self.config_data['AreaInfo'], horizon_start,
                #                                 self.electricity_pricing, self.heat_pricing)
                chalmers_outputs = optimize(self.solver, self.agents, self.grid_agents, self.config_data['AreaInfo'],
                                            horizon_start, self.electricity_pricing, self.heat_pricing)
                all_trades_list_batch.append(chalmers_outputs.trades)
                add_all_to_nested_dict(battery_levels_dict, chalmers_outputs.battery_storage_levels)
                add_all_to_nested_dict(shallow_storage_rel_dict, chalmers_outputs.shallow_storage_rel)
                add_all_to_nested_dict(deep_storage_rel_dict, chalmers_outputs.deep_storage_rel)
                add_all_to_nested_dict(shallow_storage_abs_dict, chalmers_outputs.shallow_storage_abs)
                add_all_to_nested_dict(deep_storage_abs_dict, chalmers_outputs.deep_storage_abs)
                add_all_to_nested_dict(shallow_loss_dict, chalmers_outputs.shallow_loss)
                add_all_to_nested_dict(deep_loss_dict, chalmers_outputs.deep_loss)
                add_all_to_nested_dict(shallow_charge_dict, chalmers_outputs.shallow_charge)
                add_all_to_nested_dict(shallow_discharge_dict, chalmers_outputs.shallow_discharge)
                add_all_to_nested_dict(bites_flow_dict, chalmers_outputs.bites_flow)
                add_all_to_nested_dict(hp_high_prod, chalmers_outputs.hp_high_prod)
                add_all_to_nested_dict(hp_low_prod, chalmers_outputs.hp_low_prod)

            logger.info('Saving trades to db...')
            trade_dict = trades_to_db_dict(all_trades_list_batch, self.job_id)
            bulk_insert(TableTrade, trade_dict)
            logger.info('Saving extra costs to db...')
            extra_cost_dict = extra_costs_to_db_dict(all_extra_costs_batch, self.job_id)
            bulk_insert(TableExtraCost, extra_cost_dict)
            logger.info('Saving electricity price to db...')
            bulk_insert(TableElectricityPrice, electricity_price_list_batch)

        battery_level_dicts = levels_to_db_dict(battery_levels_dict, TradeMetadataKey.BATTERY_LEVEL.name, self.job_id)
        shallow_storage_rel_dicts = levels_to_db_dict(shallow_storage_rel_dict,
                                                      TradeMetadataKey.SHALLOW_STORAGE_REL.name, self.job_id)
        shallow_storage_abs_dicts = levels_to_db_dict(shallow_storage_abs_dict,
                                                      TradeMetadataKey.SHALLOW_STORAGE_ABS.name, self.job_id)
        deep_storage_rel_dicts = levels_to_db_dict(deep_storage_rel_dict, TradeMetadataKey.DEEP_STORAGE_REL.name,
                                                   self.job_id)
        deep_storage_abs_dicts = levels_to_db_dict(deep_storage_abs_dict, TradeMetadataKey.DEEP_STORAGE_ABS.name,
                                                   self.job_id)
        shallow_loss_dicts = levels_to_db_dict(shallow_loss_dict, TradeMetadataKey.SHALLOW_LOSS.name, self.job_id)
        deep_loss_dicts = levels_to_db_dict(deep_loss_dict, TradeMetadataKey.DEEP_LOSS.name, self.job_id)
        shallow_charge_dicts = levels_to_db_dict(shallow_charge_dict, TradeMetadataKey.SHALLOW_CHARGE.name, self.job_id)
        shallow_discharge_dicts = levels_to_db_dict(shallow_discharge_dict, TradeMetadataKey.SHALLOW_DISCHARGE.name,
                                                    self.job_id)
        bites_flow_dicts = levels_to_db_dict(bites_flow_dict, TradeMetadataKey.FLOW_SHALLOW_TO_DEEP.name, self.job_id)
        hp_high_prod_dicts = levels_to_db_dict(hp_high_prod, TradeMetadataKey.HP_HIGH_HEAT_PROD.name, self.job_id)
        hp_low_prod_dicts = levels_to_db_dict(hp_low_prod, TradeMetadataKey.HP_LOW_HEAT_PROD.name, self.job_id)
        bulk_insert(TableLevel, battery_level_dicts)
        bulk_insert(TableLevel, shallow_storage_rel_dicts)
        bulk_insert(TableLevel, deep_storage_rel_dicts)
        bulk_insert(TableLevel, shallow_storage_abs_dicts)
        bulk_insert(TableLevel, deep_storage_abs_dicts)
        bulk_insert(TableLevel, shallow_loss_dicts)
        bulk_insert(TableLevel, deep_loss_dicts)
        bulk_insert(TableLevel, shallow_charge_dicts)
        bulk_insert(TableLevel, shallow_discharge_dicts)
        bulk_insert(TableLevel, bites_flow_dicts)
        bulk_insert(TableLevel, hp_high_prod_dicts)
        bulk_insert(TableLevel, hp_low_prod_dicts)

        calculate_results_and_save(self.job_id, self.agents, self.grid_agents,
                                   hp_high_heat_prod=sum(sum(subdict.values()) for subdict in hp_high_prod.values()),
                                   hp_low_heat_prod=sum(sum(subdict.values()) for subdict in hp_low_prod.values()))

        logger.info("Finished simulating trades, beginning calculations on district heating price...")

    def extract_heating_price(self):
        """
        Simulations finished. Now, we need to go through and calculate the exact district heating price for each month
        """
        logger.info('Calculating external_heating_prices')
        heating_price_list = get_external_heating_prices(self.heat_pricing, self.job_id,
                                                         self.trading_periods)
        bulk_insert(TableHeatingPrice, heating_price_list)

        logger.info('Calculating heat_cost_discrepancy_corrections')
        heating_prices = pd.DataFrame.from_records(heating_price_list)
        heat_cost_discrepancy_corrections = correct_for_exact_heating_price(self.trading_periods,
                                                                            heating_prices,
                                                                            self.job_id)
        logger.info('Saving extra costs to db...')
        extra_cost_dict = extra_costs_to_db_dict(heat_cost_discrepancy_corrections, self.job_id)
        bulk_insert(TableExtraCost, extra_cost_dict)

        logger.info('Simulation finished!')
