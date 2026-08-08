"""DeepSeek 决策引擎。

- 构造完整行情提示词 → 调用 API → 解析 JSON → 输出决策
- 网络/解析异常由上层捕获并降级（对应 HLD §6）
- HTTP 客户端可注入，便于测试 mock
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from ..models import Decision
from .base import DecisionContext, DecisionEngine, EngineResult


class HttpClient(Protocol):
    """可注入的 HTTP 客户端（requests.Session 满足该协议）"""

    def post(self, url: str, **kwargs: Any) -> Any: ...


class DeepSeekEngine(DecisionEngine):
    """基于 DeepSeek 大模型的决策引擎"""

    name: str = "ai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        http_client: HttpClient | None = None,
        lookback: int = 20,
        max_buy_count: int = 2,
        include_policy: bool = False,
        name: str = "ai",
        response_cache: dict[str, str] | None = None,
    ) -> None:
        import requests  # 延迟导入，便于测试时替换

        self.name = name
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.http: HttpClient = http_client or requests
        self.lookback = lookback
        self.max_buy_count = max_buy_count
        self.include_policy = include_policy
        # 响应缓存：以 (model, prompt) 为键（prompt 已完整编码决策输入；
        # ai 与 ai_policy 提示词相同时共享缓存，避免回测重复计费）
        self._cache = response_cache

    def decide(self, ctx: DecisionContext) -> EngineResult:
        """生成决策；异常向上抛出由 batch 统一降级"""
        prompt = self._build_prompt(ctx)
        raw_output = self._call(prompt)
        decisions = self._parse(raw_output)
        decisions = self._validate(decisions, ctx)
        return EngineResult(decisions=decisions, prompt=prompt, raw_output=raw_output)

    # ------------------------------------------------------------------
    def _build_prompt(self, ctx: DecisionContext) -> str:
        """把行情、持仓、现金整理成给模型的提示词"""
        lines: list[str] = [
            f"今天是{ctx.date:%Y-%m-%d}，A股已收盘。各标的最近{self.lookback}个交易日收盘价："
        ]
        for symbol, bars in ctx.bars.items():
            name = ctx.symbol_names.get(symbol, symbol)
            closes = " ".join(f"{b.close:.3f}" for b in bars[-self.lookback:])
            lines.append(f"{symbol}({name}): {closes}")

        lines.append(f"可用现金: {ctx.account.cash:,.0f} 元")
        if ctx.account.positions:
            pos_lines = []
            for sym, p in ctx.account.positions.items():
                pos_lines.append(
                    f"{sym}({p.name}) 持仓{p.volume}股 成本{p.cost_price:.3f} "
                    f"现价{p.last_price:.3f} 浮盈{p.unrealized_pnl:+,.0f}"
                )
            lines.append("当前持仓:\n" + "\n".join(pos_lines))
        else:
            lines.append("当前持仓: 空仓")

        if self.include_policy and ctx.policy_text:
            lines.append(
                "今日宏观政策/要闻（供参考：请甄别其对市场的真实影响，"
                "警惕利好出尽、预期差、时滞，不要仅凭单条消息追涨杀跌）:"
            )
            lines.append(ctx.policy_text)

        lines.append(
            "请基于以上信息决定今日收盘后的操作，严格按如下 JSON 输出（只输出 JSON，不要解释）：\n"
            '{"decisions":[{"symbol":"510300","action":"buy","amount":50000,"reason":"一句话理由"}]}\n'
            "规则: 1) action 仅限 buy/sell/hold; 2) buy 必带 amount(元), sell=清仓该标的; "
            f"3) 最多对{self.max_buy_count}个标的下buy; 4) 谨慎、保守、不追高、不接飞刀。"
        )
        return "\n".join(lines)

    def _call(self, prompt: str) -> str:
        import hashlib
        from ..util import retry_call

        key = hashlib.md5(f"{self.model}|{prompt}".encode("utf-8")).hexdigest()
        if self._cache is not None and key in self._cache:
            return self._cache[key]

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一名谨慎的量化交易助手，风格保守，基于行情做波段。",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
            "max_tokens": 800,
        }
        resp = retry_call(
            lambda: self.http.post(url, headers=headers, json=payload, timeout=90),
            label="DeepSeek API",
        )
        try:
            resp.raise_for_status()
        except Exception as exc:
            # 把响应文本拼进异常，供上层降级时留痕
            detail = getattr(resp, "text", "")[:200]
            raise RuntimeError(f"DeepSeek 状态异常: {exc}，响应: {detail}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"DeepSeek 响应非 JSON: {exc}，原始: {getattr(resp, 'text', '')[:200]}"
            ) from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"DeepSeek 响应结构异常: {exc}，原始: {str(data)[:200]}"
            ) from exc
        if self._cache is not None:
            self._cache[key] = content
        return content

    def _parse(self, content: str) -> list[Decision]:
        """解析模型 JSON 输出为决策列表；整体非 JSON 时抛 ValueError 触发降级。

        单条解析失败不拖垮整批：标 valid=False / validation=parse_error，其余正常解析。
        """
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("DeepSeek 返回格式非法: 非 JSON 对象")
        decisions: list[Decision] = []
        for item in data.get("decisions", []):
            try:
                decisions.append(self._parse_item(item))
            except (TypeError, ValueError, KeyError) as exc:
                decisions.append(
                    Decision(
                        symbol="",
                        action="hold",
                        reason=f"parse_error:{exc}",
                        valid=False,
                        validation="parse_error",
                    )
                )
        return decisions

    def _parse_item(self, item: dict) -> Decision:
        """解析单条决策；非法字段抛异常由调用方标记 parse_error"""
        action = str(item.get("action", "hold")).lower()
        if action not in ("buy", "sell", "hold"):
            action = "hold"
        return Decision(
            symbol=str(item.get("symbol", "")).strip(),
            action=action,
            amount=self._to_float(item.get("amount", 0)),
            reason=str(item.get("reason", "")),
        )

    @staticmethod
    def _to_float(value) -> float:
        """兼容数字与数字字符串（如 50000 / "50000" / "50,000"）；无法解析抛 ValueError"""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value.replace(",", "").strip())
        return 0.0

    def _validate(self, decisions: list[Decision], ctx: DecisionContext) -> list[Decision]:
        """语义校验：非法 symbol / 非法 amount / buy 数量超限等，标记 invalid 但保留留痕。

        无法区分"模型乱写"与"风控拒绝"是评测失真的根因，故校验结果写入 decisions.validation。
        """
        valid_symbols = set(ctx.symbol_names)
        buy_count = 0
        for d in decisions:
            # 所有决策（含 hold）都校验 symbol 是否在配置标的集合内
            if d.symbol not in valid_symbols:
                d.valid = False
                d.validation = f"invalid_symbol:{d.symbol}"
                continue
            if d.action == "hold":
                d.validation = "ok"
                continue
            if d.action == "buy":
                if d.amount <= 0:
                    d.valid = False
                    d.validation = "invalid_amount:<=0"
                    continue
                if d.amount >= ctx.account.total_assets:
                    d.valid = False
                    d.validation = "invalid_amount:>=total_assets"
                    continue
                buy_count += 1
                if buy_count > self.max_buy_count:
                    d.valid = False
                    d.validation = "too_many_buy"
                    continue
            else:  # sell
                if d.symbol not in ctx.account.positions:
                    d.valid = False
                    d.validation = "sell_without_position"
                    continue
            d.validation = "ok"
        return decisions
