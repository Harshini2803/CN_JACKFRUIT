"""Task 6: recorded traffic-jam scenario (Member 2).
 
Run with the controller ALREADY running and no Mininet session open:
    Tab 2: ryu-manager controller.py --verbose
    Tab 3: sudo python3 run_jam_test.py        (takes about 2 minutes)
 
Scenario (3 switches, server h1, authority h2, subscribers h3+h4, noise h9 -> h5):
  Phase A  baseline : 5 CRITICAL alerts on a quiet network
  jam      : h9 sends 15 Mbps UDP to h5 over the 10 Mbps s2-s3 link
  Phase B  jam      : 5 CRITICAL alerts while the link is jammed
  then the noise ends and the controller clears the jam
 
Saves everything into results/ (task6_summary.txt, task6_results.csv,
task6_controller_events.txt, task6_link_stats.csv, flows_*.txt, raw logs).
"""
import csv
import os
import re
import subprocess
import sys
import time
 
HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
SERVER_PY = os.path.join(HERE, "server.py")
SUB_PY = os.path.join(HERE, "fake_subscriber.py")
SEND_PY = os.path.join(HERE, "send_alerts.py")
CTRL_LOG = os.path.join(HERE, "controller_log.csv")
STATS_LOG = os.path.join(HERE, "link_stats.csv")
 
SERVER_IP = "10.0.0.1"
SUBS = {"h3": "10.0.0.3", "h4": "10.0.0.4"}
N_ALERTS = 5            # CRITICAL alerts per phase
NOISE_SECS = 45
NOISE_RATE = "15M"
 
 
def sh(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True).stdout
 
 
# ----------------------------------------------------------------- analysis
def read_server_log(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))
 
 
def read_rx(paths):
    """first receive time per (seq, ip): {(seq, ip): latency_ms}"""
    lat = {}
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, newline="") as f:
            for r in csv.reader(f):
                if len(r) < 5 or r[2] != "CRITICAL":
                    continue
                key = (r[3], r[1])
                if key not in lat:
                    lat[key] = (float(r[0]) - float(r[4])) * 1000.0
    return lat
 
 
def analyze(server_rows, lat, n_per_phase):
    sent = sorted({int(r["seq"]) for r in server_rows if r["event"] == "ALERT_SENT"})
    phases = {"A_baseline": sent[:n_per_phase], "B_during_jam": sent[n_per_phase:2 * n_per_phase]}
    subs = {r["subscriber_ip"] for r in server_rows if r["event"] == "REGISTERED"}
    n_subs = max(len(subs), 1)
    out = []
    for name, seqs in phases.items():
        s = {str(x) for x in seqs}
        expected = len(s) * n_subs
        acked = {(r["seq"], r["subscriber_ip"]) for r in server_rows
                 if r["event"] == "ACK_RECEIVED" and r["seq"] in s}
        resends = sum(1 for r in server_rows if r["event"] == "RESEND" and r["seq"] in s)
        failed = sum(1 for r in server_rows if r["event"] == "FAILED" and r["seq"] in s)
        ls = [v for (sq, _), v in lat.items() if sq in s]
        out.append({
            "phase": name, "alerts": len(s), "expected_deliveries": expected,
            "acked": len(acked),
            "delivery_ratio": "%.2f" % (len(acked) / expected) if expected else "0.00",
            "resends": resends, "failed": failed,
            "avg_latency_ms": "%.2f" % (sum(ls) / len(ls)) if ls else "n/a",
            "max_latency_ms": "%.2f" % max(ls) if ls else "n/a"})
    return out
 
 
def read_csv_rows(path, t0, ts_col=0):
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.reader(f):
                try:
                    if float(r[ts_col]) >= t0:
                        rows.append(r)
                except (ValueError, IndexError):
                    pass
    return rows
 
 
def controller_events_since(t0):
    return read_csv_rows(CTRL_LOG, t0)
 
 
def wait_for_event(name, t0, timeout):
    end = time.time() + timeout
    while time.time() < end:
        for r in controller_events_since(t0):
            if len(r) > 1 and r[1] == name:
                return float(r[0])
        time.sleep(1)
    return None
 
 
def snapshot(label):
    flows = sh("ovs-ofctl -O OpenFlow13 dump-flows s1")
    em = [l.strip() for l in flows.splitlines() if "tp_dst=9999" in l and
          ("nw_dst=10.0.0.3," in l or "nw_dst=10.0.0.4," in l)]
    normal = [l.strip() for l in flows.splitlines() if "nw_dst=10.0.0.3 " in l]
    queues = sh("ovs-ofctl -O OpenFlow13 queue-stats s1")
    with open(os.path.join(RESULTS, "flows_%s.txt" % label), "w") as f:
        f.write("# emergency rules on s1 (to h3/h4)\n" + "\n".join(em) + "\n\n")
        f.write("# normal rule on s1 (to h3)\n" + "\n".join(normal) + "\n\n")
        f.write("# queue-stats s1\n" + queues)
    port = None
    for l in em:
        m = re.search(r"nw_dst=10\.0\.0\.3,tp_dst=9999.*output:(\S+)", l)
        if m:
            port = m.group(1).strip('"')
    nport = None
    for l in normal:
        m = re.search(r"output:(\S+)", l)
        if m:
            nport = m.group(1).strip('"')
    return port, nport
 
 
def describe_port(p):
    if p is None:
        return "not found"
    p = str(p)
    if p.endswith("3"):
        return "port %s = primary path (s1>s2>s3)" % p
    if p.endswith("4"):
        return "port %s = direct link (s1>s3)" % p
    return "port %s" % p
 
 
# ----------------------------------------------------------------- scenario
def main():
    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 run_jam_test.py")
    if not os.path.exists(CTRL_LOG) and "y" not in os.environ.get("SKIP_CTRL_CHECK", ""):
        print("WARNING: controller_log.csv not found. Is controller.py running from this folder?")
    os.makedirs(RESULTS, exist_ok=True)
 
    from mininet.net import Mininet
    from mininet.node import RemoteController, OVSSwitch
    from mininet.log import setLogLevel, info
    from emergency_topology_m2 import (EmergencyTopo, attach_controller_port,
                                       setup_queues, verify_ports)
    setLogLevel("info")
 
    server_log = os.path.join(RESULTS, "jam_server_log.csv")
    rx = {h: os.path.join(RESULTS, "jam_rx_%s.csv" % ip) for h, ip in SUBS.items()}
    for p in [server_log] + list(rx.values()):
        if os.path.exists(p):
            os.remove(p)
 
    sh("ovs-vsctl -- --all destroy QoS -- --all destroy Queue")
    net = Mininet(topo=EmergencyTopo(), controller=None, switch=OVSSwitch,
                  autoSetMacs=False, autoStaticArp=False)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
    procs, files = [], []
    t0 = time.time()
    result = {}
    try:
        net.start()
        attach_controller_port()
        for sw in ("s1", "s2", "s3"):
            setup_queues(sw)
        verify_ports()
        time.sleep(3)
        info("*** warm-up pings\n")
        net.pingAll()
        h = {n: net.get(n) for n in ("h1", "h2", "h3", "h4", "h5", "h9")}
 
        def start(host, args, outname):
            fh = open(os.path.join(RESULTS, outname), "w")
            files.append(fh)
            procs.append(host.popen(args, stdout=fh, stderr=subprocess.STDOUT))
 
        info("*** starting server and subscribers\n")
        start(h["h1"], ["python3", "-u", SERVER_PY, "--controller-ip", "10.0.0.100",
                        "--log-file", server_log], "jam_server_stdout.txt")
        time.sleep(1)
        for name, ip in SUBS.items():
            start(h[name], ["python3", "-u", SUB_PY, ip, "normal", SERVER_IP, rx[name]],
                  "jam_sub_%s.txt" % ip)
        time.sleep(3)
 
        info("*** PHASE A: baseline, %d CRITICAL alerts on a quiet network\n" % N_ALERTS)
        h["h2"].cmd("python3 -u %s %s 5000 CRITICAL %d 1" % (SEND_PY, SERVER_IP, N_ALERTS))
        time.sleep(6)
        result["port_before"], result["nport_before"] = snapshot("before_jam")
 
        info("*** starting noise: h9 -> h5 at %s for %ds\n" % (NOISE_RATE, NOISE_SECS))
        t_noise = time.time()
        start(h["h5"], ["iperf", "-s", "-u", "-p", "5001"], "noise_server.txt")
        start(h["h9"], ["iperf", "-c", "10.0.0.5", "-u", "-b", NOISE_RATE, "-t", str(NOISE_SECS),
                        "-p", "5001"], "noise_client.txt")
        result["jam_at"] = wait_for_event("JAM_DETECTED", t_noise, 25)
        info("*** JAM_DETECTED: %s\n" % ("yes, %.1fs after noise start" % (result["jam_at"] - t_noise)
                                         if result["jam_at"] else "NOT SEEN within 25s"))
        time.sleep(3)
        result["port_jam"], result["nport_jam"] = snapshot("during_jam")
 
        info("*** PHASE B: %d CRITICAL alerts during the jam\n" % N_ALERTS)
        h["h2"].cmd("python3 -u %s %s 5000 CRITICAL %d 1" % (SEND_PY, SERVER_IP, N_ALERTS))
        time.sleep(6)
 
        remaining = NOISE_SECS - (time.time() - t_noise) + 4
        info("*** waiting %.0fs for the noise to end\n" % max(remaining, 0))
        time.sleep(max(remaining, 0))
        result["clear_at"] = wait_for_event("JAM_CLEARED", t_noise, 30)
        time.sleep(3)
        result["port_after"], result["nport_after"] = snapshot("after_jam")
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        for fh in files:
            fh.close()
        net.stop()
        sh("ip link del ctl0 2>/dev/null")
 
    # ------------------------------------------------------------- report
    rows = analyze(read_server_log(server_log), read_rx(rx.values()), N_ALERTS)
    cols = ["phase", "alerts", "expected_deliveries", "acked", "delivery_ratio",
            "resends", "failed", "avg_latency_ms", "max_latency_ms"]
    with open(os.path.join(RESULTS, "task6_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
 
    events = controller_events_since(t0)
    keep = [r for r in events if len(r) > 1 and r[1] in
            ("JAM_DETECTED", "JAM_CLEARED", "REROUTE", "LINK_DOWN", "LINK_UP",
             "SUBSCRIBER_JOINED", "EMERGENCY_START", "EMERGENCY_END")]
    with open(os.path.join(RESULTS, "task6_controller_events.txt"), "w") as f:
        for r in keep:
            f.write(",".join(r) + "\n")
    with open(os.path.join(RESULTS, "task6_link_stats.csv"), "w") as f:
        f.write("timestamp,dpid,port,link,tx_mbps,usage_pct\n")
        for r in read_csv_rows(STATS_LOG, t0):
            f.write(",".join(r) + "\n")
 
    lines = ["TASK 6: TRAFFIC JAM SCENARIO", "=" * 60, ""]
    lines.append("Setup: 3 switches, links capped at 10 Mbps, emergency queue min 8 Mbps.")
    lines.append("Noise: h9 -> h5, %s UDP for %ds over the s2-s3 link (L23)." % (NOISE_RATE, NOISE_SECS))
    lines.append("")
    if result.get("jam_at"):
        lines.append("Jam detected %.1f s after the noise started." % (result["jam_at"] - t_noise))
    else:
        lines.append("Jam NOT detected within 25 s.")
    if result.get("clear_at"):
        lines.append("Jam cleared at %.1f s after the noise started." % (result["clear_at"] - t_noise))
    else:
        lines.append("Jam clear NOT seen.")
    lines.append("")
    lines.append("Emergency rule on s1 for h3 (UDP 9999):")
    lines.append("  before jam : %s" % describe_port(result.get("port_before")))
    lines.append("  during jam : %s" % describe_port(result.get("port_jam")))
    lines.append("  after jam  : %s" % describe_port(result.get("port_after")))
    lines.append("Normal rule on s1 for h3:")
    lines.append("  before jam : %s" % describe_port(result.get("nport_before")))
    lines.append("  during jam : %s" % describe_port(result.get("nport_jam")))
    lines.append("  after jam  : %s" % describe_port(result.get("nport_after")))
    lines.append("")
    lines.append("Alert delivery (CRITICAL, 2 subscribers):")
    for r in rows:
        lines.append("  %-13s alerts=%s acked=%s/%s ratio=%s resends=%s failed=%s avg=%s ms max=%s ms" % (
            r["phase"], r["alerts"], r["acked"], r["expected_deliveries"], r["delivery_ratio"],
            r["resends"], r["failed"], r["avg_latency_ms"], r["max_latency_ms"]))
    lines.append("")
    lines.append("Controller events during the test:")
    for r in keep:
        lines.append("  " + ",".join(r))
    text = "\n".join(lines)
    with open(os.path.join(RESULTS, "task6_summary.txt"), "w") as f:
        f.write(text + "\n")
    print("\n" + text)
    print("\nSaved to %s" % RESULTS)
 
 
if __name__ == "__main__":
    main()

