# Failover control plane

This repository now ships a small external Flask control plane for
primary/secondary coordination.

## Control-plane credentials

Create three credential files on the control-plane host:

```bash
sudo install -d -m 0750 /etc/btcedu-control-plane
sudo sh -c 'umask 077 && printf "%s" "operator-secret" > /etc/btcedu-control-plane/operator.token'
sudo sh -c 'umask 077 && printf "%s" "primary-secret" > /etc/btcedu-control-plane/primary.token'
sudo sh -c 'umask 077 && printf "%s" "secondary-secret" > /etc/btcedu-control-plane/secondary.token'
```

Then create `/etc/btcedu-control-plane/node_tokens.json`:

```json
{
  "btcedu-primary": {
    "role": "primary",
    "token_file": "/etc/btcedu-control-plane/primary.token"
  },
  "btcedu-secondary": {
    "role": "secondary",
    "token_file": "/etc/btcedu-control-plane/secondary.token"
  }
}
```

## Control-plane environment

Copy `deploy/control-plane.env.example` to `/etc/btcedu-control-plane.env` and
adjust the paths if needed.

## Install the control plane

```bash
sudo cp deploy/btcedu-control-plane.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-control-plane.service
curl http://127.0.0.1:8092/ready
```

The gunicorn service must stay on `127.0.0.1:8092`. Do **not** point other
hosts at that raw HTTP socket.

## HTTPS reverse proxy for remote nodes (required)

Primary and secondary nodes must reach the control plane through an externally
reachable HTTPS hostname. For this deployment, use `https://sahimi.app/failover`.

Example Caddy block on the control-plane host:

```caddy
sahimi.app {
    @failover path /failover /failover/*
    handle_path @failover {
        reverse_proxy 127.0.0.1:8092
    }
}
```

Then reload Caddy:

```bash
sudo systemctl reload caddy
curl https://sahimi.app/failover/ready
```

If the control plane lives on a different machine, replace `sahimi.app` with
that host's public HTTPS name. The externally reachable HTTPS URL is what every
node must use in `FAILOVER_CONTROL_PLANE_URL`.

## Configure each btcedu node

Set the main node `.env`:

```bash
FAILOVER_ENABLED=true
FAILOVER_NODE_ID=btcedu-primary
FAILOVER_NODE_ROLE=primary
FAILOVER_CONTROL_PLANE_URL=https://sahimi.app/failover
FAILOVER_TOKEN_FILE=/etc/btcedu/failover.token
FAILOVER_OPERATOR_TOKEN_FILE=/etc/btcedu/operator.token   # optional, dashboard mode switch
```

The secondary node uses its own `FAILOVER_NODE_ID`, `FAILOVER_NODE_ROLE=secondary`
and its own token file. Do not configure `http://...:8092` across hosts.

## Install node heartbeats

```bash
sudo cp deploy/btcedu-node-heartbeat.service deploy/btcedu-node-heartbeat.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-node-heartbeat.timer
systemctl list-timers btcedu-node-heartbeat.timer
```

## API summary

- `GET /api/v1/status`
- `POST /api/v1/heartbeat`
- `PUT /api/v1/mode`
- `POST /api/v1/leases/acquire`
- `POST /api/v1/leases/renew`
- `POST /api/v1/leases/release`
- `POST /api/v1/broadcasts/complete`
- `GET /health`
- `GET /ready`
