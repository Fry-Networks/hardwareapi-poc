# Bandwidth Miner — One BM Per External IP: Backend API Contract

## Overview

Only one Bandwidth Miner (BM) is allowed per external IP address. The installer detects the external IP and sends it to the backend during lease operations. The backend is responsible for persisting the IP and enforcing uniqueness.

---

## Endpoints

### 1. Lease Acquire — `POST /installations/{miner_key}/leases/{install_id}`

**Request body:**

```json
{
  "mode": "acquire",
  "lease_seconds": 3600,
  "external_ip": "203.0.113.42"
}
```

> `external_ip` is only present when `miner_key` starts with `BM`. Non-BM miners will not include this field.

**Backend behavior:**

1. If `external_ip` is present and miner is BM:
   - Check if another **active** BM lease already exists for that IP
   - If conflict found → deny the lease
   - If no conflict → grant the lease and **persist `external_ip` on the lease record**
2. If `external_ip` is absent (non-BM miners): behave as before, ignore IP

**Response — success:**

```json
{
  "granted": true,
  "expires_at": "2026-02-01T15:30:00Z",
  "error_code": null
}
```

**Response — IP conflict:**

```json
{
  "granted": false,
  "expires_at": null,
  "error_code": "IP_ALREADY_REGISTERED"
}
```

---

### 2. Lease Renew — `PATCH /installations/{miner_key}/leases/{install_id}`

**Request body:**

```json
{
  "mode": "renew",
  "lease_seconds": 3600,
  "external_ip": "203.0.113.42"
}
```

> `external_ip` is only present when `miner_key` starts with `BM`.

**Backend behavior:**

1. If `external_ip` is present: **update** the stored IP on the lease record
   - If the IP changed from the previously stored value, release the old IP mapping and store the new one
   - Do **not** block renewal due to IP change — just update the mapping
2. If `external_ip` is absent: behave as before

**Response (unchanged):**

```json
{
  "granted": true
}
```

---

### 3. IP Status Check — `GET /installations/BM/ip/{external_ip}/status`

**No request body.**

**Backend behavior:**

- Query: is there any BM lease with `active = true` that has this `external_ip` stored?
- If found: return the full `miner_key` of the conflicting installation
- If not found: IP is available

**Response — available:**

```json
{
  "available": true,
  "conflicting_miner_key": null
}
```

**Response — conflict:**

```json
{
  "available": false,
  "conflicting_miner_key": "REDACTED_ROTATE_ME"
}
```

---

## Database Changes

Add an `external_ip` field (string, nullable) to the lease/installation record:

| Field         | Type          | Description                                      |
|---------------|---------------|--------------------------------------------------|
| `external_ip` | `VARCHAR(45)` | IPv4 or IPv6 address. Nullable. Only set for BM. |

**Lifecycle:**

- **Set** on lease acquire (from `external_ip` in request body)
- **Updated** on lease renew (if `external_ip` differs from stored value)
- **Cleared/released** when lease expires or is deleted

---

## Flow Summary

```
Installer                          Backend
   |                                  |
   |-- GET .../BM/ip/{ip}/status ---->|  Pre-check: is IP available?
   |<-- {available: true} ------------|
   |                                  |
   |-- POST .../leases/{id} -------->|  Acquire lease with external_ip
   |   {mode: acquire,               |  Backend checks IP uniqueness,
   |    lease_seconds: 3600,          |  persists external_ip on record
   |    external_ip: "203.0.113.42"}  |
   |<-- {granted: true} -------------|
   |                                  |
   |   ... later (service renewal) ...|
   |                                  |
   |-- PATCH .../leases/{id} ------->|  Renew lease with current IP
   |   {mode: renew,                  |  Backend updates external_ip
   |    lease_seconds: 3600,          |  if it changed
   |    external_ip: "203.0.113.42"}  |
   |<-- {granted: true} -------------|
```
