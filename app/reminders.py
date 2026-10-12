"""日程提醒(ICS `VALARM`)的取值、预设与中文描述。

本模块是「提醒」的**单一事实来源**,被以下位置共用:

- `app/events.py`:输入校验与序列化(界面、`/api/v1/events`、`/api/agent/events` 同一条路径);
- `app/ics.py`:生成 `VALARM` 组件;
- `app/routers/pages.py`:渲染表单里的常用提醒勾选项。

## 取值语义

`reminders` 是**提前分钟数**的列表,每一项表示「事件开始前多少分钟提醒」:

- `0` = 事件开始时;
- 正数 = 提前,如 `15` 表示「提前 15 分钟」;
- 负数 = 延后,如 `-10` 表示「开始后 10 分钟」;
- 全天事件以**当天 00:00** 为基准,因此 `1440` = 「提前 1 天」。

列表在入库前统一去重并按**从大到小**排序(先报远期提醒),保证同一组提醒在任何输入顺序下
导出字节一致——导出器靠内容哈希判断是否需要新版本,顺序不稳定会导致无改动的保存也重发日历。
"""

from __future__ import annotations

import json
from typing import Any

# 上限 30 天:再大的偏移在日历场景里没有意义,同时挡住明显写错的输入(如把秒当分钟)
MAX_REMINDER_MINUTES = 43200

# 表单里的常用提醒预设,顺序即界面顺序。序号不是对外契约,可以调整。
PRESET_REMINDERS: tuple[int, ...] = (0, 5, 15, 30, 60, 120, 1440, 2880, 10080)

# 自定义提醒文本接受的分隔符:半角/全角逗号与分号(空白本来就分行)
SEPARATOR_CODEPOINTS = (",", ",", ";", ";")
_SEPARATORS = {ord(char): " " for char in SEPARATOR_CODEPOINTS}


def describe_reminder(minutes: int) -> str:
    """把一个偏移量说成人话,如 `15` → 「提前 15 分钟」。"""
    value = validate_reminder(minutes)
    if value is None:
        return "无效提醒"
    if value == 0:
        return "事件开始时"
    amount = abs(value)
    unit = "提前" if value > 0 else "延后"
    if amount % 10080 == 0:
        return f"{unit} {amount // 10080} 周"
    if amount % 1440 == 0:
        return f"{unit} {amount // 1440} 天"
    if amount % 60 == 0:
        return f"{unit} {amount // 60} 小时"
    return f"{unit} {amount} 分钟"


def preset_choices() -> tuple[tuple[int, str], ...]:
    """表单勾选项:`(分钟数, 中文标签)`,顺序固定为界面顺序。

    界面按位置读取勾选状态,因此**不要重排**这个元组,只能在末尾追加。
    """
    return tuple((value, describe_reminder(value)) for value in PRESET_REMINDERS)


def preset_options(minutes: list[int]) -> list[dict[str, Any]]:
    """给表单用的勾选项:`[{minutes, label, selected}]`,顺序固定为界面顺序。

    `selected` 指示当前事件的提醒是否命中该预设;自定义值只会出现在文本框里,
    不会凭空多出一个勾选项。
    """
    chosen = set(minutes)
    return [
        {"minutes": value, "label": describe_reminder(value), "selected": value in chosen}
        for value in PRESET_REMINDERS
    ]


def custom_reminder_text(minutes: list[int]) -> str:
    """只把**非预设**的提醒回填到「自定义分钟」输入框,避免同一个值在两处重复显示。"""
    extras = [value for value in minutes if value not in PRESET_REMINDERS]
    return ",".join(str(value) for value in extras)


def validate_reminder(value: Any) -> int | None:
    """把单个提醒值转成合法分钟数;非法返回 `None`。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, float):
        if not float(value).is_integer():
            return None
        minutes = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            minutes = int(text)
        except ValueError:
            return None
    else:
        return None
    if abs(minutes) > MAX_REMINDER_MINUTES:
        return None
    return minutes


def parse_reminder_text(text: str | None) -> list[int]:
    """解析表单/查询串里的自定义提醒:`"15, 1440"` → `[15, 1440]`。

    逗号、中文逗号、空格与分号都当分隔符,空片段(来自多余的逗号)会被忽略。
    非法片段**不由本函数负责**:`normalize_reminders` 会先做严格校验并报错,
    所以这里可以假定每个片段都合法;界面脚本另有一层同样的前置过滤。
    """
    if not text:
        return []
    values: list[int] = []
    for chunk in text.translate(_SEPARATORS).split():
        minutes = validate_reminder(chunk)
        if minutes is not None:
            values.append(minutes)
    return values


def normalize_reminders(value: Any) -> tuple[list[int] | None, str | None]:
    """把任意输入归一成待入库的提醒列表。

    - 输入可以是列表、JSON 字符串或逗号分隔字符串(表单形态);
    - 返回 `(提醒列表, 错误文案)`;`None` 的列表表示「没有提醒」;
    - 列表去重后按从大到小排序,保证导出内容与输入顺序无关。
    """
    if value is None:
        return [], None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return [], None
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except ValueError:
                return None, "提醒格式应为分钟数列表,如 [15, 1440]"
            if not isinstance(decoded, list):
                return None, "提醒格式应为分钟数列表,如 [15, 1440]"
            return normalize_reminders(decoded)
        # 表单形态:逗号/空格分隔的分钟数。逐段严格校验,拼错的值必须报错而不是被忽略
        chunks = [chunk for chunk in text.translate(_SEPARATORS).split() if chunk]
        for chunk in chunks:
            if validate_reminder(chunk) is None:
                return (
                    None,
                    "提醒只能是整数分钟数,多个值用逗号分隔(如 15,1440);"
                    "0 表示事件开始时,正数表示提前,负数表示延后",
                )
        return normalize_reminders(chunks)

    if not isinstance(value, (list, tuple, set)):
        return None, "提醒格式应为分钟数列表,如 [15, 1440]"

    minutes_list: list[int] = []
    for item in value:
        minutes = validate_reminder(item)
        if minutes is None:
            return (
                None,
                f"提醒只能是 ±{MAX_REMINDER_MINUTES} 以内的整数分钟数"
                "(0 表示事件开始时,正数表示提前,负数表示延后)",
            )
        minutes_list.append(minutes)

    # 去重并按从大到小(远期在前)排序:顺序必须只取决于集合本身
    return sorted(set(minutes_list), reverse=True), None


def serialize_reminders(raw: str | None) -> list[int]:
    """读取数据库里的 JSON 文本,永远返回干净的列表(坏数据不抛异常)。"""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except ValueError:
        return []
    values, _ = normalize_reminders(decoded)
    return values or []


def dump_reminders(minutes: list[int]) -> str | None:
    """入库形态:JSON 文本;空列表存 `None`,避免出现两种「空」的表示。"""
    return json.dumps(minutes) if minutes else None


def reminder_labels(minutes: list[int]) -> list[str]:
    """给界面/接口用的中文描述,顺序与入参一致。"""
    return [describe_reminder(value) for value in minutes]
