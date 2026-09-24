"""账号口令与会话令牌的密码学工具。

不引入 bcrypt/argon2：仓库运行期依赖保持 5 项，口令哈希用标准库 `hashlib.scrypt`
（内存硬，抗 GPU 暴力，参数随哈希串一起存，未来可平滑升级代价参数）。
会话令牌与设备令牌同样处理：**只存 SHA-256**，校验用常量时间比较。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field

# scrypt 代价参数（n 必须是 2 的幂）。存进哈希串，校验时以串里的值为准。
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

# 临时密码用无歧义字符集（去掉 0/O/1/l/I），管理员口头或截图转达时不易出错
_TEMPORARY_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_email(value: str) -> str:
    """邮箱唯一性与登录都按归一化后的值判定（大小写、首尾空白不敏感）。"""

    return value.strip().lower()


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_PATTERN.match(normalize_email(value)))


def hash_password(password: str) -> str:
    """返回自描述哈希串：`scrypt$n$r$p$salt_hex$digest_hex`。"""

    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    """校验口令。没有口令（历史成员/未设密码）一律失败，不抛异常。"""

    if not encoded:
        return False
    try:
        scheme, n_raw, r_raw, p_raw, salt_hex, digest_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def validate_password(password: str) -> None:
    """口令策略。不满足时抛 ValueError，错误码直接给前端做中文提示。"""

    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("password_too_short")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("password_too_long")
    if password.isdigit():
        raise ValueError("password_all_digits")
    if password.isalpha():
        raise ValueError("password_all_letters")


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """会话令牌只存哈希：库被读走也拿不到可用令牌（与设备令牌一致）。"""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_temporary_password() -> str:
    """管理员重置密码时的一次性口令：16 位、含大小写与数字，满足策略。"""

    return "".join(secrets.choice(_TEMPORARY_ALPHABET) for _ in range(16))


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class LoginThrottle:
    """进程内登入节流：同一「邮箱 + 客户端 IP」连续失败后锁定，指数退避到上限。

    单实例部署下足够；多实例部署需要换成共享存储（已在 SERVER_DEPLOYMENT 里记为边界）。
    `clock` 可注入，便于测试不睡真实时间。
    """

    threshold: int = 5
    base_seconds: float = 60.0
    max_seconds: float = 900.0
    clock: object = time.monotonic
    _entries: dict[str, _Attempts] = field(default_factory=dict)

    def _now(self) -> float:
        return float(self.clock())  # type: ignore[operator]

    @staticmethod
    def key(email: str, client: str) -> str:
        return f"{normalize_email(email)}|{client or '-'}"

    def locked_seconds(self, key: str) -> int:
        entry = self._entries.get(key)
        if not entry:
            return 0
        remaining = entry.locked_until - self._now()
        return int(remaining) + 1 if remaining > 0 else 0

    def record_failure(self, key: str) -> None:
        entry = self._entries.setdefault(key, _Attempts())
        entry.failures += 1
        if entry.failures >= self.threshold:
            over = entry.failures - self.threshold
            wait = min(self.base_seconds * (2**over), self.max_seconds)
            entry.locked_until = self._now() + wait
        self.prune()

    def clear(self, key: str) -> None:
        self._entries.pop(key, None)

    def prune(self, *, keep: int = 4096) -> None:
        """防止内存里无限堆积：超过上限时清掉已过锁定期的条目。"""

        if len(self._entries) <= keep:
            return
        now = self._now()
        for key in [item for item, entry in self._entries.items() if entry.locked_until <= now and entry.failures < self.threshold]:
            self._entries.pop(key, None)


# 进程级共享实例：端点与测试都用它
LOGIN_THROTTLE = LoginThrottle()