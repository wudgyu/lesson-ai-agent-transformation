"""价格表与成本计算。

成本不是“token 数 × 一个价格”。至少要区分：

- uncached input：正常输入价
- cache read：命中缓存的输入，通常远低于正常输入价
- cache write：写入缓存的输入，部分供应商收溢价
- output：输出价，reasoning token 通常也按 output 计费

价格按 `effective_from` 版本化：历史 trace 必须用当时生效的价格重算，
不能用今天的价格回填昨天的账。这里的价格是示意值，不是任何供应商报价。

Python >= 3.9，仅标准库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from trace_schema import Usage

MILLION = 1_000_000.0


class UnknownModelError(KeyError):
    """价格表里没有这个 model，或者当时还没有生效价格。"""


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float
    cache_write_per_million: float
    effective_from: str = "1970-01-01"

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "ModelPrice":
        price = ModelPrice(
            model=str(raw["model"]),
            input_per_million=float(raw["input_per_million"]),
            output_per_million=float(raw["output_per_million"]),
            cache_read_per_million=float(raw["cache_read_per_million"]),
            cache_write_per_million=float(raw["cache_write_per_million"]),
            effective_from=str(raw.get("effective_from", "1970-01-01")),
        )
        price.validate()
        return price

    def validate(self) -> None:
        values = {
            "input_per_million": self.input_per_million,
            "output_per_million": self.output_per_million,
            "cache_read_per_million": self.cache_read_per_million,
            "cache_write_per_million": self.cache_write_per_million,
        }
        for name, value in values.items():
            if value < 0:
                raise ValueError("%s: %s is negative" % (self.model, name))
        # 解析一次，确保格式合法。
        date.fromisoformat(self.effective_from)


@dataclass(frozen=True)
class Cost:
    input_uncached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_savings: float = 0.0

    @property
    def total(self) -> float:
        return self.input_cost + self.output_cost

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(
            input_uncached_tokens=self.input_uncached_tokens
            + other.input_uncached_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            input_cost=self.input_cost + other.input_cost,
            output_cost=self.output_cost + other.output_cost,
            cache_savings=self.cache_savings + other.cache_savings,
        )


class PriceBook:
    def __init__(self, prices: List[ModelPrice], currency: str = "USD") -> None:
        self.prices = list(prices)
        self.currency = currency
        for price in self.prices:
            price.validate()

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "PriceBook":
        prices = [ModelPrice.from_dict(item) for item in raw.get("prices", [])]
        return PriceBook(prices, str(raw.get("currency", "USD")))

    @staticmethod
    def from_path(path: Path) -> "PriceBook":
        with path.open("r", encoding="utf-8") as handle:
            return PriceBook.from_dict(json.load(handle))

    def price_for(self, model: str, at: Optional[str] = None) -> ModelPrice:
        """选择 `at` 当天生效的最近一条价格。

        `at` 为空表示“当前”，用于实时观测；历史 trace 应传 `started_at`。
        """
        when = date.fromisoformat(at) if at else date.today()
        candidates = [
            price
            for price in self.prices
            if price.model == model and date.fromisoformat(price.effective_from) <= when
        ]
        if not candidates:
            raise UnknownModelError(
                "no price for model %r effective on %s" % (model, when.isoformat())
            )
        candidates.sort(key=lambda price: price.effective_from)
        return candidates[-1]

    def cost(self, usage: Usage, model: str, at: Optional[str] = None) -> Cost:
        price = self.price_for(model, at)
        cache_read = usage.cache_read_input_tokens
        cache_write = usage.cache_write_input_tokens
        uncached = usage.input_tokens - cache_read - cache_write
        if uncached < 0:
            raise ValueError(
                "model %s: cache tokens exceed input_tokens" % model
            )

        input_cost = (
            uncached * price.input_per_million
            + cache_read * price.cache_read_per_million
            + cache_write * price.cache_write_per_million
        ) / MILLION
        output_cost = usage.output_tokens * price.output_per_million / MILLION

        # 相对于“所有 input 都按正常价计费”省下的钱。
        # cache write 若有溢价，会抵消一部分 savings。
        counterfactual_input = (
            usage.input_tokens * price.input_per_million
        ) / MILLION
        cache_savings = counterfactual_input - input_cost

        return Cost(
            input_uncached_tokens=uncached,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_tokens=usage.output_tokens,
            input_cost=input_cost,
            output_cost=output_cost,
            cache_savings=cache_savings,
        )
