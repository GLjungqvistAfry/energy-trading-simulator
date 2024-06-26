import datetime
import logging
import math
import threading
from typing import Any, Dict, List, Tuple

import pandas as pd

from pyomo.opt import OptSolver

from tradingplatformpoc.agent.block_agent import BlockAgent
from tradingplatformpoc.agent.grid_agent import GridAgent
from tradingplatformpoc.agent.iagent import IAgent
from tradingplatformpoc.app.app_threading import StoppableThread
from tradingplatformpoc.constants import LEC_CAN_SELL_HEAT_TO_EXTERNAL
from tradingplatformpoc.database import bulk_insert
from tradingplatformpoc.digitaltwin.battery import Battery
from tradingplatformpoc.digitaltwin.static_digital_twin import StaticDigitalTwin
from tradingplatformpoc.generate_data.generate_mock_data import get_generated_mock_data
from tradingplatformpoc.generate_data.generation_functions.non_residential.heat_generation import \
    calculate_heat_production
from tradingplatformpoc.generate_data.mock_data_utils import get_cooling_cons_key, get_elec_cons_key, \
    get_hot_tap_water_cons_key, get_space_heat_cons_key
from tradingplatformpoc.market.balance_manager import correct_for_exact_price
from tradingplatformpoc.market.extra_cost import ExtraCostType
from tradingplatformpoc.market.trade import Resource, Trade, TradeMetadataKey
from tradingplatformpoc.price.electricity_price import ElectricityPrice
from tradingplatformpoc.price.heating_price import HeatingPrice
from tradingplatformpoc.settings import settings
from tradingplatformpoc.simulation_runner.chalmers_interface import InfeasibilityError, optimize
from tradingplatformpoc.simulation_runner.results_calculator import calculate_results_and_save
from tradingplatformpoc.sql.config.crud import get_all_agent_name_id_pairs_in_config, read_config
from tradingplatformpoc.sql.electricity_price.models import ElectricityPrice as TableElectricityPrice
from tradingplatformpoc.sql.extra_cost.crud import extra_costs_to_db_dict
from tradingplatformpoc.sql.extra_cost.models import ExtraCost as TableExtraCost
from tradingplatformpoc.sql.heating_price.models import HeatingPrice as TableHeatingPrice
from tradingplatformpoc.sql.input_data.crud import get_periods_from_db, read_inputs_df_for_agent_creation
from tradingplatformpoc.sql.input_electricity_price.crud import get_nordpool_data
from tradingplatformpoc.sql.job.crud import delete_job, get_config_id_for_job_id, set_error_info, update_job_with_time
from tradingplatformpoc.sql.level.crud import tmk_levels_dict_to_db_dict, tmk_overall_levels_dict_to_db_dict
from tradingplatformpoc.sql.level.models import Level as TableLevel
from tradingplatformpoc.sql.trade.crud import trades_to_db_dict
from tradingplatformpoc.sql.trade.models import Trade as TableTrade
from tradingplatformpoc.trading_platform_utils import add_all_to_nested_dict, add_all_to_twice_nested_dict, \
    calculate_solar_prod, get_external_prices, get_final_storage_level, get_glpk_solver

logger = logging.getLogger(__name__)


class TradingSimulator:
    def __init__(self, job_id: str):
        self.solver: OptSolver = get_glpk_solver()
        self.job_id: str = job_id
        self.config_id: str = get_config_id_for_job_id(self.job_id)
        self.config_data: Dict[str, Any] = read_config(self.config_id)
        self.agent_name_id_pairs: Dict[str, str] = get_all_agent_name_id_pairs_in_config(self.config_id)

    def __call__(self):
        if (self.job_id is not None) and (self.config_data is not None):
            try:
                update_job_with_time(self.job_id, 'start_time')
                self.initialize_data()
                self.agents, self.grid_agents = self.initialize_agents()
                self.block_agents: List[BlockAgent] = [agent for agent in self.agents if isinstance(agent, BlockAgent)]
                self.run()
                update_job_with_time(self.job_id, 'end_time')

            except InfeasibilityError as e:
                logger.error(e.message)
                set_error_info(self.job_id, e)
                delete_job(self.job_id, only_delete_associated_data=True)

            except Exception as other_error:
                logger.exception(other_error)
                delete_job(self.job_id)

    def initialize_data(self):
        self.trading_periods = get_periods_from_db().sort_values()

        self.local_market_enabled = self.config_data['AreaInfo']['LocalMarketEnabled']

        self.heat_pricing: HeatingPrice = HeatingPrice(
            heating_wholesale_price_fraction=self.config_data['AreaInfo']['ExternalHeatingWholesalePriceFraction'],
            effect_fee=self.config_data['AreaInfo']["HeatingEffectFee"])
        corresponding_nordpool_data = get_nordpool_data(self.config_data['AreaInfo']['ElectricityPriceYear'],
                                                        self.trading_periods)
        self.electricity_pricing: ElectricityPrice = ElectricityPrice(
            elec_wholesale_offset=self.config_data['AreaInfo']['ExternalElectricityWholesalePriceOffset'],
            elec_tax=self.config_data['AreaInfo']["ElectricityTax"],
            elec_transmission_fee=self.config_data['AreaInfo']["ElectricityTransmissionFee"],
            elec_effect_fee=self.config_data['AreaInfo']["ElectricityEffectFee"],
            elec_tax_internal=self.config_data['AreaInfo']["ElectricityTaxInternal"],
            elec_transmission_fee_internal=self.config_data['AreaInfo']["ElectricityTransmissionFeeInternal"],
            elec_effect_fee_internal=self.config_data['AreaInfo']["ElectricityEffectFeeInternal"],
            nordpool_data=corresponding_nordpool_data)
        if settings.NOT_FULL_YEAR:
            # To be used for testing only - ensure NOT_FULL_YEAR is not set (or set to False) in production environments
            self.trading_periods = self.trading_periods.take(list(range(24))  # 02-01
                                                             + list(range(3912, 3936))  # 07-14
                                                             + list(range(5664, 5688))  # 09-25
                                                             + list(range(5952, 5976))  # 10-07
                                                             + list(range(6120, 6144))  # 10-14
                                                             )
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
                agent_id = self.agent_name_id_pairs[agent['Name']]
                elec_cons_series = blocks_mock_data.get(get_elec_cons_key(agent_id))
                space_heat_cons_series = blocks_mock_data.get(get_space_heat_cons_key(agent_id))
                hot_tap_water_cons_series = blocks_mock_data.get(get_hot_tap_water_cons_key(agent_id))
                cool_cons_series = blocks_mock_data.get(get_cooling_cons_key(agent_id))
                pv_prod_series = calculate_solar_prod(inputs_df['irradiation'],
                                                      agent['PVArea'],
                                                      area_info['PVEfficiency'])

                block_digital_twin = StaticDigitalTwin(atemp=agent['Atemp'],
                                                       electricity_usage=elec_cons_series,
                                                       space_heating_usage=space_heat_cons_series,
                                                       hot_water_usage=hot_tap_water_cons_series,
                                                       cooling_usage=cool_cons_series,
                                                       electricity_production=pv_prod_series,
                                                       hp_produce_cooling=agent['HeatPumpForCooling'])

                storage_digital_twin = Battery(max_capacity_kwh=agent["BatteryCapacity"],
                                               max_charge_rate_fraction=area_info["BatteryChargeRate"],
                                               max_discharge_rate_fraction=area_info["BatteryDischargeRate"],
                                               discharging_efficiency=area_info["BatteryEfficiency"])

                agents.append(
                    BlockAgent(digital_twin=block_digital_twin, heat_pump_max_input=agent["HeatPumpMaxInput"],
                               heat_pump_max_output=agent["HeatPumpMaxOutput"],
                               booster_pump_max_input=agent["BoosterPumpMaxInput"],
                               booster_pump_max_output=agent["BoosterPumpMaxOutput"],
                               acc_tank_capacity=agent["AccumulatorTankCapacity"],
                               frac_for_bites=agent["FractionUsedForBITES"], battery=storage_digital_twin,
                               guid=agent_name))

            elif agent_type == "GroceryStoreAgent":
                # This is not used at the moment! Built to emulate the Coop store across the road from the Jonstaka
                # site, which would fully participate in the LEC. The "HeatProducerAgent" grocery store profile on the
                # other hand, only sells heating to the LEC, it doesn't participate in the LEC in any other sense.
                pv_prod_series = calculate_solar_prod(inputs_df['irradiation'],
                                                      agent['PVArea'],
                                                      agent['PVEfficiency'])
                space_heat_prod = inputs_df['coop_space_heating_produced'] if agent['SellExcessHeat'] else None
                # Scaling here to fit BDAB's estimate (docs/Heat production.md)
                space_heat_prod = space_heat_prod / 4.0
                grocery_store_digital_twin = StaticDigitalTwin(atemp=agent['Atemp'],
                                                               electricity_usage=inputs_df['coop_electricity_consumed'],
                                                               space_heating_usage=inputs_df[
                                                                   'coop_space_heating_consumed'],
                                                               hot_water_usage=inputs_df['coop_hot_tap_water_consumed'],
                                                               electricity_production=pv_prod_series,
                                                               space_heating_production=space_heat_prod,
                                                               # Cooling is handled "internally", so this is False:
                                                               hp_produce_cooling=False)
                agents.append(
                    BlockAgent(digital_twin=grocery_store_digital_twin, heat_pump_max_input=agent["HeatPumpMaxInput"],
                               heat_pump_max_output=agent["HeatPumpMaxOutput"],
                               booster_pump_max_input=agent["BoosterPumpMaxInput"],
                               booster_pump_max_output=agent["BoosterPumpMaxOutput"],
                               acc_tank_capacity=agent["AccumulatorTankCapacity"],
                               frac_for_bites=agent["FractionUsedForBITES"], guid=agent_name))

            elif agent_type == "HeatProducerAgent":
                low_heat_prod, high_heat_prod = calculate_heat_production(agent, inputs_df)
                heat_prod_digital_twin = StaticDigitalTwin(atemp=0,
                                                           space_heating_production=low_heat_prod,
                                                           hot_water_production=high_heat_prod)
                agents.append(BlockAgent(digital_twin=heat_prod_digital_twin, guid=agent_name))
            elif agent_type == "GridAgent":
                resource = Resource[agent["Resource"]]
                if resource == Resource.ELECTRICITY:
                    grid_agent = GridAgent(resource, can_buy=True,
                                           max_transfer_per_hour=agent["TransferRate"], guid=agent_name)
                elif resource == Resource.HIGH_TEMP_HEAT:
                    grid_agent = GridAgent(resource, can_buy=LEC_CAN_SELL_HEAT_TO_EXTERNAL,
                                           max_transfer_per_hour=agent["TransferRate"], guid=agent_name)
                agents.append(grid_agent)
                grid_agents[resource] = grid_agent

        # Verify that we have a Grid Agent
        if not any(isinstance(agent, GridAgent) for agent in agents):
            raise RuntimeError("No grid agent initialized")

        return agents, grid_agents

    def run(self, number_of_batches: int = 5):
        """
        The core loop of the simulation, running through the desired time period and performing trades.
        """

        logger.info("Starting trading simulations")

        shallow_storage_end: Dict[str, float] = {}
        deep_storage_end: Dict[str, float] = {}

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
            metadata_per_agent_and_period: Dict[TradeMetadataKey, Dict[str, Dict[datetime.datetime, float]]] = {}
            metadata_per_period: Dict[TradeMetadataKey, Dict[datetime.datetime, float]] = {}

            # ------- NEW --------
            for horizon_start in thsps_in_this_batch:
                logger.info("Simulating {:%Y-%m-%d}".format(horizon_start))
                chalmers_outputs = optimize(self.solver, self.block_agents, self.grid_agents,
                                            self.config_data['AreaInfo'], horizon_start,
                                            self.electricity_pricing, self.heat_pricing,
                                            shallow_storage_end, deep_storage_end)
                all_trades_list_batch.append(chalmers_outputs.trades)
                shallow_storage_end = get_final_storage_level(
                    self.trading_horizon,
                    chalmers_outputs.metadata_per_agent_and_period[TradeMetadataKey.SHALLOW_STORAGE_ABS],
                    horizon_start)
                deep_storage_end = get_final_storage_level(
                    self.trading_horizon,
                    chalmers_outputs.metadata_per_agent_and_period[TradeMetadataKey.DEEP_STORAGE_ABS],
                    horizon_start)
                add_all_to_twice_nested_dict(metadata_per_agent_and_period,
                                             chalmers_outputs.metadata_per_agent_and_period)
                add_all_to_nested_dict(metadata_per_period, chalmers_outputs.metadata_per_period)

            logger.info('Saving trades to db...')
            trade_dict = trades_to_db_dict(all_trades_list_batch, self.job_id)
            bulk_insert(TableTrade, trade_dict)

            logger.info('Saving metadata to db...')
            metadata_per_agent_and_period_dicts = tmk_levels_dict_to_db_dict(metadata_per_agent_and_period, self.job_id)
            metadata_per_period_dicts = tmk_overall_levels_dict_to_db_dict(metadata_per_period, self.job_id)
            bulk_insert(TableLevel, metadata_per_agent_and_period_dicts)
            bulk_insert(TableLevel, metadata_per_period_dicts)

        logger.info("Finished simulating trades, beginning calculations on district heating price...")

        self.extract_resource_prices()

        calculate_results_and_save(self.job_id, self.agents, self.grid_agents)

        logger.info('Simulation finished!')

    def extract_resource_prices(self):
        """
        Simulations finished. Now, we need to go through and calculate the exact resource prices for each month
        """
        agent_guids: List[str] = [a.guid for a in self.block_agents]

        # First for heating
        logger.info('Calculating heating_price_list')
        heating_price_list = get_external_prices(self.heat_pricing,
                                                 self.job_id,
                                                 self.trading_periods,
                                                 agent_guids,
                                                 self.local_market_enabled)
        bulk_insert(TableHeatingPrice, heating_price_list)
        heating_prices = pd.DataFrame.from_records(heating_price_list)

        logger.info('Calculating heat_cost_discrepancy_corrections')
        heat_cost_discrepancy_corrections = correct_for_exact_price(self.trading_periods,
                                                                    heating_prices,
                                                                    Resource.HIGH_TEMP_HEAT,
                                                                    ExtraCostType.HEAT_EXT_COST_CORR,
                                                                    self.job_id,
                                                                    self.local_market_enabled,
                                                                    agent_guids)

        # Then for electricity
        logger.info('Calculating elec_price_list')
        elec_price_list = get_external_prices(self.electricity_pricing,
                                              self.job_id,
                                              self.trading_periods,
                                              agent_guids,
                                              self.local_market_enabled)
        bulk_insert(TableElectricityPrice, elec_price_list)
        elec_prices = pd.DataFrame.from_records(elec_price_list)

        logger.info('Calculating elec_cost_discrepancy_corrections')
        elec_cost_discrepancy_corrections = correct_for_exact_price(self.trading_periods,
                                                                    elec_prices,
                                                                    Resource.ELECTRICITY,
                                                                    ExtraCostType.ELEC_EXT_COST_CORR,
                                                                    self.job_id,
                                                                    self.local_market_enabled,
                                                                    agent_guids)

        logger.info('Saving extra costs to database...')
        heat_extra_cost_dicts = extra_costs_to_db_dict(heat_cost_discrepancy_corrections, self.job_id)
        elec_extra_cost_dicts = extra_costs_to_db_dict(elec_cost_discrepancy_corrections, self.job_id)
        bulk_insert(TableExtraCost, heat_extra_cost_dicts + elec_extra_cost_dicts)

        logger.info('Extra costs saved to database')
