#!/bin/bash
# Usage: sudo ./setup_queues.sh s1
# Queue 0 = normal traffic, Queue 1 = emergency traffic (guaranteed 8 Mbps)
SW=${1:-s1}
for PORT in $(ovs-vsctl list-ports $SW); do
  ovs-vsctl -- set port $PORT qos=@newqos -- \
    --id=@newqos create qos type=linux-htb other-config:max-rate=10000000 queues:0=@q0 queues:1=@q1 -- \
    --id=@q0 create queue other-config:min-rate=1000000 other-config:max-rate=10000000 -- \
    --id=@q1 create queue other-config:min-rate=8000000 other-config:max-rate=10000000
  echo "queues set on $PORT"
done
