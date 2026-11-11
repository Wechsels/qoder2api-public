"""Usage metering for the qoder2api-public gateway.

Two jobs, both stdlib-only:

1. **旁路解析上游响应**（:class:`StreamMeter`）：网关把发给客户端的字节
   同步喂进来，这里只统计 token，不改变任何输出字节。
   - OpenAI 非流式：`usage.prompt_tokens/completion_tokens`
   - OpenAI 流式：最后一个 `data:` 帧里的 `usage`（网关可注入
     `stream_options.include_usage=true` 让它出现）
   - Anthropic：`message_start.message.usage` + `message_delta.usage`
   - 上游没给 usage（截断/异常）时按字符数估算，并在流水里标记
     `estimated=true`，绝不把估算值伪装成精确值。

2. **用量流水与配额账本**（:class:`UsageLog`）：每请求一行 JSONL，内存维护
   按 Key 的日/月累计（启动时回放最近 70 天，配额按 UTC 自然日/月）。
"""

import json
import math
import os
import threading
import time
from datetime import datetime, timezone, timedelta

PROTO_OPENAI = "openai"
PROTO_ANTHROPIC = "anthropic"

MAX_JSON_PARSE = 4 * 1024 * 1024      # 非流式响应最多解析 4MB
MAX_TEXT_TRACK = 256 * 1024           # 估算用正文最多累计 256KB
MAX_LINE_TAIL = 64 * 1024             # SSE 半行缓冲上限

ESTIMATE_CHARS_PER_TOKEN = 4.0


def protocol_for_path(path):
    path = str(path or "")
    if path.startswith("/v1/messages"):
        return PROTO_ANTHROPIC
    return PROTO_OPENAI


def _utc_date(moment=None):
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _utc_month(moment=None):
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m")


def _parse_ts(text):
    text = str(text or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment
    except ValueError:
        return None


def _as_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


class Usage(object):
    """一次请求的 token 计量结果。"""

    __slots__ = ("prompt", "completion", "total", "estimated")

    def __init__(self, prompt=0, completion=0, total=None, estimated=False):
        self.prompt = max(0, int(prompt or 0))
        self.completion = max(0, int(completion or 0))
        self.total = (self.prompt + self.completion) if total is None \
            else max(0, int(total))
        self.estimated = bool(estimated)

    @property
    def total_tokens(self):
        return max(self.total, self.prompt + self.completion)

    def as_dict(self):
        return {"prompt_tokens": self.prompt,
                "completion_tokens": self.completion,
                "total_tokens": self.total_tokens,
                "estimated": self.estimated}

    def __repr__(self):
        return "Usage(prompt=%d, completion=%d, estimated=%s)" % (
            self.prompt, self.completion, self.estimated)


def estimate_tokens(chars):
    return max(0, int(math.ceil(max(0, chars) / ESTIMATE_CHARS_PER_TOKEN)))


class StreamMeter(object):
    """旁路 meter：feed() 任意字节块，finish() 给出 Usage。"""

    def __init__(self, path, request_body_bytes=0):
        self.protocol = protocol_for_path(path)
        self.request_body_bytes = max(0, int(request_body_bytes or 0))
        self._raw = bytearray()          # 非流式：完整 body（有上限）
        self._tail = ""                  # 流式：未处理完的半行
        self._text_chars = 0             # 估算用：正文/工具参数累计字符数
        self._saw_sse = False
        self._truncated = False
        self._prompt = 0
        self._completion = 0
        self._total = None
        self._usage_seen = False
        self._closed = False

    # ------------------------------------------------------------- feeding
    def feed(self, data):
        if not data or self._closed:
            return
        chunk = bytes(data)
        if not self._saw_sse and self._looks_like_sse(chunk):
            self._saw_sse = True
            # 只保留尚未处理的旧 _raw 前缀；新 chunk 由 _feed_sse 恰好处理一次
            self._tail = self._raw.decode("utf-8", "replace")
            self._raw = bytearray()
        if self._saw_sse:
            self._feed_sse(chunk)
            return
        if len(self._raw) < MAX_JSON_PARSE:
            room = MAX_JSON_PARSE - len(self._raw)
            self._raw.extend(chunk[:room])
            if len(chunk) > room:
                self._truncated = True
        else:
            self._truncated = True

    @staticmethod
    def _looks_like_sse(chunk):
        head = chunk[:64].lstrip()
        return head.startswith(b"data:") or head.startswith(b"event:")

    def _feed_sse(self, chunk):
        text = self._tail + chunk.decode("utf-8", "replace")
        lines = text.split("\n")
        self._tail = lines.pop() if lines else ""
        if len(self._tail) > MAX_LINE_TAIL:
            self._tail = ""
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if isinstance(event, dict):
                self._absorb_event(event)

    # ------------------------------------------------------------ absorbing
    def _absorb_event(self, event):
        usage = event.get("usage")
        if isinstance(usage, dict):
            self._absorb_usage(usage)
        if self.protocol == PROTO_ANTHROPIC:
            kind = event.get("type")
            if kind == "message_start":
                message = event.get("message") or {}
                inner = message.get("usage") if isinstance(message, dict) else None
                if isinstance(inner, dict):
                    self._absorb_usage(inner)
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                self._track_text(delta.get("text"))
                self._track_text(delta.get("partial_json"))
            elif kind == "content_block_start":
                block = event.get("content_block") or {}
                self._track_text(block.get("text"))
        else:
            if event.get("type") == "response.output_text.delta":
                self._track_text(event.get("delta"))
            choices = event.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0] if isinstance(choices[0], dict) else {}
                delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
                self._track_text(delta.get("content"))
                self._track_text(first.get("text"))
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for call in tool_calls:
                        if isinstance(call, dict):
                            function = call.get("function") or {}
                            self._track_text(function.get("arguments"))

    def _absorb_usage(self, usage):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = usage.get("total_tokens")
        cached = _as_int(usage.get("cache_creation_input_tokens")) + \
            _as_int(usage.get("cache_read_input_tokens"))
        if prompt is None and "input_tokens" in usage:
            prompt = _as_int(usage.get("input_tokens")) + cached
        if prompt is None and "input" in usage:
            prompt = usage.get("input")
        if completion is None and "output_tokens" in usage:
            completion = usage.get("output_tokens")
        if completion is None and "output" in usage:
            completion = usage.get("output")
        if prompt is None and completion is None and total is None:
            return
        self._prompt = max(self._prompt, _as_int(prompt)) + (
            cached if self.protocol == PROTO_ANTHROPIC and self._usage_seen
            and prompt is None and completion is not None else 0)
        self._completion = max(self._completion, _as_int(completion))
        self._total = _as_int(total) if total is not None else None
        self._usage_seen = True

    def _track_text(self, value):
        if not value or self._text_chars >= MAX_TEXT_TRACK:
            return
        if isinstance(value, str):
            self._text_chars += len(value)

    # -------------------------------------------------------------- finish
    def finish(self):
        self._closed = True
        if not self._saw_sse and self._raw:
            self._absorb_json_blob(bytes(self._raw))
        if self.protocol == PROTO_ANTHROPIC and not self._usage_seen:
            # 非流式 Anthropic 响应也走 JSON 解析；已经解析过则不会到这
            pass
        if not self._saw_sse and self.protocol == PROTO_OPENAI:
            try:
                event = json.loads(self._raw.decode("utf-8"))
                for block in event.get("output") or []:
                    for content in block.get("content") or []:
                        self._track_text(content.get("text"))
            except (ValueError, AttributeError, TypeError):
                pass
        if self._usage_seen:
            completion = self._completion
            prompt = self._prompt
            if not completion and not prompt and self._total:
                completion = self._total
            return Usage(prompt, completion, self._total, estimated=False)
        prompt = estimate_tokens(self.request_body_bytes)
        completion = estimate_tokens(self._text_chars)
        return Usage(prompt, completion, None, estimated=True)

    def _absorb_json_blob(self, blob):
        if not blob or self._truncated:
            return
        try:
            event = json.loads(blob.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        usage = event.get("usage")
        if isinstance(usage, dict):
            self._absorb_usage(usage)
        if self.protocol == PROTO_ANTHROPIC:
            for block in event.get("content") or []:
                if isinstance(block, dict):
                    self._track_text(block.get("text"))
        else:
            for choice in event.get("choices") or []:
                if isinstance(choice, dict):
                    message = choice.get("message") or {}
                    if isinstance(message, dict):
                        self._track_text(message.get("content"))
                    self._track_text(choice.get("text"))
                    for call in message.get("tool_calls") or []:
                        if isinstance(call, dict):
                            function = call.get("function") or {}
                            self._track_text(function.get("arguments"))


class UsageLog(object):
    """JSONL 流水 + 内存日/月账本（配额判断与看板都用它）。"""

    KEEP_DAYS = 70

    def __init__(self, path, log=None, rotate_mb=20, keep=5):
        self.path = os.path.abspath(path)
        self.log = log or (lambda message, **kwargs: None)
        self.rotate_bytes = max(1, int(rotate_mb)) * 1024 * 1024
        self.keep = max(1, min(50, int(keep)))
        self._lock = threading.RLock()
        # key_id -> {"days": {"YYYY-MM-DD": [requests, prompt, completion,
        #                                  errors]}, "last_at": str}
        self._agg = {}
        self._rebuild()

    # ------------------------------------------------------------- storage
    def _rotate_if_needed(self):
        try:
            if os.path.getsize(self.path) < self.rotate_bytes:
                return
        except OSError:
            return
        try:
            for index in range(self.keep - 1, 0, -1):
                src = "%s.%d" % (self.path, index)
                dst = "%s.%d" % (self.path, index + 1)
                if os.path.isfile(src):
                    os.replace(src, dst)
            os.replace(self.path, self.path + ".1")
        except OSError as exc:
            self.log("usage: 轮转失败 %s" % exc, level="WARN")
        else:
            self.log("usage: 流水轮转（>%dMB）" % (self.rotate_bytes // (1024 * 1024)),
                     tag="usage")

    def append(self, row):
        """幂等写一行；写入失败只告警，不影响在线请求。"""
        if not isinstance(row, dict):
            return
        line = json.dumps(row, ensure_ascii=False, sort_keys=False)
        with self._lock:
            self._absorb(row)
            try:
                directory = os.path.dirname(self.path)
                if directory and not os.path.isdir(directory):
                    os.makedirs(directory, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                self._rotate_if_needed()
            except OSError as exc:
                self.log("usage: 写入失败 %s" % exc, level="WARN")

    def _absorb(self, row):
        key_id = str(row.get("key_id") or "(no-key)")
        day = _utc_date(_parse_ts(row.get("ts")))
        bucket = self._agg.setdefault(key_id, {"days": {}, "last_at": ""})
        slot = bucket["days"].get(day)
        if slot is None:
            slot = [0, 0, 0, 0]
            bucket["days"][day] = slot
        slot[0] += 1
        slot[1] += _as_int(row.get("prompt_tokens"))
        slot[2] += _as_int(row.get("completion_tokens"))
        if _as_int(row.get("status")) >= 400:
            slot[3] += 1
        bucket["last_at"] = str(row.get("ts") or bucket["last_at"])
        self._prune()

    def _prune(self):
        cutoff = _utc_date(datetime.now(timezone.utc) - 
                           timedelta(days=self.KEEP_DAYS))
        for bucket in self._agg.values():
            stale = [day for day in bucket["days"] if day < cutoff]
            for day in stale:
                bucket["days"].pop(day, None)

    def _rebuild(self):
        """重启回放：轮转文件与当前文件都算，否则重启即重置配额。"""
        cutoff = _utc_date(datetime.now(timezone.utc) -
                           timedelta(days=self.KEEP_DAYS))
        for row in self._read_lines(self._log_paths()):
            if _utc_date(_parse_ts(row.get("ts"))) < cutoff:
                continue
            self._absorb(row)

    def _read_lines(self, paths):
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict):
                            yield row
            except OSError:
                continue

    def _log_paths(self):
        rotated = [self.path + ".%d" % index
                 for index in range(1, self.keep + 1)]
        return list(reversed(rotated)) + [self.path]

    def rows(self, days=7, key_id=None, limit=200):
        """最近 N 天的请求流水（新→旧）。"""
        cutoff = _utc_date(datetime.now(timezone.utc) - timedelta(days=max(1, int(days))))
        out = []
        for row in self._read_lines(self._log_paths()):
            if _utc_date(_parse_ts(row.get("ts"))) < cutoff:
                continue
            if key_id and str(row.get("key_id") or "") != str(key_id):
                continue
            out.append(row)
        out.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
        return out[:max(1, int(limit))]

    def summary(self, days=7, names=None):
        """按 Key 聚合（看板主表）。"""
        cutoff = _utc_date(datetime.now(timezone.utc) - timedelta(days=max(1, int(days))))
        out = []
        with self._lock:
            for key_id, bucket in self._agg.items():
                requests = prompt = completion = errors = 0
                last_at = bucket.get("last_at") or ""
                for day, slot in bucket["days"].items():
                    if day < cutoff:
                        continue
                    requests += slot[0]
                    prompt += slot[1]
                    completion += slot[2]
                    errors += slot[3]
                if not requests:
                    continue
                out.append({
                    "key_id": key_id,
                    "name": (names or {}).get(key_id, ""),
                    "requests": requests,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "errors": errors,
                    "last_at": last_at,
                })
        out.sort(key=lambda item: item["requests"], reverse=True)
        return out

    def daily(self, days=7, key_id=None):
        cutoff = _utc_date(datetime.now(timezone.utc) - timedelta(days=max(1, int(days))))
        totals = {}
        with self._lock:
            for current_id, bucket in self._agg.items():
                if key_id and current_id != str(key_id):
                    continue
                for day, slot in bucket["days"].items():
                    if day < cutoff:
                        continue
                    entry = totals.setdefault(day, [0, 0, 0, 0])
                    entry[0] += slot[0]
                    entry[1] += slot[1]
                    entry[2] += slot[2]
                    entry[3] += slot[3]
        return [{"date": day, "requests": value[0],
                 "prompt_tokens": value[1], "completion_tokens": value[2],
                 "errors": value[3]}
                for day, value in sorted(totals.items())]

    def totals(self, key_id, now=None):
        """配额判断用：UTC 日/月 token 累计 + 下次重置时间。"""
        moment = now or datetime.now(timezone.utc)
        day = _utc_date(moment)
        month = _utc_month(moment)
        day_tokens = month_tokens = day_requests = 0
        with self._lock:
            bucket = self._agg.get(str(key_id))
            if bucket:
                for current_day, slot in bucket["days"].items():
                    if current_day == day:
                        day_tokens += slot[1] + slot[2]
                        day_requests += slot[0]
                    if current_day.startswith(month):
                        month_tokens += slot[1] + slot[2]
        next_day = datetime(moment.year, moment.month, moment.day,
                            tzinfo=timezone.utc) + timedelta(days=1)
        if month and int(month[-2:]) == 12:
            next_month = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(moment.year, int(month[-2:]) + 1, 1,
                                  tzinfo=timezone.utc)
        return {"day_tokens": day_tokens, "month_tokens": month_tokens,
                "day_requests": day_requests,
                "reset_at": next_day.isoformat(),
                "month_reset_at": next_month.isoformat()}
