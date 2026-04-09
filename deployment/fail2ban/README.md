Fail2ban integration for Hardware API

Overview
--------
This folder contains a fail2ban filter and a sample jail configuration to
automatically ban IP addresses that repeatedly probe for sensitive files
(e.g. ".env"). The application emits machine-parseable markers when a 404
is returned for a likely probe and when the application itself blocks an IP.

Files
-----
- hardware_api.conf - fail2ban filter (place in /etc/fail2ban/filter.d/)
- hardware_api.local - sample jail (place in /etc/fail2ban/jail.d/)

Installation (example)
----------------------
1. Copy filter and jail to system locations (requires sudo):

```bash
sudo cp deployment/fail2ban/hardware_api.conf /etc/fail2ban/filter.d/hardware_api.conf
sudo cp deployment/fail2ban/hardware_api.local /etc/fail2ban/jail.d/hardware_api.local
```

2. Reload fail2ban:

```bash
sudo systemctl reload fail2ban
```

3. Confirm the jail is active:

```bash
sudo fail2ban-client status hardware_api
```

Tuning
------
- `maxretry` controls how many matched lines before ban. Default in sample: 5.
- `findtime` is the window in seconds for counting matches. Default: 600 (10m).
- `bantime` is how long the firewall rule remains. Default: 3600 (1h).

Notes
-----
- The application writes logs to `/opt/hardware_exe_api/logs/hardware_api_YYYYMMDD.log`.
  Ensure the system user running fail2ban can read those files (adjust permissions
  or copy logs to a central place).
- This is a best-effort short-term mitigation. For production protection, combine
  with a WAF, rate limiting, and network-level rules (cloud provider or edge).
