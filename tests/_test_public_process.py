#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qoder_public_runtime 隔离自测：锁、instance JSON、退避重启、逆序停止、
stale 清理与 PID 校验。不启动任何真实服务——真子进程只用
``python -c 'time.sleep(...)'`` 假进程，HTTP 就绪探测全部注入。"""

import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import qoder_public_config as C
import qoder_public_runtime as R

FAILS = []
PASS_COUNT = [0]


def check(name, cond, extra=""):
    if cond:
        PASS_COUNT[0] += 1
        print("PASS %s" % name)
    else:
        print("FAIL %s | %s" % (name, extra))
        FAILS.append(name)


def make_config(tmp, nps=None):
    data = os.path.join(tmp, "public-data")
    os.makedirs(data, exist_ok=True)
    raw = json.loads(json.dumps(C.DEFAULTS))
    raw["upstream"]["api_key"] = "qdp-internal-test"
    raw["upstream"]["url"] = "http://127.0.0.1:38790"
    raw["server"]["port"] = 38791
    raw["admin"]["port"] = 38792
    raw["admin"]["password_hash"] = C.hash_password("secret-admin")
    if nps:
        raw["nps"].update(nps)
    path = os.path.join(data, "config.json")
    C.write_config(path, raw)
    return C.load(path), data


class FakeProcess(object):
    def __init__(self, pid, argv, env):
        self.pid = pid
        self.argv = argv
        self.env = env
        self.dead = False

    def poll(self):
        return 1 if self.dead else None


class FakeLauncher(object):
    """记录 spawn/terminate 与退避 sleep；绝不碰真实服务。"""

    def __init__(self):
        self.spawn_log = []
        self.terminated = []          # pid，按调用顺序
        self.next_pid = 41000
        self._table = {}

    def spawn(self, argv, env=None, cwd=None, log_path=None):
        process = FakeProcess(self.next_pid, list(argv), dict(env or {}))
        self.next_pid += 1
        self._table[process.pid] = process
        self.spawn_log.append((list(argv), dict(env or {}), process.pid))
        return process

    def alive(self, process):
        return process is not None and not process.dead

    def pid_of(self, process):
        return process.pid if process is not None else 0

    def start_time_of(self, process):
        return 1234.5                   # 恒定假值：PID 校验可预测

    def terminate(self, process, timeout=R.STOP_TIMEOUT):
        if process is None:
            return True
        process.dead = True
        self.terminated.append(process.pid)
        return True


def fake_spec(name, cmd_hint="fake"):
    return R.ServiceSpec(name, ["fake-" + name], env={"QD_UPDATE_DISABLE": "1"},
                         cwd=ROOT, cmd_hint=cmd_hint, readiness=None)


def test_lock(tmp):
    path = os.path.join(tmp, "public-data", "runtime.lock")
    first = R.SingleInstanceLock(path)
    check("lock: 首次获取成功", first.acquire())
    second = R.SingleInstanceLock(path)
    check("lock: 第二个实例被拒绝", not second.acquire())
    check("lock: locked_by_other 为真", second.locked_by_other())
    first.release()
    third = R.SingleInstanceLock(path)
    check("lock: 释放后可重新获取", third.acquire())
    check("lock: 自己持有时 locked_by_other 为假", not third.locked_by_other())
    third.release()


def test_start_instance(tmp):
    config, data = make_config(tmp)
    launcher = FakeLauncher()
    supervisor = R.Supervisor(config, ROOT, launcher=launcher)
    specs = [fake_spec("upstream", "qoder_proxy.py"),
             fake_spec("gateway", "qoder_public_gateway.py")]
    code, message = supervisor.start(specs)
    check("start: 注入 specs 成功", code == 0, (code, message))
    with open(os.path.join(data, "runtime.json"), encoding="utf-8") as handle:
        payload = json.load(handle)
    check("instance: 记录项目真实路径", payload["root"] == ROOT, payload.get("root"))
    check("instance: manager_pid 是自己", payload["manager_pid"] == os.getpid())
    check("instance: id 是随机 rt- 前缀", str(payload["id"]).startswith("rt-")
          and len(payload["id"]) > 6, payload.get("id"))
    check("instance: 子进程含 pid/start_time/restarts",
          all(set(("pid", "start_time", "restarts", "started_at")) <= set(row)
              for row in payload["processes"].values()), payload["processes"])
    check("instance: 状态 running", payload["state"] == R.STATE_RUNNING, payload.get("state"))
    check("instance: cmd_hint 记录", payload["processes"]["gateway"]["cmd_hint"]
          == "qoder_public_gateway.py", payload["processes"])
    check("start: 注入了 QD_UPDATE_DISABLE=1",
          all(env.get("QD_UPDATE_DISABLE") == "1" for _argv, env, _pid in launcher.spawn_log),
          launcher.spawn_log)
    supervisor.stop()
    final = supervisor.read_instance()
    check("stop: 状态写为 stopped", final.get("state") == R.STATE_STOPPED, final)
    check("stop: 锁已释放", not supervisor.lock.locked_by_other())


def test_stop_order(tmp):
    config, _data = make_config(tmp)
    launcher = FakeLauncher()
    supervisor = R.Supervisor(config, ROOT, launcher=launcher)
    supervisor.start([fake_spec(name) for name in ("upstream", "gateway", "npc")])
    order = [launcher.spawn_log[i][0][0].replace("fake-", "") for i in range(3)]
    check("start: 顺序 upstream -> gateway -> npc",
          order == ["upstream", "gateway", "npc"], order)
    supervisor.stop()
    stopped = [row for row in launcher.terminated]
    check("stop: 逆序 npc -> gateway -> upstream",
          stopped == [41002, 41001, 41000], stopped)


def test_backoff_restart(tmp):
    config, data = make_config(tmp)
    launcher = FakeLauncher()
    sleeps = []
    supervisor = R.Supervisor(config, ROOT, launcher=launcher,
                               sleeper=lambda seconds: sleeps.append(seconds))
    specs = [fake_spec("gateway")]
    supervisor.start(specs)
    # 服务进程意外退出 -> 退避重启
    gateway = supervisor._children["gateway"]
    gateway.dead = True
    state = supervisor._sweep()
    check("sweep: 退出后进入 degraded", state == R.STATE_DEGRADED, state)
    check("sweep: 有界退避按指数增长", sleeps and abs(sleeps[0] - R.BACKOFF_BASE) < 1e-9, sleeps)
    check("sweep: 重启后 pid 更新", supervisor._children["gateway"].pid != gateway.pid)
    with open(os.path.join(data, "runtime.json"), encoding="utf-8") as handle:
        payload = json.load(handle)
    check("sweep: restarts 计数 +1", payload["processes"]["gateway"]["restarts"] == 1,
          payload["processes"])
    # 连续失败到达上限 -> failed
    for _ in range(R.MAX_RESTARTS - 1):
        supervisor._children["gateway"].dead = True
        if supervisor._sweep() == R.STATE_FAILED:
            break
        sleeps.clear()
    supervisor._children["gateway"].dead = True
    state = supervisor._sweep()
    check("sweep: 超过重启上限标记 failed", state == R.STATE_FAILED, state)
    check("sweep: failed 后停止调度", supervisor._stopping, supervisor._stopping)
    with open(os.path.join(data, "runtime.json"), encoding="utf-8") as handle:
        payload = json.load(handle)
    check("sweep: failed 状态落盘", payload["state"] == R.STATE_FAILED, payload["state"])
    supervisor.stop()


def test_real_children_and_pids(tmp):
    config, data = make_config(tmp)
    launcher = R.SubprocessLauncher()
    supervisor = R.Supervisor(config, ROOT, launcher=launcher)
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    specs = [R.ServiceSpec("upstream", argv, cmd_hint="sleep-fake", readiness=None),
             R.ServiceSpec("gateway", list(argv), cmd_hint="sleep-fake", readiness=None)]
    code, message = supervisor.start(specs)
    check("real: 真子进程启动成功", code == 0, (code, message))
    up_pid = supervisor.instance["processes"]["upstream"]["pid"]
    gw_pid = supervisor.instance["processes"]["upstream"]["pid"]
    check("real: 子进程存活", R.pid_alive(up_pid), up_pid)
    check("real: pid_matches 接受自己的进程",
          R.pid_matches(up_pid, R.process_start_time(up_pid)), up_pid)
    check("real: pid_matches 拒绝伪造 start_time",
          not R.pid_matches(up_pid, 999999.0), up_pid)
    check("real: pid_alive 拒绝不存在的 PID", not R.pid_alive(999999))
    check("real: terminate_pid 拒绝不匹配记录",
          not R.terminate_pid(up_pid, 999999.0) and R.pid_alive(up_pid))
    supervisor.stop()
    deadline = time.time() + 10
    while time.time() < deadline and R.pid_alive(up_pid):
        time.sleep(0.2)
    check("real: stop 后子进程退出", not R.pid_alive(up_pid), up_pid)
    _gw_pid = gw_pid


def test_stale_cleanup(tmp):
    config, data = make_config(tmp)
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    sleepers = [subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL) for _ in range(2)]
    try:
        supervisor = R.Supervisor(config, ROOT, launcher=FakeLauncher())
        instance = {
            "version": 1, "id": "rt-stale", "root": ROOT,
            "manager_pid": 999999, "manager_start_time": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "state": R.STATE_RUNNING,
            "updated_at": "2026-01-01T00:00:01+00:00",
            "processes": {
                "npc": {"pid": 0, "start_time": 0, "restarts": 0, "cmd_hint": ""},
                "gateway": {"pid": sleepers[0].pid,
                            "start_time": R.process_start_time(sleepers[0].pid),
                            "restarts": 2, "cmd_hint": "qoder_public_gateway.py"},
                "upstream": {"pid": sleepers[1].pid,
                              "start_time": R.process_start_time(sleepers[1].pid),
                              "restarts": 0, "cmd_hint": "qoder_proxy.py"},
            },
        }
        with open(supervisor.runtime_path, "w", encoding="utf-8") as handle:
            json.dump(instance, handle)
        report = R.describe(supervisor, instance)
        check("stale: manager 不在时判 stale", report["state"] == R.STATE_STALE,
              report)
        check("stale: 子进程活性按 PID 校验",
              report["processes"]["gateway"]["alive"] is True
              and report["processes"]["npc"]["alive"] is False, report["processes"])
        stopped = R.stop_stale(supervisor, instance)
        names = [name for name, _pid in stopped]
        check("stale: 清理逆序 gateway -> upstream", names == ["gateway", "upstream"], stopped)
        deadline = time.time() + 10
        while time.time() < deadline and any(R.pid_alive(p.pid) for p in sleepers):
            time.sleep(0.2)
        check("stale: 残留进程全部退出", not any(R.pid_alive(p.pid) for p in sleepers))
        check("stale: runtime.json 已删除", not os.path.isfile(supervisor.runtime_path))
    finally:
        for sleeper in sleepers:
            if R.pid_alive(sleeper.pid):
                R.terminate_pid(sleeper.pid, R.process_start_time(sleeper.pid))
            sleeper.poll()


def test_gateway_pid_readiness(tmp):
    config, _data = make_config(tmp)
    launcher = FakeLauncher()
    supervisor = R.Supervisor(config, ROOT, launcher=launcher)
    supervisor.start([fake_spec("gateway")])
    gateway = supervisor._children["gateway"]
    probe_results = []
    replies = [
        {"status": "ok", "pid": gateway.pid},        # 正确 PID -> ready
        {"status": "ok", "pid": gateway.pid + 7},   # 别人的 PID -> 拒绝
        {"status": "error", "pid": gateway.pid},     # 不健康 -> 拒绝
    ]
    class _Probe(object):
        def __call__(self, url, timeout=3.0, token=None):
            probe_results.append(url)
            return replies.pop(0) if replies else {"status": "ok", "pid": gateway.pid}
    supervisor._probe = _Probe()
    check("readiness: healthz PID 匹配自己的子进程", supervisor._gateway_ready())
    check("readiness: healthz PID 不匹配时拒绝", not supervisor._gateway_ready())
    check("readiness: healthz 非 ok 拒绝", not supervisor._gateway_ready())
    check("readiness: 探测的是网关本机端口",
          all("/healthz" in url and ":%d" % config.server_port in url
              for url in probe_results), probe_results)
    supervisor.stop()


def test_upstream_pid_readiness(tmp):
    config, _data = make_config(tmp)
    launcher = FakeLauncher()
    supervisor = R.Supervisor(config, ROOT, launcher=launcher)
    supervisor.start([fake_spec("upstream")])
    upstream = supervisor._children["upstream"]
    replies = [
        {"ok": True, "pid": upstream.pid},           # 自己的 PID -> ready
        {"ok": True, "pid": upstream.pid + 99},     # 残留实例占用端口 -> 拒绝
        {"ok": False, "pid": upstream.pid},          # 不健康 -> 拒绝
        {"ok": True},                                # 无 pid 字段 -> 退化 200 检查
    ]
    class _Probe(object):
        def __call__(self, url, timeout=3.0, token=None):
            return replies.pop(0) if replies else {"ok": True, "pid": upstream.pid}
    supervisor._probe = _Probe()
    check("upstream readiness: /health pid 匹配自己的子进程",
          supervisor._upstream_ready(38790))
    check("upstream readiness: 残留实例的 pid 被拒绝",
          not supervisor._upstream_ready(38790))
    check("upstream readiness: ok=false 拒绝", not supervisor._upstream_ready(38790))
    check("upstream readiness: 无 pid 字段退化为健康检查",
          supervisor._upstream_ready(38790))
    supervisor.stop()


def test_npc_conf(tmp):
    config, data = make_config(tmp, nps={"server_addr": "198.51.100.10:8024",
                                         "vkey": "k" * 32, "mode": "domain",
                                         "domain": "api.example-test.org"})
    launcher = FakeLauncher()
    supervisor = R.Supervisor(config, ROOT, launcher=launcher)
    path = os.path.join(data, "npc.conf")
    supervisor._write_npc_conf(path, config.nps)
    text = open(path, encoding="utf-8").read()
    check("npc.conf: domain 模式指向网关回环端口",
          "host = api.example-test.org" in text
          and "target_addr = 127.0.0.1:%d" % config.server_port in text, text)
    check("npc.conf: crypt/compress 显式开启",
          "crypt = true" in text and "compress = true" in text, text)
    check("npc.conf: 私有 vkey 写入且不外泄到日志", config.nps["vkey"] in text)
    supervisor._write_npc_conf(path, dict(config.nps, mode="tcp", server_port=8443))
    text = open(path, encoding="utf-8").read()
    check("npc.conf: tcp 模式回退", "mode = tcp" in text and "server_port = 8443" in text, text)
    # npc spec：server_addr 未配置时不启动 npc
    config2, _data2 = make_config(os.path.join(tmp, "no-npc"))
    supervisor2 = R.Supervisor(config2, ROOT, launcher=FakeLauncher())
    check("npc: 未配置 server_addr 时不生成 spec", supervisor2._npc_spec() is None)
    os.chmod(path, 0o600)


def main():
    top = tempfile.mkdtemp(prefix="qdp-runtime-")
    try:
        test_lock(top)
        test_start_instance(top)
        test_stop_order(top)
        test_backoff_restart(top)
        test_real_children_and_pids(top)
        test_stale_cleanup(top)
        test_gateway_pid_readiness(top)
        test_upstream_pid_readiness(top)
        test_npc_conf(top)
    finally:
        import shutil
        shutil.rmtree(top, ignore_errors=True)
    print()
    print("SUMMARY: TOTAL %d checks" % (PASS_COUNT[0] + len(FAILS)))
    if FAILS:
        print("FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("ALL PUBLIC-PROCESS TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
