# Secrets

Do **not** commit real secret values to git.

This folder exists as a *template* for what must be present on a provisioned node under:

- `/etc/overdrive/secrets/`

Typical files:
- `wifi_password`
- `vm_write_password`
- `event_api_key`
- `gx_password`

Permissions:
```bash
sudo chown -R root:root /etc/overdrive/secrets
sudo chmod 700 /etc/overdrive/secrets
sudo chmod 600 /etc/overdrive/secrets/*
```
