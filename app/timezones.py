"""给用户看的时区清单,以及「序号 / 名称 → IANA 名」的解析。

背景:iOS 快捷指令与「转换时区」动作都拿不到设备的 IANA 时区名,只能交给用户
手填。为了让人不必记 `Australia/Brisbane` 这种字符串,这里给出一个**顺序固定**的
常用时区表:

- 每一项有 1 基的**序号**,设置页的默认时区下拉框就按这个序号展示(如 `1. 北京时间`);
- `event` 接口的 `timezone` 字段既能填 IANA 名,也能填序号(`"timezone": 1`)或
  中文名(`"timezone": "北京时间"`),统一由 `resolve_timezone` 解析成 IANA 名。

**序号一经发布不得调整**:外部快捷指令、脚本会把序号写死;只能在表尾追加新项。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import available_timezones

from app.timeutil import is_valid_timezone


@dataclass(frozen=True)
class TimezoneChoice:
    """一条可选时区。`index` 是对外暴露的 1 基序号。"""

    index: int
    iana: str
    label: str
    region: str
    aliases: tuple[str, ...] = ()


REGION_ORDER: tuple[str, ...] = ("亚洲", "大洋洲", "欧洲", "美洲", "非洲", "其他")

# 顺序即对外序号,只允许在末尾追加
TIMEZONE_CHOICES: tuple[TimezoneChoice, ...] = (
    TimezoneChoice(1, "Asia/Shanghai", "北京时间", "亚洲", ("北京", "中国", "上海")),
    TimezoneChoice(2, "Asia/Hong_Kong", "香港时间", "亚洲", ("香港",)),
    TimezoneChoice(3, "Asia/Taipei", "台北时间", "亚洲", ("台北", "台湾")),
    TimezoneChoice(4, "Asia/Tokyo", "东京时间", "亚洲", ("东京", "日本")),
    TimezoneChoice(5, "Asia/Seoul", "首尔时间", "亚洲", ("首尔", "韩国")),
    TimezoneChoice(6, "Asia/Singapore", "新加坡时间", "亚洲", ("新加坡",)),
    TimezoneChoice(7, "Asia/Bangkok", "曼谷时间", "亚洲", ("曼谷", "泰国")),
    TimezoneChoice(8, "Asia/Jakarta", "雅加达时间", "亚洲", ("雅加达", "印尼")),
    TimezoneChoice(9, "Asia/Kolkata", "新德里时间", "亚洲", ("新德里", "印度", "孟买")),
    TimezoneChoice(10, "Asia/Dubai", "迪拜时间", "亚洲", ("迪拜", "阿联酋")),
    TimezoneChoice(11, "Australia/Sydney", "悉尼时间", "大洋洲", ("悉尼",)),
    TimezoneChoice(12, "Australia/Brisbane", "布里斯班时间", "大洋洲", ("布里斯班",)),
    TimezoneChoice(13, "Australia/Melbourne", "墨尔本时间", "大洋洲", ("墨尔本",)),
    TimezoneChoice(14, "Australia/Adelaide", "阿德莱德时间", "大洋洲", ("阿德莱德",)),
    TimezoneChoice(15, "Australia/Perth", "珀斯时间", "大洋洲", ("珀斯",)),
    TimezoneChoice(16, "Pacific/Auckland", "奥克兰时间", "大洋洲", ("奥克兰", "新西兰")),
    TimezoneChoice(17, "Europe/London", "伦敦时间", "欧洲", ("伦敦", "英国")),
    TimezoneChoice(18, "Europe/Paris", "巴黎时间", "欧洲", ("巴黎", "法国")),
    TimezoneChoice(19, "Europe/Berlin", "柏林时间", "欧洲", ("柏林", "德国")),
    TimezoneChoice(20, "Europe/Madrid", "马德里时间", "欧洲", ("马德里", "西班牙")),
    TimezoneChoice(21, "Europe/Moscow", "莫斯科时间", "欧洲", ("莫斯科", "俄罗斯")),
    TimezoneChoice(22, "America/New_York", "纽约时间", "美洲", ("纽约", "美东")),
    TimezoneChoice(23, "America/Chicago", "芝加哥时间", "美洲", ("芝加哥", "美中")),
    TimezoneChoice(24, "America/Denver", "丹佛时间", "美洲", ("丹佛", "美山地")),
    TimezoneChoice(
        25, "America/Los_Angeles", "洛杉矶时间", "美洲", ("洛杉矶", "旧金山", "美西")
    ),
    TimezoneChoice(26, "America/Sao_Paulo", "圣保罗时间", "美洲", ("圣保罗", "巴西")),
    TimezoneChoice(27, "Pacific/Honolulu", "檀香山时间", "美洲", ("檀香山", "夏威夷")),
    TimezoneChoice(28, "Africa/Cairo", "开罗时间", "非洲", ("开罗", "埃及")),
    TimezoneChoice(29, "Africa/Johannesburg", "约翰内斯堡时间", "非洲", ("约翰内斯堡", "南非")),
    TimezoneChoice(30, "UTC", "协调世界时", "其他", ("UTC", "世界时", "格林尼治")),
)

MAX_INDEX = len(TIMEZONE_CHOICES)

_INDEX_PREFIXES = ("序号", "index:", "index", "no.", "no", "#", "第")
_INTEGER = re.compile(r"^[+-]?\d+$")
# 「11 项」「3 号」这类只有数字的写法(前缀已由 _strip_index_prefix 去掉)
_INDEX_ONLY = re.compile(r"^(\d+)\s*(?:项|个|条|号)?\s*$")
# 「3. 布里斯班时间」「11. 悉尼时间(Australia/Sydney)」这类从下拉框复制的写法。
# 开头必须是数字:这样 "+10:00"、"GMT+10" 这类偏移量不会被误当成序号。
_INDEXED_LABEL = re.compile(r"^\s*(\d+)\s*[.、,，:：)）]\s*(.+)$")
# 「布里斯班时间(Australia/Brisbane)」里括号中的 IANA 名
_PARENTHESIZED = re.compile(r"[(（]([^)）]+)[)）]")


@lru_cache(maxsize=1)
def _iana_lookup() -> dict[str, str]:
    """小写 IANA 名 → 规范写法。

    用 `available_timezones()` 而不是直接 `ZoneInfo(name)`,是因为后者在不同平台上
    对大小写的宽容度不同(Windows 的 tzdata 会接受 `europe/london`,Linux 不会)。
    这里统一成「大小写不敏感、但返回规范名」,行为可预测。
    """
    try:
        names = set(available_timezones())
    except Exception:  # noqa: BLE001 - 缺少 tzdata 时退回常用清单
        names = {choice.iana for choice in TIMEZONE_CHOICES}
    names.update(iana_names())
    return {name.lower(): name for name in names}


def canonical_iana(text: str) -> str | None:
    """把可能是 IANA 名的文本规范成标准写法;不是合法时区名时返回 `None`。"""
    candidate = text.strip()
    if not candidate:
        return None
    matched = _iana_lookup().get(candidate.lower())
    if matched is not None:
        return matched
    return candidate if is_valid_timezone(candidate) else None


def choice_at(index: int) -> TimezoneChoice | None:
    """按 1 基序号取一项;`0` 视为第一项(兼容从 0 开始数的调用方)。"""
    if 1 <= index <= MAX_INDEX:
        return TIMEZONE_CHOICES[index - 1]
    if index == 0:
        return TIMEZONE_CHOICES[0]
    return None


def _strip_index_prefix(text: str) -> str:
    lowered = text.lower()
    for prefix in _INDEX_PREFIXES:
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _match_label(text: str) -> TimezoneChoice | None:
    lowered = text.strip().lower()
    if not lowered:
        return None
    for choice in TIMEZONE_CHOICES:
        if lowered == choice.iana.lower() or lowered == choice.label.lower():
            return choice
        for alias in choice.aliases:
            if lowered == alias.lower():
                return choice
    return None


def resolve_timezone(value: object) -> str | None:
    """把序号、中文名或 IANA 名解析成 IANA 时区名;无法识别时返回 `None`。

    接受的形式(大小写不敏感):

    - `1` / `"1"` / `"#1"` / `"序号1"` / `"第 11 项"` → 序号(1 基,`0` 等同第 1 项);
    - `"北京时间"` / `"北京"` / `"悉尼时间"` → `TIMEZONE_CHOICES` 中的中文名或别名;
    - `"3. 布里斯班时间"` / `"布里斯班时间(Australia/Brisbane)"` → 下拉框里展示的完整写法;
    - `"Asia/Shanghai"` → 合法 IANA 名原样返回。
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        if isinstance(value, float) and not float(value).is_integer():
            return None
        choice = choice_at(int(value))
        return choice.iana if choice else None

    text = str(value).strip()
    if not text:
        return None

    bare = _strip_index_prefix(text)
    if _INTEGER.match(bare):
        choice = choice_at(int(bare))
        return choice.iana if choice else None

    only_index = _INDEX_ONLY.match(bare)
    if only_index:
        choice = choice_at(int(only_index.group(1)))
        return choice.iana if choice else None

    matched = _match_label(text)
    if matched is not None:
        return matched.iana

    # 「3. 布里斯班时间」「11. 悉尼时间(Australia/Sydney)」:从下拉框复制的完整写法。
    # 这里只在文字确实对得上时才认,避免把 "+10:00" 这类偏移量误读成序号。
    indexed = _INDEXED_LABEL.match(text)
    if indexed:
        label_match = _match_label(indexed.group(2).strip())
        if label_match is not None:
            return label_match.iana
        numbered = choice_at(int(indexed.group(1)))
        if numbered is not None:
            for candidate in _PARENTHESIZED.findall(indexed.group(2)):
                if canonical_iana(candidate) == numbered.iana:
                    return numbered.iana

    for candidate in _PARENTHESIZED.findall(text):
        canonical = canonical_iana(candidate)
        if canonical is not None:
            return canonical

    return canonical_iana(text)


def timezone_label(iana: str | None) -> str | None:
    """返回 IANA 名对应的中文标签;不在清单中时返回 `None`。"""
    if not iana:
        return None
    for choice in TIMEZONE_CHOICES:
        if choice.iana == iana:
            return choice.label
    return None


def describe_timezone(value: object) -> str | None:
    """给界面与错误提示用的一行描述,如 `1 · 北京时间 (Asia/Shanghai)`。"""
    resolved = resolve_timezone(value)
    if resolved is None:
        return None
    for choice in TIMEZONE_CHOICES:
        if choice.iana == resolved:
            return f"{choice.index} · {choice.label} ({choice.iana})"
    return resolved


def timezone_help(value: object | None = None) -> str:
    """校验失败时的提示文案。"""
    head = f"未识别的时区:{value}。" if value not in (None, "") else "缺少时区。"
    first = TIMEZONE_CHOICES[0]
    return (
        f"{head}可填 IANA 名(如 Asia/Shanghai),"
        f"也可填设置页列出的序号 1-{MAX_INDEX}(1 = {first.label})或中文名。"
    )


def timezone_choices() -> list[dict[str, object]]:
    """清单的字典形式,供 JSON API 返回。"""
    return [
        {
            "index": choice.index,
            "iana": choice.iana,
            "label": choice.label,
            "region": choice.region,
        }
        for choice in TIMEZONE_CHOICES
    ]


def timezone_groups() -> list[tuple[str, list[TimezoneChoice]]]:
    """按地区分组,供模板渲染 `<optgroup>`。"""
    groups: list[tuple[str, list[TimezoneChoice]]] = []
    for region in REGION_ORDER:
        items = [choice for choice in TIMEZONE_CHOICES if choice.region == region]
        if items:
            groups.append((region, items))
    return groups


def known_iana_names() -> frozenset[str]:
    """清单里出现过的 IANA 名,用于判断「当前值是否已在选项中」。"""
    return frozenset(choice.iana for choice in TIMEZONE_CHOICES)


def iana_names(choices: Iterable[TimezoneChoice] = TIMEZONE_CHOICES) -> tuple[str, ...]:
    """清单的 IANA 名序列(顺序即序号顺序)。"""
    return tuple(choice.iana for choice in choices)


__all__ = [
    "MAX_INDEX",
    "REGION_ORDER",
    "TIMEZONE_CHOICES",
    "TimezoneChoice",
    "canonical_iana",
    "choice_at",
    "describe_timezone",
    "iana_names",
    "known_iana_names",
    "resolve_timezone",
    "timezone_choices",
    "timezone_groups",
    "timezone_help",
    "timezone_label",
]
