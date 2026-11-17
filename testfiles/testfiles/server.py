import argparse
import os
import socket
import threading
import time

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5000
SUBSCRIBER_ALERT_PORT = 9999

# CLI / Environment variable configuration
parser = argparse.ArgumentParser(description="Emergency Alert UDP Server")
parser.add_argument(
    "--controller-ip",
    default=os.getenv("CONTROLLER_IP", "127.0.0.1"),
    help="SDN Controller IP address (default: 127.0.0.1 or CONTROLLER_IP env var)",
)
parser.add_argument(
    "--controller-port",
    type=int,
    default=int(os.getenv("CONTROLLER_PORT", "6000")),
    help="SDN Controller port (default: 6000 or CONTROLLER_PORT env var)",
)
parser.add_argument(
    "--log-file",
    default=os.getenv("LOG_FILE", "server_log.csv"),
    help="CSV log file path (default: server_log.csv or LOG_FILE env var)",
)
args, _ = parser.parse_known_args()

CONTROLLER_HOST = args.controller_ip
CONTROLLER_PORT = args.controller_port
LOG_FILE = args.log_file

# Thread-safe storage for subscriber endpoints: {(ip_str, port_int), ...}
subscribers_lock = threading.Lock()
subscribers = set()

# Thread-safe global sequence number counter for alerts
alert_seq_lock = threading.Lock()
alert_seq_counter = 0

# Tracking unconfirmed subscribers for CRITICAL alerts: {seq_str: set(unconfirmed_ips)}
pending_acks_lock = threading.Lock()
pending_critical_alerts = {}

# Thread-safe lock for CSV writing
log_file_lock = threading.Lock()


def init_log_file() -> None:
    """Writes the CSV header only once if the file does not exist or is empty."""
    header = "timestamp,event,seq,subscriber_ip,details\n"
    with log_file_lock:
        try:
            write_header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
            if write_header:
                with open(LOG_FILE, "a") as f:
                    f.write(header)
                    f.flush()
        except OSError as err:
            print(f"[ERROR] Failed initializing log file header: {err}")


def log_event(event: str, seq: str, subscriber_ip: str, details: str) -> None:
    """Logs activity in the required format: timestamp,event,seq,subscriber_ip,details.
    
    Uses float timestamp formatted to 3 decimal places, synchronizes via lock, 
    and flushes immediately.
    """
    current_time = f"{time.time():.3f}"
    clean_details = str(details).replace(",", " ")
    log_line = f"{current_time},{event},{seq},{subscriber_ip},{clean_details}"
    print(f"[LOG] {log_line}")

    with log_file_lock:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(log_line + "\n")
                f.flush()
        except OSError as err:
            print(f"[ERROR] Failed writing to {LOG_FILE}: {err}")


def get_next_seq() -> int:
    """Thread-safe incremental sequence generator."""
    global alert_seq_counter
    with alert_seq_lock:
        alert_seq_counter += 1
        return alert_seq_counter


def build_message(msg_type: str, severity: str, seq: str, text: str) -> str:
    """Constructs a message strictly adhering to: TYPE|SEVERITY|SEQ|TIMESTAMP|TEXT."""
    timestamp = f"{time.time():.4f}"
    clean_text = text.replace("|", "/")  # Enforce spec rule: no '|' in TEXT
    return f"{msg_type}|{severity}|{seq}|{timestamp}|{clean_text}"


def parse_message(raw_data: str):
    """Splits and validates the message format: TYPE|SEVERITY|SEQ|TIMESTAMP|TEXT."""
    cleaned = raw_data.rstrip("\r\n")
    if not cleaned:
        return None

    fields = cleaned.split("|")

    # Handle messages ending with empty text (e.g. ACK|CRITICAL|12|1761660000.25|)
    if len(fields) == 4:
        fields.append("")
    elif len(fields) > 5:
        fields = fields[:4] + ["|".join(fields[4:])]
    elif len(fields) < 4:
        return None

    return {
        "type": fields[0],
        "severity": fields[1],
        "seq": fields[2],
        "timestamp": fields[3],
        "text": fields[4],
    }


def notify_controller(event_name: str, ip_list: list) -> None:
    """Sends non-blocking control plane notification to controller without slowing server down."""
    ips_str = ",".join(ip_list)
    msg = f"EVENT|{event_name}|{ips_str}"
    payload = msg.encode("utf-8")

    def _async_send():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.sendto(payload, (CONTROLLER_HOST, CONTROLLER_PORT))
            log_event("EVENT_SENT", "0", CONTROLLER_HOST, event_name)
        except socket.error as err:
            log_event("ERROR", "0", CONTROLLER_HOST, f"Failed notifying controller: {err}")
        finally:
            sock.close()

    threading.Thread(target=_async_send, daemon=True).start()


def handle_critical_alert_tracking(seq: str, text: str, initial_ips: set) -> None:
    """Monitors ACKs in a background thread for a given CRITICAL alert sequence."""
    alert_bytes = build_message(
        msg_type="ALERT",
        severity="CRITICAL",
        seq=seq,
        text=text
    ).encode("utf-8")

    with pending_acks_lock:
        pending_critical_alerts[seq] = set(initial_ips)

    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        # Initial delivery to all targeted subscribers
        for ip in initial_ips:
            try:
                send_sock.sendto(alert_bytes, (ip, SUBSCRIBER_ALERT_PORT))
                log_event("ALERT_SENT", seq, ip, "CRITICAL")
            except socket.error as err:
                log_event("ERROR", seq, ip, f"Initial send error: {err}")

        # Wait and perform up to 3 resends
        for attempt in range(1, 4):
            time.sleep(1.0)

            with pending_acks_lock:
                unconfirmed_ips = list(pending_critical_alerts.get(seq, set()))

            if not unconfirmed_ips:
                break

            for ip in unconfirmed_ips:
                try:
                    send_sock.sendto(alert_bytes, (ip, SUBSCRIBER_ALERT_PORT))
                    log_event("RESEND", seq, ip, str(attempt))
                except socket.error as err:
                    log_event("ERROR", seq, ip, f"Resend {attempt} error: {err}")

        # Final 1-second timeout if resends occurred and subscribers were still pending
        with pending_acks_lock:
            remaining = list(pending_critical_alerts.get(seq, set()))

        if remaining:
            time.sleep(1.0)
            with pending_acks_lock:
                final_unconfirmed = list(pending_critical_alerts.get(seq, set()))
            for ip in final_unconfirmed:
                log_event("FAILED", seq, ip, "Missing ACK after 3 resends")

    finally:
        send_sock.close()
        with pending_acks_lock:
            pending_critical_alerts.pop(seq, None)

        # Notify controller that critical alert handling has concluded
        with subscribers_lock:
            current_ips = [ip for ip, _ in subscribers]
        notify_controller("EMERGENCY_END", current_ips)


def send_alert_to_subscribers(severity: str, text: str) -> int:
    """Broadcasts an ALERT to all registered subscriber IPs on UDP port 9999."""
    seq = str(get_next_seq())
    severity = severity.upper()

    with subscribers_lock:
        target_ips = set(ip for ip, _ in subscribers)

    if not target_ips:
        print(f"[ALERT] No subscribers registered to receive alert (Seq: {seq}).")
        return int(seq)

    if severity == "CRITICAL":
        notify_controller("EMERGENCY_START", list(target_ips))

        alert_thread = threading.Thread(
            target=handle_critical_alert_tracking,
            args=(seq, text, target_ips),
            daemon=True,
        )
        alert_thread.start()
    else:
        # INFO / WARNING behavior: sent once, no ACK required
        alert_msg = build_message(
            msg_type="ALERT",
            severity=severity,
            seq=seq,
            text=text
        ).encode("utf-8")

        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for ip in target_ips:
                try:
                    send_sock.sendto(alert_msg, (ip, SUBSCRIBER_ALERT_PORT))
                    log_event("ALERT_SENT", seq, ip, severity)
                except socket.error as err:
                    log_event("ERROR", seq, ip, f"Failed sending alert: {err}")
        finally:
            send_sock.close()

    return int(seq)


def process_packet(sock: socket.socket, raw_message: str, client_addr: tuple) -> None:
    """Dispatches incoming UDP datagrams without blocking the main socket loop."""
    client_ip, client_port = client_addr
    parsed = parse_message(raw_message)

    if not parsed:
        print(f"[WARN] Discarded invalid message format from {client_addr}: {repr(raw_message)}")
        log_event("ERROR", "0", client_ip, f"Invalid packet format from port {client_port}")
        return

    msg_type = parsed["type"].upper()
    seq = parsed["seq"]
    severity = parsed["severity"]
    text = parsed["text"]

    if msg_type == "REGISTER":
        with subscribers_lock:
            subscribers.add(client_addr)

        log_event("REGISTERED", seq, client_ip, f"Port {client_port}")
        notify_controller("SUBSCRIBER_JOINED", [client_ip])

        response = build_message(
            msg_type="OK",
            severity="INFO",
            seq=seq,
            text="Registered successfully"
        )
        sock.sendto(response.encode("utf-8"), client_addr)

    elif msg_type == "UNREGISTER":
        removed = False
        with subscribers_lock:
            if client_addr in subscribers:
                subscribers.remove(client_addr)
                removed = True

        if removed:
            log_event("UNREGISTERED", seq, client_ip, f"Port {client_port}")
            notify_controller("SUBSCRIBER_LEFT", [client_ip])
            response = build_message(
                msg_type="OK",
                severity="INFO",
                seq=seq,
                text="Unregistered successfully"
            )
        else:
            response = build_message(
                msg_type="OK",
                severity="INFO",
                seq=seq,
                text="Subscriber not found"
            )

        sock.sendto(response.encode("utf-8"), client_addr)

    elif msg_type == "ACK":
        log_event("ACK_RECEIVED", seq, client_ip, f"Confirmed SEQ {seq}")
        with pending_acks_lock:
            if seq in pending_critical_alerts:
                pending_critical_alerts[seq].discard(client_ip)

    elif msg_type == "ALERT":
        send_alert_to_subscribers(severity, text)


def run_server() -> None:
    init_log_file()
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        server_sock.bind((SERVER_HOST, SERVER_PORT))
        print(f"[SERVER] Listening on UDP {SERVER_HOST}:{SERVER_PORT}")
        print(f"[SERVER] Controller target configured at {CONTROLLER_HOST}:{CONTROLLER_PORT}")
        print(f"[SERVER] CSV logging to {LOG_FILE}")

        while True:
            try:
                data, client_addr = server_sock.recvfrom(2048)
                raw_message = data.decode("utf-8", errors="ignore")

                handler_thread = threading.Thread(
                    target=process_packet,
                    args=(server_sock, raw_message, client_addr),
                    daemon=True,
                )
                handler_thread.start()

            except socket.error as sock_err:
                log_event("ERROR", "0", SERVER_HOST, f"Socket error on recvfrom: {sock_err}")
            except Exception as ex:
                log_event("ERROR", "0", SERVER_HOST, f"Unexpected exception in loop: {ex}")

    finally:
        server_sock.close()


if __name__ == "__main__":
    run_server()
