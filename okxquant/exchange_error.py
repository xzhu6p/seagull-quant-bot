"""
交易所 API 通用异常（多平台统一）。

SwapTrader 只捕获 ExchangeApiError，不依赖任何具体交易所的异常类。
每个平台的客户端把自己的异常转成这个，或继承它。
"""
from __future__ import annotations

from typing import Any, Optional


class ExchangeApiError(Exception):
    """通用交易所 API 业务错误。"""

    def __init__(self, code: str, msg: str, data: Any = None):
        self.code = code
        self.msg = msg
        self.data = data
        super().__init__(f"Exchange API error {code}: {msg}")
