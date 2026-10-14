"""宿主端口映射探测(2026-10-14 新增)。

设置页需要回答一个问题:「宿主机的 `127.0.0.1:<HOST_PORT>` 真的映射到容器了吗?」
应用在容器里看不到 Docker 的端口映射,但可以**主动去连一次**:

    http://host.docker.internal:<HOST_PORT>/healthz

`host.docker.internal` 在 Docker Desktop 上开箱可用;Linux/NAS 上需要
`extra_hosts: ["host.docker.internal:host-gateway"]`(Docker 20.10+),
`compose.nas.yaml` 已经加上。

安全边界(这不是一个可被滥用的 SSRF 探测器):

- 主机名**写死** `host.docker.internal`,不接受任何用户输入;
- 端口只接受 1–65535 的整数,且必须是整数类型;
- 路径写死 `/healthz`;
- 这个函数只在管理员点「检测宿主端口」时被调用,不在页面加载时自动跑。

探测失败不是错误:**本机源码运行、没加 extra_hosts、端口没映射**都会失败,
文案里要给出下一步,而不是抛异常。
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from app.config import Settings

logger = logging.getLogger(__name__)

#: 容器内访问宿主机回环地址的固定主机名(不接受用户输入)
HOST_GATEWAY_NAME = "host.docker.internal"
#: 探测的目标路径(写死)
PROBE_PATH = "/healthz"
#: 默认超时(秒)
DEFAULT_TIMEOUT_SECONDS = 2.0


def probe_host_port(
    settings: Settings, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[bool, str]:
    """探测宿主端口映射,返回 `(是否可达, 中文说明)`。"""
    port = settings.host_port
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return False, f"宿主端口配置不合法({port!r});请把它设成 1–65535 的整数。"

    url = f"http://{HOST_GATEWAY_NAME}:{port}{PROBE_PATH}"

    def unreachable(detail: str) -> tuple[bool, str]:
        return False, (
            f"连不上 {HOST_GATEWAY_NAME}:{port} —— {detail}。"
            "检查 compose 里的 `ports`(应为 `127.0.0.1:<HOST_PORT>:8000`)、"
            "`HOST_PORT` 是否与它一致,以及是否加了 "
            '`extra_hosts: ["host.docker.internal:host-gateway"]`。'
        )

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - 主机名写死
            status = getattr(response, "status", 0) or 0
    except urllib.error.HTTPError as exc:  # 有响应但状态码不是 2xx
        return False, (
            f"{HOST_GATEWAY_NAME}:{port} 有响应但返回 HTTP {exc.code};"
            "请确认映射到的确实是本应用(容器内 `PORT` 是 8000)。"
        )
    except urllib.error.URLError as exc:
        reason = exc.reason
        detail = (
            f"连接超时(>{timeout} 秒)"
            if isinstance(reason, TimeoutError)
            else str(reason)
        )
        return unreachable(detail)
    except TimeoutError:
        return unreachable(f"连接超时(>{timeout} 秒)")
    except OSError as exc:  # pragma: no cover - 平台差异
        return unreachable(str(exc))

    if 200 <= status < 300:
        return True, (
            f"可达:宿主 `127.0.0.1:{port}` → 容器 8000 映射正常"
            f"(`GET {PROBE_PATH}` 返回 {status})。"
        )
    return False, f"{HOST_GATEWAY_NAME}:{port} 返回 HTTP {status},不像本应用的 /healthz。"


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "HOST_GATEWAY_NAME", "PROBE_PATH", "probe_host_port"]