# Oracle Cloud Deployment

This guide deploys the current testnet build to an Ubuntu-based Oracle Cloud Infrastructure VM.

Keep the first cloud deployment in testnet:

```env
TRADING_MODE=testnet
LIVE_TRADING_ENABLED=false
FUTURES_TRADING_CONFIRMED=false
```

## OCI Checklist

1. Create an Ubuntu VM with a public IPv4 address.
2. Add/confirm ingress rules:
   - TCP `22` from your IP for SSH.
   - TCP `80` from the internet for the dashboard.
3. SSH user is usually `ubuntu` for Ubuntu images.
4. Keep your private key on your local Windows machine.

Oracle documents SSH access for Linux instances in its Compute docs, and OCI security rules/security lists control ingress such as SSH and HTTP.

If `curl http://localhost/health` works on the VM but `http://YOUR_ORACLE_PUBLIC_IP/health` does not work from your PC, the app is running and the OCI network is blocking HTTP. In Oracle Cloud Console, open:

```text
Compute > Instances > your instance > Attached VNICs > Primary VNIC > Subnet > Security Lists
```

Add an ingress rule:

```text
Source CIDR: 0.0.0.0/0
IP Protocol: TCP
Destination Port Range: 80
Description: ProScalp dashboard HTTP
```

If your subnet uses a Network Security Group instead of only security lists, add the same TCP `80` ingress rule there too.

## Windows One-Command Upload

From PowerShell in the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_oracle_from_windows.ps1 `
  -HostName YOUR_ORACLE_PUBLIC_IP `
  -User ubuntu `
  -KeyPath C:\path\to\your_private_key
```

By default this does **not** copy `.env`. That is safer. The script uploads the project, installs Docker, creates `.env` from `.env.example`, and stops so you can edit secrets on the VM.

Then SSH in:

```powershell
ssh -i C:\path\to\your_private_key ubuntu@YOUR_ORACLE_PUBLIC_IP
```

On the VM:

```bash
cd /opt/proscalp-ai-trader
nano .env
bash scripts/deploy_oracle.sh
```

If you intentionally want to copy your local `.env` to the VM:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_oracle_from_windows.ps1 `
  -HostName YOUR_ORACLE_PUBLIC_IP `
  -User ubuntu `
  -KeyPath C:\path\to\your_private_key `
  -CopyEnv
```

## Verify Cloud Deployment

From your local machine:

```powershell
Invoke-WebRequest -UseBasicParsing http://YOUR_ORACLE_PUBLIC_IP/health
Invoke-WebRequest -UseBasicParsing http://YOUR_ORACLE_PUBLIC_IP/api/exchange/private-test
Invoke-WebRequest -UseBasicParsing http://YOUR_ORACLE_PUBLIC_IP/api/telegram/test
```

On the VM:

```bash
cd /opt/proscalp-ai-trader
docker compose ps
docker compose logs -f backend
```

## Optional Systemd

```bash
sudo cp systemd/proscalp.service /etc/systemd/system/proscalp.service
sudo systemctl daemon-reload
sudo systemctl enable proscalp
sudo systemctl start proscalp
```
