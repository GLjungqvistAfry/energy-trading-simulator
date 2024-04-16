from typing import List, Tuple
import numpy as np
import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import OptSolver, SolverResults


def solve_model(solver: OptSolver, summer_mode: bool, month: int, agent: int, external_elec_buy_price: pd.Series,
                external_elec_sell_price: pd.Series, external_heat_buy_price: float,
                battery_capacity: List[float], battery_charge_rate: List[float], battery_discharge_rate: List[float],
                SOCBES0: List[float], HP_Cproduct_active: list[bool], heatpump_COP: List[float],
                heatpump_max_power: List[float], heatpump_max_heat: List[float],
                booster_heatpump_COP: List[float], booster_heatpump_max_power: List[float],
                booster_heatpump_max_heat: List[float], build_area: List[float], SOCTES0: List[float],
                thermalstorage_max_temp: List[float], thermalstorage_volume: List[float], BITES_Eshallow0: List[float],
                BITES_Edeep0: List[float], borehole: List[bool],
                elec_consumption: pd.DataFrame, hot_water_heatdem: pd.DataFrame, space_heating_heatdem: pd.DataFrame,
                cold_consumption: pd.DataFrame, pv_production: pd.DataFrame, excess_heat: pd.DataFrame,
                battery_efficiency: float = 0.95,
                max_elec_transfer_between_agents: float = 500, max_elec_transfer_to_external: float = 1000,
                max_heat_transfer_between_agents: float = 500, max_heat_transfer_to_external: float = 1000,
                chiller_COP: float = 1.5, Pccmax: float = 100, thermalstorage_efficiency: float = 0.98,
                heat_trans_loss: float = 0.05, cold_trans_loss: float = 0.05, trading_horizon: int = 24) \
        -> Tuple[pyo.ConcreteModel, SolverResults]:
    """
    This function should be exposed to AFRY's trading simulator in some way.
    Which solver to use should be left to the user, so we'll request it as an argument, rather than defining it here.

    Things we'll want to return:
    * The energy flows
    * Whether the solver found a solution
    * Perhaps things like how much heat that was generated from heat pumps
    Perhaps the best way is to do as this is coded at the moment: returning the model object and the solver results
    """
    # Some validation (should probably raise specific exceptions rather than use assert)
    assert len(elec_consumption) >= trading_horizon
    assert len(hot_water_heatdem) >= trading_horizon
    assert len(space_heating_heatdem) >= trading_horizon
    assert len(cold_consumption) >= trading_horizon
    assert len(pv_production) >= trading_horizon
    assert len(excess_heat) >= trading_horizon
    assert len(external_elec_buy_price) >= trading_horizon
    assert len(external_elec_sell_price) >= trading_horizon
    assert battery_efficiency > 0  # Otherwise we'll get division by zero
    assert thermalstorage_efficiency > 0  # Otherwise we'll get division by zero
    assert heat_trans_loss > 0  # Otherwise we may get simultaneous buying and selling of heat

    # Energy per degree C in each agent's tank
    # Specific heat of Water is 4182 J/(kg C)
    # Density of water is 998 kg/m3
    # This assumes that HTESdis and HTEScha are in kW. If they are in watt,
    # 1000 should be removed from the following formulation:
    kwh_per_deg = thermalstorage_volume * 4182 * 998 / 3600000

    # All hot water is covered by discharging the accumulator tank. So hot water demand cannot be greater than the
    # maximum discharge capability, or we won't be able to find a solution (the TerminationCondition will be
    # 'infeasible'). Easier to raise this error straight away, so that the user knows specifically what went wrong.
    # TODO: Update this if/when max_HTES_dis is modified. The max discharge is equal to the max capacity plus the max
    #  input...
    max_tank_discharge = kwh_per_deg * thermalstorage_max_temp
    too_big_hot_water_demand = hot_water_heatdem.gt(max_tank_discharge, axis=0)
    problems = [tbhwd for tbhwd in too_big_hot_water_demand.tolist()]
    if sum(problems) > 0:
        raise RuntimeError(f'Unfillable hot water demand for agent indices: {agent}')

    # Similarly, check the maximum cooling produced vs the cooling demand
    max_cooling_produced_for_1_hour = Pccmax * chiller_COP \
                                      + sum([(hp_cop - 1) * max_php if hpc_active else
                                             np.inf if has_bh and month not in [6, 7, 8] else 0
                                             for hp_cop, max_php, hpc_active, has_bh in
                                             zip([heatpump_COP], [heatpump_max_power], [HP_Cproduct_active], [borehole])])
    too_big_cool_demand = cold_consumption.gt(max_cooling_produced_for_1_hour)
    if too_big_cool_demand.any():
        raise RuntimeError(f'Unfillable cooling demand in LEC for hour(s) {agent}!')

    # This share of high-temp heat need can be covered by low-temp heat (source: BDAB). The rest needs to be covered by
    # a booster heat pump.
    PERC_OF_HT_COVERABLE_BY_LT = 0.6

    # Build model for each agent
    model = pyo.ConcreteModel(name=f"Agent{agent}")
    # Sets
    model.T = pyo.Set(initialize=range(int(trading_horizon)))  # index of time intervals
    # Parameters
    model.penalty = pyo.Param(initialize=1000)
    model.price_buy = pyo.Param(model.T, initialize=external_elec_buy_price)
    model.price_sell = pyo.Param(model.T, initialize=external_elec_sell_price)
    model.Hprice_energy = pyo.Param(initialize=external_heat_buy_price)
    # Grid data
    model.Pmax_grid = pyo.Param(initialize=max_elec_transfer_between_agents)
    model.Hmax_grid = pyo.Param(initialize=max_heat_transfer_between_agents)
    model.Pmax_market = pyo.Param(initialize=max_elec_transfer_to_external)
    model.Hmax_market = pyo.Param(initialize=max_heat_transfer_to_external)
    # Demand data of agents
    model.Pdem = pyo.Param(model.T, initialize=lambda m, t: elec_consumption.iloc[t])
    model.Hhw = pyo.Param(model.T, initialize=lambda m, t: hot_water_heatdem.iloc[t])
    model.Hsh = pyo.Param(model.T, initialize=lambda m, t: space_heating_heatdem.iloc[t])
    model.Cld = pyo.Param(model.T, initialize=lambda m, t: cold_consumption.iloc[t])
    # Supply data of agents
    model.Ppv = pyo.Param(model.T, initialize=lambda m, t: pv_production.iloc[t])
    model.Hsh_excess = pyo.Param(model.T, initialize=lambda m, t: excess_heat.iloc[t])
    # BES data
    model.effe = pyo.Param(initialize=battery_efficiency)
    model.SOCBES0 = pyo.Param(initialize=SOCBES0)
    model.Emax_BES = pyo.Param(initialize=battery_capacity)
    model.Pmax_BES_Cha = pyo.Param(initialize=battery_charge_rate)
    model.Pmax_BES_Dis = pyo.Param(initialize=battery_discharge_rate)
    # Building inertia as thermal energy storage
    model.BITES_Eshallow0 = pyo.Param(initialize=lambda m: BITES_Eshallow0)
    model.BITES_Edeep0 = pyo.Param(initialize=lambda m: BITES_Edeep0)
    model.Energy_shallow_cap = pyo.Param(initialize=lambda m: 0.046 * build_area)
    model.Energy_deep_cap = pyo.Param(initialize=lambda m: 0.291 * build_area)
    model.Heat_rate_shallow = pyo.Param(initialize=lambda m: 0.023 * build_area)
    model.Kval = pyo.Param(initialize=lambda m: 0.03 * build_area)
    model.Kloss_shallow = pyo.Param(initialize=0.9913)
    model.Kloss_deep = pyo.Param(initialize=0.9963)
    # Heat pump data
    model.COPhp = pyo.Param(initialize=heatpump_COP)
    model.Phpmax = pyo.Param(initialize=heatpump_max_power)
    model.Hhpmax = pyo.Param(initialize=heatpump_max_heat)
    model.HP_Cproduct_active = pyo.Param(initialize=lambda m: HP_Cproduct_active)
    # Booster heat pump data
    model.COPhpB = pyo.Param(initialize=booster_heatpump_COP)
    model.PhpBmax = pyo.Param(initialize=booster_heatpump_max_power)
    model.HhpBmax = pyo.Param(initialize=booster_heatpump_max_heat)
    # Borehole
    model.borehole = pyo.Param(initialize=lambda m: borehole)
    # Thermal energy storage data
    model.efft = pyo.Param(initialize=thermalstorage_efficiency)
    model.SOCTES0 = pyo.Param(initialize=SOCTES0)
    model.Tmax_TES = pyo.Param(initialize=thermalstorage_max_temp)
    model.kwh_per_deg = pyo.Param(initialize=kwh_per_deg)
    # Local heat network efficiency
    model.Heat_trans_loss = pyo.Param(initialize=heat_trans_loss)
    model.cold_trans_loss = pyo.Param(initialize=cold_trans_loss)
    # Variable
    model.Pbuy_grid = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Psell_grid = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.U_power_buy_sell_grid = pyo.Var(model.T, within=pyo.Binary, initialize=0)
    model.Hbuy_grid = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Hsell_grid = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Cbuy_grid = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Pcha = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Pdis = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.SOCBES = pyo.Var(model.T, bounds=(0, 1), within=pyo.NonNegativeReals,
                           initialize=lambda m, t: SOCBES0)
    model.Hhp = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Chp = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Php = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.HTEScha = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.HTESdis = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.SOCTES = pyo.Var(model.T, bounds=(0, 1), within=pyo.NonNegativeReals,
                           initialize=lambda m, t: SOCTES0)
    model.Energy_shallow = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    # Charge/discharge of the shallow layer, a negative value meaning discharge
    model.Hcha_shallow = pyo.Var(model.T, within=pyo.Reals, initialize=0)
    model.Flow = pyo.Var(model.T, within=pyo.Reals, initialize=0)
    model.Loss_shallow = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Energy_deep = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.Loss_deep = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    if summer_mode:
        model.HhpB = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
        model.PhpB = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)
    model.heat_dump = pyo.Var(model.T, within=pyo.NonNegativeReals, initialize=0)

    #        add_obj_and_constraints(model, summer_mode, month)

    #        print(j)
    # Solve!
    #        results = solver.solve(model)
    #    return model, results

    # def add_obj_and_constraints(model: pyo.ConcreteModel, summer_mode: bool, month: int):
    # Objective function:

    # Objective function: minimize the total charging cost (eq. 1 of the report)
    def obj_rule(model):
        return sum(
            model.Pbuy_grid[t] * model.price_buy[t] - model.Psell_grid[t] * model.price_sell[t]
            + model.Hbuy_grid[t] * model.Hprice_energy
            + model.heat_dump[t] * model.penalty
            for t in model.T)

    # Constraints:
    # Buying and selling heat/electricity from agents cannot happen at the same time
    # and should be restricted to its maximum value (Pmax_grid) (eqs. 10 to 15 of the report)
    def max_Pbuy_grid(model, t):
        return model.Pbuy_grid[t] <= model.Pmax_grid * model.U_power_buy_sell_grid[t]

    def max_Hbuy_grid(model, t):
        return model.Hbuy_grid[t] <= model.Hmax_grid  # * model.U_heat_buy_sell_grid[i, t]

    def max_Psell_grid(model, t):
        return model.Psell_grid[t] <= model.Pmax_grid * (1 - model.U_power_buy_sell_grid[t])

    def max_Hsell_grid_winter(model, t):
        # Only used in winter mode : Due to high temperature of district heating (60 deg. C),
        # it is not possible to export heat from building to the district heating
        return model.Hsell_grid[t] <= 0

    def max_Hsell_grid_summer(model, t):
        # Only used in summer mode
        return model.Hsell_grid[t] <= model.Hmax_grid  # * (1 - model.U_heat_buy_sell_grid[i, t])

    # (eq. 2 and 3 of the report)
    # Electrical/heat/cool power balance equation for agents
    def agent_Pbalance_winter(model, t):
        # Only used in winter mode
        return model.Ppv[t] + model.Pdis[t] + model.Pbuy_grid[t] == \
               model.Pdem[t] + model.Php[t] + model.Pcha[t] + model.Psell_grid[t]

    def agent_Pbalance_summer(model, t):
        # Only used in summer mode
        return model.Ppv[t] + model.Pdis[t] + model.Pbuy_grid[t] == \
               model.Pdem[t] + model.Php[t] + model.PhpB[t] + model.Pcha[t] + model.Psell_grid[t]

    def agent_Hbalance_winter(model, t):
        # Only used in winter mode
        # with TES
        if model.kwh_per_deg != 0:
            return model.Hbuy_grid[t] + model.Hhp[t] == \
                   model.Hsell_grid[t] + model.Hcha_shallow[t] + model.Hsh[t] \
                   + model.HTEScha[t] + model.heat_dump[t]
        # without TES
        else:
            return model.Hbuy_grid[t] + model.Hhp[t] == \
                   model.Hsell_grid[t] + model.Hcha_shallow[t] + model.Hsh[t] \
                   + model.Hhw[t] + model.heat_dump[t]

    def agent_Hbalance_summer(model, t):
        # Only used in summer mode
        # with TES
        if model.kwh_per_deg != 0:
            return model.Hbuy_grid[t] + model.Hhp[t] \
                   + model.Hsh_excess[t] == model.Hsell_grid[t] + model.Hcha_shallow[t] \
                   + model.Hsh[t] + PERC_OF_HT_COVERABLE_BY_LT * model.HTEScha[t] + model.heat_dump[t]
        # without TES
        else:
            return model.Hbuy_grid[t] + model.Hhp[t] \
                   + model.Hsh_excess[t] == model.Hsell_grid[t] + model.Hcha_shallow[t] \
                   + model.Hsh[t] + PERC_OF_HT_COVERABLE_BY_LT * model.Hhw[t] + model.heat_dump[t]

    def agent_Cbalance_winter(model, t):
        # Only used in months [1 to 5, 9 to 12]
        # with free cooling from borehole (model.borehole[i] == 1)
        # without free cooling from borehole (model.borehole[i] == 0)
        return model.Cbuy_grid[t] + model.Chp[t] == 0 +\
               + model.Cld[t] * (1 - model.borehole)

    def agent_Cbalance_summer(model, t):
        # Only used in months [6, 7, 8]
        return model.Cbuy_grid[t] + model.Chp[t] == 0 + model.Cld[t]

    # (eq. 5 and 6 of the report)
    def HTES_supplied_by_Bhp(model, t):
        # Only used in summer mode
        # with TES
        if model.kwh_per_deg != 0:
            return model.HhpB[t] == (1 - PERC_OF_HT_COVERABLE_BY_LT) * model.HTEScha[t]
        # without TES
        else:
            return model.HhpB[t] == (1 - PERC_OF_HT_COVERABLE_BY_LT) * model.Hhw[t]

    def Hhw_supplied_by_HTES(model, t):
        # with TES
        if model.kwh_per_deg != 0:
            return model.HTESdis[t] == model.Hhw[t]
        # without TES
        else:
            return pyo.Constraint.Skip

    # (eqs. 22 to 28 of the report)
    def BITES_Eshallow_balance(model, t):
        if t == 0:
            return model.Energy_shallow[0] == model.BITES_Eshallow0 + model.Hcha_shallow[0] \
                   - model.Flow[0] - model.Loss_shallow[0]
        else:
            return model.Energy_shallow[t] == model.Energy_shallow[t - 1] + model.Hcha_shallow[t] \
                   - model.Flow[t] - model.Loss_shallow[t]

    def BITES_shallow_dis(model, t):
        # Negative charge means discharge
        return -model.Hcha_shallow[t] <= model.Heat_rate_shallow

    def BITES_shallow_cha(model, t):
        return model.Hcha_shallow[t] <= model.Heat_rate_shallow

    def BITES_Edeep_balance(model, t):
        if t == 0:
            return model.Energy_deep[0] == model.BITES_Edeep0 + model.Flow[0] - model.Loss_deep[0]
        else:
            return model.Energy_deep[t] == model.Energy_deep[t - 1] + model.Flow[t] - model.Loss_deep[t]

    def BITES_Eflow_between_storages(model, t):
        if (model.Energy_shallow_cap == 0) or (model.Energy_deep_cap == 0):
            return model.Flow[t] == 0
        return model.Flow[t] == ((model.Energy_shallow[t] / model.Energy_shallow_cap)
                                 - (model.Energy_deep[t] / model.Energy_deep_cap)) * model.Kval

    def BITES_max_Eshallow(model, t):
        return model.Energy_shallow[t] <= model.Energy_shallow_cap

    def BITES_max_Edeep(model, t):
        return model.Energy_deep[t] <= model.Energy_deep_cap

    def BITES_shallow_loss(model, t):
        if t == 0:
            return model.Loss_shallow[0] == 0
        else:
            return model.Loss_shallow[t] == model.Energy_shallow[t - 1] * (1 - model.Kloss_shallow)

    def BITES_deep_loss(model, t):
        if t == 0:
            return model.Loss_deep[0] == 0
        else:
            return model.Loss_deep[t] == model.Energy_deep[t - 1] * (1 - model.Kloss_deep)

    def BITES_max_Hdis_shallow(model, t):
        # Negative charge means discharge
        return -model.Hcha_shallow[t] <= model.Hsh[t]

    def BITES_max_Hcha_shallow(model, t):
        return model.Hcha_shallow[t] <= model.Hhpmax + model.Hmax_grid - model.Hsh[t]

    # Battery energy storage model (eqs. 16 to 19 of the report)
    # Maximum charging/discharging power limitations
    def BES_max_dis(model, t):
        return model.Pdis[t] <= model.Pmax_BES_Dis

    def BES_max_cha(model, t):
        return model.Pcha[t] <= model.Pmax_BES_Cha

    # State of charge modelling
    def BES_Ebalance(model, t):
        if model.Emax_BES == 0:
            # No storage capacity, then we need to ensure that charge and discharge are 0 as well.
            return model.Pcha[t] + model.Pdis[t] == model.Emax_BES
        # We assume that model.effe cannot be 0
        if t == 0:
            charge = model.Pcha[0] * model.effe / model.Emax_BES
            discharge = model.Pdis[0] / (model.Emax_BES * model.effe)
            return model.SOCBES[0] == model.SOCBES0 + charge - discharge
        else:
            charge = model.Pcha[t] * model.effe / model.Emax_BES
            discharge = model.Pdis[t] / (model.Emax_BES * model.effe)
            return model.SOCBES[t] == model.SOCBES[t - 1] + charge - discharge

    def BES_final_SOC(model):
        return model.SOCBES[len(model.T) - 1] == model.SOCBES0

    def BES_remove_binaries(model, t):
        if (model.Pmax_BES_Dis == 0) or (model.Pmax_BES_Cha == 0):
            # Can't charge/discharge
            return model.Pcha[t] + model.Pdis[t] <= 0
        else:
            return model.Pdis[t] / model.Pmax_BES_Dis + model.Pcha[t] / model.Pmax_BES_Cha <= 1

    # Heat pump model (eq. 20 of the report)
    def HP_Hproduct(model, t):
        return model.Hhp[t] == model.COPhp * model.Php[t]

    def HP_Cproduct(model, t):
        if model.HP_Cproduct_active:
            return model.Chp[t] == (model.COPhp - 1) * model.Php[t]
        else:
            return model.Chp[t] == 0

    def max_HP_Hproduct(model, t):
        return model.Hhp[t] <= model.Hhpmax

    def max_HP_Pconsumption(model, t):
        return model.Php[t] <= model.Phpmax

    # Booster heat pump model (eq. 20 of the report)
    def booster_HP_Hproduct(model, t):
        # Only used in summer mode
        return model.HhpB[t] == model.COPhpB * model.PhpB[t]

    def max_booster_HP_Hproduct_summer(model, t):
        # Only used in summer mode
        return model.HhpB[t] <= model.HhpBmax

    # Thermal energy storage model (eqs. 32 to 25 of the report)
    # Maximum/minimum temperature limitations of hot water inside TES
    def max_HTES_dis(model, t):
        return model.HTESdis[t] <= model.kwh_per_deg * model.Tmax_TES

    def max_HTES_cha(model, t):
        return model.HTEScha[t] <= model.kwh_per_deg * model.Tmax_TES

    # State of charge modelling
    def HTES_Ebalance(model, t):
        if model.kwh_per_deg == 0:
            # No storage capacity, then we need to ensure that charge and discharge are 0 as well.
            return model.HTESdis[t] + model.HTEScha[t] == model.kwh_per_deg
        # We assume that model.efft and model.Tmax_TES cannot be 0
        charge = model.HTEScha[t] * model.efft / (model.kwh_per_deg * model.Tmax_TES)
        discharge = model.HTESdis[t] / ((model.kwh_per_deg * model.Tmax_TES) * model.efft)
        charge_change = charge - discharge
        if t == 0:
            return model.SOCTES[0] == model.SOCTES0 + charge_change
        else:
            return model.SOCTES[t] == model.SOCTES[t - 1] + charge_change

    def HTES_final_SOC(model):
        return model.SOCTES[len(model.T) - 1] == model.SOCTES0

    model.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)
    model.con_max_Pbuy_grid = pyo.Constraint(model.T, rule=max_Pbuy_grid)
    model.con_max_Hbuy_grid = pyo.Constraint(model.T, rule=max_Hbuy_grid)
    model.con_max_Psell_grid = pyo.Constraint(model.T, rule=max_Psell_grid)
    if summer_mode:
        model.con_max_Hsell_grid_summer = pyo.Constraint(model.T, rule=max_Hsell_grid_summer)
        model.con_agent_Pbalance_summer = pyo.Constraint(model.T, rule=agent_Pbalance_summer)
        model.con_agent_Hbalance_summer = pyo.Constraint(model.T, rule=agent_Hbalance_summer)
        model.con_HTES_supplied_by_Bhp = pyo.Constraint(model.T, rule=HTES_supplied_by_Bhp)
    else:
        model.con_max_Hsell_grid_winter = pyo.Constraint(model.T, rule=max_Hsell_grid_winter)
        model.con_agent_Pbalance_winter = pyo.Constraint(model.T, rule=agent_Pbalance_winter)
        model.con_agent_Hbalance_winter = pyo.Constraint(model.T, rule=agent_Hbalance_winter)

    if month in [6, 7, 8]:
        model.con_agent_Cbalance_summer = pyo.Constraint(model.T, rule=agent_Cbalance_summer)
    else:
        model.con_agent_Cbalance_winter = pyo.Constraint(model.T, rule=agent_Cbalance_winter)
    model.con_Hhw_supplied_by_HTES = pyo.Constraint(model.T, rule=Hhw_supplied_by_HTES)
    model.con_BITES_Eshallow_balance = pyo.Constraint(model.T, rule=BITES_Eshallow_balance)
    model.con_BITES_shallow_dis = pyo.Constraint(model.T, rule=BITES_shallow_dis)
    model.con_BITES_shallow_cha = pyo.Constraint(model.T, rule=BITES_shallow_cha)
    model.con_BITES_Edeep_balance = pyo.Constraint(model.T, rule=BITES_Edeep_balance)
    model.con_BITES_Eflow_between_storages = pyo.Constraint(model.T, rule=BITES_Eflow_between_storages)
    model.con_BITES_shallow_loss = pyo.Constraint(model.T, rule=BITES_shallow_loss)
    model.con_BITES_deep_loss = pyo.Constraint(model.T, rule=BITES_deep_loss)
    model.con_BITES_max_Hdis_shallow = pyo.Constraint(model.T, rule=BITES_max_Hdis_shallow)
    model.con_BITES_max_Hcha_shallow = pyo.Constraint(model.T, rule=BITES_max_Hcha_shallow)
    model.con_BITES_max_Eshallow = pyo.Constraint(model.T, rule=BITES_max_Eshallow)
    model.con_BITES_max_Edeep = pyo.Constraint(model.T, rule=BITES_max_Edeep)
    model.con_BES_max_dis = pyo.Constraint(model.T, rule=BES_max_dis)
    model.con_BES_max_cha = pyo.Constraint(model.T, rule=BES_max_cha)
    model.con_BES_Ebalance = pyo.Constraint(model.T, rule=BES_Ebalance)
    model.con_BES_final_SOC = pyo.Constraint(rule=BES_final_SOC)
    model.con_BES_remove_binaries = pyo.Constraint(model.T, rule=BES_remove_binaries)
    model.con_HP_Hproduct = pyo.Constraint(model.T, rule=HP_Hproduct)
    model.con_HP_Cproduct = pyo.Constraint(model.T, rule=HP_Cproduct)
    model.con_max_HP_Hproduct = pyo.Constraint(model.T, rule=max_HP_Hproduct)
    model.con_max_HP_Pconsumption = pyo.Constraint(model.T, rule=max_HP_Pconsumption)
    if summer_mode:
        model.con_max_booster_HP_Hproduct_summer = pyo.Constraint(model.T,
                                                                  rule=max_booster_HP_Hproduct_summer)
    model.con_max_HTES_dis = pyo.Constraint(model.T, rule=max_HTES_dis)
    model.con_max_HTES_cha = pyo.Constraint(model.T, rule=max_HTES_cha)
    model.con_HTES_Ebalance = pyo.Constraint(model.T, rule=HTES_Ebalance)
    model.con_HTES_final_SOC = pyo.Constraint(rule=HTES_final_SOC)

    # Solve!
    results = solver.solve(model)
    return model, results
