# ProScalp AI Trader

ProScalp AI Trader is a deployable MVP for an aggressive crypto scalping system with professional safety gates. It ships with a FastAPI backend, React dashboard, PostgreSQL/SQLite support, exchange adapters for Binance and Bybit, paper trading, risk controls, strategy modules, Telegram alerts, backtesting, Docker, Nginx, and Oracle Cloud deployment scripts.

The default mode is `paper`. Live trading is blocked unless `LIVE_TRADING_ENABLED=true` is set, and futures trading also requires `FUTURES_TRADING_CONFIRMED=true`.

## Quick Start

Docker Compose requires Docker Desktop on Windows or Docker Engine on Linux. If PowerShell says `docker` is not recognized, install Docker Desktop, start it, then open a new terminal before running the command below.

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
```

Open:

- Dashboard: `http://localhost`
- Backend health: `http://localhost/health`
- API docs: `http://localhost/docs`

For direct backend development:

PowerShell:

```powershell
$env:DATABASE_URL="sqlite+aiosqlite:///./data/proscalp.db"
cd backend
& "C:\Users\PC\AppData\Local\Python\bin\python.exe" -m pip install -r requirements.txt
& "C:\Users\PC\AppData\Local\Python\bin\python.exe" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Bash:

```bash
export DATABASE_URL="sqlite+aiosqlite:///./data/proscalp.db"
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

For direct frontend development:

PowerShell:

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

Bash:

```bash
cd frontend
npm install
npm run dev
```

## Safety Model

- Paper trading is the default launch mode.
- Live spot/futures orders require explicit environment flags.
- API keys are loaded only from environment variables.
- No martingale, no revenge trading, no forced trades.
- Daily hard loss shutdown defaults to `-4%`.
- Position sizing is based on account equity, stop distance, risk grade, leverage cap, fees, slippage, and minimum order size.
- A+ / A / B thresholds default to `90 / 80 / 70`.

## External Connections

Do not paste real API keys into chat. Put them directly into `.env` on the machine running the bot.

Check current readiness on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate_env.ps1
```

### Paper Mode

Paper mode can run with public exchange data and no private exchange keys:

```env
TRADING_MODE=paper
LIVE_TRADING_ENABLED=false
FUTURES_TRADING_CONFIRMED=false
```

For the selected exchange, public market data is enough for scanner/backtesting-style workflows. Private balance/order endpoints need keys only in `testnet` or live modes.

### Binance

Use this when `EXCHANGE=binance`.

```env
EXCHANGE=binance
BINANCE_API_KEY=your_binance_key_here
BINANCE_API_SECRET=your_binance_secret_here
BINANCE_FUTURES_TESTNET_BASE_URL=https://demo-fapi.binance.com
```

Recommended first connection mode:

```env
TRADING_MODE=testnet
MARKET_TYPE=futures
LIVE_TRADING_ENABLED=false
FUTURES_TRADING_CONFIRMED=false
```

For live spot after testing:

```env
TRADING_MODE=live_spot
MARKET_TYPE=spot
LIVE_TRADING_ENABLED=true
FUTURES_TRADING_CONFIRMED=false
```

For live futures after testing:

```env
TRADING_MODE=live_futures
MARKET_TYPE=futures
LIVE_TRADING_ENABLED=true
FUTURES_TRADING_CONFIRMED=true
MAX_LEVERAGE=5
```

Keep withdrawals disabled on API keys.

### Bybit

Use this when `EXCHANGE=bybit`.

```env
EXCHANGE=bybit
BYBIT_API_KEY=your_bybit_key_here
BYBIT_API_SECRET=your_bybit_secret_here
```

Use `TRADING_MODE=testnet` before any live mode.

### Telegram

Create a bot with BotFather, message the bot once, then set:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

Then rebuild/restart the backend:

```powershell
docker compose up -d --build backend
```

Test from the dashboard under Telegram Alerts, or call:

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost/api/telegram/test
```

Test exchange connectivity:

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost/api/exchange/test
Invoke-WebRequest -UseBasicParsing http://localhost/api/exchange/private-test
```

## Backend Modules

The backend lives in `backend/app` and includes:

- `config`: settings and structured logging
- `database`: SQLAlchemy models and async DB session
- `exchanges`: shared adapter interface plus Binance and Bybit adapters
- `universe`: daily top-50 scanner
- `data`: multi-timeframe market data bundles
- `indicators`: EMA, RSI, MACD, VWAP, ATR, Bollinger Bands, volume, support/resistance, wick/body, volatility, relative volume, trend score
- `regime`: dangerous/bad/unclear/good/strong/hot detector
- `sessions`: Asia, London, and New York session awareness
- `strategies`: all requested scalping setups
- `scoring`: 0-100 setup scoring engine
- `risk`: kill switches, daily drawdown behavior, and position sizing
- `execution`: order and position management
- `paper_trading`: in-memory paper fill simulator
- `backtesting`: OHLCV strategy tester
- `alerts`: Telegram alerts
- `api`: dashboard, scanner, trades, signals, settings, strategies, risk, exchange, alerts, bot controls

## Oracle Cloud Deployment

Assume an Ubuntu Oracle Cloud VM.

1. Open ingress for TCP `22` and `80` in the Oracle Cloud security list or network security group.
2. SSH into the VM.
3. Copy this project to `/opt/proscalp-ai-trader`.
4. Run:

```bash
cd /opt/proscalp-ai-trader
bash scripts/setup.sh
nano .env
bash scripts/deploy_oracle.sh
```

Optional systemd install:

```bash
sudo cp systemd/proscalp.service /etc/systemd/system/proscalp.service
sudo systemctl daemon-reload
sudo systemctl enable proscalp
sudo systemctl start proscalp
```

For Windows-assisted deployment, see [docs/ORACLE_DEPLOYMENT.md](docs/ORACLE_DEPLOYMENT.md).

## Database

Production uses PostgreSQL from Docker Compose:

```env
DATABASE_URL=postgresql+asyncpg://proscalp:change_me@postgres:5432/proscalp
```

Local SQLite is supported:

```env
DATABASE_URL=sqlite+aiosqlite:///./data/proscalp.db
```

Tables are created automatically on startup for the MVP. For a larger production rollout, add Alembic migrations before evolving schemas.

## Backups

PostgreSQL backup:

```bash
mkdir -p backups
docker compose exec -T postgres pg_dump -U proscalp proscalp > backups/proscalp_$(date +%F).sql
```

Restore:

```bash
docker compose exec -T postgres psql -U proscalp proscalp < backups/proscalp_YYYY-MM-DD.sql
```

## Live Trading Checklist

1. Run paper mode for several sessions.
2. Run testnet mode with real exchange connectivity.
3. Verify Telegram alerts.
4. Confirm max leverage, daily loss limit, and exposure caps.
5. Set API key permissions to minimum required.
6. Keep withdrawals disabled on exchange API keys.
7. Set `LIVE_TRADING_ENABLED=true`.
8. For futures, set `FUTURES_TRADING_CONFIRMED=true`.

No setting should be changed to live mode until testnet behavior, order sizing, exchange permissions, and emergency shutdown are verified.
