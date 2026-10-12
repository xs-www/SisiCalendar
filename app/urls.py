"""公开地址前缀与链接拼装。

家庭成员的公开面统一挂在 `PUBLIC_BASE_URL` 的**路径前缀**下(默认 `/calendar`):
`https://<节点>/calendar/register`、`/calendar/login`、`/calendar/admin/...` 以及
每人的订阅地址 `https://<节点>/calendar/<令牌>.ics`。本机管理页仍可用裸路径
(`http://localhost:443/admin`)访问。

前缀不写死成常量,而是**按每次请求的路径推导**:同一个应用同时服务 `/admin`(本机)
与 `/calendar/admin`(家庭成员),页面里的链接必须跟着入口走——从裸路径进来就渲染裸
链接,从 `/calendar` 进来就渲染 `/calendar` 链接。推导结果放在 `ContextVar` 里,由
`app/main.py` 的中间件在每个请求开始时绑定,模板用全局函数 `bp()` 取值,Python 侧用
`url()` 拼重定向地址。

绝对地址(邀请链接、订阅地址、邮件正文)一律从 `PUBLIC_BASE_URL` 派生,见
`invite_url()` 与 `subscription_url()`。
"""

from __future__ import annotations

from contextvars import ContextVar
from urllib.parse import quote, urlsplit

from app.config import PROJECT_ROOT, Settings

_PREFIX: ContextVar[str] = ContextVar("sisi_url_prefix", default="")

#: 订阅地址的固定后缀(令牌 + `.ics`),路由也只匹配这个形状
FEED_SUFFIX = ".ics"


def normalize_prefix(value: object) -> str:
    """把任意前缀文本规范成 `""` 或 `/xxx`(无尾斜杠)。"""
    text = str(value or "").strip()
    if not text or text == "/":
        return ""
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/")


def public_base_url(settings: Settings) -> str:
    """对外公开的基地址(无尾斜杠),用于生成可复制的绝对链接。"""
    return (settings.public_base_url or "").strip().rstrip("/")


def public_base_path(settings: Settings) -> str:
    """基地址里的路径部分,即公开面的挂载前缀(如 `/calendar`)。"""
    return normalize_prefix(urlsplit(public_base_url(settings)).path)


def bind_prefix(path: str, base_path: str) -> str:
    """按本次请求路径判断前缀并绑定到当前上下文。返回绑定结果。"""
    prefix = ""
    if base_path and (path == base_path or path.startswith(base_path + "/")):
        prefix = base_path
    _PREFIX.set(prefix)
    return prefix


def current_prefix() -> str:
    """当前请求生效的前缀(模板全局函数 `bp()` 用它)。"""
    return _PREFIX.get()


def url(path: str) -> str:
    """站内链接:加上当前请求的前缀。"""
    if not path.startswith("/"):
        path = "/" + path
    return f"{_PREFIX.get()}{path}"


def relative(path: str) -> str:
    """去掉当前请求前缀,得到应用内部的规范路径(用于前缀无关的判断)。"""
    prefix = _PREFIX.get()
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        return path[len(prefix) :] or "/"
    return path


def is_api_path(path: str) -> bool:
    """是不是 JSON 接口:裸 `/api/...` 与 `/calendar/api/...` 都算。"""
    return relative(path).startswith("/api/")


def is_admin_path(path: str) -> bool:
    """管理/登录页(需要挂严格 CSP):`/admin`、`/login`、`/register` 及其前缀形式。"""
    return relative(path).startswith(("/admin", "/login", "/register", "/verify-email"))


def invite_url(settings: Settings, code: str) -> str:
    """邀请链接:管理员可以直接复制粘贴给家人。"""
    base = public_base_url(settings)
    return f"{base}/register?{INVITE_QUERY}={quote(code)}"


def subscription_url(settings: Settings, token: str) -> str:
    """订阅地址:每人固定的一条,`<基地址>/<令牌>.ics`。"""
    base = public_base_url(settings)
    return f"{base}/{token}{FEED_SUFFIX}"


#: 静态资源目录与指纹缓存(进程内一次,够用:静态文件只在部署时变)
_STATIC_DIR = PROJECT_ROOT / "static"
_ASSET_STAMPS: dict[str, str] = {}


def asset_version(relative_path: str) -> str:
    """静态资源的短指纹(`大小-mtime` 十六进制),取不到就返回 `0`。"""
    key = str(relative_path or "").lstrip("/")
    stamp = _ASSET_STAMPS.get(key)
    if stamp is None:
        try:
            stat = (_STATIC_DIR / key).stat()
            stamp = f"{stat.st_size:x}-{int(stat.st_mtime):x}"
        except OSError:
            stamp = "0"
        _ASSET_STAMPS[key] = stamp
    return stamp


def asset(path: str) -> str:
    """带指纹的静态资源地址:`/static/css/app.css?v=1a2b-66f0`(自动带当前前缀)。

    浏览器会长期缓存 CSS/JS;地址不带指纹时,改完样式家里人看到的还是旧版
    (典型症状:布局“错版”成几年前的样子)。指纹取文件大小 + mtime,部署新镜像即变。
    """
    key = str(path or "").lstrip("/")
    return f"{url(f'/static/{key}')}?v={asset_version(key)}"


#: 邀请链接里的参数名(`app/invites.py::INVITE_PARAM` 的镜像,避免循环导入)
INVITE_QUERY = "code"


__all__ = [
    "FEED_SUFFIX",
    "INVITE_QUERY",
    "asset",
    "asset_version",
    "bind_prefix",
    "current_prefix",
    "invite_url",
    "is_admin_path",
    "is_api_path",
    "normalize_prefix",
    "public_base_path",
    "public_base_url",
    "relative",
    "subscription_url",
    "url",
]