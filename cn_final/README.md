# Detailed Setup and Usage Guide

This README contains the file descriptions, setup instructions,
demonstration details, manual commands, output information, and
troubleshooting for the Emergency Notification Network project.

For the project introduction, key features, network summary,
technologies, and repository structure, see the [repository root
README](../README.md).

## Project Files

  -----------------------------------------------------------------------
  File / Folder                       Purpose
  ----------------------------------- -----------------------------------
  `demo.py`                           Main demonstration script; runs the
                                      project's demonstration stages

  `controller.py`                     Ryu controller: routing, ARP proxy,
                                      emergency flow rules, congestion
                                      monitoring, rerouting, link events,
                                      and logging

  `emergency_topology.py`             Creates the Mininet topology,
                                      configures links and queues, and
                                      checks ports

  `server.py`                         Alert server on `h1`; handles
                                      subscriber registration, alert
                                      delivery, ACKs, and retransmissions

  `fake_subscriber.py`                Subscriber simulator with `normal`,
                                      `silent`, and `flaky` modes

  `send_alerts.py`                    Sends test INFO and CRITICAL alerts
                                      to the server; acts as the
                                      authority on `h2`

  `demo_out/`                         Output captured by the demo,
                                      including server, subscriber, and
                                      iperf logs

  `controller_log.csv`                Controller event log, such as
                                      congestion, rerouting, and
                                      link-state events

  `link_stats.csv`                    Controller port/link statistics and
                                      utilisation measurements
  

  `results/`                          Retained experiment output, if
                                      present in the repository
  -----------------------------------------------------------------------

## Requirements and Environment

You need a Linux VM with:

-   Python 3
-   Mininet
-   Open vSwitch
-   Ryu SDN controller
-   `iperf`

Mininet setup and network creation require `sudo` privileges. The
examples below assume that the project is located at `~/cn_final` and
that the Ryu virtual environment is `~/ryu-env`. Adjust these paths if
your installation uses different locations.

## Running the Demonstration

The main demonstration is run using `demo.py`.

### Option 1: Run the controller in a separate terminal (recommended)

Open three terminals.

**Terminal 1 --- clean up any previous Mininet network:**

``` bash
sudo mn -c
```

**Terminal 2 --- activate the Ryu environment and start the
controller:**

``` bash
source ~/ryu-env/bin/activate
cd ~/cn_final
ryu-manager controller.py --verbose
```
If not working, replace ~/ryu-env with the actual path to your Ryu virtual environment.

**Terminal 3 --- run the demo:**

``` bash
cd ~/cn_final
sudo python3 demo.py
```

Start the controller before running `demo.py`. Do not run `sudo mn -c`
while the controller is running, because it can interfere with the
active Mininet network.

### Option 2: Let the demo start the controller

``` bash
cd ~/cn_final
sudo mn -c
sudo python3 demo.py --start-controller
```

In this mode, the demo looks for `ryu-manager` in `~/ryu39/bin`, then
`~/ryu-env/bin`, and then in `PATH`. If it is installed elsewhere,
provide its location:

``` bash
sudo python3 demo.py --start-controller --ryu-bin /path/to/ryu-manager
```

In this mode, the demo also stops the controller and cleans up Mininet
when it finishes.

## Demonstration Stages

By default, the demo pauses between stages. Press **Enter** to continue.
At the end, it opens the Mininet CLI; type `exit` to leave it.

  -----------------------------------------------------------------------
  Stage                               What it demonstrates
  ----------------------------------- -----------------------------------
  `connectivity`                      Runs `pingall` across the 10 hosts,
                                      checking connectivity using the
                                      controller's ARP handling and
                                      routes

  `alerts`                            Starts the server and subscribers,
                                      then demonstrates INFO and CRITICAL
                                      alert delivery and ACKs

  `priority`                          Displays the OpenFlow rule for UDP
                                      port `9999`, its priority, queue
                                      statistics, and an iperf UDP test

  `jam`                               Generates background traffic from
                                      `h9` toward `h6` to load the
                                      `s2–s3` link; the controller
                                      detects congestion, reroutes
                                      emergency traffic through `s1–s3`,
                                      and later restores the primary
                                      route when congestion clears

  `failure`                           Takes the `s1–s2` link down, checks
                                      that routes are recomputed and
                                      connectivity remains available,
                                      then restores the link

  `flaky`                             Demonstrates a subscriber that
                                      ignores the first two copies of a
                                      CRITICAL alert and acknowledges the
                                      third copy
                                      
  -----------------------------------------------------------------------

### Congestion thresholds

The controller checks port statistics periodically. In the current
project configuration:

-   Statistics polling interval: **2 seconds**
-   Congestion detection threshold: **80% link utilisation**
-   Congestion-cleared threshold: **below 60% link utilisation**

The separate thresholds help avoid repeatedly switching between
congested and clear states when utilisation fluctuates near one
threshold.

### Understanding the priority demonstration

The emergency flow rule matches UDP traffic destined for port `9999`.
Its OpenFlow priority is higher than the normal rule. The rule's action
assigns matching traffic to the emergency queue.

-   OpenFlow priority determines which matching flow rule is selected.
-   Queue configuration determines how packets are handled by the
    configured transmission queue.

The priority stage verifies the installed rule and queue statistics and
runs a basic UDP traffic test. The congestion and rerouting behaviour is
demonstrated in the `jam` stage.

### Understanding alert acknowledgements

-   `INFO` and `WARNING` alerts are sent without ACK-based
    retransmission.
-   `CRITICAL` alerts require an ACK.
-   If an ACK is missing, the server retransmits to the subscriber that
    has not acknowledged the alert, subject to the configured retry
    limit.
-   A normal subscriber acknowledges promptly.
-   A silent subscriber does not acknowledge.
-   A flaky subscriber ignores the first two copies and acknowledges the
    third copy.

The flaky subscriber's third copy consists of the initial transmission
plus two resends. This is separate from the server's overall maximum
retry limit.

## Demo Options

  -----------------------------------------------------------------------
  Option                              Meaning
  ----------------------------------- -----------------------------------
  `--auto`                            Runs without pauses and skips the
                                      Mininet CLI at the end

  `--start-controller`                Starts `controller.py`
                                      automatically

  `--only a,b`                        Runs selected stages; supported
                                      names are `connectivity`, `alerts`,
                                      `priority`, `jam`, `failure`, and
                                      `flaky`

  `--ryu-bin PATH`                    Specifies the `ryu-manager`
                                      executable; used with
                                      `--start-controller`

  `--ctrl-log PATH`                   Specifies the controller log to
                                      read; default is
                                      `./controller_log.csv`
                                      
  -----------------------------------------------------------------------

Examples:

``` bash
sudo python3 demo.py --auto
sudo python3 demo.py --start-controller --auto
sudo python3 demo.py --only jam,failure
```

## Output Files

-   `demo_out/server_log.csv`: server events, such as registration,
    alert sending, ACK receipt, resends, and failures.
-   `demo_out/rx_<ip>.csv`: alerts received by each subscriber, when
    subscriber logging is enabled.
-   `demo_out/*.txt`: captured standard output from the server,
    subscribers, iperf, background traffic processes, and (when started
    by the demo) the controller.
-   `controller_log.csv`: controller events such as `JAM_DETECTED`,
    `REROUTE`, `JAM_CLEARED`, `LINK_DOWN`, and `LINK_UP`.
-   `link_stats.csv`: recorded link and port statistics used for
    utilisation monitoring.

The controller writes its CSV logs in the working directory from which
it is started. Run it from the project directory if you want the logs
there.

## Manual Run (without `demo.py`)

The following is an alternative way to start the topology and try
individual functions.

**Terminal 1 --- clean up Mininet:**

``` bash
sudo mn -c
```

**Terminal 2 --- start the controller:**

``` bash
source ~/ryu-env/bin/activate
cd ~/cn_final
ryu-manager controller.py --verbose
```

**Terminal 3 --- start the topology:**

``` bash
cd ~/cn_final
sudo python3 emergency_topology.py
```

When the Mininet CLI appears, check connectivity:

``` text
pingall
```

The topology script reports a port check. In the expected setup, look
for:

``` text
PORT CHECK: ALL 17 PORTS OK
```

### Manual congestion traffic example

From the Mininet CLI, start a UDP iperf server on `h5`:

``` text
h5 iperf -s -u -p 5001 &
```

Then generate background UDP traffic from `h9` to `h5`:

``` text
h9 iperf -c 10.0.0.5 -u -b 15M -t 20 -p 5001
```

In another terminal, inspect the controller log and the emergency flow:

``` bash
tail -8 ~/cn_final/controller_log.csv
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep "nw_dst=10.0.0.3," | grep 9999
```

### Manual link-failure example

From the Mininet CLI:

``` text
link s1 s2 down
h1 ping -c 3 h3
link s1 s2 up
```

### Manual alert-server example

From the Mininet CLI, start the server and a subscriber:

``` text
h1 python3 -u server.py --controller-ip 10.0.0.100 &
h3 python3 -u fake_subscriber.py 10.0.0.3 normal 10.0.0.1 &
```

Send a CRITICAL alert from the authority host:

``` text
h2 python3 send_alerts.py 10.0.0.1 5000 CRITICAL 1 1
```

### Shut down

Exit the Mininet CLI, stop the controller with `Ctrl+C`, and clean up
Mininet when the controller is no longer running:

``` bash
sudo mn -c
```

## Troubleshooting

-   **`No controller found on 127.0.0.1:6653`:** Start
    `ryu-manager controller.py` before the demo, or use
    `--start-controller`.

-   **Switches do not connect to the controller:** Check the controller
    output and run `sudo ovs-vsctl show`. Make sure Mininet was not
    cleaned up while the controller was running.

-   **Missing command or tool:** Check that Mininet, Open vSwitch, and
    `iperf` are installed.

-   **Permission denied when writing CSV logs:** Check ownership and
    write permissions for `controller_log.csv` and `link_stats.csv`. If
    the files were created by `sudo`, restore ownership to your normal
    user, for example:

    ``` bash
    cd ~/cn_final
    sudo chown "$USER":"$USER" controller_log.csv link_stats.csv
    chmod u+rw controller_log.csv link_stats.csv
    ```

-   **After a failed run:** Stop the controller first, then clean up
    Mininet with `sudo mn -c`.
