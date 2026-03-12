namespace CTraderFIX;

/// <summary>
/// Aggregates ticks into OHLC bars of a given timeframe.
/// Fires OnBarClose when a bar completes, and OnNewTick on every tick.
/// </summary>
public class BarAggregator
{
    private readonly TimeSpan _period;
    private DateTime _barStart = DateTime.MinValue;
    private double   _open, _high, _low, _close;
    private bool     _hasBar;

    public event Action<Bar>?  OnBarClose;
    public event Action<double>? OnNewTick; // fires on every tick with mid price

    public BarAggregator(TimeSpan period) => _period = period;

    public void AddTick(Tick tick)
    {
        var mid = tick.Mid;
        OnNewTick?.Invoke(mid);

        var barTime = Floor(tick.Time, _period);

        if (!_hasBar || barTime != _barStart)
        {
            // Close previous bar
            if (_hasBar)
                OnBarClose?.Invoke(new Bar(_barStart, _open, _high, _low, _close));

            // Open new bar
            _barStart = barTime;
            _open = _high = _low = _close = mid;
            _hasBar = true;
        }
        else
        {
            if (mid > _high) _high = mid;
            if (mid < _low)  _low  = mid;
            _close = mid;
        }
    }

    private static DateTime Floor(DateTime dt, TimeSpan ts) =>
        new DateTime((dt.Ticks / ts.Ticks) * ts.Ticks, DateTimeKind.Utc);
}

public record Bar(DateTime Time, double Open, double High, double Low, double Close);
