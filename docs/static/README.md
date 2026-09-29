# 静态函数文档

本目录按「一个源码文件一份文档」归档各模块的**静态函数契约**:签名、参数、返回值、
副作用与对应测试。它是给后续 Agent 的索引——改代码前先看这里,不必一上来通读全部实现。

## 格式约定

每个函数一条,固定字段:

```markdown
### `name(args) -> ret`

- **位置**:`app/module.py:行号`
- **用途**:一句话说明对外行为,不写实现细节
- **参数**:只写非显然的约束(取值范围、单位、时区语义)
- **返回**:返回值语义与特殊值(`None` 代表什么)
- **副作用**:写库、发信、文件替换、限流等;无则写「无」
- **不变量**:改动时必须保持的约束(如「序号不得重排」)
- **测试**:`tests/test_x.py::test_y`
- **变更记录**:日期 + 变更摘要
```

## 索引

| 文档 | 源码 |
|---|---|
| [`app-timezones.md`](app-timezones.md) | `app/timezones.py`(新增:常用时区清单与序号解析) |
| [`app-schemas.md`](app-schemas.md) | `app/schemas.py` |
| [`app-events.md`](app-events.md) | `app/events.py` |
| [`app-settings-store.md`](app-settings-store.md) | `app/settings_store.py` |
| [`app-token-store.md`](app-token-store.md) | `app/token_store.py`(新增:远程接入令牌,哈希存放) |
| [`app-deps.md`](app-deps.md) | `app/deps.py`(模板过滤器 `localtime`、Agent 令牌鉴权) |
| [`app-routers-pages.md`](app-routers-pages.md) | `app/routers/pages.py` |
| [`app-routers-api-settings.md`](app-routers-api-settings.md) | `app/routers/api_settings.py` |
| [`templates.md`](templates.md) | `templates/`(设置页、事件表单、邮件队列片段) |

## 与其它文档的分工

- 需求与验收标准:`docs/本地日程与订阅日历服务-开发文档.md`、`docs/验收测试记录.md`
- 部署与排错:`docs/部署与运维.md`
- 行为约束总览:`AGENTS.md`(必须保持的不变量)
- 本目录:函数级契约,回答「这个函数收什么、给什么、改哪些数据」
