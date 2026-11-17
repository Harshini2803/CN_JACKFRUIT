# Emergency Notification Network (SDN)

A 3-switch Mininet network controlled by a Ryu (OpenFlow 1.3) controller. An alert server (h1) sends
emergency alerts to subscribers; the controller gives alert traffic (UDP 9999) priority, detects link
jams, reroutes emergency traffic, and recovers from link failures.

## Files

| File | Purpose |
|------|---------|
| `demo.py` | **One-command demo** of everything below (start here) |
| `controller.py` | Ryu controller: routing, ARP proxy, emergency priority, jam detection, reroute, server events |
| `emergency_topology.py` | 3-switch, 10-host Mininet topology (pinned ports, queues, controller address). Can also be run on its own |
| `server.py` | Alert server (runs on h1): receives alerts, forwards to subscribers, handles ACKs and resends |
| `fake_subscriber.py` | Test subscriber (modes: `normal`, `silent`, `flaky`) |
| `send_alerts.py` | Sends test INFO / CRITICAL alerts to the server (acts as the authority, h2) |
| `tests/run_jam_test.py` | Recorded traffic-jam experiment (about 2 min), saves results to `results/` |
| `demo_out/` | Output files written by `demo.py` (server log, subscriber logs, iperf output) |
| `controller_log.csv`, `link_stats.csv` | Written by the controller in the folder it was started from |

## Requirements

Linux VM with Mininet, Open vSwitch, `iperf`, Python 3, and Ryu (e.g. in `~/ryu-env`).
`demo.py` must be run with `sudo`.

---

## See the demo with `demo.py`

`demo.py` builds the network and runs the whole demo for you, so you do not have to type the commands by hand.

### Option 1: controller in its own tab (recommended)

```
Tab 1:  sudo mn -c
Tab 2:  source ~/ryu-env/bin/activate
        cd ~/cn_final
        ryu-manager controller.py --verbose
Tab 3:  cd ~/cn_final
        sudo python3 demo.py
```

Start the controller **first**, then run `demo.py`. Never run `sudo mn -c` while the controller is running.

### Option 2: let the demo start the controller

```
cd ~/cn_final
sudo mn -c
sudo python3 demo.py --start-controller
```

Looks for `ryu-manager` in `~/ryu39/bin`, then `~/ryu-env/bin`, then `PATH`. Use `--ryu-bin /path/to/ryu-manager` if yours is elsewhere.
In this mode the demo also stops the controller and cleans up Mininet when it finishes.

### What you will see

By default the demo pauses between steps, so press **Enter** to move on. At the end it drops you into the Mininet CLI (type `exit` to finish).

| Step | What it shows |
|------|---------------|
| `connectivity` | `pingall` across all 10 hosts (ARP proxy + proactive routes) |
| `alerts` | Server and subscribers start; an INFO alert is delivered, then a CRITICAL alert that every subscriber ACKs |
| `priority` | OpenFlow rule for UDP 9999 with priority 100 and emergency queue 1, queue counters, iperf test on port 9999 |
| `jam` | h9 floods the 10 Mbps s2-s3 link with 15 Mbps; the controller reports `JAM_DETECTED`, moves emergency traffic to the direct s1-s3 link, a CRITICAL alert is still ACKed, then `JAM_CLEARED` and the route returns |
| `failure` | Link s1-s2 goes down, routes are recomputed, ping still works; link comes back up |
| `flaky` | A subscriber drops the first 2 copies of a CRITICAL alert; the server resends until it gets the ACK |

The final line prints totals: alerts sent, ACKs, resends, failed.

### Options

| Option | Meaning |
|--------|---------|
| `--auto` | No pauses, run all steps straight through (no Mininet CLI at the end) |
| `--start-controller` | Start `controller.py` automatically (skip Tab 2) |
| `--only a,b` | Run only some steps: `connectivity,alerts,priority,jam,failure,flaky` |
| `--ryu-bin PATH` | Path to `ryu-manager` (used with `--start-controller`) |
| `--ctrl-log PATH` | Controller log to read (default `./controller_log.csv`) |

Examples:

```
sudo python3 demo.py --auto
sudo python3 demo.py --start-controller --auto
sudo python3 demo.py --only jam,failure
```

### Output files

- `demo_out/server_log.csv`: server events (REGISTERED, ALERT_SENT, ACK_RECEIVED, RESEND, FAILED)
- `demo_out/rx_<ip>.csv`: alerts received by each subscriber
- `demo_out/*.txt`: stdout of the server, subscribers, iperf and (with `--start-controller`) the controller
- `controller_log.csv`, `link_stats.csv`: controller events (JAM_DETECTED, REROUTE, LINK_DOWN, ...) and link utilization

### Troubleshooting

- **"No controller found on 127.0.0.1:6653"**: start `ryu-manager controller.py` first, or use `--start-controller`.
- **"Switches did not connect to the controller"**: check `demo_out/controller_stdout.txt` and `sudo ovs-vsctl show`; make sure nothing ran `mn -c` after the controller started.
- **"Missing tool"**: install Mininet / Open vSwitch / `iperf`.
- After a crash, clean up with `sudo mn -c` (only when the controller is not running).

---

## Manual run (without `demo.py`)

```
Tab 1:  sudo mn -c
Tab 2:  source ~/ryu-env/bin/activate && cd ~/cn_final && ryu-manager controller.py --verbose
Tab 3:  cd ~/cn_final && sudo python3 emergency_topology.py
        (look for: PORT CHECK: ALL 17 PORTS OK)
Tab 3 (mininet>):  pingall
```

Jam test (mininet>):
```
h5 iperf -s -u -p 5001 &
h9 iperf -c 10.0.0.5 -u -b 15M -t 20 -p 5001
```
Tab 1:
```
tail -8 controller_log.csv
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep "nw_dst=10.0.0.3," | grep 9999
```

Link failure (mininet>):
```
link s1 s2 down
h1 ping -c 3 h3
link s1 s2 up
```

Server events (mininet>):
```
h1 python3 -u server.py --controller-ip 10.0.0.100 &
h3 python3 -u fake_subscriber.py 10.0.0.3 normal 10.0.0.1 &
h2 python3 send_alerts.py 10.0.0.1 5000 CRITICAL 1 1
```

Shut down: `exit` (Tab 3), Ctrl+C (Tab 2), `sudo mn -c` (Tab 1).

## Recorded jam experiment

With the controller running and no Mininet session open:

```
cd ~/cn_final && sudo python3 tests/run_jam_test.py
```

Takes about 2 minutes and saves summaries, flow dumps and logs into `results/`.
