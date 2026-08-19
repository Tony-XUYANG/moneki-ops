from __future__ import annotations

import json
import re
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .metrics import DateRange, MetricsService


MONTH_PATTERN = re.compile(r"(?:(20\d{2})年)?([一二三四五六七八九十]{1,3}|[1-9]|1[0-2])月")
CHINESE_MONTHS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


@dataclass
class ConversationState:
    intent: str | None = None
    product_id: str | None = None
    product_name: str | None = None
    period: DateRange | None = None


class ConversationStore:
    def __init__(self):
        self._states: dict[str, ConversationState] = {}

    def get(self, session_id: str | None) -> tuple[str, ConversationState]:
        identifier = session_id or str(uuid.uuid4())
        return identifier, self._states.setdefault(identifier, ConversationState())


class AiService:
    """Routes questions to audited tools; templates own every numeric claim."""

    def __init__(self, metrics: MetricsService, api_key: str = "", model: str = "deepseek-chat"):
        self.metrics = metrics
        self.api_key = api_key
        self.model = model
        self.conversations = ConversationStore()

    @property
    def provider(self) -> str:
        return "deepseek" if self.api_key else "deterministic-mock"

    def _period_from_text(self, message: str, previous: DateRange | None = None) -> DateRange | None:
        match = MONTH_PATTERN.search(message)
        if not match:
            return previous if any(token in message for token in ("那", "这个", "同期")) else None
        bounds = self.metrics.date_bounds()
        year = int(match.group(1) or bounds["max_date"][:4])
        month_text = match.group(2)
        month = CHINESE_MONTHS.get(month_text, int(month_text) if month_text.isdigit() else 0)
        start = date(year, month, 1)
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        end = date.fromordinal(next_month.toordinal() - 1)
        return DateRange(start.isoformat(), end.isoformat())

    def _deepseek_route(self, message: str, state: ConversationState) -> dict[str, Any] | None:
        if not self.api_key:
            return None
        prompt = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You only route Chinese restaurant analytics questions. Return JSON with intent, "
                        "product_name, year, month. Allowed intents: store_category_revenue, "
                        "product_revenue, aov_trend, unsupported. Never answer or invent a number."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message,
                            "context": {
                                "intent": state.intent,
                                "product_name": state.product_name,
                                "period": state.period.__dict__ if state.period else None,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(prompt, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            return json.loads(data["choices"][0]["message"]["content"])
        except Exception:
            return None

    def _route(self, message: str, state: ConversationState) -> dict[str, Any]:
        model_route = self._deepseek_route(message, state)
        if model_route:
            return model_route
        compact = message.casefold().replace(" ", "")
        if any(word in compact for word in ("客单价", "客单", "涨", "跌")):
            return {"intent": "aov_trend"}
        if any(word in compact for word in ("门店营业额", "门店品类", "哪个品类", "门店类别")):
            return {"intent": "store_category_revenue"}
        product_matches = [
            product for product in self.metrics.products()
            if product["product_name"].casefold().replace(" ", "") in compact
        ]
        if product_matches:
            return {"intent": "product_revenue", "product_name": product_matches[0]["product_name"]}
        if state.intent == "product_revenue" and state.product_name and any(
            token in compact for token in ("那", "五月", "六月", "七月")
        ):
            return {"intent": "product_revenue", "product_name": state.product_name}
        return {"intent": "unsupported"}

    def ask(self, message: str, session_id: str | None = None) -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise ValueError("message cannot be empty")
        identifier, state = self.conversations.get(session_id)
        route = self._route(text, state)
        intent = route.get("intent", "unsupported")
        bounds = self.metrics.date_bounds()
        full_period = DateRange(bounds["min_date"], bounds["max_date"])
        requested_period = self._period_from_text(text, state.period)

        if intent == "store_category_revenue":
            period = requested_period or full_period
            rows = self.metrics.store_category_revenue(period)
            if not rows:
                return self._empty(identifier, text, "该时间范围内没有可用销售数据。")
            winner = rows[0]
            state.intent, state.period = intent, period
            return {
                "session_id": identifier,
                "provider": self.provider,
                "status": "answered",
                "answer": (
                    f"{period.start} 至 {period.end}，营业额最高的门店品类是“{winner['category']}”，"
                    f"营业额为 ¥{winner['revenue']}，共 {winner['order_count']} 单。"
                ),
                "tool_call": {"name": "query_store_category_revenue", "arguments": period.__dict__},
                "evidence": {"metric": "revenue", "value": winner["revenue"], "unit": "CNY", "rows": rows},
                "chart_action": {"start_date": period.start, "end_date": period.end, "store_ids": [], "view": "stores"},
            }

        if intent == "product_revenue":
            product_name = route.get("product_name") or state.product_name or ""
            matches = self.metrics.find_product(product_name)
            if not matches:
                return self._empty(
                    identifier,
                    text,
                    f"数据中没有找到“{product_name or '该商品'}”。请使用商品表中的名称提问。",
                    tool="find_product",
                )
            if len(matches) > 1:
                names = "、".join(item["product_name"] for item in matches)
                return self._empty(identifier, text, f"找到多个相近商品：{names}。请指定一个。", tool="find_product")
            product = matches[0]
            period = requested_period or state.period or full_period
            result = self.metrics.product_revenue(product["product_id"], period)
            state.intent, state.product_id, state.product_name, state.period = (
                intent,
                product["product_id"],
                product["product_name"],
                period,
            )
            return {
                "session_id": identifier,
                "provider": self.provider,
                "status": "answered",
                "answer": (
                    f"{period.start} 至 {period.end}，{result['product_name']}营业额为 ¥{result['revenue']}，"
                    f"售出 {result['qty']} 份，涉及 {result['order_count']} 单。"
                ),
                "tool_call": {
                    "name": "query_product_revenue",
                    "arguments": {"product_id": product["product_id"], **period.__dict__},
                },
                "evidence": {"metric": "product_revenue", "value": result["revenue"], "unit": "CNY", "row": result},
                "chart_action": {"start_date": period.start, "end_date": period.end, "store_ids": [], "view": "products"},
            }

        if intent == "aov_trend":
            rows = self.metrics.monthly_aov(months=3)
            previous, latest = rows[-2], rows[-1]
            delta = Decimal(latest["aov"]) - Decimal(previous["aov"])
            direction = "上涨" if delta > 0 else "下跌" if delta < 0 else "持平"
            pct = (abs(delta) / Decimal(previous["aov"]) * 100).quantize(Decimal("0.01")) if Decimal(previous["aov"]) else Decimal(0)
            period = DateRange(rows[0]["start"], rows[-1]["end"])
            state.intent, state.period = intent, period
            return {
                "session_id": identifier,
                "provider": self.provider,
                "status": "answered",
                "answer": (
                    f"最近一个完整数据月（{latest['month']}）客单价为 ¥{latest['aov']}，"
                    f"较 {previous['month']} 的 ¥{previous['aov']} {direction} ¥{abs(delta):.2f}（{pct}%）。"
                ),
                "tool_call": {"name": "query_monthly_aov", "arguments": {"months": 3}},
                "evidence": {"metric": "aov", "value": latest["aov"], "unit": "CNY/order", "rows": rows},
                "chart_action": {"start_date": period.start, "end_date": period.end, "store_ids": [], "view": "trend"},
            }

        return self._empty(
            identifier,
            text,
            "这个问题无法用现有的销售、门店和商品字段可靠回答。我可以查询营业额、订单数、客单价、门店品类或商品表现。",
        )

    def _empty(self, session_id: str, message: str, answer: str, tool: str | None = None) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "provider": self.provider,
            "status": "not_answerable",
            "answer": answer,
            "tool_call": {"name": tool, "arguments": {"message": message}} if tool else None,
            "evidence": None,
            "chart_action": None,
        }
