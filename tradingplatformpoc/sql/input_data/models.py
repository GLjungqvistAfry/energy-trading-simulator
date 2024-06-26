import datetime

from sqlalchemy import Column, DateTime

from sqlmodel import Field, SQLModel


class InputData(SQLModel, table=True):
    __tablename__ = 'input_data'

    period: datetime.datetime = Field(
        title="Period",
        sa_column=Column(DateTime(timezone=True), primary_key=True, nullable=False)
    )
    irradiation: float = Field(
        primary_key=False,
        default=None,
        title='Solar irradiation, according to SMHI, in Watt per square meter.',
        nullable=False
    )
    temperature: float = Field(
        primary_key=False,
        default=None,
        title='Outdoor temperature, degrees Celsius.',
        nullable=False
    )
    rad_energy: float = Field(
        primary_key=False,
        default=None,
        title='Radiator heating energy consumption, in kW.',
        nullable=False
    )
    hw_energy: float = Field(
        primary_key=False,
        default=None,
        title='Hot water heating energy consumption, in kW.',
        nullable=False
    )
    coop_electricity_consumed: float = Field(
        primary_key=False,
        default=None,
        title='Coop electricity consumed (cooling and other), in kWh.',
        nullable=False
    )
    coop_hot_tap_water_consumed: float = Field(
        primary_key=False,
        default=None,
        title='Coop hot tap water consumed, in kWh.',
        nullable=False
    )
    coop_space_heating_consumed: float = Field(
        primary_key=False,
        default=None,
        title='Coop space heating demand, in kWh (after having covered as much as possible of this need with excess '
              'heat from their own cooling machines).',
        nullable=False
    )
    coop_space_heating_produced: float = Field(
        primary_key=False,
        default=None,
        title='Coop space heating produced, in kWh (i.e. what remains of the excess heat from cooling machines, after '
              'having covered their own space heating demand).',
        nullable=False
    )
    office_cooling: float = Field(
        primary_key=False,
        default=None,
        title='Office cooling consumption in kW(h).',
        nullable=False
    )
    office_space_heating: float = Field(
        primary_key=False,
        default=None,
        title='Office space heating consumption in kW(h).',
        nullable=False
    )
