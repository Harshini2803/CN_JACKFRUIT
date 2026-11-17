#!/usr/bin/env python3
"""Mininet packet-loss experiment for the emergency alert system.

Topology: one switch; h1 = server (10.0.0.1), h2 = authority (10.0.0.2),
h3..h6 = subscribers (10.0.0.3..10.0.0.6).

For each loss level the script starts a fresh server and subscribers, lets the
subscribers register over a clean network, THEN applies netem loss on both ends
of every subscriber link, sends 30 WARNING + 30 CRITICAL alerts, waits for
resends, and tears everything down.

Run:  sudo python3 run_loss_test.py
Needs server.py, fake_subscriber.py, send_alerts.py in the same directory.
"""
import os
import subprocess
import sys
import time

from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSBridge

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
SERVER_PY = os.path.join(HERE, "server.py")
SUB_PY = os.path.join(HERE, "fake_subscriber.py")
SEND_PY = os.path.join(HERE, "send_alerts.py")

SERVER_IP = "10.0.0.1"
SERVER_PORT = 5000
AUTH_IP = "10.0.0.2"
SUBSCRIBERS = {"h3": "10.0.0.3", "h4": "10.0.0.4", "h5": "10.0.0.5", "h6": "10.0.0.6"}

LOSS_LEVELS = [0, 10, 20, 30]
N_WARNING = 30
N_CRITICAL = 30
INTERVAL = 1
REGISTER_WAIT = 2
RESEND_WAIT = 6


def rm(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def build_net():
    net = Mininet(switch=OVSBridge, link=TCLink, controller=None, autoSetMacs=True)
    s1 = net.addSwitch("s1")
    hosts = {"h1": net.addHost("h1", ip=SERVER_IP + "/24"),
             "h2": net.addHost("h2", ip=AUTH_IP + "/24")}
    for name, ip in SUBSCRIBERS.items():
        hosts[name] = net.addHost(name, ip=ip + "/24")
    sub_intfs = {}  # host name -> [host-side intf, switch-side intf]
    for name, h in hosts.items():
        link = net.addLink(h, s1)
        if name in SUBSCRIBERS:
            sub_intfs[name] = [link.intf1, link.intf2]
    net.start()
    return net, hosts, sub_intfs


def set_loss(sub_intfs, loss: int) -> None:
    for intfs in sub_intfs.values():
        for intf in intfs:
            intf.node.cmd(f"tc qdisc replace dev {intf.name} root netem loss {loss}%")


def clear_loss(sub_intfs) -> None:
    for intfs in sub_intfs.values():
        for intf in intfs:
            intf.node.cmd(f"tc qdisc del dev {intf.name} root 2>/dev/null; true")


def run_level(net, hosts, sub_intfs, loss: int) -> None:
    info(f"\n===== loss {loss}% =====\n")
    procs, files = [], []

    def start(host, args, outname):
        fh = open(os.path.join(RESULTS, outname), "w")
        files.append(fh)
        p = host.popen(args, stdout=fh, stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    server_log = os.path.join(RESULTS, f"server_log_loss{loss}.csv")
    rm(server_log)
    rx_logs = {name: os.path.join(RESULTS, f"rx_loss{loss}_{ip}.csv")
               for name, ip in SUBSCRIBERS.items()}
    for p in rx_logs.values():
        rm(p)

    try:
        # (1) server with a fresh log
        start(hosts["h1"], ["python3", "-u", SERVER_PY, "--log-file", server_log],
              f"server_stdout_loss{loss}.txt")
        time.sleep(1)

        # (2) subscribers
        for name, ip in SUBSCRIBERS.items():
            start(hosts[name],
                  ["python3", "-u", SUB_PY, ip, "normal", SERVER_IP, rx_logs[name]],
                  f"sub_stdout_loss{loss}_{ip}.txt")

        # (3) let everyone register over a clean network
        time.sleep(REGISTER_WAIT)

        # (4) only now add loss, both ends of each subscriber link
        set_loss(sub_intfs, loss)

        # (5) same alert sequence every level
        for sev, n in (("WARNING", N_WARNING), ("CRITICAL", N_CRITICAL)):
            info(f"sending {n} {sev} alerts\n")
            out = hosts["h2"].cmd(
                f"python3 -u {SEND_PY} {SERVER_IP} {SERVER_PORT} {sev} {n} {INTERVAL}")
            with open(os.path.join(RESULTS, f"sender_{sev}_loss{loss}.txt"), "w") as f:
                f.write(out)

        # (6) let the last CRITICAL alert finish its resends
        time.sleep(RESEND_WAIT)
    finally:
        # (7) stop everything, remove netem
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        for fh in files:
            fh.close()
        clear_loss(sub_intfs)
        time.sleep(1)


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 run_loss_test.py")
    os.makedirs(RESULTS, exist_ok=True)
    setLogLevel("info")
    net, hosts, sub_intfs = build_net()
    try:
        net.pingAll()  # sanity check + warms up the switch's MAC table
        for loss in LOSS_LEVELS:
            run_level(net, hosts, sub_intfs, loss)
    finally:
        net.stop()
    info(f"\nDone. Logs are in {RESULTS}. Next: python3 analyze_loss.py\n")


if __name__ == "__main__":
    main()
