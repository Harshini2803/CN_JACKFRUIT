# Emergency Notification Network (SDN)

A Software Defined Networking (SDN) project that demonstrates reliable
emergency alert delivery using **Ryu**, **OpenFlow 1.3**, **Mininet**,
and **Open vSwitch**.

The network gives emergency traffic preferential treatment, monitors
link congestion, reroutes emergency traffic through an alternate path,
and responds to link failures.

## Key Features

-   Emergency traffic identification using UDP port `9999`.
-   Higher-priority OpenFlow rules and a separate emergency queue.
-   Congestion detection using switch port statistics.
-   Emergency traffic rerouting when the primary path is congested.
-   Route recovery after congestion clears or a link failure occurs.
-   Subscriber registration, ACK handling, and retransmission for
    critical alerts.
-   Demonstration of normal, silent, and flaky subscribers.

## Network at a Glance

-   **3 switches:** `s1`, `s2`, `s3`
-   **10 hosts:** alert server, authority, subscribers, and background
    traffic hosts
-   **Primary path:** `s1 → s2 → s3`
-   **Alternate path:** `s1 → s3`

## Technologies Used

  Technology     Role
  -------------- ----------------------------------------
  Python 3       Project implementation
  Ryu            SDN controller
  OpenFlow 1.3   Controller-to-switch communication
  Mininet        Virtual network emulation
  Open vSwitch   Software switches
  iperf          Network traffic generation and testing

## Repository Structure

``` text
CN_JACKFRUIT/
├── README.md              # Project overview (this page)
├── .gitignore
└── cn_final/
    ├── README.md          # Detailed setup, usage, and troubleshooting guide
    ├── controller.py
    ├── emergency_topology.py
    ├── server.py
    ├── send_alerts.py
    ├── fake_subscriber.py
    ├── demo.py
    └── ...
```

## Run the Project

The detailed README contains prerequisites, terminal-by-terminal setup
instructions, demo stages, command-line options, manual test commands,
and troubleshooting.

**[Open the detailed project README](cn_final/README.md)**

## Project Demonstration

The main demo (`cn_final/demo.py`) covers:

1.  Network connectivity
2.  Emergency alert delivery
3.  Emergency traffic priority and queue handling
4.  Congestion detection and emergency traffic rerouting
5.  Link failure and route recovery
6.  Flaky subscriber acknowledgement and retransmission behaviour

## Contributors

See the repository's commit history for project contributions.
