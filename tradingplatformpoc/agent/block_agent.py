import datetime
import logging
from typing import Optional

from tradingplatformpoc.agent.iagent import IAgent
from tradingplatformpoc.constants import ACC_TANK_TEMPERATURE
from tradingplatformpoc.digitaltwin.battery import Battery
from tradingplatformpoc.digitaltwin.static_digital_twin import StaticDigitalTwin
from tradingplatformpoc.market.trade import Resource
from tradingplatformpoc.trading_platform_utils import energy_to_water_volume

logger = logging.getLogger(__name__)


class BlockAgent(IAgent):

    digital_twin: StaticDigitalTwin
    battery: Battery
    heat_pump_max_input: float
    heat_pump_max_output: float
    booster_pump_max_input: float
    booster_pump_max_output: float
    acc_tank_volume: float
    frac_for_bites: float

    def __init__(self, digital_twin: StaticDigitalTwin, heat_pump_max_input: float = 0, heat_pump_max_output: float = 0,
                 booster_pump_max_input: float = 0, booster_pump_max_output: float = 0, acc_tank_capacity: float = 0,
                 frac_for_bites: float = 0, battery: Optional[Battery] = None, guid: str = "BlockAgent"):
        super().__init__(guid)
        self.digital_twin = digital_twin
        self.heat_pump_max_input = heat_pump_max_input
        self.heat_pump_max_output = heat_pump_max_output
        self.booster_pump_max_input = booster_pump_max_input
        self.booster_pump_max_output = booster_pump_max_output
        self.acc_tank_volume = energy_to_water_volume(acc_tank_capacity, ACC_TANK_TEMPERATURE)
        self.frac_for_bites = frac_for_bites
        self.battery = Battery(0, 0, 0, 0) if battery is None else battery

    def get_actual_usage_for_resource(self, period: datetime.datetime, resource: Resource) -> float:
        actual_consumption = self.digital_twin.get_consumption(period, resource)
        actual_production = self.digital_twin.get_production(period, resource)
        return actual_consumption - actual_production
