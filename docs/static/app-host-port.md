# `app/host_port.py`

宿主机端口映射探测(2026-10-14 新增)。设置页需要回答一个问题:
「宿主机的 `127.0.0.1:<HOST_PORT>` 真的映射到容器了吗?」应用在容器里看不到 Docker
的端口映射,但可以**主动去连一次** `http://host.docker.internal:<HOST_PORT>/healthz`。

`compose.nas.yaml` 为它补了两样东西:`extra_hosts: ["host.docker.internal:host-gateway"]`
(让容器内能解析到宿主机)与 `HOST_PORT`(命令与探测用的宿主端口,见
[`ci-docker-publish.md`](ci-docker-publish.md))。

## 常量

### `HOST_GATEWAY_NAME`

- **位置**:`app/host_port.py:34`
- **值**:`"host.docker.internal"`
- **用途**:容器内访问宿主机回环地址的固定主机名
- **不变量**:它**不接受任何用户输入**,也不是配置项;探测 URL 的主机部分只能来自它
  (可变就等于把接口变成 SSRF 探测器)

### `PROBE_PATH`

- **位置**:`app/host_port.py:36`
- **值**:`"/healthz"`
- **用途**:探测的目标路径,同时也是「这就是本应用」的判据
- **不变量**:路径写死;它必须与 Dockerfile `HEALTHCHECK`、`compose*.yaml` 的健康检查
  打的是同一个端点

### `DEFAULT_TIMEOUT_SECONDS`

- **位置**:`app/host_port.py:38`
- **值**:`2.0`
- **用途**:`probe_host_port` 的默认超时(秒)
- **参数语义**:超时是针对**整个连接 + 响应**的;跨 tailnet/跨容器的探测不应更宽,
  否则管理员点一下要等很久

## 函数

### `probe_host_port(settings, *, timeout=DEFAULT_TIMEOUT_SECONDS) -> tuple[bool, str]`

- **位置**:`app/host_port.py:41`(函数体 `:44` 起)
- **用途**:探测宿主端口映射是否通,返回 `(是否可达, 中文说明)`;文案直接渲染到设置页
- **参数**:
  - `settings.host_port` 必须能解释成 **1–65535 的整数**;越界、非整数类型、`bool`
    都直接短路,不发出任何请求;
  - `timeout` 单位秒,默认 `DEFAULT_TIMEOUT_SECONDS`;
  - 端口本身**不是**入参 —— 只认 `Settings.host_port`,避免多一条可被伪造的输入通道
- **返回**:`(True, 说明)` 或 `(False, 说明)`;**不向外抛异常**(下面四类异常都被接住)。
  探测失败不是错误:本机源码运行、没加 `extra_hosts`、端口没映射都会失败,
  文案里要给出下一步
- **行为**:`GET http://HOST_GATEWAY_NAME:<host_port>/healthz`
- **副作用**:一次 HTTP 探测(只读);不写库、不发信、不改配置。
  调用方(`app/routers/pages.py::host_port_check_action`)只额外记一条审计
- **安全边界(这不是可被滥用的 SSRF 探测器)**:
  - 主机名**写死** `HOST_GATEWAY_NAME`;
  - 端口只认 1–65535 的整数,且必须是整数类型;
  - 路径写死 `PROBE_PATH`;
  - 只在**管理员点击**「检测宿主端口映射」时被调用,页面加载时不会自动跑
- **失败分类与文案**:入参短路一类,异常捕获**四类**(`HTTPError` / `URLError` /
  `TimeoutError` / `OSError`,顺序即 `except` 顺序):

  | 分类 | 触发条件 | 文案要点 |
  |---|---|---|
  | 端口不合法(入参短路) | 非整数 / 越界 / `bool` | `宿主端口配置不合法(<原值>);请把它设成 1–65535 的整数。`(**不发请求**) |
  | `urllib.error.HTTPError` | 有响应但状态码非 2xx | `<主机>:<端口> 有响应但返回 HTTP <code>;请确认映射到的确实是本应用(容器内 PORT 是 8000)。` |
  | `urllib.error.URLError` | 连不上;`exc.reason` 是 `TimeoutError` 时单独写「连接超时(>N 秒)」,否则用 `str(reason)` | 统一退回 `unreachable(detail)` |
  | `TimeoutError` | 裸超时(不被 URLError 包裹时) | 同「连接超时(>N 秒)」 |
  | `OSError` | 其它系统错误(`# pragma: no cover`,平台差异) | `str(exc)` |

  - `unreachable(detail)` 的完整文案会给出修法:`compose` 里的 `ports`
    (应为 `127.0.0.1:<HOST_PORT>:8000`)、`HOST_PORT` 是否与它一致,
    以及是否加了 `extra_hosts: ["host.docker.internal:host-gateway"]`
  - 另有两条**未走异常**的分支:状态码 2xx → `可达:宿主 127.0.0.1:<端口> → 容器 8000 映射正常(GET /healthz 返回 <status>)。`;
    拿到状态码但它不是 2xx(默认 opener 会跟随 3xx,因此这一支通常只在自定义/异常实现下到达)
    → `<主机>:<端口> 返回 HTTP <status>,不像本应用的 /healthz。`
- **不变量**:
  - 端口与主机名都不来自用户输入;
  - 页面加载时**不得**自动调用(它是同步阻塞探测,2 秒超时);
  - 失败必须给出下一步而不是抛异常 —— 调用方拿不到结果就没法渲染片段
- **测试**:`tests/test_settings_public_base_url.py::test_probe_rejects_an_out_of_range_port`、
  `::test_probe_reports_a_reachable_port`(起一个真的 HTTP 服务,把主机名 monkeypatch 成
  `127.0.0.1`)、`::test_probe_explains_how_to_fix_an_unreachable_port`
  (断言文案含 `extra_hosts` 与主机名)、
  `::test_host_port_check_endpoint_returns_a_partial`(路由层,用测试替身)

## `__all__`

- **位置**:`app/host_port.py:88`
- **导出**:`DEFAULT_TIMEOUT_SECONDS`、`HOST_GATEWAY_NAME`、`PROBE_PATH`、`probe_host_port`

## 找不到对应测试的点

- `HTTPError` 与裸 `TimeoutError` 两条分支没有专门用例(现有用例只覆盖「端口不合法」
  「可达」「连不上(`URLError` 那条)」三种);`OSError` 分支在源码里就标了
  `# pragma: no cover`。
- 拿到状态码但**不是 2xx** 的那条分支没有用例(默认 opener 会跟随重定向,
  实际到达它需要实现差异)。

## 变更记录

- 2026-10-14:新建。`probe_host_port` 与三个常量;主机名写死、端口只认 1–65535 的整数、
  路径写死 `/healthz`,2 秒超时,四类失败各给中文文案与修法;只在管理员点击时跑,
  是只读探测(不写库、不改配置)。