#!/usr/bin/env python3
"""Emergency Notification Network topology for Member 2's controller.

Based on Member 3's emergency_topology.py, with four changes:
  1. Switch ports are PINNED, because controller.py hardcodes them.
  2. Host MACs are set explicitly (00:00:00:00:00:NN, NN = last IP octet).
  3. The controller gets its own address 10.0.0.100 on s1 port 5, so hosts can
     send events to it (server.py --controller-ip 10.0.0.100).
  4. Queues (queue 0 normal, queue 1 emergency, 10 Mbps cap) are created
     automatically on every switch port.

Ports:  s1: 1=h1 2=h2 3=s2 4=s3 5=controller
        s2: 1=h9 2=h10 3=s1 4=s3
        s3: 1..6=h3..h8 7=s1 8=s2

Run (controller must already be running):  sudo python3 emergency_topology_m2.py
"""
import subprocess

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info

CTL_IP = "10.0.0.100"
CTL_PORT_ON_S1 = 5


def mac(n):
    return "00:00:00:00:00:%02x" % n


def run(cmd):
    return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True)


class EmergencyTopo(Topo):
    def build(self):
        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")
        s3 = self.addSwitch("s3", protocols="OpenFlow13")

        h1 = self.addHost("h1", ip="10.0.0.1/24", mac=mac(1))      # server
        h2 = self.addHost("h2", ip="10.0.0.2/24", mac=mac(2))      # authority
        self.addLink(h1, s1, port2=1)
        self.addLink(h2, s1, port2=2)

        for i in range(3, 9):                                       # subscribers
            h = self.addHost("h%d" % i, ip="10.0.0.%d/24" % i, mac=mac(i))
            self.addLink(h, s3, port2=i - 2)

        h9 = self.addHost("h9", ip="10.0.0.9/24", mac=mac(9))       # background traffic
        h10 = self.addHost("h10", ip="10.0.0.10/24", mac=mac(10))
        self.addLink(h9, s2, port2=1)
        self.addLink(h10, s2, port2=2)

        self.addLink(s1, s2, port1=3, port2=3)    # L12  primary
        self.addLink(s2, s3, port1=4, port2=8)    # L23  primary
        self.addLink(s1, s3, port1=4, port2=7)    # L13  alternate


def attach_controller_port():
    """Give the VM (where Ryu runs) an address on the Mininet network."""
    run("ip link del ctl0 2>/dev/null")
    run("ip link add ctl0 type veth peer name ctl0-sw")
    run("ip link set ctl0 address %s" % mac(100))
    run("ip addr add %s/24 dev ctl0" % CTL_IP)
    run("ip link set ctl0 up")
    run("ip link set ctl0-sw up")
    run("ovs-vsctl add-port s1 ctl0-sw -- set Interface ctl0-sw ofport_request=%d" % CTL_PORT_ON_S1)


def setup_queues(sw):
    """Queue 0 normal (1-10 Mbps), queue 1 emergency (8-10 Mbps), port cap 10 Mbps."""
    for port in run("ovs-vsctl list-ports %s" % sw).stdout.split():
        run("ovs-vsctl -- set port %s qos=@newqos -- "
            "--id=@newqos create qos type=linux-htb other-config:max-rate=10000000 "
            "queues:0=@q0 queues:1=@q1 -- "
            "--id=@q0 create queue other-config:min-rate=1000000 other-config:max-rate=10000000 -- "
            "--id=@q1 create queue other-config:min-rate=8000000 other-config:max-rate=10000000" % port)
        info("queues set on %s\n" % port)


def verify_ports():
    expected = ([("s1", p) for p in (1, 2, 3, 4, 5)] + [("s2", p) for p in (1, 2, 3, 4)] +
                [("s3", p) for p in range(1, 9)])
    bad = 0
    for sw, p in expected:
        name = "ctl0-sw" if (sw, p) == ("s1", CTL_PORT_ON_S1) else "%s-eth%d" % (sw, p)
        got = run("ovs-vsctl get Interface %s ofport" % name).stdout.strip()
        if got != str(p):
            bad += 1
            info("*** PORT MISMATCH: %s should be OpenFlow port %d but is %s\n" % (name, p, got))
    info("*** PORT CHECK: %s\n" % ("ALL %d PORTS OK" % len(expected) if not bad else "%d PROBLEM(S)" % bad))


def main():
    setLogLevel("info")
    run("ovs-vsctl -- --all destroy QoS -- --all destroy Queue")   # clear leftovers (ok if it fails)
    net = Mininet(topo=EmergencyTopo(), controller=None, switch=OVSSwitch,
                  autoSetMacs=False, autoStaticArp=False)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
    net.start()
    try:
        attach_controller_port()
        for sw in ("s1", "s2", "s3"):
            setup_queues(sw)
        verify_ports()
        info("\n*** Controller address for the server: %s\n" % CTL_IP)
        info("*** Example: h1 python3 -u server.py --controller-ip %s &\n\n" % CTL_IP)
        CLI(net)
    finally:
        net.stop()
        run("ip link del ctl0 2>/dev/null")


if __name__ == "__main__":
    main()
