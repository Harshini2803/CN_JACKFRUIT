"""Member 2: Ryu controller for the Emergency Notification Network.

Topology (see emergency_topology_m2.py; ports are pinned there):
    s1: 1=h1(server) 2=h2(authority) 3=s2 4=s3 5=ctl (controller's own address)
    s2: 1=h9 2=h10 (background traffic) 3=s1 4=s3
    s3: 1..6=h3..h8 (subscribers) 7=s1 8=s2

What it does
  Task 1  Forwarding: proactive routes + ARP proxy (no flooding, so the s1-s2-s3
          triangle can never create a broadcast storm).
  Task 2  UDP port 9999 (as source or destination) -> priority 100, emergency queue 1.
  Task 3  Polls port stats every 2 s; link usage above 80% = JAM. Logs a warning
          and moves emergency traffic onto a link that is not jammed.
  Task 4  Port-status events: when a link goes down, all routes are recomputed.
  Task 5  Listens on UDP 6000 for EVENT|NAME|ip,ip from the server.
Logs: controller_log.csv (decisions) and link_stats.csv (usage, for graphs).
"""
import heapq
import os
import socket
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER,
                                    DEAD_DISPATCHER, set_ev_cls)
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, arp, ether_types

EMERGENCY_PORT = 9999
EMERGENCY_QUEUE = 1
PRIO_NORMAL = 20
PRIO_EMERGENCY = 100
EVENT_PORT = 6000
POLL_INTERVAL = 2                 # seconds between port-stats requests
LINK_CAPACITY_BPS = 10000000      # 10 Mbps, matches the queue cap
JAM_HIGH = 0.80                   # usage at/above this = jam
JAM_LOW = 0.60                    # usage below this clears the jam (avoids flapping)
LOG_FILE = "controller_log.csv"
STATS_FILE = "link_stats.csv"


def _mac(n):
    return "00:00:00:00:00:%02x" % n


# ip -> (mac, switch dpid, switch port)
HOSTS = {
    "10.0.0.1": (_mac(1), 1, 1),
    "10.0.0.2": (_mac(2), 1, 2),
    "10.0.0.9": (_mac(9), 2, 1),
    "10.0.0.10": (_mac(10), 2, 2),
    "10.0.0.100": (_mac(100), 1, 5),      # the controller itself (events from server)
}
for _i in range(3, 9):
    HOSTS["10.0.0.%d" % _i] = (_mac(_i), 3, _i - 2)

# Links between switches. weight: primary path s1-s2-s3 costs 2, direct s1-s3 costs 3,
# so normal traffic prefers the primary path and the direct link is the alternate.
LINKS = {
    "L12": {"ends": ((1, 3), (2, 3)), "weight": 1},
    "L23": {"ends": ((2, 4), (3, 8)), "weight": 1},
    "L13": {"ends": ((1, 4), (3, 7)), "weight": 3},
}
ADJ = {1: [], 2: [], 3: []}      # dpid -> [(neighbour dpid, local port, link, weight)]
PORT_TO_LINK = {}                # (dpid, port) -> link name
NEIGHBOR = {}                    # (dpid, port) -> neighbour dpid
for _name, _info in LINKS.items():
    (_d1, _p1), (_d2, _p2) = _info["ends"]
    ADJ[_d1].append((_d2, _p1, _name, _info["weight"]))
    ADJ[_d2].append((_d1, _p2, _name, _info["weight"]))
    PORT_TO_LINK[(_d1, _p1)] = _name
    PORT_TO_LINK[(_d2, _p2)] = _name
    NEIGHBOR[(_d1, _p1)] = _d2
    NEIGHBOR[(_d2, _p2)] = _d1


class EmergencyController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}            # dpid -> datapath
        self.down_ports = set()        # (dpid, port) reported link-down
        self.jammed_links = set()
        self.prev_stats = {}           # (dpid, port) -> (tx_bytes, time)
        self.port_util = {}            # (dpid, port) -> latest usage 0..1
        # state from the server's EVENT messages (Task 5)
        self.subscribers = set()
        self.emergency_active = False
        self.emergency_ips = set()
        self.init_logs()
        hub.spawn(self._event_listener)
        hub.spawn(self._poll_loop)

    # ------------------------------------------------------------ logging
    def init_logs(self):
        for path, header in ((LOG_FILE, "timestamp,event,details\n"),
                             (STATS_FILE, "timestamp,dpid,port,link,tx_mbps,usage_pct\n")):
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                with open(path, "a") as f:
                    f.write(header)

    def log_event(self, event, details=""):
        details = str(details).replace(",", " ")
        line = "%.3f,%s,%s" % (time.time(), event, details)
        self.logger.info("[LOG] %s", line)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")

    # ------------------------------------------------------------ routing model
    def current_down_links(self):
        return {PORT_TO_LINK[k] for k in self.down_ports if k in PORT_TO_LINK}

    def _first_hop(self, src, dst, excluded):
        """Dijkstra over the 3 switches. Returns the local port at src to leave by."""
        dist = {src: 0}
        first = {src: None}
        heap = [(0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist.get(u, 1e9):
                continue
            if u == dst:
                return first[u]
            for v, port, link, weight in ADJ[u]:
                if link in excluded:
                    continue
                nd = d + weight
                if nd < dist.get(v, 1e9):
                    dist[v] = nd
                    first[v] = port if u == src else first[u]
                    heapq.heappush(heap, (nd, v))
        return None

    def next_port(self, dpid, dst_ip, emergency):
        """Output port on switch dpid for traffic to dst_ip.
        Normal traffic avoids only broken links. Emergency traffic also avoids
        jammed links (if a path exists), which is how it is protected."""
        _, dst_dpid, dst_port = HOSTS[dst_ip]
        if dpid == dst_dpid:
            return dst_port
        down = self.current_down_links()
        if emergency:
            port = self._first_hop(dpid, dst_dpid, down | self.jammed_links)
            if port is not None:
                return port
        return self._first_hop(dpid, dst_dpid, down)

    def path_str(self, src_dpid, dst_ip, emergency):
        dst_dpid = HOSTS[dst_ip][1]
        cur = src_dpid
        names = ["s%d" % cur]
        for _ in range(6):
            if cur == dst_dpid:
                break
            port = self.next_port(cur, dst_ip, emergency)
            if port is None or (cur, port) not in NEIGHBOR:
                names.append("UNREACHABLE")
                break
            cur = NEIGHBOR[(cur, port)]
            names.append("s%d" % cur)
        return ">".join(names)

    # ------------------------------------------------------------ flow helpers
    def add_flow(self, dp, priority, match, actions, idle_timeout=0):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                instructions=inst, idle_timeout=idle_timeout)
        dp.send_msg(mod)

    def install_routes(self, dp):
        """(Re)install normal + emergency rules for every known host on this switch.
        Re-sending a rule with the same match replaces it, so this also reroutes."""
        parser = dp.ofproto_parser
        for ip in HOSTS:
            out = self.next_port(dp.id, ip, False)
            if out is not None:
                match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip)
                self.add_flow(dp, PRIO_NORMAL, match, [parser.OFPActionOutput(out)])
            eout = self.next_port(dp.id, ip, True)
            if eout is not None:
                actions = [parser.OFPActionSetQueue(EMERGENCY_QUEUE),
                           parser.OFPActionOutput(eout)]
                for field in ("udp_dst", "udp_src"):
                    match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip, ip_proto=17,
                                            **{field: EMERGENCY_PORT})
                    self.add_flow(dp, PRIO_EMERGENCY, match, actions)

    def reroute_all(self, reason):
        for dp in list(self.datapaths.values()):
            self.install_routes(dp)
        down = "/".join(sorted(self.current_down_links())) or "none"
        jam = "/".join(sorted(self.jammed_links)) or "none"
        self.log_event("REROUTE",
                       "reason=%s down=%s jammed=%s emergency_path=%s normal_path=%s" % (
                           reason, down, jam,
                           self.path_str(1, "10.0.0.3", True),
                           self.path_str(1, "10.0.0.3", False)))

    # ------------------------------------------------------------ switch events
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        # wipe old rules, then table-miss (ARP and unknown go to the controller)
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=ofp.OFPTT_ALL,
                                      command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=parser.OFPMatch()))
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, parser.OFPMatch(), actions)
        self.install_routes(dp)
        self.log_event("SWITCH_CONNECTED", "dpid=%s" % dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if dp is None:
            return
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    # ------------------------------------------------------------ Task 1: ARP proxy
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        a = pkt.get_protocol(arp.arp)
        if a is None or a.opcode != arp.ARP_REQUEST:
            return                      # ignore LLDP, IPv6 and everything else
        entry = HOSTS.get(a.dst_ip)
        if entry is None:
            return
        self.send_arp_reply(dp, in_port, entry[0], a.dst_ip, a.src_mac, a.src_ip)

    def send_arp_reply(self, dp, out_port, my_mac, my_ip, their_mac, their_ip):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        e = ethernet.ethernet(dst=their_mac, src=my_mac, ethertype=ether_types.ETH_TYPE_ARP)
        a = arp.arp(opcode=arp.ARP_REPLY, src_mac=my_mac, src_ip=my_ip,
                    dst_mac=their_mac, dst_ip=their_ip)
        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(a)
        p.serialize()
        out = parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                  in_port=ofp.OFPP_CONTROLLER,
                                  actions=[parser.OFPActionOutput(out_port)], data=p.data)
        dp.send_msg(out)

    # ------------------------------------------------------------ Task 3: jam detection
    def _poll_loop(self):
        while True:
            for dp in list(self.datapaths.values()):
                parser = dp.ofproto_parser
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            hub.sleep(POLL_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        for st in ev.msg.body:
            key = (dpid, st.port_no)
            link = PORT_TO_LINK.get(key)
            if link is None:
                continue                # only inter-switch links are monitored
            prev = self.prev_stats.get(key)
            self.prev_stats[key] = (st.tx_bytes, now)
            if prev is None or st.tx_bytes < prev[0] or now <= prev[1]:
                continue
            bps = (st.tx_bytes - prev[0]) * 8.0 / (now - prev[1])
            util = bps / LINK_CAPACITY_BPS
            self.port_util[key] = util
            with open(STATS_FILE, "a") as f:
                f.write("%.3f,%s,%s,%s,%.3f,%.1f\n" % (
                    now, dpid, st.port_no, link, bps / 1e6, util * 100))
        self.evaluate_jams()

    def evaluate_jams(self):
        changed = False
        down = self.current_down_links()
        for name, info in LINKS.items():
            if name in down:
                continue
            utils = [self.port_util[k] for k in info["ends"] if k in self.port_util]
            if not utils:
                continue
            u = max(utils)
            if u >= JAM_HIGH and name not in self.jammed_links:
                self.jammed_links.add(name)
                changed = True
                self.logger.warning("WARNING: link %s usage %.0f%% is above %.0f%%: JAM",
                                    name, u * 100, JAM_HIGH * 100)
                self.log_event("JAM_DETECTED", "link=%s usage=%.0f%% emergency_active=%s" % (
                    name, u * 100, self.emergency_active))
            elif u < JAM_LOW and name in self.jammed_links:
                self.jammed_links.discard(name)
                changed = True
                self.log_event("JAM_CLEARED", "link=%s usage=%.0f%%" % (name, u * 100))
        if changed:
            self.reroute_all("jam state changed")

    # ------------------------------------------------------------ Task 4: link failure
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        key = (dp.id, msg.desc.port_no)
        link = PORT_TO_LINK.get(key)
        if link is None:
            return
        is_down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN) or \
            bool(msg.desc.config & ofp.OFPPC_PORT_DOWN) or \
            msg.reason == ofp.OFPPR_DELETE
        was_down = link in self.current_down_links()
        if is_down:
            self.down_ports.add(key)
        else:
            self.down_ports.discard(key)
        now_down = link in self.current_down_links()
        if now_down and not was_down:
            self.jammed_links.discard(link)
            self.log_event("LINK_DOWN", "link=%s dpid=%s port=%s" % (link, dp.id, key[1]))
            self.reroute_all("link %s down" % link)
        elif was_down and not now_down:
            self.log_event("LINK_UP", "link=%s" % link)
            self.reroute_all("link %s up" % link)

    # ------------------------------------------------------------ Task 5: server events
    def _event_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", EVENT_PORT))
        self.logger.info("Listening for server events on UDP %s", EVENT_PORT)
        while True:
            try:
                data, addr = sock.recvfrom(2048)
                self.handle_event(data.decode("utf-8", errors="replace").strip(), addr)
            except Exception as err:
                self.logger.error("Event listener error: %s", err)

    def handle_event(self, text, addr):
        parts = text.split("|")
        if len(parts) < 3 or parts[0] != "EVENT":
            self.log_event("BAD_EVENT", "from %s: %s" % (addr[0], text))
            return
        name = parts[1]
        ips = [ip for ip in parts[2].split(",") if ip]
        if name == "SUBSCRIBER_JOINED":
            self.subscribers.update(ips)
        elif name == "SUBSCRIBER_LEFT":
            self.subscribers.difference_update(ips)
        elif name == "EMERGENCY_START":
            self.emergency_active = True
            self.emergency_ips = set(ips)
        elif name == "EMERGENCY_END":
            self.emergency_active = False
            self.emergency_ips = set()
        else:
            self.log_event("UNKNOWN_EVENT", text)
            return
        self.log_event(name, "ips=%s subscribers=%d emergency_active=%s emergency_path=%s" % (
            "/".join(ips), len(self.subscribers), self.emergency_active,
            self.path_str(1, "10.0.0.3", True)))
