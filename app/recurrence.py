"""重复规则(RRULE)解析、校验与表单互转。

第一版支持:不重复、每天、每周、每月、每年,以及可选的截止日期(UNTIL)。
复杂例外(单次修改、COUNT 展开)不在第一版范围内,但 API 允许传入标准 RRULE 字符串。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from icalendar import vRecur

from app.timeutil import as_utc, parse_date, parse_iso

ALLOWED_FREQ = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")

FREQ_TO_SIMPLE = {
    "DAILY": "daily",
    "WEEKLY": "weekly",
    "MONTHLY": "monthly",
    "YEARLY": "yearly",
}
SIMPLE_TO_FREQ = {value: key for key, value in FREQ_TO_SIMPLE.items()}

SIMPLE_LABELS = {
    "none": "不重复",
    "daily": "每天",
    "weekly": "每周",
    "monthly": "每月",
    "yearly": "每年",
}


class RRuleError(ValueError):
    """RRULE 校验失败。"""


@dataclass(frozen=True)
class ParsedRRule:
    raw: str
    freq: str
    until: datetime | None
    until_is_date: bool = False

    @property
    def simple(self) -> str:
        return FREQ_TO_SIMPLE.get(self.freq, "none")


def rule_extras(parsed: ParsedRRule) -> list[str]:
    """除 FREQ/UNTIL 之外的原始参数,重建规则时按原样保留。"""
    return [
        chunk
        for chunk in parsed.raw.split(";")
        if chunk and not chunk.startswith(("FREQ=", "UNTIL="))
    ]


def rebuild_rule(parsed: ParsedRRule, until_text: str | None) -> str:
    chunks = [f"FREQ={parsed.freq}"]
    if until_text:
        chunks.append(f"UNTIL={until_text}")
    chunks.extend(rule_extras(parsed))
    return ";".join(chunks)


def strip_prefix(value: str) -> str:
    text = value.strip()
    if text.upper().startswith("RRULE:"):
        text = text[len("RRULE:") :]
    return text.strip()


def validate_rrule(value: str) -> ParsedRRule:
    """校验并规范化一个标准 RRULE 字符串。"""
    text = strip_prefix(value)
    if not text:
        raise RRuleError("重复规则不能为空")
    try:
        parsed = vRecur.from_ical(text)
    except Exception as exc:  # noqa: BLE001 - icalendar 抛出多种异常
        raise RRuleError(f"无法解析重复规则:{exc}") from exc

    if not parsed:
        raise RRuleError("重复规则不能为空")

    supported = {"FREQ", "UNTIL", "COUNT", "INTERVAL", "WKST", "BYDAY", "BYMONTHDAY", "BYMONTH"}
    unsupported = {str(key).upper() for key in parsed} - supported
    if unsupported:
        raise RRuleError(f"第一版不支持这些重复参数:{', '.join(sorted(unsupported))}")

    freq_values = parsed.get("FREQ")
    if not freq_values:
        raise RRuleError("重复规则缺少 FREQ")
    freq = str(freq_values[0]).upper()
    if freq not in ALLOWED_FREQ:
        raise RRuleError("仅支持每天、每周、每月、每年四种重复频率")

    if "UNTIL" in parsed and "COUNT" in parsed:
        raise RRuleError("UNTIL 与 COUNT 不能同时使用")

    until: datetime | None = None
    until_is_date = False
    raw_until: object | None = None
    if "UNTIL" in parsed:
        raw_until = parsed["UNTIL"][0]
        if isinstance(raw_until, datetime):
            until = as_utc(raw_until)
        elif isinstance(raw_until, date):
            # 纯日期形式的 UNTIL:语义是“截止到当地这一天的结束”
            until_is_date = True
            until = datetime(raw_until.year, raw_until.month, raw_until.day, 23, 59, 59)
        else:
            raise RRuleError("UNTIL 必须是日期或 UTC 时间")

    if until_is_date and isinstance(raw_until, date):
        normalized = rebuild_rule(
            ParsedRRule(raw="", freq=freq, until=until, until_is_date=True),
            format_until(raw_until),
        )
    else:
        normalized = rebuild_rule(
            ParsedRRule(raw="", freq=freq, until=until), format_until(until) if until else None
        )

    # 保留用户显式给出的 INTERVAL/BYDAY 等参数
    for key in ("INTERVAL", "COUNT", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"):
        if key in parsed:
            normalized += f";{key}={parsed[key][0]}"
    return ParsedRRule(raw=normalized, freq=freq, until=until, until_is_date=until_is_date)


def build_rrule(freq: str, until: datetime | date | None = None) -> str:
    freq = freq.upper()
    if freq not in ALLOWED_FREQ:
        raise RRuleError("仅支持每天、每周、每月、每年四种重复频率")
    rule = f"FREQ={freq}"
    if until is not None:
        rule += f";UNTIL={format_until(until)}"
    return rule


def format_until(value: datetime | date) -> str:
    if isinstance(value, datetime):
        utc = as_utc(value)
        assert utc is not None
        return utc.strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


def build_rrule_from_form(simple: str, until_text: str | None) -> str | None:
    """把表单里的“重复频率 + 截止日期”组装成 RRULE 字符串。

    截止日期是纯日期,因此生成的 UNTIL 也是 DATE 形式,表示“截止到当地这一天结束”。
    """
    simple = (simple or "none").strip().lower()
    if simple in ("", "none"):
        if until_text:
            raise RRuleError("未选择重复频率时不能设置截止日期")
        return None
    freq = SIMPLE_TO_FREQ.get(simple)
    if freq is None:
        raise RRuleError("不支持的重复频率")
    until: date | None = None
    if until_text:
        until = parse_date(until_text)
        if until is None:
            raise RRuleError("截止日期格式应为 YYYY-MM-DD")
    return build_rrule(freq, until)


def split_rrule(rule: str | None) -> tuple[str, str]:
    """把 RRULE 拆成表单需要的 (simple, until 文本)。"""
    if not rule:
        return "none", ""
    try:
        parsed = validate_rrule(rule)
    except RRuleError:
        return "none", ""
    simple = parsed.simple
    if parsed.until is None:
        return simple, ""
    return simple, parsed.until.strftime("%Y-%m-%d")


def describe_rrule(rule: str | None) -> str:
    if not rule:
        return "不重复"
    try:
        parsed = validate_rrule(rule)
    except RRuleError:
        return rule
    label = SIMPLE_LABELS.get(parsed.simple, parsed.simple)
    if parsed.until is not None:
        return f"{label}(截止 {parsed.until.strftime('%Y-%m-%d')})"
    return label


def parse_until_from_api(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed_iso = parse_iso(value)
    if parsed_iso is not None:
        return parsed_iso
    parsed_date = parse_date(value)
    if parsed_date is not None:
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, 23, 59, 59)
    raise RRuleError("截止时间格式无法识别")
