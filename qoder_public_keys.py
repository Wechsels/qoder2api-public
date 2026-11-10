"""API-key store for the qoder2api-public gateway (hashed, per-user).

One JSON file, no database.  Each key is stored as a SHA-256 digest plus a
display prefix -- the plaintext is only ever returned **once** by
:meth:`KeyStore.create` / :meth:`KeyStore.rotate`.

Record shape (see docs/design-public-gateway.md §5.1)::

    {"id": "k_7f3a91c2", "name": "alice-laptop", "owner": "alice",
     "prefix": "qdp-3f2a91", "hash": "sha256:9b1f...", "enabled": true,
     "created_at": "...", "expires_at": null,
     "rpm": 60, "max_concurrent": 4, "daily_tokens": 200000,
     "monthly_tokens": 0, "allowed_models": ["qwen*"], "allowed_ips": [],
     "realm": "", "note": "", "last_used_at": null,
     "totals": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "errors": 0}}

Runtime state (RPM windows, concurrency, daily counters) lives in the
gateway process, not here -- this module only owns durable policy.
"""

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import qoder_public_config as public_config

KEY_PREFIX = "qdp-"
SECRET_BYTES = 32
MAX_KEYS = 200

REALMS = ("", "cn", "intl")


class KeyStoreError(ValueError):
    """调用方错误（参数非法/找不到 Key），信息可直接展示给管理员。"""


def _now():
    return datetime.now().astimezone()


def iso_now():
    return _now().replace(microsecond=0).isoformat()


def _normalize_iso(value, field):
    """接受 ISO8601（含 Z / 带偏移），返回带本地时区的 ISO 串；None 合法。"""
    if value in (None, "", 0):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise KeyStoreError("%s 不是合法的 ISO8601 时间: %r" % (field, value))
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.replace(microsecond=0).isoformat()


def _digest(secret):
    return "sha256:" + hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


def _clean_patterns(value, field="allowed_models"):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        raise KeyStoreError("%s 必须是列表" % field)
    out = []
    for item in value:
        pattern = str(item or "").strip().lower()
        if not pattern:
            continue
        if len(pattern) > 64:
            raise KeyStoreError("%s 里有超长 pattern: %r" % (field, pattern))
        if pattern not in out:
            out.append(pattern)
    if len(out) > 100:
        raise KeyStoreError("%s 最多 100 条" % field)
    return out


def _clean_ips(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        raise KeyStoreError("allowed_ips 必须是列表")
    out = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise KeyStoreError("allowed_ips 里有非法 IP/CIDR: %r" % (text,))
        normalized = str(network)
        if normalized not in out:
            out.append(normalized)
    return out


def _clean_int(value, field, low, high, default=0):
    if value is None:
        return default
    if isinstance(value, bool):
        raise KeyStoreError("%s 不能是布尔值" % field)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise KeyStoreError("%s 必须是整数: %r" % (field, value))
    if not low <= number <= high:
        raise KeyStoreError("%s 超出范围 [%d, %d]: %r" % (field, low, high, number))
    return number


def _clean_realm(value):
    realm = str(value or "").strip().lower()
    if realm not in REALMS:
        raise KeyStoreError("realm 只能是 cn / intl / 空")
    return realm


class KeyStore(object):
    """线程安全的 Key 存储；每次变更原子落盘，统计字段低频合并。"""

    def __init__(self, path, log=None, defaults=None):
        self.path = os.path.abspath(path)
        self.log = log or (lambda message, **kwargs: None)
        self.defaults = dict(defaults or {})
        self._lock = threading.RLock()
        self._keys = []
        self._dirty = False
        self._stop = threading.Event()
        self._flusher = None
        self.load()

    # ------------------------------------------------------------- storage
    def load(self):
        with self._lock:
            if not os.path.isfile(self.path):
                self._keys = []
                return []
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except ValueError as exc:
                raise KeyStoreError("keys.json 不是合法 JSON: %s" % exc)
            keys = data.get("keys") if isinstance(data, dict) else None
            if not isinstance(keys, list):
                raise KeyStoreError("keys.json 缺少 keys 列表")
            cleaned = []
            for raw in keys:
                record = self._normalize(raw)
                if record is not None:
                    cleaned.append(record)
            self._keys = cleaned
            return cleaned

    def _normalize(self, raw):
        """坏记录跳过（不 fail-open 成无限额）。"""
        if not isinstance(raw, dict):
            return None
        digest = str(raw.get("hash") or "")
        key_id = str(raw.get("id") or "")
        if not digest.startswith("sha256:") or not key_id:
            self.log("keys: 跳过无法识别的记录 id=%r" % (key_id,), level="WARN")
            return None
        record = {
            "id": key_id,
            "name": str(raw.get("name") or "未命名")[:64],
            "owner": str(raw.get("owner") or "")[:64],
            "prefix": str(raw.get("prefix") or "")[:16],
            "hash": digest,
            "enabled": raw.get("enabled", True) is not False,
            "deleted_at": _normalize_iso(raw.get("deleted_at"), "deleted_at"),
            "created_at": str(raw.get("created_at") or iso_now()),
            "expires_at": _normalize_iso(raw.get("expires_at"), "expires_at"),
            "rpm": _clean_int(raw.get("rpm"), "rpm", 0, 100000, 0),
            "max_concurrent": _clean_int(
                raw.get("max_concurrent"), "max_concurrent", 1, 64, 1),
            "daily_tokens": _clean_int(
                raw.get("daily_tokens"), "daily_tokens", 0, 10 ** 12, 0),
            "monthly_tokens": _clean_int(
                raw.get("monthly_tokens"), "monthly_tokens", 0, 10 ** 12, 0),
            "allowed_models": _clean_patterns(raw.get("allowed_models")),
            "allowed_ips": _clean_ips(raw.get("allowed_ips")),
            "realm": _clean_realm(raw.get("realm")),
            "note": str(raw.get("note") or "")[:160],
            "last_used_at": _normalize_iso(raw.get("last_used_at"), "last_used_at"),
            "totals": self._clean_totals(raw.get("totals")),
        }
        return record

    @staticmethod
    def _clean_totals(raw):
        raw = raw if isinstance(raw, dict) else {}
        out = {}
        for field in ("requests", "prompt_tokens", "completion_tokens", "errors"):
            try:
                out[field] = max(0, int(raw.get(field) or 0))
            except (TypeError, ValueError):
                out[field] = 0
        return out

    def save(self):
        with self._lock:
            public_config.write_json_atomic(
                self.path, {"version": 1, "keys": self._keys}, mode=0o600)
            self._dirty = False

    def mark_dirty(self):
        self._dirty = True

    def flush(self, force=False):
        with self._lock:
            if not self._dirty and not force:
                return False
            try:
                self.save()
            except Exception as exc:  # 落盘失败不能影响在线请求
                self.log("keys: 保存失败 %s" % exc, level="ERROR")
                return False
            return True

    def start_autosave(self, interval=5.0):
        if self._flusher is not None:
            return
        def _loop():
            while not self._stop.wait(interval):
                self.flush()
        self._flusher = threading.Thread(
            target=_loop, name="qdp-keys-flush", daemon=True)
        self._flusher.start()

    def stop_autosave(self):
        self._stop.set()
        self.flush(force=True)
        self._flusher = None

    # -------------------------------------------------------------- queries
    def list(self, include_deleted=False):
        with self._lock:
            return [dict(record) for record in self._keys
                    if include_deleted or not record.get("deleted_at")]

    def get(self, key_id, include_deleted=True):
        with self._lock:
            for record in self._keys:
                if record["id"] == key_id:
                    if record.get("deleted_at") and not include_deleted:
                        return None
                    return dict(record)
            return None

    def find_by_name(self, name):
        name = str(name or "").strip()
        with self._lock:
            for record in self._keys:
                if record.get("deleted_at"):
                    continue
                if record["name"] == name:
                    return dict(record)
        return None

    def lookup(self, secret):
        """按明文 Key 找到记录：返回 (record|None, reason)。

        reason ∈ {"ok", "unknown", "disabled", "expired"}；调用方对外的
        响应一律 401，reason 只进审计，避免区分「不存在/被禁用」。
        """
        secret = str(secret or "").strip()
        if not secret:
            return None, "unknown"
        digest = _digest(secret).encode("ascii")
        found = None
        with self._lock:
            for record in self._keys:
                if record.get("deleted_at"):
                    continue
                if hmac.compare_digest(record["hash"].encode("ascii"), digest):
                    found = dict(record)
                    break
        if found is None:
            return None, "unknown"
        if not found["enabled"]:
            return found, "disabled"
        if found["expires_at"] and _is_past(found["expires_at"]):
            return found, "expired"
        return found, "ok"

    # ------------------------------------------------------------- mutation
    def _policy_from_defaults(self, overrides):
        defaults = self.defaults or {}
        policy = {
            "rpm": overrides.get("rpm", defaults.get("rpm", 60)),
            "max_concurrent": overrides.get(
                "max_concurrent", defaults.get("max_concurrent", 4)),
            "daily_tokens": overrides.get(
                "daily_tokens", defaults.get("daily_tokens", 0)),
            "monthly_tokens": overrides.get(
                "monthly_tokens", defaults.get("monthly_tokens", 0)),
            "allowed_models": overrides.get(
                "allowed_models", defaults.get("allowed_models", [])),
            "realm": overrides.get("realm", defaults.get("realm", "")),
        }
        return policy

    def create(self, name, owner="", note="", expires_at=None, allowed_ips=None,
               **policy_overrides):
        name = str(name or "").strip()
        if not 1 <= len(name) <= 64:
            raise KeyStoreError("name 必填，1~64 字符")
        with self._lock:
            if self.find_by_name(name):
                raise KeyStoreError("已存在同名 Key: %s" % name)
            if len([r for r in self._keys if not r.get("deleted_at")]) >= MAX_KEYS:
                raise KeyStoreError("Key 数量达到上限 %d" % MAX_KEYS)
            policy = self._policy_from_defaults(policy_overrides)
            secret = KEY_PREFIX + secrets.token_urlsafe(SECRET_BYTES)
            record = {
                "id": "k_" + secrets.token_hex(4),
                "name": name,
                "owner": str(owner or "")[:64],
                "prefix": secret[:12],
                "hash": _digest(secret),
                "enabled": True,
                "deleted_at": None,
                "created_at": iso_now(),
                "expires_at": _normalize_iso(expires_at, "expires_at"),
                "rpm": _clean_int(policy["rpm"], "rpm", 0, 100000, 60),
                "max_concurrent": _clean_int(
                    policy["max_concurrent"], "max_concurrent", 1, 64, 4),
                "daily_tokens": _clean_int(
                    policy["daily_tokens"], "daily_tokens", 0, 10 ** 12, 0),
                "monthly_tokens": _clean_int(
                    policy["monthly_tokens"], "monthly_tokens", 0, 10 ** 12, 0),
                "allowed_models": _clean_patterns(policy["allowed_models"]),
                "allowed_ips": _clean_ips(allowed_ips),
                "realm": _clean_realm(policy["realm"]),
                "note": str(note or "")[:160],
                "last_used_at": None,
                "totals": self._clean_totals({}),
            }
            self._keys.append(record)
            self.save()
            self.log("keys: 新建 %s (%s)" % (record["id"], record["name"]),
                     tag="keys")
            return dict(record), secret

    def update(self, key_id, **fields):
        editable = ("name", "owner", "note", "expires_at", "rpm",
                    "max_concurrent", "daily_tokens", "monthly_tokens",
                    "allowed_models", "allowed_ips", "realm")
        unknown = set(fields) - set(editable)
        if unknown:
            raise KeyStoreError("不可修改的字段: %s" % ", ".join(sorted(unknown)))
        with self._lock:
            record = self._require(key_id)
            if "name" in fields:
                name = str(fields["name"] or "").strip()
                if not 1 <= len(name) <= 64:
                    raise KeyStoreError("name 必填，1~64 字符")
                existing = self.find_by_name(name)
                if existing and existing["id"] != key_id:
                    raise KeyStoreError("已存在同名 Key: %s" % name)
                record["name"] = name
            if "owner" in fields:
                record["owner"] = str(fields["owner"] or "")[:64]
            if "note" in fields:
                record["note"] = str(fields["note"] or "")[:160]
            if "expires_at" in fields:
                record["expires_at"] = _normalize_iso(
                    fields["expires_at"], "expires_at")
            if "rpm" in fields:
                record["rpm"] = _clean_int(fields["rpm"], "rpm", 0, 100000, 0)
            if "max_concurrent" in fields:
                record["max_concurrent"] = _clean_int(
                    fields["max_concurrent"], "max_concurrent", 1, 64, 1)
            if "daily_tokens" in fields:
                record["daily_tokens"] = _clean_int(
                    fields["daily_tokens"], "daily_tokens", 0, 10 ** 12, 0)
            if "monthly_tokens" in fields:
                record["monthly_tokens"] = _clean_int(
                    fields["monthly_tokens"], "monthly_tokens", 0, 10 ** 12, 0)
            if "allowed_models" in fields:
                record["allowed_models"] = _clean_patterns(
                    fields["allowed_models"])
            if "allowed_ips" in fields:
                record["allowed_ips"] = _clean_ips(fields["allowed_ips"])
            if "realm" in fields:
                record["realm"] = _clean_realm(fields["realm"])
            self.save()
            return dict(record)

    def rotate(self, key_id):
        with self._lock:
            record = self._require(key_id)
            secret = KEY_PREFIX + secrets.token_urlsafe(SECRET_BYTES)
            record["hash"] = _digest(secret)
            record["prefix"] = secret[:12]
            self.save()
            self.log("keys: 轮换 %s (%s)" % (record["id"], record["name"]),
                     tag="keys")
            return dict(record), secret

    def set_enabled(self, key_id, enabled):
        with self._lock:
            record = self._require(key_id)
            record["enabled"] = bool(enabled)
            self.save()
            return dict(record)

    def delete(self, key_id):
        """软删：保留流水可追溯（totals 与 id 仍在文件里）。"""
        with self._lock:
            record = self._require(key_id)
            if not record.get("deleted_at"):
                record["deleted_at"] = iso_now()
                record["enabled"] = False
                self.save()
            return dict(record)

    def _require(self, key_id):
        for record in self._keys:
            if record["id"] == key_id:
                return record
        raise KeyStoreError("找不到 Key: %s" % key_id)

    # -------------------------------------------------------------- runtime
    def add_totals(self, key_id, requests=0, prompt=0, completion=0, errors=0,
                   touch=True):
        with self._lock:
            for record in self._keys:
                if record["id"] == key_id:
                    totals = record["totals"]
                    totals["requests"] += int(requests)
                    totals["prompt_tokens"] += int(prompt)
                    totals["completion_tokens"] += int(completion)
                    totals["errors"] += int(errors)
                    if touch:
                        record["last_used_at"] = iso_now()
                    self._dirty = True
                    return dict(record)
        return None

    # --------------------------------------------------------------- policy
    def model_allowed(self, record, model):
        patterns = _clean_patterns((record or {}).get("allowed_models"))
        if not patterns:
            return True
        name = str(model or "").strip().lower()
        if not name:
            return False
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)

    def ip_allowed(self, record, ip):
        networks = _clean_ips((record or {}).get("allowed_ips"))
        if not networks:
            return True
        try:
            address = ipaddress.ip_address(str(ip or "").strip())
        except ValueError:
            return False
        return any(address in ipaddress.ip_network(net, strict=False)
                   for net in networks)

    def is_active(self, record):
        if not record or record.get("deleted_at") or not record.get("enabled"):
            return False
        expires_at = record.get("expires_at")
        return not (expires_at and _is_past(expires_at))


def _is_past(iso_text):
    try:
        moment = datetime.fromisoformat(
            str(iso_text).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment <= datetime.now(timezone.utc).astimezone()
    except Exception:
        return False
