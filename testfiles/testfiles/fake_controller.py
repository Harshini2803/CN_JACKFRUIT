import socket, time

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", 6000))
print("fake controller listening on UDP 6000...", flush=True)

while True:
    data, addr = s.recvfrom(2048)
    t = time.strftime("%H:%M:%S")
    print(f"{t} EVENT from {addr[0]}: {data.decode(errors='replace').strip()}", flush=True)
