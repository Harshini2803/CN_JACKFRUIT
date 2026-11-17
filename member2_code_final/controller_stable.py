import os
import socket
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet

EMERGENCY_PORT = 9999
EMERGENCY_QUEUE = 1
PRIORITY_NORMAL = 1
PRIORITY_EMERGENCY = 100
EVENT_PORT = 6000
LOG_FILE = "controller_log.csv"


class EmergencyController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.subscribers = set()
        self.emergency_active = False
        self.emergency_ips = set()
        self.init_log()
        hub.spawn(self._event_listener)

    def init_log(self):
        if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
            with open(LOG_FILE, "a") as f:
                f.write("timestamp,event,details\n")

    def log_event(self, event, details=""):
        details = str(details).replace(",", " ")
        line = f"{time.time():.3f},{event},{details}"
        self.logger.info("[LOG] %s", line)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")

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
            self.log_event("BAD_EVENT", f"from {addr[0]}: {text}")
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
        self.log_event(name, f"ips={'/'.join(ips)} subscribers={len(self.subscribers)} "
                             f"emergency_active={self.emergency_active}")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions)
        self.log_event("SWITCH_CONNECTED", f"dpid={dp.id}")

    def add_flow(self, dp, priority, match, actions, idle_timeout=0):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                instructions=inst, idle_timeout=idle_timeout)
        dp.send_msg(mod)

    def install_emergency_rules(self, dp, eth_dst, out_port):
        parser = dp.ofproto_parser
        actions = [parser.OFPActionSetQueue(EMERGENCY_QUEUE),
                   parser.OFPActionOutput(out_port)]
        for field in ("udp_dst", "udp_src"):
            match = parser.OFPMatch(eth_type=0x0800, eth_dst=eth_dst,
                                    ip_proto=17, **{field: EMERGENCY_PORT})
            self.add_flow(dp, PRIORITY_EMERGENCY, match, actions, idle_timeout=0)
        self.log_event("EMERGENCY_RULES", f"dst={eth_dst} out_port={out_port} queue={EMERGENCY_QUEUE}")

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == 0x88cc:
            return

        dpid = dp.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        if eth.dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][eth.dst]
        else:
            out_port = ofp.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst, eth_src=eth.src)
            self.add_flow(dp, PRIORITY_NORMAL, match, actions, idle_timeout=30)
            self.install_emergency_rules(dp, eth.dst, out_port)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)
