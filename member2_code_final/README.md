# Member 2 folder (controller)

controller.py              NEW controller: routing, ARP proxy, emergency priority, jam detection, reroute, server events
controller_stable.py       OLD controller (learning switch + priority + events). Already ran on the VM. Use as fallback
emergency_topology_m2.py   3-switch topology matching controller.py (pinned ports, queues, controller address)
setup_queues.sh            queues for one switch (needed only with controller_stable.py on single,3)
server.py, fake_subscriber.py, send_alerts.py   Member 1's files, unchanged, for testing the event link

Tabs: Tab 1 = normal terminal, Tab 2 = controller only, Tab 3 = Mininet only.
Always: controller first, then Mininet. Never run `sudo mn -c` while the controller runs.

## A) Stable demo (single switch)  -- use this for the evaluation fallback
Tab 1: sudo mn -c
Tab 2: source ~/ryu-env/bin/activate && cd ~/member2_code && ryu-manager controller_stable.py --verbose
Tab 3: sudo mn --controller=remote,ip=127.0.0.1,port=6653 --topo single,3 --switch ovsk,protocols=OpenFlow13
Tab 1: cd ~/member2_code && sudo ./setup_queues.sh s1
Tab 3 (mininet>): pingall
                  h2 iperf -s -u -p 9999 &
                  h1 iperf -c 10.0.0.2 -u -p 9999 -b 2M -t 5
Tab 1: sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep 9999
       sudo ovs-ofctl -O OpenFlow13 queue-stats s1

## B) Full build (3 switches, jam + reroute)
Tab 1: sudo mn -c
Tab 2: source ~/ryu-env/bin/activate && cd ~/member2_code && ryu-manager controller.py --verbose
Tab 3: cd ~/member2_code && sudo python3 emergency_topology_m2.py
       (look for: PORT CHECK: ALL 17 PORTS OK)
Tab 3 (mininet>): pingall
Jam test (mininet>):   h5 iperf -s -u -p 5001 &
                       h9 iperf -c 10.0.0.5 -u -b 15M -t 20 -p 5001
Tab 1: tail -8 controller_log.csv
       sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep "nw_dst=10.0.0.3," | grep 9999
Link failure (mininet>): link s1 s2 down
                         h1 ping -c 3 h3
                         link s1 s2 up
Server events (mininet>): h1 python3 -u server.py --controller-ip 10.0.0.100 &
                          h3 python3 -u fake_subscriber.py 10.0.0.3 normal 10.0.0.1 &
                          h2 python3 send_alerts.py 10.0.0.1 5000 CRITICAL 1 1
Shut down: exit (Tab 3), Ctrl+C (Tab 2), sudo mn -c (Tab 1)
