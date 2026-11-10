"""Runtime configuration for the qoder2api-public gateway.

Zero third-party dependencies, Python 3.9+.  The file on disk is
`public-data/config.json` (mode 0600).  Everything secret lives there:
the internal qoder2api API key, the admin password hash and the nps vkey.

Design notes
------------
* **fail closed**: a missing/invalid config raises :class:`ConfigError`.
  The gateway never starts with "authentication disabled by accident".
* Every value has a default in :data:`DEFAULTS` so a partially written
  config still starts, except for the three secrets above.
* Env overrides exist for container/systemd deployment:
  `QDP_CONFIG`, `QDP_HOST`, `QDP_PORT`, `QDP_UPSTREAM_KEY`,
  `QDP_ADMIN_PASSWORD` (plaintext), `QDP_DATA_DIR`.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

DEFAULT_CONFIG_NAME = "config.json"
DEFAULT_DATA_DIR = "public-data"

PBKDF2_ROUNDS = 210000
PBKDF2_SALT_BYTES = 16

REALMS = ("", "cn", "intl")

# 与 public.example.json 保持一致
DEFAULTS = {
    "version": 1,
    "server": {"host": "127.0.0.1", "port": 8791, "max_concurrent": 16},
    "tls": {"enabled": False, "cert_file": "", "key_file": ""},
    "upstream": {
        "url": "http://127.0.0.1:8790",
        "api_key": "",
        "panel_password": "",
        "connect_timeout": 10,
        "read_timeout": 600,
        "max_body_bytes": 33554432,
        "verify_tls": True,
    },
    "admin": {
        "host": "127.0.0.1",
        "port": 8792,
        "password_hash": "",
        "session_ttl": 43200,
        "allow_remote": False,
    },
    "defaults": {
        "rpm": 60,
        "max_concurrent": 4,
        "daily_tokens": 0,
        "monthly_tokens": 0,
        "allowed_models": [],
        "realm": "",
        "allow_client_realm": False,
    },
    "endpoints": {
        "allowed_paths": [
            "/v1/models",
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/messages",
            "/v1/messages/count_tokens",
        ],
        "auth_fail_limit": 20,
        "auth_fail_window": 60,
    },
    "metering": {"inject_stream_usage": True, "estimate_when_missing": True},
    "logging": {"dir": "logs", "level": "info", "rotate_mb": 20, "keep": 5},
    "nps": {
        "server_addr": "",
        "vkey": "",
        "mode": "domain",
        "server_port": 8443,
        "domain": "",
        "crypt": True,
        "compress": True,
    },
}


class ConfigError(Exception):
    """"配置无法使用"（信息直接面向运维人员，中文可读）。"""


# --------------------------------------------------------------------- utils
def new_secret(nbytes=32):
    """URL-safe random secret (no padding)."""
    return secrets.token_urlsafe(nbytes)


def hash_password(password, rounds=PBKDF2_ROUNDS):
    """`pbkdf2_sha256$rounds$salt_b64$hash_b64`（与面板口令同量级强度）。"""
    if not password:
        raise ConfigError("password must not be empty")
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(rounds)
    )
    return "pbkdf2_sha256$%d$%s$%s" % (
        int(rounds),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored_hash):
    """常量时间校验；格式不认识时返回 False（绝不抛）。"""
    try:
        scheme, rounds, salt_b64, hash_b64 = str(stored_hash).split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), salt, int(rounds)
        )
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def mask(secret, keep=4):
    """日志/接口里的脱敏显示：保留尾部 4 位。"""
    secret = str(secret or "")
    if len(secret) <= keep:
        return "***"
    return "***" + secret[-keep:]


def write_json_atomic(path, data, mode=0o600):
    """临时文件 + os.replace，避免半截 JSON；尽量把权限收到 0600。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, mode)
    except Exception:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def _deep_merge(base, override):
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# -------------------------------------------------------------------- config
class Config(object):
    """已校验的配置视图。属性只读使用，改配置请重新 load。"""

    def __init__(self, raw, path=None, env=None):
        self.raw = raw
        self.path = os.path.abspath(path) if path else None
        self.env = dict(env if env is not None else os.environ)
        if self.path:
            self.data_dir = os.path.dirname(self.path)
        else:
            self.data_dir = os.path.abspath(
                self.env.get("QDP_DATA_DIR") or DEFAULT_DATA_DIR
            )
        # 环境变量覆盖（只覆盖少数运维项）
        if self.env.get("QDP_UPSTREAM_KEY"):
            self.raw["upstream"]["api_key"] = self.env["QDP_UPSTREAM_KEY"]
        if self.env.get("QDP_HOST"):
            self.raw["server"]["host"] = self.env["QDP_HOST"]
        if self.env.get("QDP_PORT"):
            try:
                self.raw["server"]["port"] = int(self.env["QDP_PORT"])
            except ValueError:
                raise ConfigError("QDP_PORT 不是整数: %r" % self.env["QDP_PORT"])
        self.admin_password_override = self.env.get("QDP_ADMIN_PASSWORD") or ""
        self.validate()

    # -- 通用取值 ---------------------------------------------------------
    def section(self, name):
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def value(self, dotted, default=None):
        node = self.raw
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path_in_data(self, relative, default):
        relative = relative or default
        if os.path.isabs(relative):
            return relative
        return os.path.abspath(os.path.join(self.data_dir, relative))

    # -- 常用属性 ---------------------------------------------------------
    @property
    def server_host(self):
        return str(self.value("server.host", "127.0.0.1"))

    @property
    def server_port(self):
        return int(self.value("server.port", 8791))

    @property
    def server_max_concurrent(self):
        return int(self.value("server.max_concurrent", 16))

    @property
    def tls_enabled(self):
        return bool(self.value("tls.enabled", False))

    @property
    def tls_cert_file(self):
        return str(self.value("tls.cert_file", "") or "")

    @property
    def tls_key_file(self):
        return str(self.value("tls.key_file", "") or "")

    @property
    def upstream_url(self):
        return str(self.value("upstream.url", "http://127.0.0.1:8790")).rstrip("/")

    @property
    def upstream_api_key(self):
        return str(self.value("upstream.api_key", ""))

    @property
    def upstream_panel_password(self):
        """qoder2api 面板口令；空 = 沿用上游默认 admin（启动脚本会 WARN）。"""
        return str(self.value("upstream.panel_password", "") or "")

    @property
    def upstream_connect_timeout(self):
        return float(self.value("upstream.connect_timeout", 10))

    @property
    def upstream_read_timeout(self):
        return float(self.value("upstream.read_timeout", 600))

    @property
    def upstream_max_body(self):
        return int(self.value("upstream.max_body_bytes", 33554432))

    @property
    def upstream_verify_tls(self):
        return bool(self.value("upstream.verify_tls", True))

    @property
    def admin_host(self):
        return str(self.value("admin.host", "127.0.0.1"))

    @property
    def admin_port(self):
        return int(self.value("admin.port", 8792))

    @property
    def admin_password_hash(self):
        return str(self.value("admin.password_hash", ""))

    @property
    def admin_session_ttl(self):
        return int(self.value("admin.session_ttl", 43200))

    @property
    def admin_allow_remote(self):
        return bool(self.value("admin.allow_remote", False))

    @property
    def defaults(self):
        return self.section("defaults")

    @property
    def endpoints(self):
        return self.section("endpoints")

    @property
    def allowed_paths(self):
        paths = self.value("endpoints.allowed_paths") or []
        return [str(p) for p in paths if str(p).strip()]

    @property
    def metering(self):
        return self.section("metering")

    @property
    def logging(self):
        return self.section("logging")

    @property
    def nps(self):
        return self.section("nps")

    @property
    def keys_path(self):
        return os.path.join(self.data_dir, "keys.json")

    @property
    def usage_dir(self):
        return os.path.join(self.data_dir, "usage")

    @property
    def usage_log_path(self):
        return os.path.join(self.usage_dir, "usage.jsonl")

    @property
    def log_dir(self):
        return self.path_in_data(self.logging.get("dir"), "logs")

    # -- 校验 -------------------------------------------------------------
    def validate(self):
        if not isinstance(self.raw.get("version", 1), int):
            raise ConfigError("version 必须是整数")
        port = self.value("server.port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError("server.port 非法: %r" % (port,))
        if not str(self.value("server.host", "")).strip():
            raise ConfigError("server.host 不能为空")
        if not re_match(r"^https?://[^\s/]+", self.upstream_url):
            raise ConfigError("upstream.url 必须是 http(s)://host[:port]")
        if not self.upstream_api_key.strip():
            raise ConfigError(
                "upstream.api_key 为空：网关必须用内部 Key 访问 qoder2api"
                "（运行 qoder_public_cli.py init 可自动生成）"
            )
        if not 1 <= self.upstream_max_body <= (1024 ** 3):
            raise ConfigError("upstream.max_body_bytes 超出范围")
        if self.upstream_connect_timeout <= 0 or self.upstream_read_timeout <= 0:
            raise ConfigError("upstream 超时必须为正数")
        admin_port = self.value("admin.port")
        if not isinstance(admin_port, int) or not 1 <= admin_port <= 65535:
            raise ConfigError("admin.port 非法: %r" % (admin_port,))
        if admin_port == port:
            raise ConfigError("admin.port 不能与 server.port 相同")
        if not self.admin_allow_remote and self.admin_host not in (
            "127.0.0.1", "::1", "localhost"
        ):
            raise ConfigError(
                "admin.host=%r 但 admin.allow_remote=false；要么改回 127.0.0.1，"
                "要么显式打开 allow_remote" % (self.admin_host,)
            )
        password_hash = self.admin_password_hash
        if not password_hash and not self.admin_password_override:
            raise ConfigError(
                "admin.password_hash 为空：运行 qoder_public_cli.py init 生成随机口令"
            )
        if password_hash and not password_hash.startswith("pbkdf2_sha256$"):
            raise ConfigError("admin.password_hash 格式不认识（应为 pbkdf2_sha256$...）")
        if not self.allowed_paths:
            raise ConfigError("endpoints.allowed_paths 不能为空")
        for path in self.allowed_paths:
            if not path.startswith("/") or ".." in path or "//" in path:
                raise ConfigError("endpoint 路径非法: %r" % (path,))
        realm = str(self.defaults.get("realm") or "").strip().lower()
        if realm not in REALMS:
            raise ConfigError("defaults.realm 只能是 cn / intl / 空")
        if self.tls_enabled:
            for label, path in (
                ("tls.cert_file", self.tls_cert_file),
                ("tls.key_file", self.tls_key_file),
            ):
                if not path:
                    raise ConfigError("%s 未配置但 tls.enabled=true" % label)
                if not os.path.isfile(path):
                    raise ConfigError("%s 不存在: %s" % (label, path))
        nps_mode = str(self.nps.get("mode") or "domain").strip().lower()
        if nps_mode not in ("domain", "tcp"):
            raise ConfigError("nps.mode 只能是 domain / tcp")
        if nps_mode == "tcp":
            nps_port = self.nps.get("server_port")
            if not isinstance(nps_port, int) or not 1 <= nps_port <= 65535:
                raise ConfigError("nps.server_port 非法（tcp 模式必填）")
        return self

    # -- 便捷 -------------------------------------------------------------
    def verify_admin_password(self, password):
        if self.admin_password_override:
            return hmac.compare_digest(
                str(password).encode("utf-8"),
                self.admin_password_override.encode("utf-8"),
            )
        return bool(password) and verify_password(password, self.admin_password_hash)

    def describe(self):
        """给 doctor/status 用的脱敏快照。"""
        return {
            "config_path": self.path,
            "data_dir": self.data_dir,
            "server": "%s:%d" % (self.server_host, self.server_port),
            "tls": self.tls_enabled,
            "upstream": self.upstream_url,
            "upstream_key": mask(self.upstream_api_key),
            "admin": "%s:%d" % (self.admin_host, self.admin_port),
            "nps_mode": self.nps.get("mode"),
            "allowed_paths": self.allowed_paths,
        }


def re_match(pattern, text):
    import re
    return bool(re.match(pattern, str(text or "")))


def default_config_path(data_dir=None):
    data_dir = data_dir or DEFAULT_DATA_DIR
    return os.path.join(os.path.abspath(data_dir), DEFAULT_CONFIG_NAME)


def load(path=None, env=None, require=True):
    """加载并校验配置。require=False 时返回 None（仅用于 init 前探测）。"""
    env = dict(os.environ if env is None else env)
    path = path or env.get("QDP_CONFIG") or default_config_path(
        env.get("QDP_DATA_DIR"))
    if not os.path.isfile(path):
        if require:
            raise ConfigError(
                "配置文件不存在: %s\n先运行: python qoder_public_cli.py init" % path
            )
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except ValueError as exc:
        raise ConfigError("配置文件不是合法 JSON: %s (%s)" % (path, exc))
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象: %s" % path)
    merged = _deep_merge(DEFAULTS, raw)
    cfg = Config(merged, path=path, env=env)
    return cfg


def write_config(path, raw):
    write_json_atomic(path, raw, mode=0o600)
    return path
