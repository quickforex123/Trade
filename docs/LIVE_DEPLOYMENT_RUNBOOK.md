# LIVE Deployment Runbook

How the Groww API key and secret are handled, and the only sanctioned path to
live trading. Read together with `GO_LIVE_READINESS_REPORT.md` (the gate) and
`docs/RISK_POLICY.md` (the limits).

## 0. Credential rules (absolute)

- The API key and approval secret exist ONLY in the execution daemon's process
  environment on the trading host. Never in this repository, never in shell
  history, never in chat with any AI assistant, never in research/backtest/
  dashboard processes, never in logs (the logging layer scrubs, but do not rely
  on it).
- The daemon host must be the machine whose **static IP is registered with
  Groww** (SEBI retail-algo framework requirement).
- Rotate the secret immediately if it is ever exposed anywhere outside the
  daemon environment.

## 1. What to do with the credentials TODAY (read-only phase)

The credentials' first job is market data, not orders.

1. On your machine (not in the repo), generate an access token:

   ```bash
   # one-off, interactive, on the trading host only
   python - <<'EOF'
   import getpass, hashlib, time, requests, uuid
   api_key = getpass.getpass("API key: ")
   secret  = getpass.getpass("API secret: ")
   ts = int(time.time())
   checksum = hashlib.sha256(f"{secret}{ts}".encode()).hexdigest()
   r = requests.post(
       "https://api.groww.in/v1/token/api/access",
       json={"key_type": "approval", "checksum": checksum, "timestamp": ts},
       headers={"Authorization": f"Bearer {api_key}",
                "x-request-id": str(uuid.uuid4()), "x-api-version": "1.0"},
       timeout=15)
   r.raise_for_status()
   print("token (expires daily — regenerate each session):", r.json()["token"])
   EOF
   ```

2. Export it for the DATA processes only: `export QFT_GROWW_READONLY_TOKEN=...`
3. Backfill history into the PIT store, run PAPER, then SHADOW. No order code
   ever sees these values.

## 2. Preconditions for LIVE (all must be green — see readiness report §2)

- [ ] Real intraday history ingested; strategies pass Stage-A gates and score in Stage-B walk-forward
- [ ] ≥ 20 PAPER trading days within expectation
- [ ] ≥ 10 SHADOW trading days: schemas verified against live responses, zero unexplained rejections, reconciliation clean
- [ ] Static IP registered with Groww; current official rate limits encoded in config
- [ ] Event blackout calendar populated for the trading year
- [ ] Chaos drills run in SHADOW (disconnect, restart with open position, expiry day)
- [ ] GO_LIVE_READINESS_REPORT.md re-issued with every blocker resolved

## 3. Injecting the production secret (LIVE daemon only)

Use a root-owned environment file consumed exclusively by the daemon's systemd
unit (or your secret manager's equivalent):

```bash
sudo install -m 600 -o root -g root /dev/null /etc/qft/daemon.env
sudo editor /etc/qft/daemon.env
```

```ini
# /etc/qft/daemon.env — readable by root only
QFT_ENVIRONMENT=LIVE
GROWW_API_KEY=<key>
GROWW_API_SECRET=<approval secret>   # or GROWW_API_TOTP for the TOTP flow
```

```ini
# /etc/systemd/system/qft-daemon.service
[Unit]
Description=QFT execution daemon (LIVE)
After=network-online.target

[Service]
EnvironmentFile=/etc/qft/daemon.env
User=qft
WorkingDirectory=/opt/qft
ExecStart=/opt/qft/.venv/bin/python -m qft.execution.main   # daemon entrypoint
Restart=on-failure
# NOTE: a restart DISARMS live by design; the daemon comes back up refusing
# orders until an operator re-arms. This is intentional. Do not "fix" it.

[Install]
WantedBy=multi-user.target
```

The dashboard, research, and any Claude-assisted process run as a different
user with NO access to `/etc/qft/daemon.env`.

## 4. Arming procedure (every live session)

1. `qft status` — confirm: kill switch NONE, ledger reconciled, feed fresh.
2. Reconciliation runs automatically at daemon start; any broker/ledger
   mismatch blocks arming-effective trading (firewall rejects with
   RECONCILIATION_MISMATCH).
3. `qft arm-live --hours 2` and type the exact phrase
   `ARM LIVE TRADING I ACCEPT THE RISK`.
4. Arming expires after the window, and ANY restart disarms. Re-arming is a
   deliberate human act each time.

## 5. First live days protocol

- Max 1 lot, one strategy, the tightest config in `config/risk.yaml`.
- Operator watches the dashboard for the full session for at least the first
  3 live days; verify each order's ledger trail (intent → decision → order →
  ack → fill) matches the broker app.
- Drill the kill switch on day 1 with a live position: trip SOFT, verify
  entries blocked and exit allowed; practice FLATTEN_ALL once.
- Any anomaly (unexplained rejection, slippage far beyond model, mismatch)
  ⇒ HARD kill, flatten, investigate before the next session.

## 6. Emergencies

- `HARD_KILL`: dashboard button-equivalent is `KillSwitchManager.trip(HARD)`
  via `qft` CLI on the host; positions can still be exited.
- Broker app remains the ultimate manual override: closing positions in the
  Groww app is always legitimate — the reconciler will detect the divergence
  and halt the system rather than fight you.
- Lost confidence in state ⇒ stop daemon, flatten manually in the app,
  reconcile, investigate with the ledger before any restart.
