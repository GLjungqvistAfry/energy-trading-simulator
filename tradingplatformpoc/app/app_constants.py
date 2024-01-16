from pkg_resources import resource_filename

from tradingplatformpoc import trading_platform_utils

WHOLESALE_PRICE_STR = 'Wholesale price'
RETAIL_PRICE_STR = 'Retail price'
LOCAL_PRICE_STR = 'Local price'
DATA_PATH = "tradingplatformpoc.data"

ELEC_CONS = "Electricity consumption"
ELEC_PROD = "Electricity production"
HEAT_CONS = "Heat consumption"
HEAT_PROD = "Heat production"

ALTAIR_BASE_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                      "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
ALTAIR_DARK_COLORS = ["#144d75", "#ab5509", "#175217", "#691414"]
ALTAIR_STROKE_DASH = [[2, 1], [8, 8], [0, 0], [2, 4], [1, 0], [1, 1], [4, 4], [8, 4]]

HEAT_PUMP_CHART_COLOR = 'gray'
BATTERY_CHART_COLOR = 'gray'

CURRENT_CONFIG_FILENAME = resource_filename("tradingplatformpoc.config", "current_config.json")
LAST_SIMULATION_RESULTS = resource_filename("tradingplatformpoc.data", "last_simulation_results.pbz2")

DEFAULT_CONFIG_NAME = "default"

CONFIG_ID_MAX_LENGTH = 20
CONFIG_DESCRIPTION_MAX_LENGTH = 100
