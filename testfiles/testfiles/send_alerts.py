#!/usr/bin/env python3
"""Send test ALERT messages to the emergency-alert server.

Usage: python3 send_alerts.py <server_ip> <server_port> <severity> <count> <interval_s>

Each datagram is:  ALERT|<severity>|0|<time.time()>|Test alert <n>
(the server assigns the real SEQ, so the SEQ field is 0).
"""
import argparse
import socket
import time


def main() -> None:
    ap = argparse.ArgumentParser(description="Send test alerts to the server")
    ap.add_argument("server_ip")
    ap.add_argument("server_port", type=int)
    ap.add_argument("severity")
    ap.add_argument("count", type=int)
    ap.add_argument("interval", type=float, help="seconds between messages")
    a = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start = time.monotonic()
    try:
        for n in range(1, a.count + 1):
            msg = f"ALERT|{a.severity}|0|{time.time()}|Test alert {n}"
            sock.sendto(msg.encode("utf-8"), (a.server_ip, a.server_port))
            print(f"sent {n}/{a.count}: {msg}", flush=True)
            if n < a.count:
                # schedule against the start time so timing errors don't accumulate
                delay = start + n * a.interval - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
