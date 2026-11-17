# Member 2 demo helper. Run every target from inside ~/member2_code
# Tab 1 = normal terminal, Tab 2 = controller, Tab 3 = Mininet
SHELL := /bin/bash
VENV  := $(HOME)/ryu-env/bin/activate

.DEFAULT_GOAL := help

help:
	@echo "TAB 1 (normal terminal)"
	@echo "  make clean       clean Mininet + delete old logs   (run first)"
	@echo "  make log         live controller log (Ctrl+C to stop)"
	@echo "  make events      last 8 lines of controller log"
	@echo "  make flows       all rules on s1"
	@echo "  make flows9999   emergency rules (port 9999) on s1"
	@echo "  make queues      queue statistics on s1"
	@echo "  make jamflows    emergency rule for h3 (shows port 3 vs 4)"
	@echo "  make results     show the run_jam_test.py results"
	@echo "  make shutdown    final cleanup"
	@echo "TAB 2 (controller)"
	@echo "  make controller  start Ryu controller (start BEFORE Mininet)"
	@echo "TAB 3 (Mininet)"
	@echo "  make topology    start the 3-switch network"
	@echo "  make jamtest     recorded jam test (exit Mininet first, ~2 min)"
	@echo "Inside mininet> :  source demo1_forwarding.mn  (…demo2…demo5)"

# ---------- Tab 1 ----------
clean:
	sudo mn -c
	rm -f controller_log.csv link_stats.csv

log:
	tail -f controller_log.csv

events:
	tail -8 controller_log.csv

flows:
	sudo ovs-ofctl -O OpenFlow13 dump-flows s1

flows9999:
	sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep 9999

queues:
	sudo ovs-ofctl -O OpenFlow13 queue-stats s1

jamflows:
	sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep "nw_dst=10.0.0.3," | grep 9999

results:
	cat results/task6_summary.txt
	@echo "-------- controller events --------"
	cat results/task6_controller_events.txt
	@echo "-------- link stats (first lines) --------"
	head results/task6_link_stats.csv

shutdown:
	sudo mn -c

# ---------- Tab 2 ----------
controller:
	source $(VENV) && ryu-manager controller.py --verbose

# ---------- Tab 3 ----------
topology:
	sudo python3 emergency_topology_m2.py

jamtest:
	sudo python3 run_jam_test.py

.PHONY: help clean log events flows flows9999 queues jamflows results shutdown controller topology jamtest
