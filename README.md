# cTrader FIX API - .NET Trading App
### ACG Markets Demo Account | US30 / XAUUSD

---

## Quick Setup (3 steps)

### Step 1 — Prerequisites
```
dotnet --version   # Must be 6.0 or higher
```
Download .NET 6 SDK from https://dotnet.microsoft.com/download if needed.

---

### Step 2 — Set your password

Open `src/Program.cs` and replace line 9:
```csharp
const string PASSWORD = "YOUR_PASSWORD_HERE";
```
With your actual ACG Markets account password.

---

### Step 3 — Run
```bash
cd CTraderFIX
dotnet run
```

---

## Your Connection Details (from cTrader)

| Setting       | Value                                  |
|---------------|----------------------------------------|
| Host          | demo-uk-eqx-01.p.c-trader.com         |
| Trade Port    | 5212 (SSL)                             |
| Account       | 5084930                                |
| SenderCompID  | demo.acgmarkets.5084930                |
| TargetCompID  | cServer                                |
| SenderSubID   | TRADE                                  |

---

## Project Structure

```
CTraderFIX/
├── CTraderFIX.csproj       # .NET project (QuickFIXn dependency)
├── fix_trade.cfg           # FIX session config (pre-filled with your details)
├── src/
│   ├── Program.cs          # Entry point + interactive menu
│   └── CTraderFixApp.cs    # FIX app: session handling + order methods
├── store/                  # Auto-created: FIX message store
└── log/                    # Auto-created: FIX session logs (check here if errors)
```

---

## Menu Options

```
1. Market BUY  (US30, 0.45 lots)   — instant buy at market price
2. Market SELL (US30, 0.45 lots)   — instant sell at market price
3. Limit BUY   (custom price)      — buy only if price reaches your level
4. Limit SELL  (custom price)      — sell only if price reaches your level
5. Custom order                    — choose symbol, side, qty, type
Q. Quit
```

---

## Bot Parameters (from your .cbotset file)

Your saved bot was configured with these values — replicate them in orders:

| Parameter           | Value     |
|---------------------|-----------|
| Symbol              | US30.pro  |
| Timeframe           | M1        |
| Lot Size            | 0.45      |
| Take Profit (pips)  | 1100      |
| Stop Loss (pips)    | 1500      |
| EMA Fast            | 13        |
| EMA Medium          | 69        |
| EMA Slow            | 50        |
| RSI Period          | 16        |
| RSI Overbought      | 61        |
| RSI Oversold        | 26        |
| Max Daily Loss %    | 5%        |
| Max Drawdown %      | 10%       |

---

## Common Issues

**"Could not log on"**
- Double-check your password in `Program.cs`
- Make sure your firewall allows outbound TCP on port 5212
- Check the `log/` folder — the FIX session log will show the exact rejection reason

**"Symbol not found / rejected"**
- Try `US30` instead of `US30.pro` in the FIX field (brokers vary)
- Check the execution report reject reason printed in the console

**SSL certificate error**
- `SSLValidateCertificates=N` is set in the config to skip cert validation on demo

---

## Extending the App

To replicate the bot's signal logic externally, add your own indicator calculations
in `Program.cs` before calling `fixApp.SendMarketOrder(...)`.

For example, a simplified EMA crossover check:
```csharp
// Get prices from your data source, compute EMAs, then:
if (emaFast > emaSlow && rsi < 61)
    fixApp.SendMarketOrder("US30", Side.BUY, 0.45);
else if (emaFast < emaSlow && rsi > 26)
    fixApp.SendMarketOrder("US30", Side.SELL, 0.45);
```
