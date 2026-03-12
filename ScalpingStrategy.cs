namespace CTraderFIX;

/// <summary>
/// Mirrors the XAUUSDScalpingBot logic from your .algo file.
/// Indicators: EMA triple, RSI, ATR, MACD, Bollinger Bands.
/// </summary>
public class ScalpingStrategy
{
    private readonly int    _emaFastP, _emaMidP, _emaSlowP;
    private readonly int    _rsiP;
    private readonly double _rsiOB, _rsiOS;
    private readonly int    _atrP;
    private readonly double _minAtr;
    private readonly int    _macdFast, _macdSlow, _macdSig;
    private readonly int    _bbP;
    private readonly double _bbStd;
    private readonly bool   _useTrend;
    private readonly int    _sigRequired;

    private readonly List<double> _closes = new();
    private readonly List<double> _highs  = new();
    private readonly List<double> _lows   = new();

    public int    Bars   => _closes.Count;
    public double EmaFast { get; private set; }
    public double EmaMid  { get; private set; }
    public double EmaSlow { get; private set; }
    public double Rsi     { get; private set; }
    public double Atr     { get; private set; }
    public double MacdLine { get; private set; }
    public double MacdSig  { get; private set; }
    public double BbUpper  { get; private set; }
    public double BbLower  { get; private set; }
    public int    LastSignalStrength { get; private set; }

    public ScalpingStrategy(int emaFast, int emaMedium, int emaSlow,
        int rsiPeriod, double rsiOverbought, double rsiOversold,
        int atrPeriod, double minAtr,
        int macdFast, int macdSlow, int macdSignal,
        int bbPeriod, double bbStdDev,
        bool useTrendFilter, int signalStrengthRequired)
    {
        _emaFastP = emaFast; _emaMidP = emaMedium; _emaSlowP = emaSlow;
        _rsiP = rsiPeriod; _rsiOB = rsiOverbought; _rsiOS = rsiOversold;
        _atrP = atrPeriod; _minAtr = minAtr;
        _macdFast = macdFast; _macdSlow = macdSlow; _macdSig = macdSignal;
        _bbP = bbPeriod; _bbStd = bbStdDev;
        _useTrend = useTrendFilter; _sigRequired = signalStrengthRequired;
    }

    public void AddBar(double close, double high, double low)
    {
        _closes.Add(close);
        _highs.Add(high);
        _lows.Add(low);

        if (_closes.Count < 3) return;

        EmaFast = Ema(_closes, _emaFastP);
        EmaMid  = Ema(_closes, _emaMidP);
        EmaSlow = Ema(_closes, _emaSlowP);
        Rsi     = CalcRsi(_closes, _rsiP);
        Atr     = CalcAtr(_highs, _lows, _closes, _atrP);
        (MacdLine, MacdSig) = CalcMacd(_closes, _macdFast, _macdSlow, _macdSig);
        (BbUpper, BbLower)  = CalcBollinger(_closes, _bbP, _bbStd);
    }

    /// <summary>Returns "BUY", "SELL", or "HOLD"</summary>
    public string GetSignal()
    {
        if (_closes.Count < 5) return "HOLD";

        double close = _closes[^1];
        int buyScore = 0, sellScore = 0;

        // ── Trend filter (EMA alignment) ──────────────────────────────────
        bool bullTrend = EmaFast > EmaMid && EmaMid > EmaSlow;
        bool bearTrend = EmaFast < EmaMid && EmaMid < EmaSlow;
        if (_useTrend && !bullTrend && !bearTrend) { LastSignalStrength = 0; return "HOLD"; }

        // ── Volatility filter ─────────────────────────────────────────────
        if (Atr < _minAtr) { LastSignalStrength = 0; return "HOLD"; }

        // ── Signal scoring ────────────────────────────────────────────────

        // EMA crossover
        if (EmaFast > EmaSlow) buyScore++;
        else sellScore++;

        // RSI
        if (Rsi < _rsiOS) buyScore++;
        else if (Rsi > _rsiOB) sellScore++;

        // MACD
        if (MacdLine > MacdSig) buyScore++;
        else if (MacdLine < MacdSig) sellScore++;

        // Bollinger
        if (close <= BbLower) buyScore++;
        else if (close >= BbUpper) sellScore++;

        LastSignalStrength = Math.Max(buyScore, sellScore);

        if (buyScore  >= _sigRequired && (!_useTrend || bullTrend)) return "BUY";
        if (sellScore >= _sigRequired && (!_useTrend || bearTrend)) return "SELL";
        return "HOLD";
    }

    // ── Indicator calculations ────────────────────────────────────────────────

    private static double Ema(List<double> data, int period)
    {
        if (data.Count < period) return data[^1];
        double k   = 2.0 / (period + 1);
        double ema = data.Take(period).Average();
        foreach (var v in data.Skip(period))
            ema = v * k + ema * (1 - k);
        return ema;
    }

    private static double CalcRsi(List<double> data, int period)
    {
        if (data.Count <= period) return 50;
        double avgGain = 0, avgLoss = 0;
        for (int i = data.Count - period; i < data.Count; i++)
        {
            double d = data[i] - data[i - 1];
            if (d > 0) avgGain += d; else avgLoss -= d;
        }
        avgGain /= period; avgLoss /= period;
        if (avgLoss == 0) return 100;
        double rs = avgGain / avgLoss;
        return 100 - 100 / (1 + rs);
    }

    private static double CalcAtr(List<double> highs, List<double> lows, List<double> closes, int period)
    {
        if (closes.Count <= 1) return 0;
        var trs = new List<double>();
        for (int i = Math.Max(1, closes.Count - period); i < closes.Count; i++)
        {
            double tr = Math.Max(highs[i] - lows[i],
                        Math.Max(Math.Abs(highs[i] - closes[i - 1]),
                                 Math.Abs(lows[i]  - closes[i - 1])));
            trs.Add(tr);
        }
        return trs.Average();
    }

    private static (double macd, double signal) CalcMacd(List<double> data, int fast, int slow, int sig)
    {
        if (data.Count < slow + sig) return (0, 0);
        double emaFast = Ema(data, fast);
        double emaSlow = Ema(data, slow);
        double macd    = emaFast - emaSlow;

        // Build recent MACD values to compute signal EMA
        var macdHistory = new List<double>();
        int start = Math.Max(slow, data.Count - slow - sig - 10);
        for (int i = start; i < data.Count; i++)
        {
            var slice = data.Take(i + 1).ToList();
            if (slice.Count >= slow)
                macdHistory.Add(Ema(slice, fast) - Ema(slice, slow));
        }
        double sigLine = macdHistory.Count >= sig ? Ema(macdHistory, sig) : macd;
        return (macd, sigLine);
    }

    private static (double upper, double lower) CalcBollinger(List<double> data, int period, double stdMult)
    {
        if (data.Count < period) return (data[^1], data[^1]);
        var slice = data.TakeLast(period).ToList();
        double mean = slice.Average();
        double std  = Math.Sqrt(slice.Sum(x => (x - mean) * (x - mean)) / period);
        return (mean + stdMult * std, mean - stdMult * std);
    }
}
