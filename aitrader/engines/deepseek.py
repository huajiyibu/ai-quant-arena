"""DeepSeek 决策引擎。

- 构造完整行情提示词 → 调用 API → 解析 JSON → 输出决策
- 网络/解析异常由上层捕获并降级（对应 HLD §6）
- HTTP 客户端可注入，便于测试 mock
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from ..features import compute_features
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
        temperature: float = 0.3,
        system_prompt_extra: str = "",
        feature_inject: bool = False,
        market_env_inject: bool = False,
        feedback_n: int = 0,
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
        self.temperature = temperature
        self.system_prompt_extra = system_prompt_extra
        self.feature_inject = feature_inject
        self.market_env_inject = market_env_inject
        self.feedback_n = feedback_n  # PP-6：历史盈亏反馈笔数（0=关闭）
        # 响应缓存：以 (model, prompt) 为键（prompt 已完整编码决策输入；
        # ai 与 ai_policy 提示词相同时共享缓存，避免回测重复计费）
        self._cache = response_cache
        # N-8：进程内 API 调用/缓存命中计数（写 last_run 供成本记账）
        self.api_calls: int = 0
        self.cache_hits: int = 0

    def decide(self, ctx: DecisionContext) -> EngineResult:
        """生成决策；异常向上抛出由 batch 统一降级"""
        prompt = self._build_prompt(ctx)
        raw_output = self._call(prompt)
        decisions = self._parse(raw_output)
        decisions = self._validate(decisions, ctx)
        return EngineResult(decisions=decisions, prompt=prompt, raw_output=raw_output)

    # ------------------------------------------------------------------
    def _system_prompt(self) -> str:
        """三段式 system：角色与目标 / 决策框架 / 输出契约（PP-3）。"""
        base = (
            "你是 AI Quant Arena 的量化决策助手，在虚拟资金上做客观评测。\n"
            "目标：追求长期正期望，不是每次都对；信号不明确就 hold，宁缺毋滥。\n"
            "决策框架：先看趋势与动量，再看波动率风险，最后决定是否动仓；"
            "只对高置信信号买卖，不追高、不接飞刀、不用杠杆。\n"
            "输出契约：只输出 JSON 决策；buy 必须给出明确理由，reason 以标签开头"
            "（标签限：趋势/回调/政策/超买/超卖/其他，如 \"[趋势] 放量突破20日线\"）。"
        )
        if self.system_prompt_extra:
            return f"{base}\n{self.system_prompt_extra}"
        return base

    def _feature_bars(self, ctx: DecisionContext, symbol: str) -> list:
        """特征计算专用 K 线（N-1）：优先复权 bars（无除权跳空），缺失/为空回退原始。

        回测路径 ctx.adjusted_bars 为 None → 用 ctx.bars（回测全链路 hfq，bars 即复权价）。
        """
        if ctx.adjusted_bars is not None:
            adj = ctx.adjusted_bars.get(symbol)
            if adj:
                return adj
        return ctx.bars.get(symbol, [])

    def _format_market_env(self, ctx: DecisionContext) -> str:
        """市场温度计：用基准标的小特征拼一行市况（B-3，默认关）。"""
        if not ctx.bars:
            return ""
        sym = next(iter(ctx.bars))
        bars = self._feature_bars(ctx, sym)
        if not bars:
            return ""
        f = compute_features(bars)
        if not f:
            return ""
        name = ctx.symbol_names.get(sym, sym)
        parts = []
        if "pct_from_high20" in f:
            parts.append(f"距20日高点{f['pct_from_high20']:+.1%}")
        if "ret_20d" in f:
            parts.append(f"20日涨{f['ret_20d']:+.1%}")
        if "vol_20d" in f:
            parts.append(f"波动率{f['vol_20d']:.1%}")
        if "rsi14" in f:
            parts.append(f"RSI{f['rsi14']:.0f}")
        return f"市场({name}): " + " ".join(parts)

    def _format_features(self, f: dict) -> str:
        """把特征字典拼成一行提示词文本（PP-2）。字段缺失时优雅省略。"""
        parts = []
        for k, label in (("ma5", "ma5"), ("ma20", "ma20")):
            if k in f:
                parts.append(f"{label}={f[k]:.4g}")
        if "ret_5d" in f:
            parts.append(f"ret5={f['ret_5d']:+.2%}")
        if "ret_20d" in f:
            parts.append(f"ret20={f['ret_20d']:+.2%}")
        if "vol_20d" in f:
            parts.append(f"vol20={f['vol_20d']:.2%}")
        if "rsi14" in f:
            parts.append(f"rsi14={f['rsi14']:.0f}")
        if "pct_from_high20" in f:
            parts.append(f"距高={f['pct_from_high20']:+.2%}")
        if "volume_ratio" in f:
            parts.append(f"量比={f['volume_ratio']:.2f}")
        return "  特征: " + " ".join(parts)

    def _build_prompt(self, ctx: DecisionContext) -> str:
        """把行情、持仓、现金整理成给模型的提示词"""
        lines: list[str] = [
            f"今天是{ctx.date:%Y-%m-%d}，A股已收盘。各标的最近{self.lookback}个交易日收盘价："
        ]
        # B-3：市场环境（让模型感知当前市况，默认关）
        if self.market_env_inject:
            env = self._format_market_env(ctx)
            if env:
                lines.insert(0, env)
        for symbol, bars in ctx.bars.items():
            name = ctx.symbol_names.get(symbol, symbol)
            closes = " ".join(f"{b.close:.3f}" for b in bars[-self.lookback:])
            lines.append(f"{symbol}({name}): {closes}")
            # PP-2：注入确定性技术特征（替代模型心算；N-1 用复权 bars 避免除权跳空失真）
            if self.feature_inject:
                f = compute_features(self._feature_bars(ctx, symbol))
                if f:
                    lines.append(self._format_features(f))

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

        # PP-6：历史盈亏反馈（近 N 笔已平仓交易复盘；只含实际成交，无前视）
        if self.feedback_n > 0 and ctx.recent_closed_trades:
            lines.append("近期已平仓交易（复盘参考，从结果学习，避免重复犯错）：")
            for p in ctx.recent_closed_trades[: self.feedback_n]:
                lines.append(
                    f"  {p['symbol']} 买{p['buy_date']}@{p['buy_price']:.3f} "
                    f"卖{p['sell_date']}@{p['sell_price']:.3f} "
                    f"盈亏{p['pnl_pct']:+.1%} 当时理由:{str(p['reason'])[:24]}"
                )

        if self.include_policy and ctx.policy_text:
            lines.append(
                "今日宏观政策/要闻（供参考：请甄别其对市场的真实影响，"
                "警惕利好出尽、预期差、时滞，不要仅凭单条消息追涨杀跌）:"
            )
            lines.append(ctx.policy_text)

        lines.append(
            "请基于以上信息决定今日收盘后的操作，严格按如下 JSON 输出（只输出 JSON，不要解释）：\n"
            '{"decisions":[{"symbol":"510300","action":"buy","amount":50000,'
            '"confidence":0.7,"reason":"一句话理由"}]}\n'
            "规则: 1) action 仅限 buy/sell/hold; 2) buy 必带 amount(元), sell=清仓该标的; "
            f"3) 最多对{self.max_buy_count}个标的下buy; "
            "4) confidence 是 0~1 的数值，表示你对这个信号带来正收益的信心（不是市场必然性），"
            "信号足够明确时才给高 confidence；5) 谨慎、保守、不追高、不接飞刀；"
            "6) reason 以 [趋势]/[回调]/[政策]/[超买]/[超卖]/[其他] 之一开头，再接一句话理由。"
        )
        return "\n".join(lines)

    def _call(self, prompt: str) -> str:
        import hashlib
        from ..util import retry_call

        system = self._system_prompt()
        # 缓存键含 model/temperature/system/prompt：改 system 或温度不会误用旧缓存（PP-3）
        key = hashlib.md5(
            f"{self.model}|{self.temperature}|{system}|{prompt}".encode("utf-8")
        ).hexdigest()
        if self._cache is not None and key in self._cache:
            self.cache_hits += 1  # N-8
            return self._cache[key]

        self.api_calls += 1  # N-8：缓存未命中才计一次 API 会话（重试同属一次）
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.temperature,
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
        try:
            confidence = self._to_float(item.get("confidence", 0.5))
        except ValueError:
            confidence = 0.5  # 缺失/非数字置信度 → 默认 0.5
        return Decision(
            symbol=str(item.get("symbol", "")).strip(),
            action=action,
            amount=self._to_float(item.get("amount", 0)),
            reason=str(item.get("reason", "")),
            confidence=confidence,
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
                # P1-2：已持仓再下 buy → 标记（与 execute 的 risk_rejected:已持仓 口径一致）
                if d.symbol in ctx.account.positions:
                    d.valid = False
                    d.validation = "already_holding"
                    continue
                if not (0.0 <= d.confidence <= 1.0):
                    d.valid = False
                    d.validation = "invalid_confidence"
                    continue
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
