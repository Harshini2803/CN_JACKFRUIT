import socket, sys, time

# Usage: python3 fake_subscriber.py <my_ip> [mode] [server_ip] [logfile]
#   mode: normal = ACK every CRITICAL alert
#         silent = never ACK
#         flaky  = ignore the first 2 copies of a CRITICAL alert, ACK the 3rd
my_ip     = sys.argv[1]
mode      = sys.argv[2] if len(sys.argv) > 2 else "normal"
server_ip = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
logfile   = sys.argv[4] if len(sys.argv) > 4 else None

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind((my_ip, 9999))                      # listen for alerts on 9999
s.sendto(b"REGISTER|INFO|0|0|hi", (server_ip, 5000))
print(f"[{my_ip}] registered with {server_ip}:5000, mode={mode}", flush=True)

log = open(logfile, "a") if logfile else None
copies = {}                                # SEQ -> copies received

while True:
    data, addr = s.recvfrom(2048)
    now = time.time()
    msg = data.decode(errors="replace").strip()
    print(f"[{my_ip}] got from {addr[0]}:{addr[1]}: {msg}", flush=True)

    p = msg.split("|")
    if len(p) < 4 or p[0] != "ALERT":
        continue                           # OK replies etc. are just printed
    severity, seq, sent_time = p[1], p[2], p[3]

    if log:                                # recv_time,ip,severity,seq,sent_time
        log.write(f"{now:.3f},{my_ip},{severity},{seq},{sent_time}\n")
        log.flush()

    if severity != "CRITICAL":
        continue                           # INFO and WARNING need no ACK

    copies[seq] = copies.get(seq, 0) + 1
    if mode == "silent":
        print(f"[{my_ip}] silent mode: not ACKing SEQ {seq}", flush=True)
        continue
    if mode == "flaky" and copies[seq] < 3:
        print(f"[{my_ip}] flaky mode: ignoring copy {copies[seq]} of SEQ {seq}", flush=True)
        continue

    ack = f"ACK|CRITICAL|{seq}|{now}|"
    s.sendto(ack.encode(), (server_ip, 5000))
    print(f"[{my_ip}] sent ACK for SEQ {seq}", flush=True)
