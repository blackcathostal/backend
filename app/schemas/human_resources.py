from pydantic import BaseModel, Field


class HrPeriodAmountLine(BaseModel):
    accounting_account_id: int
    code: str = ""
    name: str = ""
    amount: int = 0


class HrPeriodOut(BaseModel):
    period: str
    lines: list[HrPeriodAmountLine] = Field(default_factory=list)


class HrPeriodAmountIn(BaseModel):
    accounting_account_id: int
    amount: int = 0


class HrPeriodUpdate(BaseModel):
    period: str = Field(min_length=4, max_length=40)
    lines: list[HrPeriodAmountIn] = Field(default_factory=list)
