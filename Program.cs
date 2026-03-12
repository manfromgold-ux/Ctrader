using CTraderFIX;

// ─────────────────────────────────────────────────────────────────────────────
// ★ Set your password here ★
// ─────────────────────────────────────────────────────────────────────────────
const string PASSWORD    = "Drugs@1988";

const string HOST        = "demo-uk-eqx-01.p.c-trader.com";
const string SENDER_COMP = "demo.acgmarkets.5084930";
const string TARGET_COMP = "cServer";
const string ACCOUNT     = "5084930";
const string SYMBOL      = "US30";
const double LOT_SIZE    = 0.45;

// ── Bot parameters (from your .cbotset) ──────────────────────────────────────
const int    EMA_FAST         = 13;
const int    EMA_MEDIUM       = 69;
const int    EMA_SLOW         = 50;
const int    RSI_PERIOD       = 16;
const double RSI_OVERBOUGHT   = 61.0;
const double RSI_OVERSOLD     = 26.0;
const int    ATR_PERIOD       = 16;
const double MIN_ATR          = 4.4;
const int    MACD_FAST        = 26;
const int    MACD_SLOW        = 82;
const int    MACD_SIGNAL      = 6;
const int    BOLLINGER_PERIOD = 12;
const double BOLLINGER_STDDEV = 2.1;
const int    SIGNAL_STRENGTH  = 2;
const int    WARMUP_BARS      = 90;  // bars needed before trading

Console.WriteLine("╔═══════════════════════════════════════════════╗");
Console.WriteLine("║   XAUUSDScalpingBot — ACG Markets Demo        ║");
Console.WriteLine($"║   Account : {ACCOUNT,-34}║");
Console.WriteLine($"║   Symbol  : {SYMBOL,-34}║");
Console.WriteLine($"║   Timeframe: M1 (auto from live ticks)       ║");
Console.WriteLine("╚═══════════════════════════════════════════════╝");

if (PASSWORD == "YOUR_PASSWORD_HERE")
{
    Console.ForegroundColor = ConsoleColor.Red;
    Console.WriteLine("[ERROR] Set your password in Program.cs line 7.");
    Console.ResetColor();
    return;
}

// ── Strategy ──────────────────────────────────────────────────────────────────
var bot = new ScalpingStrategy(
    EMA_FAST, EMA_MEDIUM, EMA_SLOW,
    RSI_PERIOD, RSI_OVERBOUGHT, RSI_OVERSOLD,
    ATR_PERIOD, MIN_ATR,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    BOLLINGER_PERIOD, BOLLINGER_STDDEV,
    useTrendFilter: true, signalStrengthRequired: SIGNAL_STRENGTH
);

// ── Trade client declared early so lambda can capture it ─────────────────────
FixClient? tradeClient = null;

// ── Bar aggregator: 1-minute bars from live ticks ─────────────────────────────
var bars = new BarAggregator(TimeSpan.FromMinutes(1));
double lastBid = 0, lastAsk = 0;
int    barsReceived = 0;
bool   botEnabled  = false;    // false = manual mode, true = auto-trade

bars.OnNewTick += mid =>
{
    // Just update display price
};

bars.OnBarClose += bar =>
{
    barsReceived++;
    bot.AddBar(bar.Close, bar.High, bar.Low);

    Console.ForegroundColor = ConsoleColor.DarkCyan;
    Console.WriteLine($"\n[BAR #{barsReceived}] {bar.Time:HH:mm} O={bar.Open:F2} H={bar.High:F2} L={bar.Low:F2} C={bar.Close:F2}");
    Console.ResetColor();

    if (barsReceived < WARMUP_BARS)
    {
        Console.WriteLine($"  Warming up: {barsReceived}/{WARMUP_BARS} bars...");
        return;
    }

    if (!botEnabled) return;

    var signal = bot.GetSignal();
    Console.WriteLine($"  EMA {bot.EmaFast:F1}/{bot.EmaMid:F1}/{bot.EmaSlow:F1}  RSI={bot.Rsi:F1}  ATR={bot.Atr:F2}  MACD={bot.MacdLine:F4}  Str={bot.LastSignalStrength}");

    if (signal == "BUY")
    {
        Console.ForegroundColor = ConsoleColor.Green;
        Console.WriteLine($"  ▲ AUTO BUY → {LOT_SIZE} {SYMBOL}");
        Console.ResetColor();
        tradeClient?.SendMarketOrder(SYMBOL, '1', LOT_SIZE);
    }
    else if (signal == "SELL")
    {
        Console.ForegroundColor = ConsoleColor.Red;
        Console.WriteLine($"  ▼ AUTO SELL → {LOT_SIZE} {SYMBOL}");
        Console.ResetColor();
        tradeClient?.SendMarketOrder(SYMBOL, '2', LOT_SIZE);
    }
    else
    {
        Console.ForegroundColor = ConsoleColor.DarkGray;
        Console.WriteLine($"  — HOLD (signal={signal}, str={bot.LastSignalStrength})");
        Console.ResetColor();
    }
};

// ── Trade connection (port 5212) ──────────────────────────────────────────────
tradeClient = new(HOST, 5212, SENDER_COMP, TARGET_COMP, "TRADE", PASSWORD, ACCOUNT);
tradeClient.OnError += msg => { Console.ForegroundColor = ConsoleColor.Red; Console.WriteLine($"[TRADE ERR] {msg}"); Console.ResetColor(); };
tradeClient.OnExecutionReport += (status, detail) =>
{
    Console.ForegroundColor = status.Contains("FILL") ? ConsoleColor.Green : status.Contains("REJECT") ? ConsoleColor.Red : ConsoleColor.Yellow;
    Console.WriteLine($"  ► {status}: {detail}");
    Console.ResetColor();
};

// ── Price connection (port 5211) ──────────────────────────────────────────────
var priceClient = new PriceClient(HOST, 5211, SENDER_COMP, TARGET_COMP, "QUOTE", PASSWORD, ACCOUNT);
priceClient.OnError += msg => { Console.ForegroundColor = ConsoleColor.Red; Console.WriteLine($"[PRICE ERR] {msg}"); Console.ResetColor(); };
priceClient.OnTick  += tick =>
{
    lastBid = tick.Bid;
    lastAsk = tick.Ask;
    bars.AddTick(tick);
};
priceClient.OnLogon += () => priceClient.RequestSecurityList();
priceClient.OnSecurityListReceived += ids =>
{
    Console.WriteLine($"[INFO] Looking up '{SYMBOL}' in {ids.Count} symbols...");
    if (!priceClient.SubscribeByName(SYMBOL))
    {
        // Print all symbols so user can pick the right name
        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine("[INFO] All available symbols:");
        foreach (var kv in ids.OrderBy(x => x.Value))
            Console.WriteLine($"  ID={kv.Key,-10} Name={kv.Value}");
        Console.ResetColor();
    }
};

// ── Connect both sessions ─────────────────────────────────────────────────────
Console.WriteLine("[CONN] Connecting trade session (port 5212)...");
bool tradeOk = await tradeClient.ConnectAsync(15);
if (!tradeOk) { Console.WriteLine("[FAIL] Trade session failed."); return; }

Console.WriteLine("[CONN] Connecting price session (port 5211)...");
bool priceOk = await priceClient.ConnectAsync(15);
if (!priceOk) { Console.WriteLine("[WARN] Price feed failed — manual mode only."); }

// ── Menu ──────────────────────────────────────────────────────────────────────
Console.WriteLine();
Console.WriteLine("Both sessions connected! Bot is in MANUAL mode until warmed up.");
Console.WriteLine();

while (true)
{
    Console.WriteLine();
    Console.WriteLine("──────────────────────────────────────────────────────────");
    Console.Write($" Bid={lastBid:F2}  Ask={lastAsk:F2}  Bars={barsReceived}/{WARMUP_BARS}");
    Console.WriteLine($"  AutoTrade={botEnabled}");
    Console.WriteLine("──────────────────────────────────────────────────────────");
    Console.WriteLine("  A  Toggle AUTO-TRADE on/off");
    Console.WriteLine("  S  Show strategy indicators now");
    Console.WriteLine("  1  Manual market BUY");
    Console.WriteLine("  2  Manual market SELL");
    Console.WriteLine("  3  Manual limit BUY  (enter price)");
    Console.WriteLine("  4  Manual limit SELL (enter price)");
    Console.WriteLine("  Q  Quit");
    Console.Write("\nChoice: ");

    var input = Console.ReadLine()?.Trim().ToUpper();
    switch (input)
    {
        case "A":
            if (barsReceived < WARMUP_BARS)
            {
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[WARN] Still warming up ({barsReceived}/{WARMUP_BARS} bars). Wait for more data.");
                Console.ResetColor();
            }
            else
            {
                botEnabled = !botEnabled;
                Console.ForegroundColor = botEnabled ? ConsoleColor.Green : ConsoleColor.Yellow;
                Console.WriteLine($"[BOT] Auto-trade is now {(botEnabled ? "ON ✓" : "OFF")}");
                Console.ResetColor();
            }
            break;

        case "S":
            if (barsReceived < 5)
            { Console.WriteLine("[BOT] Not enough bars yet."); break; }
            Console.WriteLine($"  EMA  fast={bot.EmaFast:F2}  mid={bot.EmaMid:F2}  slow={bot.EmaSlow:F2}");
            Console.WriteLine($"  RSI  {bot.Rsi:F1}  (OS<{RSI_OVERSOLD} OB>{RSI_OVERBOUGHT})");
            Console.WriteLine($"  ATR  {bot.Atr:F2}  (min={MIN_ATR})");
            Console.WriteLine($"  MACD line={bot.MacdLine:F4}  signal={bot.MacdSig:F4}");
            Console.WriteLine($"  BB   upper={bot.BbUpper:F2}  lower={bot.BbLower:F2}");
            Console.WriteLine($"  Signal: {bot.GetSignal()}  (strength={bot.LastSignalStrength}/{SIGNAL_STRENGTH})");
            break;

        case "1": tradeClient.SendMarketOrder(SYMBOL, '1', LOT_SIZE); break;
        case "2": tradeClient.SendMarketOrder(SYMBOL, '2', LOT_SIZE); break;
        case "3":
            Console.Write("Limit price: ");
            if (double.TryParse(Console.ReadLine(), out double bp))
                tradeClient.SendLimitOrder(SYMBOL, '1', LOT_SIZE, bp);
            break;
        case "4":
            Console.Write("Limit price: ");
            if (double.TryParse(Console.ReadLine(), out double sp))
                tradeClient.SendLimitOrder(SYMBOL, '2', LOT_SIZE, sp);
            break;
        case "Q":
            Console.WriteLine("[INFO] Shutting down...");
            tradeClient.Disconnect();
            priceClient.Dispose();
            return;
        default:
            Console.WriteLine("[WARN] Enter A, S, 1-4, or Q.");
            break;
    }
    await Task.Delay(300);
}
