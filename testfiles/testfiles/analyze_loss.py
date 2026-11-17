#!/usr/bin/env python3
"""Summarise the loss experiment into results/loss_results.csv.

Reads results/server_log_loss<N>.csv (header: timestamp,event,seq,subscriber_ip,details)
and results/rx_loss<N>_<ip>.csv (no header: recv_time,ip,severity,seq,sent_time).

Usage: python3 analyze_loss.py [results_dir]
"""
import csv
import glob
import os
import re
import sys

SEVERITIES = ["WARNING", "CRITICAL"]
COLUMNS = ["loss_pct", "severity", "alerts_sent", "expected_deliveries",
           "delivered", "delivery_ratio", "resends", "failed"]


def read_server_log(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def read_rx_logs(results_dir, loss):
    """Return list of (ip, severity, seq) for every received ALERT copy."""
    rows = []
    for path in sorted(glob.glob(os.path.join(results_dir, f"rx_loss{loss}_*.csv"))):
        with open(path, newline="") as f:
            for r in csv.reader(f):
                if len(r) >= 4:
                    rows.append((r[1], r[2], r[3]))
    return rows


def analyze_level(results_dir, loss):
    server = read_server_log(os.path.join(results_dir, f"server_log_loss{loss}.csv"))
    rx = read_rx_logs(results_dir, loss)

    subscribers = {r["subscriber_ip"] for r in server if r["event"] == "REGISTERED"}
    if not subscribers:
        subscribers = {ip for ip, _, _ in rx}
    n_subs = len(subscribers)

    out = []
    for sev in SEVERITIES:
        sent_seqs = {r["seq"] for r in server
                     if r["event"] == "ALERT_SENT" and r["details"].strip() == sev}
        alerts = len(sent_seqs)
        expected = alerts * n_subs

        if sev == "CRITICAL":
            delivered = len({(r["seq"], r["subscriber_ip"]) for r in server
                             if r["event"] == "ACK_RECEIVED" and r["seq"] in sent_seqs})
            resends = sum(1 for r in server
                          if r["event"] == "RESEND" and r["seq"] in sent_seqs)
            failed = sum(1 for r in server
                         if r["event"] == "FAILED" and r["seq"] in sent_seqs)
        else:
            delivered = sum(1 for _, s, _ in rx if s == sev)
            resends = failed = 0

        ratio = delivered / expected if expected else 0.0
        out.append({"loss_pct": loss, "severity": sev, "alerts_sent": alerts,
                    "expected_deliveries": expected, "delivered": delivered,
                    "delivery_ratio": f"{ratio:.4f}", "resends": resends,
                    "failed": failed})
    return out


def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "results"
    levels = sorted(int(m.group(1))
                    for p in glob.glob(os.path.join(results_dir, "server_log_loss*.csv"))
                    if (m := re.search(r"server_log_loss(\d+)\.csv$", p)))
    if not levels:
        sys.exit(f"No server_log_loss*.csv files found in {results_dir}/")

    rows = []
    for loss in levels:
        rows.extend(analyze_level(results_dir, loss))

    out_path = os.path.join(results_dir, "loss_results.csv")
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    print(",".join(COLUMNS))
    for r in rows:
        print(",".join(str(r[c]) for c in COLUMNS))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
