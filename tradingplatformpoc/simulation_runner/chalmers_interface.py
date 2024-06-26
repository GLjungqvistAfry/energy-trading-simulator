import datetime
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

import pandas as pd

import pyomo.environ as pyo
from pyomo.core.base.param import IndexedParam, ScalarParam
from pyomo.core.base.var import IndexedVar
from pyomo.opt import OptSolver, SolverResults, TerminationCondition
from pyomo.util.infeasible import find_infeasible_constraints, log_infeasible_constraints

from tradingplatformpoc import constants
from tradingplatformpoc.agent.block_agent import BlockAgent
from tradingplatformpoc.agent.grid_agent import GridAgent
from tradingplatformpoc.market.trade import Action, Market, Resource, Trade, TradeMetadataKey
from tradingplatformpoc.price.electricity_price import ElectricityPrice
from tradingplatformpoc.price.heating_price import HeatingPrice
from tradingplatformpoc.price.iprice import IPrice
from tradingplatformpoc.simulation_runner.chalmers import AgentEMS, CEMS_function
from tradingplatformpoc.simulation_runner.chalmers.domain import CEMSError
from tradingplatformpoc.trading_platform_utils import add_to_nested_dict, should_use_summer_mode

VERY_SMALL_NUMBER = 0.000001  # to avoid trades with quantity 1e-7, for example
DECIMALS_TO_ROUND_TO = 6  # To avoid saving for example storage levels of -1e-8

logger = logging.getLogger(__name__)


"""
Here we keep methods that do either
 1. Construct inputs to Chalmers' solve_model function, from agent data
 2. Translate the optimized pyo.ConcreteModel back to our domain (Trades, metadata etc)
"""


class ChalmersOutputs:
    trades: List[Trade]
    # (TradeMetadataKey, agent_guid, (period, level)))
    metadata_per_agent_and_period: Dict[TradeMetadataKey, Dict[str, Dict[datetime.datetime, float]]]
    # Data which isn't agent-individual: (TradeMetadataKey, (period, level))
    metadata_per_period: Dict[TradeMetadataKey, Dict[datetime.datetime, float]]

    def __init__(self, trades: List[Trade],
                 metadata_per_agent_and_period: Dict[TradeMetadataKey, Dict[str, Dict[datetime.datetime, float]]],
                 metadata_per_period: Dict[TradeMetadataKey, Dict[datetime.datetime, float]]):
        self.trades = trades
        self.metadata_per_agent_and_period = metadata_per_agent_and_period
        self.metadata_per_period = metadata_per_period


class InfeasibilityError(CEMSError):
    agent_names: List[str]
    horizon_start: datetime.datetime
    horizon_end: datetime.datetime
    constraints: Set[str]

    def __init__(self, message: str, agent_names: List[str], hour_indices: List[int],
                 horizon_start: datetime.datetime, horizon_end: datetime.datetime, constraints: Set[str]):
        super().__init__(message, [], hour_indices)
        self.agent_names = agent_names
        self.horizon_start = horizon_start
        self.horizon_end = horizon_end
        self.constraints = constraints


def optimize(solver: OptSolver, block_agents: List[BlockAgent], grid_agents: Dict[Resource, GridAgent],
             area_info: Dict[str, Any], start_datetime: datetime.datetime,
             elec_pricing: ElectricityPrice, heat_pricing: HeatingPrice,
             shallow_storage_start_dict: Dict[str, float], deep_storage_start_dict: Dict[str, float]) \
        -> ChalmersOutputs:
    elec_grid_agent_guid = grid_agents[Resource.ELECTRICITY].guid
    heat_grid_agent_guid = grid_agents[Resource.HIGH_TEMP_HEAT].guid
    agent_guids = [agent.guid for agent in block_agents]
    # The order specified in "agents" will be used throughout
    trading_horizon = area_info['TradingHorizon']

    elec_demand_df, elec_supply_df, high_heat_demand_df, high_heat_supply_df, \
        low_heat_demand_df, low_heat_supply_df, cooling_demand_df, cooling_supply_df = \
        build_supply_and_demand_dfs(block_agents, start_datetime, trading_horizon)

    battery_capacities = [agent.battery.max_capacity_kwh for agent in block_agents]
    battery_max_charge = [agent.battery.charge_limit_kwh for agent in block_agents]
    battery_max_discharge = [agent.battery.discharge_limit_kwh for agent in block_agents]
    acc_tank_volumes = [agent.acc_tank_volume for agent in block_agents]
    heatpump_max_power = [agent.heat_pump_max_input for agent in block_agents]
    heatpump_max_heat = [agent.heat_pump_max_output for agent in block_agents]
    booster_max_power = [agent.booster_pump_max_input for agent in block_agents]
    booster_max_heat = [agent.booster_pump_max_output for agent in block_agents]
    atemp_for_bites = [agent.digital_twin.atemp * agent.frac_for_bites for agent in block_agents]
    hp_produce_cooling = [agent.digital_twin.hp_produce_cooling for agent in block_agents]
    shallow_storage_start = [(shallow_storage_start_dict[agent] if agent in shallow_storage_start_dict.keys() else 0.0)
                             for agent in agent_guids]
    deep_storage_start = [(deep_storage_start_dict[agent] if agent in shallow_storage_start_dict.keys() else 0.0)
                          for agent in agent_guids]

    nordpool_prices: pd.Series = elec_pricing.get_nordpool_price_for_periods(start_datetime, trading_horizon)
    nordpool_prices = nordpool_prices.reset_index(drop=True)
    heat_retail_price = heat_pricing.get_retail_price_excl_effect_fee(start_datetime)

    n_agents = len(block_agents)
    summer_mode = should_use_summer_mode(start_datetime)
    heat_pump_cop = area_info['COPHeatPumpsLowTemp'] if summer_mode else area_info['COPHeatPumpsHighTemp']
    try:
        if area_info['LocalMarketEnabled']:
            optimized_model, results = CEMS_function.solve_model(
                solver=solver,
                summer_mode=summer_mode,
                month=start_datetime.month,
                n_agents=n_agents,
                nordpool_price=nordpool_prices,
                external_heat_buy_price=heat_retail_price,
                battery_capacity=battery_capacities,
                battery_charge_rate=battery_max_charge,
                battery_discharge_rate=battery_max_discharge,
                SOCBES0=[area_info['StorageEndChargeLevel']] * n_agents,
                heatpump_COP=[heat_pump_cop] * n_agents,
                heatpump_max_power=heatpump_max_power,
                heatpump_max_heat=heatpump_max_heat,
                HP_Cproduct_active=hp_produce_cooling,
                borehole=hp_produce_cooling,
                booster_heatpump_COP=[area_info['COPBoosterPumps']] * n_agents,
                booster_heatpump_max_power=booster_max_power,
                booster_heatpump_max_heat=booster_max_heat,
                build_area=atemp_for_bites,
                SOCTES0=[area_info['StorageEndChargeLevel']] * n_agents,
                thermalstorage_max_temp=[constants.ACC_TANK_TEMPERATURE] * n_agents,
                thermalstorage_volume=acc_tank_volumes,
                BITES_Eshallow0=shallow_storage_start,
                BITES_Edeep0=deep_storage_start,
                elec_consumption=elec_demand_df,
                hot_water_heatdem=high_heat_demand_df,
                space_heating_heatdem=low_heat_demand_df,
                cold_consumption=cooling_demand_df,
                pv_production=elec_supply_df,
                excess_low_temp_heat=low_heat_supply_df,
                excess_high_temp_heat=high_heat_supply_df,
                battery_efficiency=area_info['BatteryEfficiency'],
                thermalstorage_efficiency=area_info['AccTankEfficiency'],
                max_elec_transfer_between_agents=area_info['InterAgentElectricityTransferCapacity'],
                max_elec_transfer_to_external=grid_agents[Resource.ELECTRICITY].max_transfer_per_hour,
                max_heat_transfer_between_agents=area_info['InterAgentHeatTransferCapacity'],
                max_heat_transfer_to_external=grid_agents[Resource.HIGH_TEMP_HEAT].max_transfer_per_hour,
                chiller_COP=area_info['CompChillerCOP'],
                chiller_heat_recovery=area_info['CompChillerHeatRecovery'],
                Pccmax=area_info['CompChillerMaxInput'],
                cold_trans_loss=area_info['CoolingTransferLoss'],
                heat_trans_loss=area_info['HeatTransferLoss'],
                trading_horizon=trading_horizon,
                elec_tax_fee=elec_pricing.tax,
                elec_trans_fee=elec_pricing.transmission_fee,
                elec_peak_load_fee=elec_pricing.get_effect_fee_per_day(start_datetime),
                heat_peak_load_fee=heat_pricing.get_effect_fee_per_day(start_datetime),
                incentive_fee=elec_pricing.wholesale_offset,
                hist_top_three_elec_peak_load=elec_pricing.get_top_three_hourly_outtakes_for_month(start_datetime),
                hist_monthly_heat_peak_energy=heat_pricing.get_avg_peak_for_month(start_datetime)
            )
            handle_infeasibility(optimized_model, results, start_datetime, trading_horizon, [])
            return extract_outputs_for_lec(optimized_model, start_datetime,
                                           elec_grid_agent_guid, heat_grid_agent_guid,
                                           elec_pricing, heat_pricing,
                                           agent_guids)
        else:
            all_trades: List[Trade] = []
            all_metadata: Dict[str, Dict[TradeMetadataKey, Dict[datetime.datetime, float]]] = {}
            for i_agent in range(len(block_agents)):
                agent_id = agent_guids[i_agent]
                optimized_model, results = AgentEMS.solve_model(
                    solver=solver,
                    month=start_datetime.month,
                    agent=i_agent,
                    nordpool_price=nordpool_prices,
                    external_heat_buy_price=heat_retail_price,
                    battery_capacity=battery_capacities[i_agent],
                    battery_charge_rate=battery_max_charge[i_agent],
                    battery_discharge_rate=battery_max_discharge[i_agent],
                    SOCBES0=area_info['StorageEndChargeLevel'],
                    heatpump_COP=heat_pump_cop,
                    heatpump_max_power=heatpump_max_power[i_agent],
                    heatpump_max_heat=heatpump_max_heat[i_agent],
                    HP_Cproduct_active=hp_produce_cooling[i_agent],
                    borehole=hp_produce_cooling[i_agent],
                    build_area=atemp_for_bites[i_agent],
                    SOCTES0=area_info['StorageEndChargeLevel'],
                    thermalstorage_max_temp=constants.ACC_TANK_TEMPERATURE,
                    thermalstorage_volume=acc_tank_volumes[i_agent],
                    BITES_Eshallow0=shallow_storage_start[i_agent],
                    BITES_Edeep0=deep_storage_start[i_agent],
                    elec_consumption=elec_demand_df.iloc[i_agent, :],
                    hot_water_heatdem=high_heat_demand_df.iloc[i_agent, :],
                    space_heating_heatdem=low_heat_demand_df.iloc[i_agent, :],
                    cold_consumption=cooling_demand_df.iloc[i_agent, :],
                    pv_production=elec_supply_df.iloc[i_agent, :],
                    excess_high_temp_heat=high_heat_supply_df.iloc[i_agent, :],
                    battery_efficiency=area_info['BatteryEfficiency'],
                    thermalstorage_efficiency=area_info['AccTankEfficiency'],
                    max_elec_transfer_to_external=grid_agents[Resource.ELECTRICITY].max_transfer_per_hour,
                    max_heat_transfer_to_external=grid_agents[Resource.HIGH_TEMP_HEAT].max_transfer_per_hour,
                    heat_trans_loss=area_info['HeatTransferLoss'],
                    trading_horizon=trading_horizon,
                    elec_tax_fee=elec_pricing.tax,
                    elec_trans_fee=elec_pricing.transmission_fee,
                    elec_peak_load_fee=elec_pricing.get_effect_fee_per_day(start_datetime),
                    heat_peak_load_fee=heat_pricing.get_effect_fee_per_day(start_datetime),
                    incentive_fee=elec_pricing.wholesale_offset,
                    hist_top_three_elec_peak_load=elec_pricing.get_top_three_hourly_outtakes_for_month(
                        start_datetime, agent_id),
                    hist_monthly_heat_peak_energy=heat_pricing.get_avg_peak_for_month(start_datetime, agent_id)
                )
                handle_infeasibility(optimized_model, results, start_datetime, trading_horizon,
                                     [block_agents[i_agent].guid])
                trades, metadata = extract_outputs_for_agent(optimized_model, start_datetime,
                                                             elec_grid_agent_guid, heat_grid_agent_guid,
                                                             elec_pricing, heat_pricing,
                                                             agent_id)
                all_trades.extend(trades)
                all_metadata[agent_id] = metadata

        metadata_per_agent_and_period = flip_dict_keys(all_metadata)
        metadata_per_period: Dict[TradeMetadataKey, Dict[datetime.datetime, float]] = {
            TradeMetadataKey.HEAT_DUMP: sum_for_all_agents(metadata_per_agent_and_period[TradeMetadataKey.HEAT_DUMP]),
            TradeMetadataKey.COOL_DUMP: sum_for_all_agents(metadata_per_agent_and_period[TradeMetadataKey.COOL_DUMP])
        }
        return ChalmersOutputs(all_trades, metadata_per_agent_and_period, metadata_per_period)
    except CEMSError as e:
        raise InfeasibilityError(message=e.message,
                                 agent_names=e.agent_names if isinstance(e, InfeasibilityError) else
                                 [agent_guids[i] for i in e.agent_indices],
                                 hour_indices=e.hour_indices,
                                 horizon_start=start_datetime,
                                 horizon_end=start_datetime + datetime.timedelta(hours=trading_horizon),
                                 constraints=set())


def sum_for_all_agents(dict_per_agent_and_period: Dict[str, Dict[datetime.datetime, float]]) \
        -> Dict[datetime.datetime, float]:
    return {date: sum(inner_dict[date] for inner_dict in dict_per_agent_and_period.values() if date in inner_dict)
            for date in set(date for inner_dict in dict_per_agent_and_period.values() for date in inner_dict)}


def flip_dict_keys(all_metadata: Dict[str, Dict[TradeMetadataKey, Dict[datetime.datetime, float]]]) \
        -> Dict[TradeMetadataKey, Dict[str, Dict[datetime.datetime, float]]]:
    metadata_per_agent: Dict[TradeMetadataKey, Dict[str, Dict[datetime.datetime, float]]] = {}
    for agent_name, inner_dict in all_metadata.items():
        for metadata_key, date_value_dict in inner_dict.items():
            if metadata_key not in metadata_per_agent:
                metadata_per_agent[metadata_key] = {}
            metadata_per_agent[metadata_key][agent_name] = date_value_dict
    return metadata_per_agent


def handle_infeasibility(optimized_model: pyo.ConcreteModel, results: SolverResults, start_datetime: datetime.datetime,
                         trading_horizon: int, agent_names: List[str]):
    """If the solver exits with infeasibility, log this, and raise an informative error."""
    if results.solver.termination_condition != TerminationCondition.optimal:
        constraint_names_no_index: Set[str] = set()
        for constraint, _body_value, _infeasible in find_infeasible_constraints(optimized_model):
            constraint_names_no_index.add(constraint.name.split('[')[0])
        log_infeasible_constraints(optimized_model)
        raise InfeasibilityError(message='Infeasible optimization problem',
                                 agent_names=agent_names,
                                 hour_indices=[],
                                 horizon_start=start_datetime,
                                 horizon_end=start_datetime + datetime.timedelta(hours=trading_horizon),
                                 constraints=constraint_names_no_index)


def extract_outputs_for_agent(optimized_model: pyo.ConcreteModel,
                              start_datetime: datetime.datetime,
                              elec_grid_agent_guid: str,
                              heat_grid_agent_guid: str,
                              electricity_price_data: ElectricityPrice,
                              heating_price_data: HeatingPrice,
                              agent_guid: str) -> \
        Tuple[List[Trade], Dict[TradeMetadataKey, Dict[datetime.datetime, float]]]:
    elec_trades = get_power_transfers(optimized_model, start_datetime, elec_grid_agent_guid, [agent_guid],
                                      electricity_price_data, local_market_enabled=False)
    heat_trades = get_heat_transfers(optimized_model, start_datetime, heat_grid_agent_guid, [agent_guid],
                                     heating_price_data, local_market_enabled=False)
    metadata = {
        TradeMetadataKey.BATTERY_LEVEL: get_value_per_period(optimized_model, start_datetime, 'SOCBES'),
        TradeMetadataKey.ACC_TANK_LEVEL: get_value_per_period(optimized_model, start_datetime, 'SOCTES'),
        TradeMetadataKey.SHALLOW_STORAGE_REL: get_value_per_period(optimized_model, start_datetime, 'Energy_shallow'),
        TradeMetadataKey.DEEP_STORAGE_REL: get_value_per_period(optimized_model, start_datetime, 'Energy_deep'),
        TradeMetadataKey.SHALLOW_STORAGE_ABS: get_value_per_period(optimized_model, start_datetime, 'Energy_shallow'),
        TradeMetadataKey.DEEP_STORAGE_ABS: get_value_per_period(optimized_model, start_datetime, 'Energy_deep'),
        TradeMetadataKey.SHALLOW_LOSS: get_value_per_period(optimized_model, start_datetime, 'Loss_shallow'),
        TradeMetadataKey.DEEP_LOSS: get_value_per_period(optimized_model, start_datetime, 'Loss_deep'),
        TradeMetadataKey.SHALLOW_CHARGE: get_value_per_period(optimized_model, start_datetime, 'Hcha_shallow'),
        TradeMetadataKey.FLOW_SHALLOW_TO_DEEP: get_value_per_period(optimized_model, start_datetime, 'Flow'),
        TradeMetadataKey.HP_COOL_PROD: get_value_per_period(optimized_model, start_datetime, 'Chp'),
        TradeMetadataKey.HP_HIGH_HEAT_PROD: get_value_per_period(optimized_model, start_datetime, 'Hhp'),
        # Heat dump and cool dump will have to be aggregated later
        TradeMetadataKey.HEAT_DUMP: get_value_per_period(optimized_model, start_datetime, 'heat_dump'),
        TradeMetadataKey.COOL_DUMP: get_value_per_period(optimized_model, start_datetime, 'cool_dump')
    }
    return elec_trades + heat_trades, metadata


def extract_outputs_for_lec(optimized_model: pyo.ConcreteModel,
                            start_datetime: datetime.datetime,
                            elec_grid_agent_guid: str,
                            heat_grid_agent_guid: str,
                            electricity_price_data: ElectricityPrice,
                            heating_price_data: HeatingPrice,
                            agent_guids: List[str]) -> ChalmersOutputs:
    elec_trades = get_power_transfers(optimized_model, start_datetime, elec_grid_agent_guid, agent_guids,
                                      electricity_price_data, local_market_enabled=True)
    heat_trades = get_heat_transfers(optimized_model, start_datetime, heat_grid_agent_guid, agent_guids,
                                     heating_price_data, local_market_enabled=True)
    cool_trades = get_cool_transfers(optimized_model, start_datetime, agent_guids)
    metadata_per_agent_and_period = {
        TradeMetadataKey.BATTERY_LEVEL: get_value_per_agent(optimized_model, start_datetime, 'SOCBES', agent_guids,
                                                            lambda i: optimized_model.Emax_BES[i] > 0),
        TradeMetadataKey.ACC_TANK_LEVEL: get_value_per_agent(optimized_model, start_datetime, 'SOCTES', agent_guids,
                                                             lambda i: optimized_model.kwh_per_deg[i] > 0),
        TradeMetadataKey.SHALLOW_STORAGE_REL: get_value_per_agent(optimized_model, start_datetime, 'Energy_shallow',
                                                                  agent_guids,
                                                                  lambda i: optimized_model.Energy_shallow_cap[i] > 0,
                                                                  lambda i: optimized_model.Energy_shallow_cap[i]),
        TradeMetadataKey.DEEP_STORAGE_REL: get_value_per_agent(optimized_model, start_datetime, 'Energy_deep',
                                                               agent_guids,
                                                               lambda i: optimized_model.Energy_deep_cap[i] > 0,
                                                               lambda i: optimized_model.Energy_deep_cap[i]),
        TradeMetadataKey.SHALLOW_STORAGE_ABS: get_value_per_agent(optimized_model, start_datetime, 'Energy_shallow',
                                                                  agent_guids,
                                                                  lambda i: optimized_model.Energy_shallow_cap[i] > 0),
        TradeMetadataKey.DEEP_STORAGE_ABS: get_value_per_agent(optimized_model, start_datetime, 'Energy_deep',
                                                               agent_guids,
                                                               lambda i: optimized_model.Energy_deep_cap[i] > 0),
        TradeMetadataKey.SHALLOW_LOSS: get_value_per_agent(optimized_model, start_datetime, 'Loss_shallow', agent_guids,
                                                           lambda i: optimized_model.Energy_shallow_cap[i] > 0),
        TradeMetadataKey.DEEP_LOSS: get_value_per_agent(optimized_model, start_datetime, 'Loss_deep', agent_guids,
                                                        lambda i: optimized_model.Energy_deep_cap[i] > 0),
        TradeMetadataKey.SHALLOW_CHARGE: get_value_per_agent(optimized_model, start_datetime, 'Hcha_shallow',
                                                             agent_guids,
                                                             lambda i: optimized_model.Energy_shallow_cap[i] > 0),
        TradeMetadataKey.FLOW_SHALLOW_TO_DEEP: get_value_per_agent(optimized_model, start_datetime, 'Flow', agent_guids,
                                                                   lambda i: optimized_model.Energy_deep_cap[i] > 0),
        TradeMetadataKey.HP_COOL_PROD: get_value_per_agent(optimized_model, start_datetime, 'Chp', agent_guids,
                                                           lambda i: optimized_model.Phpmax[i] > 0)
    }
    if should_use_summer_mode(start_datetime):
        metadata_per_agent_and_period[TradeMetadataKey.HP_LOW_HEAT_PROD] = \
            get_value_per_agent(optimized_model, start_datetime, 'Hhp', agent_guids,
                                lambda i: optimized_model.Phpmax[i] > 0)
        metadata_per_agent_and_period[TradeMetadataKey.HP_HIGH_HEAT_PROD] = \
            get_value_per_agent(optimized_model, start_datetime, 'HhpB', agent_guids,
                                lambda i: optimized_model.PhpBmax[i] > 0)
    else:
        metadata_per_agent_and_period[TradeMetadataKey.HP_LOW_HEAT_PROD] = {}
        metadata_per_agent_and_period[TradeMetadataKey.HP_HIGH_HEAT_PROD] = \
            get_value_per_agent(optimized_model, start_datetime, 'Hhp', agent_guids,
                                lambda i: optimized_model.Phpmax[i] > 0)

    # Build metadata per period (not agent-individual)
    metadata_per_period = {
        TradeMetadataKey.COOL_DUMP: get_value_per_period(optimized_model, start_datetime, 'cool_dump'),
        TradeMetadataKey.CM_COOL_PROD: get_value_per_period(optimized_model, start_datetime, 'Ccc'),
        TradeMetadataKey.CM_HEAT_PROD: get_value_per_period(optimized_model, start_datetime, 'Hcc'),
        TradeMetadataKey.CM_ELEC_CONS: get_value_per_period(optimized_model, start_datetime, 'Pcc')
    }
    heat_dump_per_agent = get_value_per_agent(optimized_model, start_datetime, 'heat_dump', agent_guids,
                                              lambda i: True)
    heat_dump_total = {dt: sum(inner_dict[dt] for inner_dict in heat_dump_per_agent.values() if dt in inner_dict)
                       for dt in set(key for inner_dict in heat_dump_per_agent.values() for key in inner_dict)}
    metadata_per_period[TradeMetadataKey.HEAT_DUMP] = heat_dump_total
    return ChalmersOutputs(elec_trades + heat_trades + cool_trades, metadata_per_agent_and_period, metadata_per_period)


def build_supply_and_demand_dfs(agents: List[BlockAgent], start_datetime: datetime.datetime, trading_horizon: int) -> \
        Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame,
              pd.DataFrame]:
    elec_demand = []
    elec_supply = []
    high_heat_demand = []
    high_heat_supply = []
    low_heat_demand = []
    low_heat_supply = []
    cooling_demand = []
    cooling_supply = []
    for agent in agents:
        agent_elec_demand: List[float] = []
        agent_elec_supply: List[float] = []
        agent_high_heat_demand: List[float] = []
        agent_high_heat_supply: List[float] = []
        agent_low_heat_demand: List[float] = []
        agent_low_heat_supply: List[float] = []
        agent_cooling_demand: List[float] = []
        agent_cooling_supply: List[float] = []
        for hour in range(trading_horizon):
            usage_per_resource = agent.get_actual_usage(start_datetime + datetime.timedelta(hours=hour))
            add_usage_to_demand_list(agent_elec_demand, usage_per_resource[Resource.ELECTRICITY])
            add_usage_to_supply_list(agent_elec_supply, usage_per_resource[Resource.ELECTRICITY])
            add_usage_to_demand_list(agent_high_heat_demand, usage_per_resource[Resource.HIGH_TEMP_HEAT])
            add_usage_to_supply_list(agent_high_heat_supply, usage_per_resource[Resource.HIGH_TEMP_HEAT])
            add_usage_to_demand_list(agent_low_heat_demand, usage_per_resource[Resource.LOW_TEMP_HEAT])
            add_usage_to_supply_list(agent_low_heat_supply, usage_per_resource[Resource.LOW_TEMP_HEAT])
            add_usage_to_demand_list(agent_cooling_demand, usage_per_resource[Resource.COOLING])
            add_usage_to_supply_list(agent_cooling_supply, usage_per_resource[Resource.COOLING])
        elec_demand.append(agent_elec_demand)
        elec_supply.append(agent_elec_supply)
        high_heat_demand.append(agent_high_heat_demand)
        high_heat_supply.append(agent_high_heat_supply)
        low_heat_demand.append(agent_low_heat_demand)
        low_heat_supply.append(agent_low_heat_supply)
        cooling_demand.append(agent_cooling_demand)
        cooling_supply.append(agent_cooling_supply)
    elec_demand_df = pd.DataFrame(elec_demand)
    elec_supply_df = pd.DataFrame(elec_supply)
    high_heat_demand_df = pd.DataFrame(high_heat_demand)
    high_heat_supply_df = pd.DataFrame(high_heat_supply)
    low_heat_demand_df = pd.DataFrame(low_heat_demand)
    low_heat_supply_df = pd.DataFrame(low_heat_supply)
    cooling_demand_df = pd.DataFrame(cooling_demand)
    cooling_supply_df = pd.DataFrame(cooling_supply)
    return (elec_demand_df, elec_supply_df, high_heat_demand_df, high_heat_supply_df,
            low_heat_demand_df, low_heat_supply_df, cooling_demand_df, cooling_supply_df)


def add_usage_to_demand_list(agent_list: List[float], usage_of_resource: float):
    agent_list.append(usage_of_resource if usage_of_resource > 0 else 0)


def add_usage_to_supply_list(agent_list: List[float], usage_of_resource: float):
    agent_list.append(-usage_of_resource if usage_of_resource < 0 else 0)


def get_power_transfers(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime, grid_agent_guid: str,
                        agent_guids: List[str], resource_price_data: ElectricityPrice, local_market_enabled: bool) \
        -> List[Trade]:
    total_bought = get_sum_of_param(optimized_model.Pbuy_market)
    if local_market_enabled:
        # For example: Pbuy_market is how much the LEC bought from the external grid operator
        agent_trades = get_agent_transfers_with_lec(optimized_model, start_datetime,
                                                    sold_internal_name='Psell_grid', bought_internal_name='Pbuy_grid',
                                                    resource=Resource.ELECTRICITY, agent_guids=agent_guids, loss=0.0,
                                                    resource_price_data=resource_price_data, total_bought=total_bought)
        external_trades = get_external_elec_transfers(optimized_model, start_datetime,
                                                      sold_to_external_name='Psell_market',
                                                      bought_from_external_name='Pbuy_market',
                                                      grid_agent_guid=grid_agent_guid,
                                                      loss=0.0, resource_price_data=resource_price_data,
                                                      market=Market.LOCAL, total_bought=total_bought)
    else:
        agent_trades = get_agent_transfers_no_lec(optimized_model, start_datetime,
                                                  sold_internal_name='Psell_market', bought_internal_name='Pbuy_market',
                                                  resource=Resource.ELECTRICITY, agent_guid=agent_guids[0], loss=0.0,
                                                  resource_price_data=resource_price_data, total_bought=total_bought)
        external_trades = get_external_elec_transfers(optimized_model, start_datetime,
                                                      sold_to_external_name='Psell_market',
                                                      bought_from_external_name='Pbuy_market',
                                                      grid_agent_guid=grid_agent_guid,
                                                      loss=0.0, resource_price_data=resource_price_data,
                                                      market=Market.EXTERNAL, total_bought=total_bought)
    return agent_trades + external_trades


def get_heat_transfers(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime, grid_agent_guid: str,
                       agent_guids: List[str], resource_price_data: HeatingPrice, local_market_enabled: bool) \
        -> List[Trade]:
    resource = Resource.LOW_TEMP_HEAT if (should_use_summer_mode(start_datetime) and local_market_enabled) \
        else Resource.HIGH_TEMP_HEAT
    total_bought = get_sum_of_param(optimized_model.Hbuy_market)
    if local_market_enabled:
        agent_trades = get_agent_transfers_with_lec(optimized_model, start_datetime,
                                                    sold_internal_name='Hsell_grid', bought_internal_name='Hbuy_grid',
                                                    resource=resource, agent_guids=agent_guids,
                                                    loss=optimized_model.Heat_trans_loss,
                                                    resource_price_data=resource_price_data, total_bought=total_bought)
        external_trades = get_external_heat_transfers(optimized_model, start_datetime,
                                                      sold_to_external_name='NA',
                                                      bought_from_external_name='Hbuy_market',
                                                      grid_agent_guid=grid_agent_guid,
                                                      loss=optimized_model.Heat_trans_loss,
                                                      resource_price_data=resource_price_data, market=Market.LOCAL,
                                                      total_bought=total_bought)
    else:
        agent_trades = get_agent_transfers_no_lec(optimized_model, start_datetime,
                                                  sold_internal_name='NA', bought_internal_name='Hbuy_market',
                                                  resource=resource, agent_guid=agent_guids[0],
                                                  loss=optimized_model.Heat_trans_loss,
                                                  resource_price_data=resource_price_data, total_bought=total_bought)
        external_trades = get_external_heat_transfers(optimized_model, start_datetime,
                                                      sold_to_external_name='NA',
                                                      bought_from_external_name='Hbuy_market',
                                                      grid_agent_guid=grid_agent_guid,
                                                      loss=optimized_model.Heat_trans_loss,
                                                      resource_price_data=resource_price_data, market=Market.EXTERNAL,
                                                      total_bought=total_bought)
    return agent_trades + external_trades


def get_cool_transfers(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime, agent_guids: List[str]) \
        -> List[Trade]:
    return get_agent_transfers_with_lec(optimized_model, start_datetime,
                                        sold_internal_name='Csell_grid', bought_internal_name='Cbuy_grid',
                                        resource=Resource.COOLING, agent_guids=agent_guids,
                                        loss=optimized_model.cold_trans_loss, resource_price_data=None,
                                        total_bought=0.0)  # No external grid operator for cooling


def get_external_elec_transfers(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime,
                                sold_to_external_name: str, bought_from_external_name: str,
                                grid_agent_guid: str, loss: float,
                                resource_price_data: ElectricityPrice, market: Market,
                                total_bought: float) -> List[Trade]:
    transfers: List[Trade] = []
    for hour in optimized_model.T:
        add_external_elec_trade(transfers, bought_from_external_name, hour, optimized_model, sold_to_external_name,
                                start_datetime, grid_agent_guid, loss,
                                resource_price_data, market, total_bought)
    return transfers


def get_external_heat_transfers(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime,
                                sold_to_external_name: str, bought_from_external_name: str,
                                grid_agent_guid: str, loss: float,
                                resource_price_data: HeatingPrice, market: Market, total_bought: float) -> List[Trade]:
    transfers: List[Trade] = []
    retail_price = calculate_estimated_heating_retail_price(optimized_model, total_bought)
    wholesale_price = calculate_estimated_heating_wholesale_price()
    for hour in optimized_model.T:
        add_external_heat_trade(transfers, bought_from_external_name, hour, optimized_model, sold_to_external_name,
                                start_datetime, grid_agent_guid, loss,
                                resource_price_data, market, retail_price, wholesale_price)
    return transfers


def get_agent_transfers_with_lec(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime,
                                 sold_internal_name: str, bought_internal_name: str,
                                 resource: Resource, agent_guids: List[str], loss: float,
                                 resource_price_data: Optional[IPrice], total_bought: float) -> List[Trade]:
    transfers: List[Trade] = []
    for hour in optimized_model.T:
        for i_agent in optimized_model.I:
            add_agent_trade(transfers, bought_internal_name, sold_internal_name, hour, i_agent, optimized_model,
                            start_datetime, resource, agent_guids, loss, Market.LOCAL, resource_price_data,
                            total_bought)
    return transfers


def get_agent_transfers_no_lec(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime,
                               sold_internal_name: str, bought_internal_name: str,
                               resource: Resource, agent_guid: str, loss: float,
                               resource_price_data: IPrice, total_bought: float) -> List[Trade]:
    transfers: List[Trade] = []
    for hour in optimized_model.T:
        add_agent_trade(transfers, bought_internal_name, sold_internal_name, hour, None, optimized_model,
                        start_datetime, resource, [agent_guid], loss, Market.EXTERNAL, resource_price_data,
                        total_bought)
    return transfers


def add_agent_trade(trade_list: List[Trade], bought_internal_name: str, sold_internal_name: str, hour: int,
                    i_agent: Optional[int], optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime,
                    resource: Resource, agent_guids: List[str], loss: float, market: Market,
                    resource_price_data: Optional[IPrice], total_bought: float):
    bought_internal: IndexedVar = getattr(optimized_model, bought_internal_name)
    sold_internal: Optional[IndexedVar] = getattr(optimized_model, sold_internal_name) \
        if hasattr(optimized_model, sold_internal_name) else None
    if i_agent is not None:
        net = bought_internal[i_agent, hour] - sold_internal[i_agent, hour] \
            if sold_internal is not None else bought_internal[i_agent, hour]
        agent_name = agent_guids[i_agent]
    else:
        net = bought_internal[hour] - sold_internal[hour] \
            if sold_internal is not None else bought_internal[hour]
        agent_name = agent_guids[0]
    quantity = pyo.value(net)
    period = start_datetime + datetime.timedelta(hours=hour)
    if quantity > VERY_SMALL_NUMBER or quantity < -VERY_SMALL_NUMBER:
        quantity_pre_loss = quantity / (1 - loss)
        trade_quantity = abs(quantity_pre_loss if quantity > 0 else quantity)

        if resource_price_data is None:
            estimated_marginal_price = np.nan
            wholesale_price = np.nan
            grid_fee_per_kwh = 0.0
        elif resource == Resource.ELECTRICITY:
            nordpool_prices = optimized_model.nordpool_price
            estimated_marginal_price, tax_per_kwh, grid_fee_per_kwh = calculate_estimated_electricity_retail_price(
                hour, nordpool_prices, optimized_model, total_bought)
            wholesale_price = calculate_estimated_electricity_wholesale_price(hour, nordpool_prices, optimized_model)
            resource_price_data.add_price_estimate_for_agent(period, estimated_marginal_price, agent_name)
        else:
            estimated_marginal_price = calculate_estimated_heating_retail_price(optimized_model, total_bought)
            wholesale_price = calculate_estimated_heating_wholesale_price()
            resource_price_data.add_price_estimate_for_agent(period, estimated_marginal_price, agent_name)
            grid_fee_per_kwh = 0.0

        trade_list.append(Trade(period=period,
                                action=Action.BUY if quantity > 0 else Action.SELL, resource=resource,
                                quantity=trade_quantity,
                                price=estimated_marginal_price if quantity > 0 else wholesale_price,
                                source=agent_name, by_external=False, market=market, loss=loss,
                                grid_fee_paid=grid_fee_per_kwh))
    else:
        trade_quantity = 0.0

    # Add to resource price data - needs to be done even if quantity is 0
    if market == Market.EXTERNAL and resource_price_data is not None:
        resource_price_data.add_external_sell_for_agent(period, trade_quantity, agent_name)


def add_external_elec_trade(trade_list: List[Trade], bought_from_external_name: str, hour: int,
                            optimized_model: pyo.ConcreteModel, sold_to_external_name: str,
                            start_datetime: datetime.datetime, grid_agent_guid: str,
                            loss: float, elec_price_data: ElectricityPrice, market: Market,
                            total_bought: float):
    external_quantity = pyo.value(get_variable_value_or_else(optimized_model, sold_to_external_name, hour)
                                  - get_variable_value_or_else(optimized_model, bought_from_external_name, hour))
    period = start_datetime + datetime.timedelta(hours=hour)
    nordpool_prices = optimized_model.nordpool_price
    if external_quantity > VERY_SMALL_NUMBER:
        wholesale_price = calculate_estimated_electricity_wholesale_price(hour, nordpool_prices, optimized_model)
        trade_list.append(Trade(period=period,
                                action=Action.BUY, resource=Resource.ELECTRICITY,
                                quantity=external_quantity / (1 - loss),
                                price=wholesale_price, source=grid_agent_guid, by_external=True, market=market,
                                loss=loss))
    else:
        if external_quantity < -VERY_SMALL_NUMBER:
            trade_quantity = -external_quantity
            retail_price, elec_tax_fee, grid_fee_per_kwh = calculate_estimated_electricity_retail_price(
                hour, nordpool_prices, optimized_model, total_bought)
            if market == Market.LOCAL:
                # Means this is for LEC, so we add a price estimate. If it is not for LEC, the price estimate will be
                # different for each agent, so the price estimates are added when the agent trade is created instead.
                elec_price_data.add_price_estimate(period, retail_price)

            trade_list.append(Trade(period=period,
                                    action=Action.SELL, resource=Resource.ELECTRICITY, quantity=trade_quantity,
                                    price=retail_price,
                                    source=grid_agent_guid, by_external=True, market=market,
                                    loss=loss,
                                    tax_paid=elec_tax_fee))
        else:
            trade_quantity = 0.0
        # Add to ElectricityPrice - needs to be done even if quantity is 0
        elec_price_data.add_external_sell(period, trade_quantity)


def calculate_estimated_electricity_retail_price(hour: int,
                                                 nordpool_prices,
                                                 optimized_model: pyo.ConcreteModel,
                                                 total_bought: float) -> Tuple[float, float, float]:
    """
    Reconstructs the retail price estimate from the objective functions in CEMS_function.py and AgentEMS.py.
    Returns the estimated total price, the tax, and the grid fee (all marginal, i.e. per kWh)
    """
    elec_trans_fee_param = optimized_model.elec_trans_fee
    elec_tax_fee_param = optimized_model.elec_tax_fee
    elec_peak_load_fee_param = optimized_model.elec_peak_load_fee
    avg_elec_peak_load_var = optimized_model.avg_elec_peak_load
    elec_tax_fee = get_value_from_param(elec_tax_fee_param)
    elec_trans_fee = get_value_from_param(elec_trans_fee_param)
    elec_peak_load_fee = get_value_from_param(elec_peak_load_fee_param)
    price_per_kwh = get_value_from_param(nordpool_prices, hour) + elec_trans_fee + elec_tax_fee
    # Get the total effect fee, and then per kWh
    total_effect_fee_for_horizon = elec_peak_load_fee * avg_elec_peak_load_var.value
    effect_fee_per_kwh = total_effect_fee_for_horizon / total_bought
    # The "grid fee" is essentially what goes into the pocket of the external grid operator
    grid_fee_per_kwh = effect_fee_per_kwh + elec_trans_fee
    estimated_marginal_price = price_per_kwh + effect_fee_per_kwh
    return estimated_marginal_price, elec_tax_fee, grid_fee_per_kwh


def calculate_estimated_electricity_wholesale_price(hour: int,
                                                    nordpool_prices,
                                                    optimized_model: pyo.ConcreteModel) -> float:
    """
    Reconstructs the wholesale price estimate from the objective functions in CEMS_function.py and AgentEMS.py.
    """
    incentive_fee = optimized_model.incentive_fee
    return get_value_from_param(nordpool_prices, hour) + get_value_from_param(incentive_fee)


def calculate_estimated_heating_retail_price(optimized_model: pyo.ConcreteModel, total_bought: float) -> float:
    """
    Reconstructs the retail price estimate from the objective functions in CEMS_function.py and AgentEMS.py.
    Returns the estimated marginal price (i.e. per kWh)
    """
    heat_price_param = optimized_model.Hprice_energy
    heat_peak_load_fee_param = optimized_model.heat_peak_load_fee
    monthly_heat_peak_energy_var = optimized_model.monthly_heat_peak_energy
    heat_price = get_value_from_param(heat_price_param)
    heat_peak_load_fee = get_value_from_param(heat_peak_load_fee_param)
    # Get the total effect fee, and then per kWh
    total_effect_fee_for_horizon = (heat_peak_load_fee / 24) * monthly_heat_peak_energy_var.value
    effect_fee_per_kwh = total_effect_fee_for_horizon / total_bought
    return heat_price + effect_fee_per_kwh


def calculate_estimated_heating_wholesale_price() -> float:
    """
    Reconstructs the wholesale price estimate from the objective functions in CEMS_function.py and AgentEMS.py.
    """
    return np.nan  # Selling of heat is not defined!


def add_external_heat_trade(trade_list: List[Trade], bought_from_external_name: str, hour: int,
                            optimized_model: pyo.ConcreteModel, sold_to_external_name: str,
                            start_datetime: datetime.datetime, grid_agent_guid: str,
                            loss: float, heat_price_data: HeatingPrice, market: Market,
                            retail_price: float, wholesale_price: float):
    external_quantity = pyo.value(get_variable_value_or_else(optimized_model, sold_to_external_name, hour)
                                  - get_variable_value_or_else(optimized_model, bought_from_external_name, hour))
    period = start_datetime + datetime.timedelta(hours=hour)
    if external_quantity > VERY_SMALL_NUMBER:
        trade_list.append(Trade(period=period,
                                action=Action.BUY, resource=Resource.HIGH_TEMP_HEAT,
                                quantity=external_quantity / (1 - loss),
                                price=wholesale_price, source=grid_agent_guid, by_external=True, market=market,
                                loss=loss))
    else:
        if external_quantity < -VERY_SMALL_NUMBER:
            trade_quantity = -external_quantity
            if market == Market.LOCAL:
                # Means this is for LEC, so we add a price estimate. If it is not for LEC, the price estimate will be
                # different for each agent, so the price estimates are added when the agent trade is created instead.
                heat_price_data.add_price_estimate(period, retail_price)
            trade_list.append(Trade(period=period,
                                    action=Action.SELL, resource=Resource.HIGH_TEMP_HEAT, quantity=trade_quantity,
                                    price=retail_price, source=grid_agent_guid, by_external=True, market=market,
                                    loss=loss))
        else:
            trade_quantity = 0.0
        # Add to HeatingPrice - needs to be done even if quantity is 0
        heat_price_data.add_external_sell(period, trade_quantity)


def get_value_from_param(maybe_indexed_param: Union[IndexedParam, ScalarParam], index: int = 0) -> float:
    """If maybe_indexed_param is indexed, gets the 'index':th value. If it is a scalar, gets its value."""
    if isinstance(maybe_indexed_param, IndexedParam):
        return maybe_indexed_param[index]
    elif isinstance(maybe_indexed_param, ScalarParam):
        return maybe_indexed_param.value
    raise RuntimeError('Unsupported type: {}'.format(type(maybe_indexed_param)))


def get_sum_of_param(indexed_param: IndexedParam) -> float:
    return sum(indexed_param)


def get_variable_value_or_else(optimized_model: pyo.ConcreteModel, variable_name: str, index: int,
                               if_not_exists: float = 0.0) -> float:
    if hasattr(optimized_model, variable_name):
        return getattr(optimized_model, variable_name)[index]
    return if_not_exists


def add_value_per_agent_to_dict(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime,
                                dict_to_add_to: Dict[str, Dict[datetime.datetime, Any]],
                                variable_name: str, agent_guids: List[str]):
    """
    Example variable names: "Hhp" for heat pump production, "SOCBES" for state of charge of battery storage.
    Adds to a nested dict where agent GUID is the first key, the period the second.
    """
    for hour in optimized_model.T:
        for i_agent in optimized_model.I:
            quantity = pyo.value(getattr(optimized_model, variable_name)[i_agent, hour])
            period = start_datetime + datetime.timedelta(hours=hour)
            add_to_nested_dict(dict_to_add_to, agent_guids[i_agent], period, quantity)


def get_value_per_agent(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime,
                        variable_name: str, agent_guids: List[str],
                        should_add_for_agent: Callable[[int], bool],
                        divide_by: Callable[[int], float] = lambda i: 1.0) \
        -> Dict[str, Dict[datetime.datetime, Any]]:
    """
    Example variable names: "Hhp" for heat pump production, "SOCBES" for state of charge of battery storage.
    Returns a nested dict where agent GUID is the first key, the period the second.
    Will only add values for which "should_add_for_agent(agent_index)" is True. This can be used to ensure that battery
    charge state is only added for agents that actually have a battery.
    If "divide_by" is specified, all quantities will be divided by "divide_by(agent_index)". Can be used to translate
    energy quantities to % of max, for example.
    """
    dict_to_add_to: Dict[str, Dict[datetime.datetime, Any]] = {}
    for hour in optimized_model.T:
        period = start_datetime + datetime.timedelta(hours=hour)
        for i_agent in optimized_model.I:
            if should_add_for_agent(i_agent):
                quantity = pyo.value(getattr(optimized_model, variable_name)[i_agent, hour])
                value = round(quantity / divide_by(i_agent), DECIMALS_TO_ROUND_TO)
                add_to_nested_dict(dict_to_add_to, agent_guids[i_agent], period, value)
    return dict_to_add_to


def get_value_per_period(optimized_model: pyo.ConcreteModel, start_datetime: datetime.datetime, variable_name: str) \
        -> Dict[datetime.datetime, Any]:
    """
    Example variable names: "heat_dump" for heat reservoir.
    """
    dict_to_add_to: Dict[datetime.datetime, Any] = {}
    for hour in optimized_model.T:
        period = start_datetime + datetime.timedelta(hours=hour)
        quantity = pyo.value(getattr(optimized_model, variable_name)[hour])
        dict_to_add_to[period] = round(quantity, DECIMALS_TO_ROUND_TO)
    return dict_to_add_to
