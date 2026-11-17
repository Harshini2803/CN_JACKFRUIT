#!/usr/bin/env python3
"""Member 2: fully automated demo + self-test.

ONE command does everything (it starts and restarts the controller itself):

    sudo python3 auto_demo.py            # Demos 1-5, then the recorded jam test (~5 min)
    sudo python3 auto_demo.py --quick    # Demos 1-5 only (~2 min)

Do NOT start the controller or Mininet yourself first.

Checks (each prints PASS / FAIL):
  1 Forwarding        pingall 0% dropped, routes installed for every host
  2 Emergency queue   port-9999 traffic increases queue 1 on the s1 -> s2 port
  3 Jam + reroute     JAM_DETECTED, emergency path s1>s3 (port 4), JAM_CLEARED, back to port 3
  4 Link failure      LINK_DOWN, reroute via s1>s3, ping still works, LINK_UP, restored
  5 Server events     SUBSCRIBER_JOINED, EMERGENCY_START, EMERGENCY_END reach the controller
  6 Recorded test     run_jam_test.py (skipped with --quick)

Report saved to results/auto_demo_report.txt
Needs: Ryu in ~/ryu-env, Mininet, Open vSwitch, iperf (v2), run from the project folder.
"""
import csv
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
RESULTS = os.path.join(HERE, "results")
CTRL_LOG = os.path.join(HERE, "controller_log.csv")
STATS_LOG = os.path.join(HERE, "link_stats.csv")
SERVER_PY = os.path.join(HERE, "server.py")
SUB_PY = os.path.join(HERE, "fake_subscriber.py")
SEND_PY = os.path.join(HERE, "send_alerts.py")
JAMTEST_PY = os.path.join(HERE, "run_jam_test.py")
OF_PORT = 6653

CHECKS = []          # (demo, description, ok, detail)


# ------------------------------------------------------------------ helpers
def sh(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True).stdout


def say(msg):
    print(msg, flush=True)


def check(demo, desc, ok, detail=""):
    CHECKS.append((demo, desc, bool(ok), detail))
    say("   [%s] %s%s" % ("PASS" if ok else "FAIL", desc, ("  (%s)" % detail) if detail else ""))
    return bool(ok)


def log_rows_since(t0):
    rows = []
    if os.path.exists(CTRL_LOG):
        with open(CTRL_LOG, newline="") as f:
            for r in csv.reader(f):
                try:
                    if len(r) >= 2 and float(r[0]) >= t0:
                        rows.append((float(r[0]), r[1], r[2] if len(r) > 2 else ""))
                except ValueError:
                    pass
    return rows


def wait_event(name, t0, timeout, contains=()):
    """First controller-log row named `name` after t0 whose details hold every string in `contains`."""
    end = time.time() + timeout
    while True:
        for ts, ev, det in log_rows_since(t0):
            if ev == name and all(c in det for c in contains):
                return (ts, ev, det)
        if time.time() >= end:
            return None
        time.sleep(0.5)


def port_of(text):
    """'4' or '"s1-eth4"' -> 4"""
    m = re.search(r"(\d+)\W*$", text or "")
    return int(m.group(1)) if m else None


def emergency_out(sw="s1", dst="10.0.0.3"):
    """Output port of the udp 9999 emergency rule on switch sw for destination dst."""
    out = sh("ovs-ofctl -O OpenFlow13 dump-flows %s" % sw)
    for line in out.splitlines():
        if "tp_dst=9999" in line and ("nw_dst=%s," % dst) in line:
            m = re.search(r'output:("[^"]+"|[^\s,]+)', line)
            if m:
                return port_of(m.group(1).strip('"'))
    return None


def queue1_pkts(sw, ifname):
    out = sh("ovs-ofctl -O OpenFlow13 queue-stats %s" % sw)
    m = re.search(r'port\s+"?%s"?\s+queue 1:\s+bytes=(\d+),\s+pkts=(\d+)' % re.escape(ifname), out)
    return int(m.group(2)) if m else None


def controller_port_open():
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", OF_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start_controller():
    user = os.environ.get("SUDO_USER") or "root"
    home = pwd.getpwnam(user).pw_dir
    act = os.environ.get("RYU_ACTIVATE", os.path.join(home, "ryu-env", "bin", "activate"))
    if not os.path.exists(act):
        sys.exit("Ryu environment not found at %s (set RYU_ACTIVATE=/path/to/activate)" % act)
    for p in (CTRL_LOG, STATS_LOG):
        if os.path.exists(p):
            os.remove(p)
    out = open(os.path.join(RESULTS, "auto_controller_stdout.txt"), "w")
    proc = subprocess.Popen(["bash", "-c", "source '%s' && exec ryu-manager controller.py" % act],
                            cwd=HERE, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(40):
        if proc.poll() is not None:
            sys.exit("Controller exited immediately. See results/auto_controller_stdout.txt")
        if controller_port_open():
            time.sleep(1)
            return proc, out
        time.sleep(0.5)
    sys.exit("Controller did not start listening on port %d" % OF_PORT)


def stop_controller(proc, out):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
    out.close()


# ------------------------------------------------------------------ demos
def demo1(net, t0):
    say("\n=== DEMO 1: Forwarding (ARP proxy + pre-installed routes) ===")
    ok = wait_event("SWITCH_CONNECTED", t0, 15, ["dpid=3"]) is not None
    n = len({d for _, e, d in log_rows_since(t0) if e == "SWITCH_CONNECTED"})
    check(1, "all 3 switches connected to the controller", ok and n >= 3, "%d SWITCH_CONNECTED" % n)
    loss = net.pingAll()
    check(1, "pingall: every host reaches every host", loss == 0, "%.0f%% dropped" % loss)
    flows = sh("ovs-ofctl -O OpenFlow13 dump-flows s1")
    normal = sum(1 for l in flows.splitlines() if "priority=20," in l)
    emerg = sum(1 for l in flows.splitlines() if "priority=100," in l)
    check(1, "s1 has a route for every host (11 normal + 22 emergency rules)",
          normal >= 11 and emerg >= 22, "normal=%d emergency=%d" % (normal, emerg))
    p = emergency_out()
    check(1, "s1 emergency rule for h3 goes out port 3 (via s2)", p == 3, "output=%s" % p)


def demo2(net, h, procs):
    say("\n=== DEMO 2: Emergency priority (UDP 9999 -> queue 1) ===")
    before = queue1_pkts("s1", "s1-eth3")
    fh = open(os.path.join(RESULTS, "auto_iperf_server_9999.txt"), "w")
    procs.append((h["h3"].popen(["iperf", "-s", "-u", "-p", "9999"], stdout=fh,
                                stderr=subprocess.STDOUT), fh))
    time.sleep(1)
    out = h["h1"].cmd("iperf -c 10.0.0.3 -u -p 9999 -b 2M -t 5")
    with open(os.path.join(RESULTS, "auto_iperf_client_9999.txt"), "w") as f:
        f.write(out)
    time.sleep(1)
    after = queue1_pkts("s1", "s1-eth3")
    ok = before is not None and after is not None and after - before > 100
    check(2, "queue 1 on s1-eth3 counted the port-9999 traffic", ok,
          "queue1 packets %s -> %s" % (before, after))
    h["h3"].cmd("pkill iperf")


def demo3(net, h, procs):
    say("\n=== DEMO 3: Jam detection and reroute (15 Mbps noise on the 10 Mbps L23 link) ===")
    fh = open(os.path.join(RESULTS, "auto_noise_server.txt"), "w")
    procs.append((h["h5"].popen(["iperf", "-s", "-u", "-p", "5001"], stdout=fh,
                                stderr=subprocess.STDOUT), fh))
    time.sleep(1)
    t_noise = time.time()
    fh2 = open(os.path.join(RESULTS, "auto_noise_client.txt"), "w")
    procs.append((h["h9"].popen(["iperf", "-c", "10.0.0.5", "-u", "-b", "15M", "-t", "20",
                                 "-p", "5001"], stdout=fh2, stderr=subprocess.STDOUT), fh2))
    ev = wait_event("JAM_DETECTED", t_noise, 25, ["link=L23"])
    check(3, "JAM_DETECTED on link L23", ev, ev[2] if ev else "not seen in 25 s")
    rr = wait_event("REROUTE", t_noise, 10, ["jammed=L23"])
    check(3, "REROUTE: emergency path s1>s3, normal path unchanged",
          rr and "emergency_path=s1>s3" in rr[2] and "normal_path=s1>s2>s3" in rr[2],
          rr[2] if rr else "no REROUTE")
    time.sleep(2)
    p = emergency_out()
    check(3, "s1 emergency rule for h3 moved to port 4 (direct s1-s3 link)", p == 4, "output=%s" % p)
    wait = 20 - (time.time() - t_noise) + 3
    say("   ... waiting %.0f s for the noise to end" % max(wait, 0))
    time.sleep(max(wait, 0))
    ev = wait_event("JAM_CLEARED", t_noise, 25, ["link=L23"])
    check(3, "JAM_CLEARED", ev, ev[2] if ev else "not seen")
    time.sleep(3)
    p = emergency_out()
    check(3, "s1 emergency rule for h3 back on port 3", p == 3, "output=%s" % p)
    h["h5"].cmd("pkill iperf")


def demo4(net, h):
    say("\n=== DEMO 4: Link failure (s1-s2 down) ===")
    t = time.time()
    net.configLinkStatus("s1", "s2", "down")
    ev = wait_event("LINK_DOWN", t, 10, ["link=L12"])
    check(4, "LINK_DOWN for L12", ev, ev[2] if ev else "not seen")
    rr = wait_event("REROUTE", t, 10, ["down=L12"])
    check(4, "REROUTE: both paths become s1>s3",
          rr and "emergency_path=s1>s3" in rr[2] and "normal_path=s1>s3" in rr[2],
          rr[2] if rr else "no REROUTE")
    time.sleep(1)
    out = h["h1"].cmd("ping -c 3 -W 2 10.0.0.3")
    m = re.search(r"(\d+)% packet loss", out)
    loss = int(m.group(1)) if m else 100
    check(4, "h1 still reaches h3 while the link is down", loss <= 34, "%d%% loss" % loss)
    t_up = time.time()
    net.configLinkStatus("s1", "s2", "up")
    ev = wait_event("LINK_UP", t_up, 10, ["link=L12"])
    check(4, "LINK_UP for L12", ev, ev[2] if ev else "not seen")
    rr = wait_event("REROUTE", t_up, 10, ["down=none"])
    check(4, "REROUTE: paths restored to s1>s2>s3",
          rr and "emergency_path=s1>s2>s3" in rr[2] and "normal_path=s1>s2>s3" in rr[2],
          rr[2] if rr else "no REROUTE")


def demo5(net, h, procs):
    say("\n=== DEMO 5: Events from the server (UDP 6000) ===")
    srv_log = os.path.join(RESULTS, "auto_server_log.csv")
    if os.path.exists(srv_log):
        os.remove(srv_log)
    t = time.time()
    fh = open(os.path.join(RESULTS, "auto_server_stdout.txt"), "w")
    procs.append((h["h1"].popen(["python3", "-u", SERVER_PY, "--controller-ip", "10.0.0.100",
                                 "--log-file", srv_log], stdout=fh, stderr=subprocess.STDOUT), fh))
    time.sleep(1)
    fh2 = open(os.path.join(RESULTS, "auto_subscriber_stdout.txt"), "w")
    procs.append((h["h3"].popen(["python3", "-u", SUB_PY, "10.0.0.3", "normal", "10.0.0.1"],
                                stdout=fh2, stderr=subprocess.STDOUT), fh2))
    ev = wait_event("SUBSCRIBER_JOINED", t, 8, ["10.0.0.3"])
    check(5, "controller got SUBSCRIBER_JOINED for 10.0.0.3", ev, ev[2] if ev else "not seen")
    t_alert = time.time()
    h["h2"].cmd("python3 -u %s 10.0.0.1 5000 CRITICAL 1 1" % SEND_PY)
    ev = wait_event("EMERGENCY_START", t_alert, 8)
    check(5, "controller got EMERGENCY_START", ev, ev[2] if ev else "not seen")
    ev = wait_event("EMERGENCY_END", t_alert, 10)
    check(5, "controller got EMERGENCY_END", ev, ev[2] if ev else "not seen")


# ------------------------------------------------------------------ main
def write_report():
    lines = ["MEMBER 2 AUTOMATED DEMO REPORT", "=" * 60, ""]
    for demo, desc, ok, detail in CHECKS:
        lines.append("Demo %s  %-4s  %s%s" % (demo, "PASS" if ok else "FAIL", desc,
                                              ("  (%s)" % detail) if detail else ""))
    passed = sum(1 for c in CHECKS if c[2])
    lines += ["", "RESULT: %d / %d checks passed" % (passed, len(CHECKS))]
    text = "\n".join(lines)
    with open(os.path.join(RESULTS, "auto_demo_report.txt"), "w") as f:
        f.write(text + "\n")
    return text, passed == len(CHECKS)


def main():
    quick = "--quick" in sys.argv
    if os.geteuid() != 0:
        sys.exit("Run with sudo:  sudo python3 auto_demo.py")
    for tool in ("iperf", "ovs-ofctl"):
        if not shutil.which(tool):
            sys.exit("'%s' not found. Install it (for iperf: sudo apt install iperf)." % tool)
    for f in (SERVER_PY, SUB_PY, SEND_PY, "emergency_topology_m2.py", "controller.py"):
        if not os.path.exists(os.path.join(HERE, f) if not os.path.isabs(f) else f):
            sys.exit("Missing file: %s (run from the project folder)" % f)
    if controller_port_open():
        sys.exit("A controller is already running on port %d. Stop it (Ctrl+C in its tab) first; "
                 "this script starts its own." % OF_PORT)
    os.makedirs(RESULTS, exist_ok=True)

    from mininet.net import Mininet
    from mininet.node import RemoteController, OVSSwitch
    from mininet.log import setLogLevel
    from emergency_topology_m2 import (EmergencyTopo, attach_controller_port,
                                       setup_queues, verify_ports)
    setLogLevel("warning")

    say("*** cleaning old Mininet state")
    sh("mn -c")
    sh("ovs-vsctl -- --all destroy QoS -- --all destroy Queue")
    say("*** starting the controller")
    ctl, ctl_out = start_controller()
    t0 = time.time() - 1
    procs, net = [], None
    try:
        say("*** building the network")
        net = Mininet(topo=EmergencyTopo(), controller=None, switch=OVSSwitch,
                      autoSetMacs=False, autoStaticArp=False)
        net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=OF_PORT)
        net.start()
        attach_controller_port()
        for sw in ("s1", "s2", "s3"):
            setup_queues(sw)
        verify_ports()
        time.sleep(3)
        h = {n: net.get(n) for n in ("h1", "h2", "h3", "h4", "h5", "h9")}
        for fn, args in ((demo1, (net, t0)), (demo2, (net, h, procs)), (demo3, (net, h, procs)),
                         (demo4, (net, h)), (demo5, (net, h, procs))):
            try:
                fn(*args)
            except Exception as err:       # keep going, record the problem
                check(fn.__name__[-1], "%s crashed" % fn.__name__, False, repr(err))
    finally:
        for p, fh in procs:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            fh.close()
        if net:
            net.stop()
        sh("ip link del ctl0 2>/dev/null")
        if os.path.exists(CTRL_LOG):
            shutil.copy(CTRL_LOG, os.path.join(RESULTS, "auto_controller_log.csv"))
        stop_controller(ctl, ctl_out)

    if not quick and os.path.exists(JAMTEST_PY):
        say("\n=== DEMO 6: recorded jam test (run_jam_test.py, ~2 min, fresh controller) ===")
        ctl, ctl_out = start_controller()
        try:
            r = subprocess.run([sys.executable, JAMTEST_PY], cwd=HERE,
                               env=dict(os.environ, SKIP_CTRL_CHECK="y"))
            summ = os.path.join(RESULTS, "task6_summary.txt")
            check(6, "run_jam_test.py finished and wrote task6_summary.txt",
                  r.returncode == 0 and os.path.exists(summ), "exit code %d" % r.returncode)
            if os.path.exists(summ):
                txt = open(summ).read()
                check(6, "jam was detected during the recorded test",
                      "Jam detected" in txt and "NOT detected" not in txt)
        finally:
            sh("ip link del ctl0 2>/dev/null")
            stop_controller(ctl, ctl_out)

    text, all_ok = write_report()
    say("\n" + text)
    say("\nReport: %s" % os.path.join(RESULTS, "auto_demo_report.txt"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
