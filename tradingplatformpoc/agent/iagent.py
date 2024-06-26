import datetime
from abc import ABC, abstractmethod
from typing import Dict

from tradingplatformpoc.market.trade import Resource


class IAgent(ABC):
    """Interface for agents to implement"""

    guid: str

    def __init__(self, guid: str):
        self.guid = guid

    @abstractmethod
    def get_actual_usage_for_resource(self, period: datetime.datetime, resource: Resource) -> float:
        # Return actual usage/supply for the trading horizon, and the specified resource
        # If negative, it means the agent was a net-producer for the trading period
        pass

    def get_actual_usage(self, period: datetime.datetime) -> Dict[Resource, float]:
        # If negative, it means the agent was a net-producer for the trading period
        return {res: self.get_actual_usage_for_resource(period, res) for res in Resource}
