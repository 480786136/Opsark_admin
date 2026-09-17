import json
import re
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken

from .cloud_models import ClientPolicy
from .gateway import cipher
from .security import ApiError

VERSION = r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})"


def version_tuple(value):
    if not re.fullmatch(VERSION, value):
        raise ApiError(422, "INVALID_VERSION", "版本格式应为 major.minor.patch")
    return tuple(map(int, value.split(".")))


def check_cloud_version(request, db):
    p = db.get(ClientPolicy, 1)
    minimum = p.min_cloud_version if p else "0.0.0"
    if minimum != "0.0.0":
        try:
            old = version_tuple(request.headers.get("x-opsark-version", "0.0.0")) < version_tuple(minimum)
        except ApiError:
            old = True
        if old:
            raise ApiError(426, "CLIENT_UPDATE_REQUIRED", f"官方云功能需要 Core {minimum} 或更高版本；本地功能仍可使用")


def secure_url(value):
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        raise ApiError(422, "INVALID_HTTPS_URL")
    if (
        u.scheme != "https"
        or not u.hostname
        or u.username
        or u.password
        or u.fragment
        or (port is not None and port < 1)
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(c.isspace() for c in value)
    ):
        raise ApiError(422, "INVALID_HTTPS_URL", "请提供不含凭据的 HTTPS 地址")
    return value


def reject_credentials(value):
    text = json.dumps(value, ensure_ascii=False)
    if re.search(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:ouc_|our_|gh[pousr]_)[A-Za-z0-9_-]{20,}|\bsk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}|(?:password|passwd|api[_-]?key|token)\s*[=:]\s*[\"']?(?!\$\{|\[REDACTED\])[^\s\"',;]{8,}",
        text,
        re.IGNORECASE,
    ):
        raise ApiError(422, "SENSITIVE_CONTENT", "内容疑似含凭据，请移除或替换为 secret 引用后重试")


def seal(value):
    return cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def unseal(value):
    if value is None:
        return None
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError):
        raise ApiError(503, "PRIVATE_CONTENT_UNAVAILABLE", "加密内容暂不可读取，请联系管理员核对密钥配置")
