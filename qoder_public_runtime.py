#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-platform supervisor for the qoder2api-public stack.

Starts and watches, in order:

    1. qoder2api (qoder_proxy.py)        127.0.0.1:8790  --api-key <internal>
    2. public gateway (--with-admin)      127.0.0.1:8791  + admin 8792
    3. npc (domain tunnel)                only when nps.server_addr is configured

and stops them in reverse order (npc -> gateway -> upstream).  The gateway's
/healthz carries the child PID, so readiness proves the supervisor really
started *its own* process, not a leftover from an earlier run.

State lives in ``public-data/runtime.json`` (atomic writes) and a single
instance is enforced with an OS lock (fcntl on POSIX, msvcrt on Windows).
Never kill by name: only the exact PIDs recorded in runtime.json are
touched, and stale instances are re-verified before being cleaned up.

Usage::

    python qoder_public_runtime.py start|stop|restart|status|run [--config PATH]

stdlib only.  start/stop-qoder-public.(bat|sh) are thin wrappers around this.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

import qoder_public_config as public_config

try:
    import fcntl
except ImportError:                                    # Windows
    fcntl = None
    import msvcrt

VERSION = "1.0.0"
STATE_RUNNING = "running"
STATE_DEGRADED = "degraded"
STATE_FAILED = "failed"
STATE_STOPPING = "stopping"
STATE_STOPPED = "stopped"
STATE_STALE = "stale"

MAX_RESTARTS = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
HEARTBEAT_INTERVAL = 5.0
STOP_TIMEOUT = 10.0
READY_TIMEOUT = 60.0

WINDOWS = os.name == "nt"


def _now():
    return time.time()


def _iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def log(message):
    sys.stdout.write("[runtime] %s\n" % message)
    sys.stdout.flush()


# ------------------------------------------------------------------- locking
class SingleInstanceLock(object):
    """OS-level advisory lock on public-data/runtime.lock.

    Holding the fd keeps the lock; closing it releases.  ``locked_by_other``
    answers "is some other supervisor alive" without stealing the lock.
    """

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            return True
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        except OSError:
            pass
        self._fd = fd
        return True

    def release(self):
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            else:
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def locked_by_other(self):
        if self._fd is not None:
            return False
        probe = SingleInstanceLock(self.path)
        if probe.acquire():
            probe.release()
            return False
        return True


# ------------------------------------------------------------- pid handling
def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        if WINDOWS:
            import ctypes
            SYNCHRONIZE = 0x00100000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not handle:
                return False
            try:
                WAIT_TIMEOUT = 0x102
                return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, OverflowError):
        return False


def process_start_time(pid):
    """Process start time as unix seconds; 0 when unavailable."""
    if not pid or pid <= 0:
        return 0.0
    try:
        if WINDOWS:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
            if not handle:
                return 0.0
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                        handle, ctypes.byref(created), ctypes.byref(exited),
                        ctypes.byref(kernel), ctypes.byref(user)):
                    return 0.0
                stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return stamp / 10.0 ** 7 - 11644473600.0  # FILETIME -> unix
            finally:
                kernel32.CloseHandle(handle)
        with open("/proc/%d/stat" % int(pid), "r", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
            ticks = float(fields[19])
            with open("/proc/stat", "r", encoding="utf-8") as stat_handle:
                for line in stat_handle:
                    if line.startswith("btime "):
                        return float(line.split()[1]) + ticks / os.sysconf("SC_CLK_TCK")
        return 0.0
    except (OSError, ValueError, IndexError, OverflowError):
        return 0.0


def pid_matches(pid, recorded_start, tolerance=2.0):
    """PID reuse guard: the live process must have started when we recorded it."""
    if not pid_alive(pid):
        return False
    if not recorded_start:
        return True
    actual = process_start_time(pid)
    if not actual:
        return True
    return abs(actual - float(recorded_start)) <= tolerance


def terminate_pid(pid, recorded_start, timeout=STOP_TIMEOUT):
    """Stop exactly this PID (never by name); True when it exited."""
    if not pid_matches(pid, recorded_start):
        return False
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           capture_output=True)
        else:
            os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError):
        return not pid_alive(pid)
    deadline = _now() + timeout
    while _now() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        if not WINDOWS:
            os.kill(int(pid), signal.SIGKILL)
    except (OSError, ValueError):
        pass
    deadline = _now() + 3.0
    while _now() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


# -------------------------------------------------------------- http probes
def http_json(url, timeout=3.0, token=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(65536).decode("utf-8", "replace"))


# ---------------------------------------------------------------- launcher
class SubprocessLauncher(object):
    """Spawns children detached from this console; tests can subclass this to
    fake the whole process tree without touching real services."""

    def spawn(self, argv, env=None, cwd=None, log_path=None):
        environment = dict(os.environ)
        if env:
            environment.update(env)
        kwargs = {}
        if WINDOWS:
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True            # own process group
        handle = open(log_path, "ab") if log_path else None
        try:
            return subprocess.Popen(
                argv, env=environment, cwd=cwd,
                stdout=handle or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if handle else subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, **kwargs)
        finally:
            if handle:
                handle.close()

    def alive(self, process):
        return process is not None and process.poll() is None

    def pid_of(self, process):
        return process.pid if process is not None else 0

    def start_time_of(self, process):
        return process_start_time(self.pid_of(process))

    def terminate(self, process, timeout=STOP_TIMEOUT):
        if process is None:
            return True
        pid = self.pid_of(process)
        if WINDOWS:
            # /T 终止整棵进程树，但仍只按这一个精确 PID 出发
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        else:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(3.0)
            except subprocess.TimeoutExpired:
                pass
        return process.poll() is not None


class ServiceSpec(object):
    def __init__(self, name, argv, env=None, cwd=None, cmd_hint="",
                 readiness=None, log_name=None):
        self.name = name
        self.argv = list(argv)
        self.env = dict(env or {})
        self.cwd = cwd
        self.cmd_hint = cmd_hint or " ".join(argv)
        self.readiness = readiness          # callable() -> bool
        self.log_name = log_name or (name + ".log")


# -------------------------------------------------------------- supervisor
class Supervisor(object):
    """Owns exactly one instance; every PID it touches is its own child or
    comes from runtime.json (stale cleanup), never from a name search."""

    ORDER = ("npc", "gateway", "upstream")      # stop order

    def __init__(self, config, root, launcher=None, sleeper=time.sleep,
                 prober=None):
        self.config = config
        self.root = os.path.abspath(root)
        self.launcher = launcher or SubprocessLauncher()
        self.sleep = sleeper
        self._probe = prober or http_json
        self.data_dir = getattr(config, "data_dir", public_config.DEFAULT_DATA_DIR)
        self.runtime_path = os.path.join(self.data_dir, "runtime.json")
        self.lock_path = os.path.join(self.data_dir, "runtime.lock")
        self.control_path = os.path.join(self.data_dir, "runtime.control")
        self.log_dir = os.path.join(self.data_dir, "runtime-logs")
        self.lock = SingleInstanceLock(self.lock_path)
        self.instance = None
        self._specs = []
        self._children = {}                     # name -> Popen-like
        self._restarts = {}
        self._stopping = False
        self._last_heartbeat = 0.0

    # ------------------------------------------------------------- plumbing
    def _base_instance(self):
        return {
            "id": "rt-" + public_config.new_secret(8),
            "created_at": _iso(),
            "manager_start_time": process_start_time(os.getpid()),
        }

    def _write_instance(self, state, extra=None):
        previous = self.instance or self._base_instance()
        processes = {}
        for name, child in self._children.items():
            prior = (self.instance or {}).get("processes", {}).get(name, {})
            processes[name] = {
                "pid": self.launcher.pid_of(child),
                "started_at": prior.get("started_at") or _iso(),
                "start_time": prior.get("start_time")
                or self.launcher.start_time_of(child),
                "restarts": self._restarts.get(name, 0),
                "cmd_hint": prior.get("cmd_hint", ""),
            }
        payload = {
            "version": 1,
            "id": previous.get("id", "rt-unknown"),
            "root": self.root,
            "manager_pid": os.getpid(),
            "manager_start_time": previous.get("manager_start_time", 0),
            "created_at": previous.get("created_at", _iso()),
            "state": state,
            "updated_at": _iso(),
            "python": sys.executable,
            "processes": processes,
        }
        if extra:
            payload.update(extra)
        public_config.write_json_atomic(self.runtime_path, payload)
        self.instance = payload

    def read_instance(self):
        try:
            with open(self.runtime_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    def manager_alive(self, instance):
        if not instance:
            return False
        manager_pid = int(instance.get("manager_pid") or 0)
        if manager_pid == os.getpid():
            return True
        return pid_matches(manager_pid,
                           instance.get("manager_start_time") or 0)

    def _stop_requested(self):
        try:
            with open(self.control_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload.get("action") == "stop"
        except (OSError, ValueError):
            return False

    def _clear_control(self):
        try:
            os.unlink(self.control_path)
        except OSError:
            pass

    # --------------------------------------------------------------- specs
    def build_specs(self):
        config = self.config
        parts = urlsplit(config.upstream_url)
        upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
        upstream_argv = [
            sys.executable, os.path.join(self.root, "qoder_proxy.py"),
            "--host", parts.hostname or "127.0.0.1",
            "--port", str(upstream_port),
            "--api-key", config.upstream_api_key,
        ]
        if config.upstream_panel_password:
            upstream_argv += ["--panel-password", config.upstream_panel_password]
        upstream_argv += ["--pid-file", os.path.join(self.data_dir, "upstream.pid")]
        gateway_argv = [
            sys.executable, os.path.join(self.root, "qoder_public_gateway.py"),
            "--config", config.path or os.path.join(self.data_dir, "config.json"),
        ]
        env = {"QD_UPDATE_DISABLE": "1"}
        specs = [
            ServiceSpec("upstream", upstream_argv, env=env, cwd=self.root,
                        cmd_hint="qoder_proxy.py",
                        readiness=lambda: self._upstream_ready(upstream_port)),
            ServiceSpec("gateway", gateway_argv, env=env, cwd=self.root,
                        cmd_hint="qoder_public_gateway.py",
                        readiness=lambda: self._gateway_ready()),
        ]
        npc = self._npc_spec()
        if npc is not None:
            specs.append(npc)
        self._specs = specs
        return specs

    def _upstream_ready(self, port):
        upstream_pid = self.launcher.pid_of(self._children.get("upstream"))
        try:
            data = self._probe("http://127.0.0.1:%d/health" % port,
                               token=self.config.upstream_api_key)
            if not isinstance(data, dict) or data.get("ok") is False:
                return False
            # /health 回报的 pid 必须是我们自己的子进程：端口上若已有
            # 残留实例（或别人在用），绝不能把它的健康当成我们的成功
            reported = data.get("pid")
            if reported is None:
                return True          # 旧版本上游不带 pid 字段，退化为 200 检查
            return bool(upstream_pid) and int(reported) == int(upstream_pid)
        except Exception:
            return False

    def _gateway_ready(self):
        gateway_pid = self.launcher.pid_of(self._children.get("gateway"))
        try:
            data = self._probe("http://%s:%d/healthz"
                               % (self.config.server_host, self.config.server_port))
            if not isinstance(data, dict) or data.get("status") != "ok":
                return False
            # /healthz 回报的 PID 必须是我们这个子进程：证明不是残留实例
            reported = int(data.get("pid") or 0)
            return bool(gateway_pid) and reported == int(gateway_pid)
        except Exception:
            return False

    def _npc_spec(self):
        nps = self.config.nps or {}
        if not str(nps.get("server_addr") or "").strip():
            return None
        binary = self._npc_binary()
        if not binary:
            raise RuntimeError(
                "nps.server_addr 已配置但找不到 npc 可执行文件；"
                "放到 %s（或设 QDP_NPC_BIN）。二进制不进仓库。"
                % os.path.join(self.data_dir, "tools"))
        conf_path = os.path.join(self.data_dir, "npc.conf")
        self._write_npc_conf(conf_path, nps)
        argv = [binary, "-config=" + conf_path]
        return ServiceSpec("npc", argv, cwd=self.data_dir, cmd_hint="npc",
                           readiness=lambda: True)

    def _npc_binary(self):
        explicit = os.environ.get("QDP_NPC_BIN") or ""
        if explicit:
            return explicit if os.path.isfile(explicit) else ""
        names = ("npc.exe", "npc") if WINDOWS else ("npc", "npc.exe")
        tools = os.path.join(self.data_dir, "tools")
        for name in names:
            candidate = os.path.join(tools, name)
            if os.path.isfile(candidate):
                return candidate
        return ""

    def _write_npc_conf(self, path, nps):
        """domain 模式 host 指公网域名、target 是本机网关；绝不用 public_vkey。"""
        mode = str(nps.get("mode") or "domain").strip().lower()
        target = "127.0.0.1:%d" % self.config.server_port
        if mode == "domain":
            if not str(nps.get("domain") or "").strip():
                raise RuntimeError("nps.mode=domain 需要设置 nps.domain")
            tunnel = "host = %s\ntarget_addr = %s" % (nps["domain"], target)
        else:
            tunnel = ("mode = tcp\nserver_port = %s\ntarget_addr = %s"
                      % (nps.get("server_port", 8443), target))
        text = ("[common]\nserver_addr = %s\nconn_type = tcp\nvkey = %s\n"
                "auto_reconnection = true\ncrypt = %s\ncompress = %s\n"
                "disconnect_timeout = 60\n\n[api]\n%s\n"
                % (nps["server_addr"], nps["vkey"],
                   str(bool(nps.get("crypt", True))).lower(),
                   str(bool(nps.get("compress", True))).lower(), tunnel))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    # ---------------------------------------------------------------- start
    def start(self, specs=None, ready_timeout=READY_TIMEOUT):
        if not self.lock.acquire():
            return 3, "已有 supervisor 在运行（锁 %s 被占用）" % self.lock_path
        specs = list(specs) if specs is not None else self.build_specs()
        self._specs = specs
        os.makedirs(self.log_dir, exist_ok=True)
        self.instance = self._base_instance()
        self._children = {}
        self._restarts = {}
        try:
            for spec in specs:
                self._spawn(spec)
                if not self._await_ready(spec, ready_timeout):
                    self._write_instance(
                        STATE_FAILED, {"error": "%s 就绪检查未通过" % spec.name})
                    self.stop_children()
                    self.lock.release()
                    return 4, "%s 启动后未通过就绪检查" % spec.name
            self._write_instance(STATE_RUNNING)
            return 0, "started"
        except Exception as exc:
            self._write_instance(STATE_FAILED, {"error": str(exc)})
            self.stop_children()
            self.lock.release()
            return 4, str(exc)

    def _spawn(self, spec):
        if self.launcher.alive(self._children.get(spec.name)):
            return
        log_path = os.path.join(self.log_dir, spec.log_name)
        process = self.launcher.spawn(spec.argv, env=spec.env, cwd=spec.cwd,
                                      log_path=log_path)
        self._children[spec.name] = process
        prior = dict((self.instance or {}).get("processes", {}).get(spec.name, {}))
        processes = dict((self.instance or {}).get("processes", {}))
        processes[spec.name] = {
            "pid": self.launcher.pid_of(process),
            "started_at": _iso(),
            "start_time": self.launcher.start_time_of(process),
            "restarts": self._restarts.get(spec.name, 0),
            "cmd_hint": spec.cmd_hint,
        }
        self.instance = dict(self.instance or {}, processes=processes)
        prior.clear()
        log("%s 启动 pid=%d" % (spec.name, self.launcher.pid_of(process)))

    def _await_ready(self, spec, timeout):
        if spec.readiness is None:
            return True
        deadline = _now() + timeout
        while _now() < deadline:
            if not self.launcher.alive(self._children.get(spec.name)):
                return False
            try:
                if spec.readiness():
                    return True
            except Exception:
                pass
            self.sleep(0.3)
        return False

    # ------------------------------------------------------------------ run
    def run(self, specs=None):
        code, _message = self.start(specs)
        if code != 0:
            return code

        def _handle(_signum, _frame):
            self._stopping = True
        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), _handle)
                except (ValueError, OSError):
                    pass
        log("supervisor 运行中（Ctrl+C / stop 命令停止全部服务）")
        try:
            while not self._stopping:
                if self._stop_requested():
                    self._clear_control()
                    break
                state = self._sweep()
                self._heartbeat(state)
                if state == STATE_FAILED:
                    return 1
                self.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return 0

    def _sweep(self):
        """Restart dead children with bounded backoff; classify the state."""
        state = STATE_RUNNING
        for name, child in list(self._children.items()):
            if self.launcher.alive(child):
                continue
            restarts = self._restarts.get(name, 0)
            if restarts >= MAX_RESTARTS:
                log("%s 第 %d 次退出且超过重启上限，标记 failed" % (name, restarts))
                self._stopping = True
                self._write_instance(
                    STATE_FAILED,
                    {"error": "%s 重启超限（%d 次）" % (name, restarts)})
                return STATE_FAILED
            backoff = min(BACKOFF_BASE * (2 ** restarts), BACKOFF_CAP)
            log("%s 意外退出，%.1fs 后第 %d 次重启" % (name, backoff, restarts + 1))
            state = STATE_DEGRADED
            self.sleep(backoff)
            if self._stop_requested() or self._stopping:
                return state
            self._restarts[name] = restarts + 1
            self._spawn(self._spec_for(name))
            # 重启完成后再落盘，restarts/新 PID 是最新值
            self._write_instance(STATE_DEGRADED)
        return state

    def _spec_for(self, name):
        for spec in self._specs:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def _heartbeat(self, state):
        if _now() - self._last_heartbeat < HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = _now()
        self._write_instance(state)

    # ----------------------------------------------------------------- stop
    def stop_children(self):
        for name in self.ORDER:
            child = self._children.pop(name, None)
            if child is not None:
                log("%s 停止 pid=%d" % (name, self.launcher.pid_of(child)))
                self.launcher.terminate(child)
        for name, child in list(self._children.items()):
            log("%s 停止 pid=%d" % (name, self.launcher.pid_of(child)))
            self.launcher.terminate(child)
            self._children.pop(name, None)

    def stop(self):
        self._stopping = True
        self._write_instance(STATE_STOPPING)
        self.stop_children()
        self._write_instance(STATE_STOPPED)
        self.lock.release()
        log("全部服务已停止")


# ------------------------------------------------------------------- status
def describe(supervisor, instance):
    """status 报告：manager/子进程活性与 stale 判定。"""
    if instance is None:
        return {"state": STATE_STOPPED, "runtime_file": supervisor.runtime_path,
                "note": "无 runtime.json"}
    manager = supervisor.manager_alive(instance)
    state = instance.get("state") or STATE_RUNNING
    if not manager and state != STATE_STOPPED:
        state = STATE_STALE
    processes = {}
    for name, row in (instance.get("processes") or {}).items():
        pid = row.get("pid") or 0
        processes[name] = {
            "pid": pid,
            "alive": pid_matches(pid, row.get("start_time")),
            "restarts": row.get("restarts", 0),
            "cmd_hint": row.get("cmd_hint", ""),
        }
    return {"state": state, "manager_pid": instance.get("manager_pid"),
            "manager_alive": manager, "instance_id": instance.get("id"),
            "root": instance.get("root"), "updated_at": instance.get("updated_at"),
            "processes": processes}


def stop_stale(supervisor, instance):
    """Manager 已死：按 runtime.json 里校验过的 PID 精确清理，绝不按名字杀。"""
    rows = instance.get("processes") or {}
    order = Supervisor.ORDER + tuple(k for k in rows if k not in Supervisor.ORDER)
    stopped = []
    for name in order:
        row = rows.get(name) or {}
        pid = row.get("pid") or 0
        if pid and pid_matches(pid, row.get("start_time")):
            if terminate_pid(pid, row.get("start_time")):
                stopped.append((name, pid))
    try:
        os.unlink(supervisor.runtime_path)
    except OSError:
        pass
    return stopped


# -------------------------------------------------------------------- main
def _load_config(path):
    try:
        return public_config.load(path)
    except public_config.ConfigError as exc:
        sys.stderr.write("[ERROR] %s\n" % exc)
        return None


class _NoConfig(object):
    """配置不可用时的占位：stop/status 仍能定位 runtime.json。"""

    def __init__(self, args):
        explicit = getattr(args, "config", None)
        if explicit:
            data_dir = os.path.dirname(os.path.abspath(explicit))
        else:
            data_dir = os.environ.get("QDP_DATA_DIR") or public_config.DEFAULT_DATA_DIR
        self.data_dir = os.path.abspath(data_dir)


def _supervisor_for(args):
    config = _load_config(getattr(args, "config", None))
    if config is None:
        config = _NoConfig(args)
    root = os.path.dirname(os.path.abspath(__file__))
    return Supervisor(config, root)


def command_start(args):
    config = _load_config(getattr(args, "config", None))
    if config is None:
        return 2
    lock = SingleInstanceLock(os.path.join(config.data_dir, "runtime.lock"))
    if lock.locked_by_other():
        print("[ERROR] 已有 supervisor 在运行；先 stop，或查看 status。")
        return 3
    argv = [sys.executable, os.path.abspath(__file__), "run"]
    if getattr(args, "config", None):
        argv += ["--config", args.config]
    environment = dict(os.environ)
    environment["QD_UPDATE_DISABLE"] = "1"
    kwargs = {}
    if WINDOWS:
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, env=environment, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     **kwargs)
    print("supervisor 已在后台启动；运行 status 查看状态。")
    return 0


def command_stop(args):
    supervisor = _supervisor_for(args)
    instance = supervisor.read_instance()
    if instance is None:
        print("[INFO] 没有 runtime.json，无需停止。")
        return 0
    if supervisor.manager_alive(instance):
        os.makedirs(supervisor.data_dir, exist_ok=True)
        with open(supervisor.control_path, "w", encoding="utf-8") as handle:
            json.dump({"action": "stop", "at": _iso()}, handle)
        deadline = _now() + STOP_TIMEOUT + 5
        while _now() < deadline:
            if not supervisor.lock.locked_by_other():
                break
            time.sleep(0.3)
        if not supervisor.lock.locked_by_other():
            print("[OK] supervisor 已逆序停止全部服务。")
            return 0
        print("[WARN] supervisor 未在限期内退出；按 runtime.json 精确清理 PID。")
    stopped = stop_stale(supervisor, instance)
    if stopped:
        print("[OK] 已清理残留进程: %s"
              % ", ".join("%s(pid %d)" % (name, pid) for name, pid in stopped))
        return 0
    print("[OK] 没有存活子进程；runtime.json 已清理。")
    return 0


def command_status(args):
    supervisor = _supervisor_for(args)
    report = describe(supervisor, supervisor.read_instance())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("state") in (STATE_RUNNING, STATE_STOPPED) else 1


def command_restart(args):
    code = command_stop(args)
    if code != 0:
        return code
    return command_start(args)


def command_run(args):
    config = _load_config(getattr(args, "config", None))
    if config is None:
        return 2
    root = os.path.dirname(os.path.abspath(__file__))
    supervisor = Supervisor(config, root)
    return supervisor.run()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="qoder2api-public supervisor（qoder2api + 网关 + npc）")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认 QDP_CONFIG 或 public-data/config.json）")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
            ("start", "后台启动整套服务"),
            ("stop", "逆序停止 npc -> 网关 -> qoder2api"),
            ("restart", "stop + start"),
            ("status", "实例状态与各进程活性"),
            ("run", "前台运行 supervisor（Ctrl+C 停止）")):
        sub = commands.add_parser(name, help=help_text)
        # 子命令位置也接受 --config（argparse 不回溯根参数）
        sub.add_argument("--config", default=None,
                        help="配置文件路径（默认 QDP_CONFIG 或 public-data/config.json")
    args = parser.parse_args(argv)
    table = {"start": command_start, "stop": command_stop,
             "restart": command_restart, "status": command_status,
             "run": command_run}
    return table[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
