import datetime
from enum import Enum
from typing import Iterable

from tradingplatformpoc.bid import Action, Resource, action_string, resource_string


class Market(Enum):
    LOCAL = 0
    EXTERNAL = 1


class Trade:
    """The Trade class is used to keep track of energy trades that have taken place

    Parameters:
        action: Buy/sell
        resource: Electricity
        quantity: Amount in kWh
        price: SEK/kWh
        source: String specifying which entity that did the trade (used for debugging)
        by_external: True if trade is made by an external grid agent, False otherwise. Needed for example when
            calculating extra cost distribution in balance_manager
        market: LOCAL or EXTERNAL (agents can decide to buy/sell directly to external grids)
        period: What period the trade happened
    """

    action: Action
    resource: Resource
    quantity: float
    price: float
    source: str
    by_external: bool
    market: Market
    period: datetime.datetime

    def __init__(self, action: Action, resource: Resource, quantity: float, price: float, source: str,
                 by_external: bool, market: Market, period: datetime.datetime):
        if quantity <= 0:
            raise RuntimeError('Trade must have quantity > 0, but was ' + str(quantity))
        self.action = action
        self.resource = resource
        self.quantity = quantity
        self.price = price
        self.source = source
        self.by_external = by_external
        self.market = market
        self.period = period

    def with_modified_price(self, new_price: float):
        return Trade(self.action, self.resource, self.quantity, new_price, self.source, self.by_external, self.market,
                     self.period)

    def __str__(self):
        return "{},{},{},{},{},{},{},{}".format(self.period,
                                                self.source,
                                                self.by_external,
                                                action_string(self.action),
                                                resource_string(self.resource),
                                                market_string(self.market),
                                                self.quantity,
                                                self.price)

    def get_cost_of_trade(self) -> float:
        """Negative if it is an income, i.e. if the trade is a SELL"""
        if self.action == Action.BUY:
            return self.quantity * self.price
        else:
            return -self.quantity * self.price


def market_string(market: Market) -> str:
    return "LOCAL" if market == Market.LOCAL else "EXTERNAL"


def write_rows(trades: Iterable[Trade]) -> str:
    full_string = ""
    for trade in trades:
        full_string = full_string + str(trade) + "\n"
    return full_string
