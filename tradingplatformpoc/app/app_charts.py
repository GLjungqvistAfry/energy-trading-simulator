from typing import Dict, List, Optional

import altair as alt

import pandas as pd

from tradingplatformpoc.app import app_constants
from tradingplatformpoc.digitaltwin.static_digital_twin import StaticDigitalTwin
from tradingplatformpoc.market.trade import Action, Resource, TradeMetadataKey
from tradingplatformpoc.sql.level.crud import db_to_viewable_level_df


def altair_base_chart(df: pd.DataFrame, domain: List[str], range_color: List[str],
                      y_label: str, title_str: str, legend: bool, freq: str = 'H') -> alt.Chart:
    """Altair chart for one or more variables over period, without specified mark."""
    selection = alt.selection_multi(fields=['variable'], bind='legend')
    alt_title = alt.TitleParams(title_str, anchor='middle')
    datetime_format = datetime_format_from_freq(freq)
    x_encoding = encoding_shorthand_from_freq(freq)
    chart = alt.Chart(df, title=alt_title). \
        encode(x=alt.X(x_encoding, axis=alt.Axis(title='Period (UTC)'), scale=alt.Scale(type="utc")),
               y=alt.Y('value:Q', axis=alt.Axis(title=y_label), stack=None),
               opacity=alt.condition(selection, alt.value(0.8), alt.value(0.0)),
               tooltip=[alt.Tooltip(field='period', title='Period', type='temporal', format=datetime_format),
                        alt.Tooltip(field='variable', title='Variable'),
                        alt.Tooltip(field='value', title='Value', format='.5f')]). \
        add_selection(selection).interactive(bind_y=False)
    if legend:
        return chart.encode(color=alt.Color('variable', scale=alt.Scale(domain=domain, range=range_color)))
    else:
        return chart.encode(color=alt.Color('variable', scale=alt.Scale(domain=domain, range=range_color), legend=None))


def altair_line_chart(df: pd.DataFrame, domain: List[str], range_color: List[str],
                      range_dash: List[List[int]], y_label: str, title_str: str,
                      legend: bool = True, freq: str = 'H') -> alt.Chart:
    """Altair base chart with line mark."""
    return altair_base_chart(df, domain, range_color, y_label, title_str, legend, freq).encode(
        strokeDash=alt.StrokeDash('variable', scale=alt.Scale(domain=domain, range=range_dash))).mark_line()


def altair_area_chart(df: pd.DataFrame, domain: List[str], range_color: List[str],
                      y_label: str, title_str: str, legend: bool = False) -> alt.Chart:
    """Altair base chart with area mark."""
    return altair_base_chart(df, domain, range_color, y_label, title_str, legend)\
        .mark_area(interpolate='step-after')


def construct_agent_energy_chart(digital_twin: StaticDigitalTwin, agent_chosen_guid: str,
                                 heat_pump_df: pd.DataFrame) -> alt.Chart:
    """
    Constructs a multi-line chart from a StaticDigitalTwin, containing all data held therein.
    """
    df = pd.DataFrame()
    # Defining colors manually, so that for example heat consumption has the same color for every agent, even if for
    # example electricity production doesn't exist for one of them.
    domain: List[str] = []
    range_color: List[str] = []
    df = add_to_df_and_lists(df, digital_twin.electricity_production, domain, range_color,
                             "Electricity production", app_constants.ALTAIR_BASE_COLORS[0])
    df = add_to_df_and_lists(df, digital_twin.electricity_usage, domain, range_color,
                             "Electricity consumption", app_constants.ALTAIR_BASE_COLORS[1])
    df = add_to_df_and_lists(df, digital_twin.hot_water_production, domain, range_color,
                             "High heat production", app_constants.ALTAIR_BASE_COLORS[2])
    df = add_to_df_and_lists(df, digital_twin.hot_water_usage, domain, range_color,
                             "High heat consumption", app_constants.ALTAIR_BASE_COLORS[3])
    df = add_to_df_and_lists(df, digital_twin.space_heating_production, domain, range_color,
                             "Low heat production", app_constants.ALTAIR_BASE_COLORS[4])
    df = add_to_df_and_lists(df, digital_twin.space_heating_usage, domain, range_color,
                             "Low heat consumption", app_constants.ALTAIR_BASE_COLORS[5])
    df = add_to_df_and_lists(df, digital_twin.cooling_usage, domain, range_color,
                             "Cooling consumption", app_constants.ALTAIR_BASE_COLORS[6])
    df = add_to_df_and_lists(df, digital_twin.cooling_production, domain, range_color,
                             "Cooling production", app_constants.ALTAIR_BASE_COLORS[7])
    if 'level_high' in heat_pump_df.columns:
        df = add_to_df_and_lists(df, heat_pump_df['level_high'], domain, range_color,
                                 "HP high heat production", app_constants.ALTAIR_BASE_COLORS[8])
    if 'level_low' in heat_pump_df.columns:
        df = add_to_df_and_lists(df, heat_pump_df['level_low'], domain, range_color,
                                 "HP low heat production", app_constants.ALTAIR_BASE_COLORS[9])
    if 'level_cool' in heat_pump_df.columns:
        df = add_to_df_and_lists(df, heat_pump_df['level_cool'], domain, range_color,
                                 "HP cooling production", app_constants.ALTAIR_BASE_COLORS[10])
    return altair_line_chart(df, domain, range_color, [], "Energy [kWh]",
                             "Energy production/consumption for " + agent_chosen_guid)


def add_to_df_and_lists(df: pd.DataFrame, series: pd.Series, domain: List[str], range_color: List[str], var_name: str,
                        color: str):
    if series is not None:
        df = pd.concat((df, pd.DataFrame({'period': series.index,
                                          'value': series.values,
                                          'variable': var_name})))
        if (df.value != 0).any():
            domain.append(var_name)
            range_color.append(color)
    return df


def construct_traded_amount_by_agent_chart(agent_chosen_guid: str,
                                           agent_trade_df: pd.DataFrame,
                                           freq: str) -> alt.Chart:
    """
    Plot amount of electricity and heating sold and bought.
    @param agent_chosen_guid: Name of chosen agent
    @param agent_trade_df: All trades by agent
    @param freq: Frequency to aggregate on. Will be passed to pd.Grouper. For example 'H' or 'h', 'M', 'D'
    @return: Altair chart with plot of sold and bought resources
    """

    df = pd.DataFrame()

    domain = []
    range_color = []
    plot_lst: List[dict] = []
    col_counter = 0
    for resource in [Resource.ELECTRICITY, Resource.HIGH_TEMP_HEAT, Resource.LOW_TEMP_HEAT, Resource.COOLING]:
        plot_lst.append({'title': 'Amount of {} bought'.format(resource.get_display_name()),
                         'color_num': col_counter, 'resource': resource, 'action': Action.BUY})
        plot_lst.append({'title': 'Amount of {} sold'.format(resource.get_display_name()),
                         'color_num': col_counter + 1, 'resource': resource, 'action': Action.SELL})
        col_counter = col_counter + 2

    for elem in plot_lst:
        mask = (agent_trade_df.resource.values == elem['resource'].name) \
            & (agent_trade_df.action.values == elem['action'].name)
        if not agent_trade_df.loc[mask].empty:
            # Can have multiple trades for the same period (for grid agents in the no-LEC case)
            quantity_series = agent_trade_df.loc[mask]['quantity_pre_loss']
            summed_quantities = quantity_series.groupby(pd.Grouper(freq=freq)).sum()
            df = pd.concat((df, pd.DataFrame({'period': summed_quantities.index,
                                              'value': summed_quantities.values,
                                              'variable': elem['title']})))

            domain.append(elem['title'])
            range_color.append(app_constants.ALTAIR_BASE_COLORS[elem['color_num']])

    for elem in plot_lst:
        # Adding zeros for missing timestamps
        missing_timestamps = pd.unique(df.loc[~df.period.isin(df[df.variable == elem['title']].period)].period)
        df = pd.concat((df, pd.DataFrame({'period': missing_timestamps,
                                          'value': 0.0,
                                          'variable': elem['title']})))

    return altair_line_chart(df, domain, range_color, [], "Energy [kWh]",
                             'Energy traded for ' + agent_chosen_guid)


def construct_price_chart(prices_df: pd.DataFrame, resource: Resource) -> alt.Chart:
    data_to_use = prices_df.loc[prices_df['Resource'] == resource].drop('Resource', axis=1)
    domain = [app_constants.RETAIL_PRICE_STR, app_constants.WHOLESALE_PRICE_STR]
    range_color = ['green', 'red']
    return altair_line_chart(data_to_use, domain, range_color, [], "Price [SEK]", "Price over Time")


def construct_storage_level_chart(storage_level_dfs: Dict[TradeMetadataKey, pd.DataFrame]) -> alt.Chart:
    df = pd.DataFrame()
    domain = []
    range_color = []

    titles = {TradeMetadataKey.SHALLOW_STORAGE_REL: 'BITES shallow storage',
              TradeMetadataKey.DEEP_STORAGE_REL: 'BITES deep storage',
              TradeMetadataKey.BATTERY_LEVEL: 'Battery charging level',
              TradeMetadataKey.ACC_TANK_LEVEL: 'Accumulator tank charging level'}
    colors = {TradeMetadataKey.BATTERY_LEVEL: app_constants.ALTAIR_BASE_COLORS[0],
              TradeMetadataKey.SHALLOW_STORAGE_REL: app_constants.ALTAIR_BASE_COLORS[1],
              TradeMetadataKey.DEEP_STORAGE_REL: app_constants.ALTAIR_BASE_COLORS[2],
              TradeMetadataKey.ACC_TANK_LEVEL: app_constants.ALTAIR_BASE_COLORS[3]}

    for (tmk, sub_df) in storage_level_dfs.items():
        df = pd.concat((df, pd.DataFrame({'period': sub_df['level'].index,
                                          'value': sub_df['level'],
                                          'variable': titles[tmk]})))
        domain.append(titles[tmk])
        range_color.append(colors[tmk])

    chart = altair_line_chart(df, domain, range_color, [], "% of capacity used",
                              "Charging level")
    chart.encoding.y.axis = alt.Axis(format='%')
    chart.encoding.tooltip[2].format = '.2%'
    return chart


def construct_bites_chart(bites_dfs: Dict[TradeMetadataKey, pd.DataFrame]) -> alt.Chart:
    df = pd.DataFrame()
    domain = []
    range_color = []

    titles = {TradeMetadataKey.SHALLOW_STORAGE_ABS: 'Shallow storage',
              TradeMetadataKey.DEEP_STORAGE_ABS: 'Deep storage',
              TradeMetadataKey.SHALLOW_CHARGE: 'Shallow charge (+) or discharge (-)',
              TradeMetadataKey.FLOW_SHALLOW_TO_DEEP: 'Deep charge (+) or discharge (-)',
              TradeMetadataKey.SHALLOW_LOSS: 'Shallow storage loss',
              TradeMetadataKey.DEEP_LOSS: 'Deep storage loss'}
    colors = {TradeMetadataKey.SHALLOW_STORAGE_ABS: app_constants.ALTAIR_BASE_COLORS[0],
              TradeMetadataKey.DEEP_STORAGE_ABS: app_constants.ALTAIR_BASE_COLORS[1],
              TradeMetadataKey.SHALLOW_CHARGE: app_constants.ALTAIR_BASE_COLORS[2],
              TradeMetadataKey.FLOW_SHALLOW_TO_DEEP: app_constants.ALTAIR_BASE_COLORS[3],
              TradeMetadataKey.SHALLOW_LOSS: app_constants.ALTAIR_BASE_COLORS[4],
              TradeMetadataKey.DEEP_LOSS: app_constants.ALTAIR_BASE_COLORS[5]}

    for (tmk, sub_df) in bites_dfs.items():
        df = pd.concat((df, pd.DataFrame({'period': sub_df['level'].index,
                                          'value': sub_df['level'],
                                          'variable': titles[tmk]})))
        domain.append(titles[tmk])
        range_color.append(colors[tmk])

    chart = altair_line_chart(df, domain, range_color, [], "Heating [kWh]",
                              "Building inertia as thermal energy storage")
    return chart

    
def construct_avg_day_elec_chart(elec_use_df: pd.DataFrame, period: tuple) -> alt.Chart:
    """
    Creates a chart of average monthly electricity use with points and error bars.
    The points are colored by the weekday.
    """

    title_str = "Average hourly net electricity consumed from " + period[0] + " to " + period[1]
    var_title_str = "Average of net electricity consumed [kWh]"
    domain = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    range_color = app_constants.ALTAIR_BASE_COLORS[:len(domain)]

    alt_title = alt.TitleParams(title_str, anchor='middle')
    selection = alt.selection_multi(fields=['weekday'], bind='legend')

    elec_use_df['ymin'] = elec_use_df['mean_total_elec'] - elec_use_df['std_total_elec']
    elec_use_df['ymax'] = elec_use_df['mean_total_elec'] + elec_use_df['std_total_elec']

    base = alt.Chart(elec_use_df, title=alt_title)

    points = base.mark_point(filled=True, size=80).encode(
        x=alt.X('hour', axis=alt.Axis(title='Hour')),
        y=alt.Y('mean_total_elec:Q', axis=alt.Axis(title=var_title_str, format='.2f'), scale=alt.Scale(zero=False)),
        color=alt.Color('weekday', scale=alt.Scale(domain=domain, range=range_color)),
        opacity=alt.condition(selection, alt.value(0.7), alt.value(0.0))
    )

    error_bars = base.mark_rule(strokeWidth=2).encode(
        x='hour',
        y='ymin:Q',
        y2='ymax:Q',
        color=alt.Color('weekday', scale=alt.Scale(domain=domain, range=range_color)),
        opacity=alt.condition(selection, alt.value(0.8), alt.value(0.0))
    )

    combined_chart = points + error_bars

    return combined_chart.add_selection(selection).interactive(bind_y=False)


def construct_reservoir_chart(job_id: str, tmk: TradeMetadataKey, resource_name: str) -> Optional[alt.Chart]:
    df = db_to_viewable_level_df(job_id, tmk.name)
    if len(df.index) == 0:
        return None
    df = df.reset_index().rename(columns={'index': 'period', 'level': 'value'})
    name = 'Unused ' + resource_name.lower()
    df['variable'] = name
    return altair_line_chart(df, [name], [app_constants.ALTAIR_BASE_COLORS[0]], [],
                             resource_name + ' [kWh]', name, legend=False)


def construct_cooling_machine_chart(job_id: str) -> Optional[alt.Chart]:
    df_cool = db_to_viewable_level_df(job_id, TradeMetadataKey.CM_COOL_PROD.name)
    if len(df_cool.index) == 0:
        return None
    df_cool = df_cool.reset_index().rename(columns={'index': 'period', 'level': 'value'})
    cooling_produced = 'Cooling produced'
    df_cool['variable'] = cooling_produced

    df_heat = db_to_viewable_level_df(job_id, TradeMetadataKey.CM_HEAT_PROD.name)
    df_heat = df_heat.reset_index().rename(columns={'index': 'period', 'level': 'value'})
    heat_produced = 'Low-temp heat produced'
    df_heat['variable'] = heat_produced

    df_elec = db_to_viewable_level_df(job_id, TradeMetadataKey.CM_ELEC_CONS.name)
    df_elec = df_elec.reset_index().rename(columns={'index': 'period', 'level': 'value'})
    elec_consumed = 'Electricity consumed'
    df_elec['variable'] = elec_consumed

    df = pd.concat((df_cool, df_heat, df_elec), axis=0)
    return altair_line_chart(df,
                             [cooling_produced, heat_produced, elec_consumed],
                             app_constants.ALTAIR_BASE_COLORS[:3],
                             [],
                             'Energy [kWh]',
                             'Centralized cooling machine')


def datetime_format_from_freq(freq: str) -> str:
    freq = freq.lower()
    if freq == 'h':
        return '%Y-%m-%d %H:%M'
    if freq == 'd':
        return '%Y-%m-%d'
    if freq == 'm':
        return '%Y-%m'
    raise ValueError('Unexpected freq: ' + freq)


def encoding_shorthand_from_freq(freq: str) -> str:
    freq = freq.lower()
    if freq == 'h':
        return 'period:T'
    if freq == 'd':
        return 'yearmonthdate(period):T'
    if freq == 'm':
        return 'month(period):T'
    raise ValueError('Unexpected freq: ' + freq)
