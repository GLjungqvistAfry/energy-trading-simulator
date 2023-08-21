import datetime

from pydantic.types import Optional

from sqlalchemy import Column, DateTime, Integer

from sqlmodel import Field, SQLModel


class ExactElectricityPrice(SQLModel, table=True):
    __tablename__ = 'exact_electricity_price'

    id: int = Field(
        title='Unique integer ID',
        sa_column=Column(Integer, autoincrement=True, primary_key=True, nullable=False)
    )
    job_id: str = Field(
        primary_key=False,
        default=None,
        title='Unique job ID',
        nullable=False
    )
    period: datetime.datetime = Field(
        primary_key=False,
        title="Period",
        nullable=False,
        sa_column=Column(DateTime(timezone=True))
    )
    retail_price: Optional[float] = Field(
        primary_key=False,
        default=None,
        title='Retail price',
        nullable=True
    )
    wholesale_price: Optional[float] = Field(
        primary_key=False,
        default=None,
        title='Wholesale price',
        nullable=True
    )