#!/usr/bin/env python3
"""One-command demo of the Emergency Notification Network.

Replaces typing the README commands by hand. Run it from this folder:

    sudo python3 demo.py                      # controller already running in another tab
    sudo python3 demo.py --start-controller   # demo starts (and stops) the Ryu controller itself
    sudo python3 demo.py --auto               # no "press Enter" pauses between steps
    sudo python3 demo.py --only jam,failure   # run only some steps

Steps (in order):
    connectivity  pingall across the 3-switch topology
    alerts        server + subscribers up, INFO then CRITICAL alert, ACKs shown
    priority      OpenFlow rules: UDP 9999 -> priority 100 + emergency queue
    jam           h9 floods the s2-s3 link, controller detects the jam and
                  moves emergency traffic to the s1-s3 link, then clears it
    failure       s1-s2 link goes down, routes recomputed, ping still works
    flaky         one subscriber loses 2 copies, server resends until the ACK

Output files from the demo go to ./demo_out/ . The controller itself still
writes controller_log.csv and link_stats.csv in the folder it was started from.
"""
import argparse
import csv
import os
import pwd
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "demo_out")
SERVER_PY = os.path.join(HERE, "server.py")
SUB_PY = os.path.join(HERE, "fake_subscriber.py")
SEND_PY = os.path.join(HERE, "send_alerts.py")

SERVER_IP = "10.0.0.1"
CTRL_IP = "10.0.0.100"
SUBS = {"h3": "10.0.0.3", "h4": "10.0.0.4", "h5": "10.0.0.5"}
ALL_STEPS = ["connectivity", "alerts", "priority", "jam", "failure", "flaky"]

# ----------------------------------------------------------------- pretty output
BOLD, GREEN, YELLOW, CYAN, RED, RESET = "\033[1m", "\033[92m", "\033[93m", "\033[96m", "\033[91m", "\033[0m"
if not sys.stdout.isatty():
    BOLD = GREEN = YELLOW = CYAN = RED = RESET = ""


def banner(text):
    print("\n" + CYAN + "=" * 70 + RESET)
    print(CYAN + BOLD + text + RESET)
    print(CYAN + "=" * 70 + RESET)


def say(text):
    print(YELLOW + "  > " + text + RESET, flush=True)


def ok(text):
    print(GREEN + "  [OK] " + text + RESET, flush=True)


def bad(text):
    print(RED + "  [!!] " + text + RESET, flush=True)


def sh(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True).stdout


# ----------------------------------------------------------------- helpers
class Demo:
    def __init__(self, args):
        self.args = args
        self.net = None
        self.procs = []
        self.files = []
        self.ctrl_proc = None
        self.ctrl_log = os.path.abspath(args.ctrl_log)
        self.server_log = os.path.join(OUT, "server_log.csv")
        self.t0 = time.time()
        self.hosts = {}

    # -- flow control
    def pause(self, msg="Press Enter to continue"):
        if not self.args.auto:
            try:
                input(BOLD + "\n  [%s] " % msg + RESET)
            except EOFError:
                pass

    # -- background processes on mininet hosts
    def start(self, host, argv, outname):
        fh = open(os.path.join(OUT, outname), "w")
        self.files.append(fh)
        p = host.popen(argv, stdout=fh, stderr=subprocess.STDOUT)
        self.procs.append(p)
        return p

    # -- log readers
    def controller_events(self, since):
        rows = []
        if os.path.exists(self.ctrl_log):
            with open(self.ctrl_log, newline="") as f:
                for r in csv.reader(f):
                    try:
                        if float(r[0]) >= since:
                            rows.append(r)
                    except (ValueError, IndexError):
                        pass
        return rows

    def wait_event(self, name, since, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for r in self.controller_events(since):
                if len(r) > 1 and r[1] == name:
                    return r
            time.sleep(1)
        return None

    def show_events(self, since, names=None):
        for r in self.controller_events(since):
            if len(r) > 1 and (names is None or r[1] in names):
                ts = time.strftime("%H:%M:%S", time.localtime(float(r[0])))
                print("    %s  %-18s %s" % (ts, r[1], ",".join(r[2:])))

    def server_rows(self, since=0.0):
        rows = []
        if os.path.exists(self.server_log):
            with open(self.server_log, newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        if float(r["timestamp"]) >= since:
                            rows.append(r)
                    except (ValueError, KeyError, TypeError):
                        pass
        return rows

    def count(self, event, since=0.0):
        return sum(1 for r in self.server_rows(since) if r["event"] == event)

    def flows(self, switch="s1"):
        return sh("ovs-ofctl -O OpenFlow13 dump-flows %s" % switch).splitlines()

    def show_rules(self, dst_ip="10.0.0.3"):
        fl = self.flows("s1")
        em = [l.strip() for l in fl if "tp_dst=9999" in l and "nw_dst=%s," % dst_ip in l]
        nm = [l.strip() for l in fl if "nw_dst=%s " % dst_ip in l and "tp_dst" not in l]
        print("    emergency rule (UDP 9999) on s1 for %s:" % dst_ip)
        for l in em:
            print("      " + shorten(l))
        print("    normal rule on s1 for %s:" % dst_ip)
        for l in nm:
            print("      " + shorten(l))

    # ------------------------------------------------------------ setup / teardown
    def preflight(self):
        if os.geteuid() != 0:
            sys.exit("Run with sudo:  sudo python3 demo.py")
        for tool in ("ovs-vsctl", "ovs-ofctl", "iperf"):
            if shutil.which(tool) is None:
                sys.exit("Missing tool: %s (is Mininet / Open vSwitch installed?)" % tool)
        os.makedirs(OUT, exist_ok=True)
        for f in os.listdir(OUT):
            if f.startswith(("server_log", "rx_")):
                os.remove(os.path.join(OUT, f))

        if controller_up():
            ok("controller already listening on 6653 (not running 'mn -c', it would kill it)")
        elif self.args.start_controller:
            sh("mn -c >/dev/null 2>&1")          # safe: no controller is running yet
            self.launch_controller()
        else:
            sys.exit("No controller found on 127.0.0.1:6653.\n"
                     "Start it first (Tab 2):  ryu-manager controller.py --verbose\n"
                     "or re-run with:          sudo python3 demo.py --start-controller")

    def launch_controller(self):
        ryu = self.args.ryu_bin
        if ryu is None:
            user = os.environ.get("SUDO_USER") or "root"
            home = pwd.getpwnam(user).pw_dir
            for env in ("ryu39", "ryu-env"):
                cand = os.path.join(home, env, "bin", "ryu-manager")
                if os.path.isfile(cand):
                    ryu = cand
                    break
            else:
                ryu = shutil.which("ryu-manager")
        elif os.path.isdir(ryu):                    # user gave the bin/ folder
            ryu = os.path.join(ryu, "ryu-manager")
        if not ryu or not os.path.isfile(ryu):
            sys.exit("Could not find ryu-manager. Pass --ryu-bin /full/path/to/ryu-manager")
        say("starting controller: %s controller.py" % ryu)
        log = open(os.path.join(OUT, "controller_stdout.txt"), "w")
        self.files.append(log)
        self.ctrl_proc = subprocess.Popen([ryu, os.path.join(HERE, "controller.py")], cwd=HERE,
                                          stdout=log, stderr=subprocess.STDOUT)
        self.ctrl_log = os.path.join(HERE, "controller_log.csv")
        for _ in range(20):
            if controller_up():
                ok("controller is up")
                return
            if self.ctrl_proc.poll() is not None:
                sys.exit("Controller exited early, see demo_out/controller_stdout.txt")
            time.sleep(1)
        sys.exit("Controller did not open port 6653 within 20 s")

    def build_network(self):
        from mininet.net import Mininet
        from mininet.node import RemoteController, OVSSwitch
        from mininet.log import setLogLevel
        try:
            import emergency_topology_m2 as topo
        except ImportError:
            import emergency_topology as topo      # the zip ships it under this name
        setLogLevel("warning")
        sh("ovs-vsctl -- --all destroy QoS -- --all destroy Queue")
        self.net = Mininet(topo=topo.EmergencyTopo(), controller=None, switch=OVSSwitch,
                           autoSetMacs=False, autoStaticArp=False)
        self.net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
        self.t_net = time.time()
        self.net.start()
        topo.attach_controller_port()
        for sw in ("s1", "s2", "s3"):
            topo.setup_queues(sw)
        topo.verify_ports()
        self.wait_switches()
        self.hosts = {n: self.net.get(n) for n in ["h%d" % i for i in range(1, 11)]}
        ok("3 switches, 10 hosts up; queues + controller address configured")

    def wait_switches(self, timeout=30):
        say("waiting for s1, s2, s3 to connect to the controller...")
        end = time.time() + timeout
        while time.time() < end:
            if self.ctrl_proc and self.ctrl_proc.poll() is not None:
                sys.exit("Controller process died. See demo_out/controller_stdout.txt")
            dpids = {r[2] for r in self.controller_events(self.t_net)
                     if len(r) > 2 and r[1] == "SWITCH_CONNECTED"}
            if len(dpids) >= 3:
                ok("all 3 switches connected to the controller")
                time.sleep(2)               # let the proactive routes finish installing
                return
            time.sleep(1)
        sys.exit("Switches did not connect to the controller within %d s.\n"
                 "  Check: demo_out/controller_stdout.txt and 'sudo ovs-vsctl show'.\n"
                 "  Note: do NOT run 'sudo mn -c' while the controller is running." % timeout)

    def cleanup(self):
        banner("Cleaning up")
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        for fh in self.files:
            try:
                fh.close()
            except Exception:
                pass
        if self.net:
            try:
                self.net.stop()
            except Exception:
                pass
        sh("ip link del ctl0 2>/dev/null")
        if self.ctrl_proc:
            self.ctrl_proc.terminate()
            try:
                self.ctrl_proc.wait(timeout=5)
            except Exception:
                self.ctrl_proc.kill()
            sh("mn -c >/dev/null 2>&1")       # safe now: our controller is stopped
        ok("done. Demo files are in %s" % OUT)

    # ------------------------------------------------------------ demo steps
    def step_connectivity(self):
        banner("STEP 1  Connectivity (ARP proxy + proactive routes)")
        say("pingall across all 10 hosts")
        loss = self.net.pingAll()
        (ok if loss == 0 else bad)("packet loss: %.0f%%" % loss)
        if loss >= 100:
            sys.exit("Nothing is reachable, so the later steps cannot work. "
                     "Check demo_out/controller_stdout.txt")
        say("controller log so far:")
        self.show_events(self.t0, {"SWITCH_CONNECTED"})

    def ensure_services(self):
        """Server on h1, subscribers h3-h5 registered. Started once, reused by later steps."""
        if getattr(self, "_services", False):
            return
        say("starting server on h1 (controller events -> %s:6000)" % CTRL_IP)
        self.start(self.hosts["h1"], ["python3", "-u", SERVER_PY, "--controller-ip", CTRL_IP,
                                      "--log-file", self.server_log], "server_stdout.txt")
        time.sleep(1.5)
        for name, ip in SUBS.items():
            say("starting subscriber on %s (%s), mode=normal" % (name, ip))
            self.start(self.hosts[name], ["python3", "-u", SUB_PY, ip, "normal", SERVER_IP,
                                          os.path.join(OUT, "rx_%s.csv" % ip)], "sub_%s.txt" % name)
        time.sleep(3)
        n = self.count("REGISTERED")
        (ok if n == len(SUBS) else bad)("%d/%d subscribers registered" % (n, len(SUBS)))
        self._services = True

    def send(self, severity, count=1, interval=1):
        return self.hosts["h2"].cmd("python3 -u %s %s 5000 %s %d %s" % (
            SEND_PY, SERVER_IP, severity, count, interval))

    def step_alerts(self):
        banner("STEP 2  Alert delivery (INFO, then CRITICAL with ACKs)")
        self.ensure_services()
        t = time.time()
        say("authority (h2) sends an INFO alert -> delivered once, no ACK needed")
        print("    " + self.send("INFO").strip().replace("\n", "\n    "))
        time.sleep(2)
        ok("INFO alert sent to %d subscribers" % self.count("ALERT_SENT", t))

        t = time.time()
        say("authority (h2) sends a CRITICAL alert -> every subscriber must ACK")
        print("    " + self.send("CRITICAL").strip().replace("\n", "\n    "))
        time.sleep(4)
        acks, resends = self.count("ACK_RECEIVED", t), self.count("RESEND", t)
        ok("CRITICAL alert: %d/%d ACKs received, %d resends, %d failed" % (
            acks, len(SUBS), resends, self.count("FAILED", t)))
        say("what the controller learned from the server's EVENT messages:")
        self.show_events(t, {"EMERGENCY_START", "EMERGENCY_END", "SUBSCRIBER_JOINED"})

    def step_priority(self):
        banner("STEP 3  Emergency priority (UDP 9999 -> priority 100 + queue 1)")
        say("OpenFlow rules on s1 (emergency match is on UDP port 9999)")
        self.show_rules("10.0.0.3")
        say("queue counters on s1 (queue 1 = emergency)")
        for l in sh("ovs-ofctl -O OpenFlow13 queue-stats s1").splitlines():
            if "queue" in l.lower() and "tx_bytes" in l:
                print("    " + shorten(l.strip(), 110))
        say("iperf test: UDP 9999 between h1 and h2 for 4 s")
        self.hosts["h2"].cmd("iperf -s -u -p 9999 > %s/iperf_9999_srv.txt 2>&1 &" % OUT)
        out = self.hosts["h1"].cmd("iperf -c 10.0.0.2 -u -p 9999 -b 2M -t 4")
        self.hosts["h2"].cmd("kill %iperf 2>/dev/null; pkill -f 'iperf -s' 2>/dev/null")
        for l in out.splitlines()[-4:]:
            print("    " + l)

    def step_jam(self):
        banner("STEP 4  Traffic jam -> emergency traffic rerouted")
        self.ensure_services()
        say("BEFORE the jam:")
        self.show_rules("10.0.0.3")
        self.pause("Press Enter to start the 15 Mbps background flood h9 -> h6")
        t_noise = time.time()
        self.start(self.hosts["h6"], ["iperf", "-s", "-u", "-p", "5001"], "noise_server.txt")
        self.start(self.hosts["h9"], ["iperf", "-c", "10.0.0.6", "-u", "-b", "15M", "-t", "30",
                                      "-p", "5001"], "noise_client.txt")
        say("h9 floods the 10 Mbps s2-s3 link; waiting for the controller to notice (polls every 2 s)")
        ev = self.wait_event("JAM_DETECTED", t_noise, 25)
        if ev:
            ok("JAM_DETECTED after %.1f s: %s" % (float(ev[0]) - t_noise, ",".join(ev[2:])))
        else:
            bad("controller did not report a jam within 25 s")
        time.sleep(3)
        say("DURING the jam: emergency rule should leave s1 on port 4 (direct s1-s3 link)")
        self.show_rules("10.0.0.3")
        self.show_events(t_noise, {"JAM_DETECTED", "REROUTE"})

        t = time.time()
        say("sending a CRITICAL alert while the primary path is jammed")
        self.send("CRITICAL")
        time.sleep(4)
        ok("during jam: %d/%d ACKs, %d resends, %d failed" % (
            self.count("ACK_RECEIVED", t), len(SUBS), self.count("RESEND", t), self.count("FAILED", t)))

        remaining = 30 - (time.time() - t_noise) + 3
        say("waiting %.0f s for the flood to end..." % max(remaining, 0))
        time.sleep(max(remaining, 0))
        ev = self.wait_event("JAM_CLEARED", t_noise, 20)
        (ok if ev else bad)("JAM_CLEARED" if ev else "jam clear not seen")
        time.sleep(2)
        say("AFTER the jam: emergency rule back on the primary path (port 3)")
        self.show_rules("10.0.0.3")

    def step_failure(self):
        banner("STEP 5  Link failure -> routes recomputed")
        t = time.time()
        say("h1 ping h3 before failure")
        print("    " + last_lines(self.hosts["h1"].cmd("ping -c 2 -W 1 10.0.0.3"), 2))
        say("taking link s1-s2 DOWN")
        self.net.configLinkStatus("s1", "s2", "down")
        time.sleep(2)
        self.show_events(t, {"LINK_DOWN", "REROUTE"})
        out = self.hosts["h1"].cmd("ping -c 3 -W 1 10.0.0.3")
        loss = [l for l in out.splitlines() if "packet loss" in l]
        (ok if loss and " 0% packet loss" in loss[0] else bad)("ping over the alternate path: " +
                                                              (loss[0] if loss else "no result"))
        say("bringing link s1-s2 back UP")
        self.net.configLinkStatus("s1", "s2", "up")
        time.sleep(3)
        self.show_events(t, {"LINK_UP"})
        out = self.hosts["h1"].cmd("ping -c 2 -W 1 10.0.0.3")
        ok("after recovery: " + last_lines(out, 2).replace("\n", " | "))

    def step_flaky(self):
        banner("STEP 6  Reliability: flaky subscriber, server resends until ACK")
        self.ensure_services()
        flaky_ip = "10.0.0.8"
        say("starting a FLAKY subscriber on h8 (drops the first 2 copies of each CRITICAL alert)")
        self.start(self.hosts["h8"], ["python3", "-u", SUB_PY, flaky_ip, "flaky", SERVER_IP,
                                      os.path.join(OUT, "rx_%s.csv" % flaky_ip)], "sub_h8.txt")
        time.sleep(2)
        t = time.time()
        self.send("CRITICAL")
        time.sleep(6)
        rs = [r for r in self.server_rows(t) if r["event"] in ("RESEND", "ACK_RECEIVED", "FAILED")
              and r["subscriber_ip"] == flaky_ip]
        for r in rs:
            print("    %-13s seq=%s ip=%s %s" % (r["event"], r["seq"], r["subscriber_ip"], r["details"]))
        got = any(r["event"] == "ACK_RECEIVED" for r in rs)
        (ok if got else bad)("flaky subscriber ACKed after %d resends" %
                             sum(1 for r in rs if r["event"] == "RESEND"))


# ----------------------------------------------------------------- small utilities
def controller_up(host="127.0.0.1", port=6653):
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def shorten(line, width=150):
    line = " ".join(line.split())
    return line if len(line) <= width else line[:width - 3] + "..."


def last_lines(text, n):
    return "\n".join(text.strip().splitlines()[-n:])


def main():
    ap = argparse.ArgumentParser(description="One-command Emergency Notification Network demo")
    ap.add_argument("--auto", action="store_true", help="no pauses between steps")
    ap.add_argument("--start-controller", action="store_true",
                    help="launch ryu-manager controller.py automatically")
    ap.add_argument("--ryu-bin", default=None, help="path to ryu-manager (default: ~/ryu39 or ~/ryu-env, bin/ryu-manager)")
    ap.add_argument("--ctrl-log", default=os.path.join(HERE, "controller_log.csv"),
                    help="controller_log.csv written by the running controller")
    ap.add_argument("--only", default=",".join(ALL_STEPS),
                    help="comma list of steps: " + ",".join(ALL_STEPS))
    args = ap.parse_args()
    steps = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [s for s in steps if s not in ALL_STEPS]
    if unknown:
        sys.exit("unknown step(s): %s (choose from %s)" % (",".join(unknown), ",".join(ALL_STEPS)))

    sys.path.insert(0, HERE)
    d = Demo(args)
    try:
        banner("Emergency Notification Network demo")
        d.preflight()
        d.build_network()
        d.pause("Network is up. Press Enter to begin")
        for s in ALL_STEPS:
            if s in steps:
                getattr(d, "step_" + s)()
                d.pause("Step done. Press Enter for the next step")
        banner("Demo finished")
        ok("alerts sent=%d  ACKs=%d  resends=%d  failed=%d" % (
            d.count("ALERT_SENT"), d.count("ACK_RECEIVED"), d.count("RESEND"), d.count("FAILED")))
        if args.auto is False:
            d.pause("Press Enter to drop into the Mininet CLI (type exit to finish)")
            from mininet.cli import CLI
            CLI(d.net)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        d.cleanup()


if __name__ == "__main__":
    main()
