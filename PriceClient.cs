using System.Net.Security;
using System.Net.Sockets;
using System.Text;

namespace CTraderFIX;

/// <summary>
/// Connects to cTrader FIX Price feed (port 5211).
/// Subscribes to market data and fires OnTick with each new price.
/// </summary>
public class PriceClient : IDisposable
{
    private const char SOH = '\u0001';

    private readonly string _host;
    private readonly int    _port;
    private readonly string _senderCompId;
    private readonly string _targetCompId;
    private readonly string _senderSubId;
    private readonly string _password;
    private readonly string _account;

    private TcpClient? _tcp;
    private SslStream? _ssl;
    private Thread?    _readThread;
    private bool       _running;
    private int        _seqNum = 1;
    private readonly object _sendLock = new();

    public bool IsLoggedOn { get; private set; }

    // ── Events ────────────────────────────────────────────────────────────────
    public event Action<Tick>?                               OnTick;
    public event Action<string>?                             OnError;
    public event Action?                                     OnLogon;
    public event Action<IReadOnlyDictionary<string,string>>? OnSecurityListReceived;

    public PriceClient(string host, int port,
                       string senderCompId, string targetCompId,
                       string senderSubId, string password, string account)
    {
        _host = host; _port = port;
        _senderCompId = senderCompId; _targetCompId = targetCompId;
        _senderSubId  = senderSubId;  _password = password; _account = account;
    }

    public async Task<bool> ConnectAsync(int timeoutSeconds = 15)
    {
        try
        {
            _tcp = new TcpClient();
            await _tcp.ConnectAsync(_host, _port);
            _ssl = new SslStream(_tcp.GetStream(), false, (_, _, _, _) => true);
            await _ssl.AuthenticateAsClientAsync(_host);

            _running = true;
            _readThread = new Thread(ReadLoop) { IsBackground = true };
            _readThread.Start();

            SendLogon();

            var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
            while (!IsLoggedOn && DateTime.UtcNow < deadline)
                await Task.Delay(200);

            return IsLoggedOn;
        }
        catch (Exception ex) { OnError?.Invoke($"Price connect failed: {ex.Message}"); return false; }
    }

    // ── Subscribe to symbol market data ──────────────────────────────────────
    private readonly Dictionary<string, string> _symbolIds = new();
    public IReadOnlyDictionary<string, string> SymbolIds => _symbolIds;

    /// <summary>Request full security list — cTrader returns numeric IDs for all symbols</summary>
    public void RequestSecurityList()
    {
        // SecurityListRequest (x): ReqType 0 = all securities
        var reqId = $"SLR{DateTime.UtcNow:HHmmssff}";
        var body  = $"320={reqId}{SOH}559=0{SOH}";
        Send("x", body);
        Console.WriteLine("[PRICE] Requesting security list...");
    }

    /// <summary>Subscribe using numeric symbol ID (required by cTrader FIX)</summary>
    public void Subscribe(string symbolId, string symbolName)
    {
        var reqId = $"MDR{DateTime.UtcNow:HHmmssff}";
        var body  = $"262={reqId}{SOH}" +
                    $"263=1{SOH}" +   // Snapshot+Updates
                    $"264=1{SOH}" +   // Top of book
                    $"265=1{SOH}" +   // Incremental
                    $"267=2{SOH}" +   // NoMDEntryTypes=2
                    $"269=0{SOH}" +   // Bid
                    $"269=1{SOH}" +   // Ask
                    $"146=1{SOH}" +   // NoRelatedSym=1
                    $"55={symbolId}{SOH}"; // numeric ID
        Send("V", body);
        Console.WriteLine($"[PRICE] Subscribed to {symbolName} (ID={symbolId})");
    }

    /// <summary>Subscribe by name — looks up ID from security list</summary>
    public bool SubscribeByName(string symbolName)
    {
        // Try exact match, then partial
        foreach (var kv in _symbolIds)
        {
            if (kv.Value.Equals(symbolName, StringComparison.OrdinalIgnoreCase) ||
                kv.Value.StartsWith(symbolName, StringComparison.OrdinalIgnoreCase))
            {
                Subscribe(kv.Key, kv.Value);
                return true;
            }
        }
        Console.ForegroundColor = ConsoleColor.Yellow;
        Console.WriteLine($"[PRICE] Symbol '{symbolName}' not found in security list.");
        Console.WriteLine($"[PRICE] Available symbols containing 'US': " +
            string.Join(", ", _symbolIds.Values.Where(v => v.Contains("US", StringComparison.OrdinalIgnoreCase)).Take(10)));
        Console.ResetColor();
        return false;
    }

    // ── FIX framing (same pattern as FixClient) ───────────────────────────────
    private string BuildMessage(string msgType, string body)
    {
        var now = DateTime.UtcNow.ToString("yyyyMMdd-HH:mm:ss.fff");
        var sb  = new StringBuilder();
        sb.Append($"35={msgType}{SOH}");
        sb.Append($"49={_senderCompId}{SOH}");
        if (!string.IsNullOrEmpty(_senderSubId))
            sb.Append($"50={_senderSubId}{SOH}");
        sb.Append($"56={_targetCompId}{SOH}");
        sb.Append($"57=QUOTE{SOH}");         // Price session sub-ID is QUOTE
        sb.Append($"34={_seqNum++}{SOH}");
        sb.Append($"52={now}{SOH}");
        sb.Append(body);

        var bodyStr    = sb.ToString();
        var bodyLength = Encoding.ASCII.GetByteCount(bodyStr);
        var preCheck   = $"8=FIX.4.4{SOH}9={bodyLength}{SOH}{bodyStr}";
        var checksum   = Encoding.ASCII.GetBytes(preCheck).Aggregate(0, (a, b) => a + b) % 256;
        return $"{preCheck}10={checksum:D3}{SOH}";
    }

    private void Send(string msgType, string body)
    {
        var msg   = BuildMessage(msgType, body);
        var bytes = Encoding.ASCII.GetBytes(msg);
        lock (_sendLock) { _ssl?.Write(bytes); _ssl?.Flush(); }
    }

    private void SendLogon()
    {
        var body = $"98=0{SOH}108=30{SOH}553={_account}{SOH}554={_password}{SOH}";
        Send("A", body);
    }

    // ── Read loop ─────────────────────────────────────────────────────────────
    private void ReadLoop()
    {
        var buf      = new byte[65536];
        var leftover = "";
        var lastHb   = DateTime.UtcNow;

        while (_running)
        {
            try
            {
                if ((DateTime.UtcNow - lastHb).TotalSeconds > 25)
                { Send("0", ""); lastHb = DateTime.UtcNow; }

                _ssl!.ReadTimeout = 1000;
                int n;
                try { n = _ssl.Read(buf, 0, buf.Length); } catch (IOException) { continue; }
                if (n == 0) break;

                leftover += Encoding.ASCII.GetString(buf, 0, n);
                while (true)
                {
                    var end = leftover.IndexOf("10=", StringComparison.Ordinal);
                    if (end < 0) break;
                    var eom = leftover.IndexOf(SOH, end);
                    if (eom < 0) break;
                    var msg = leftover[..(eom + 1)];
                    leftover = leftover[(eom + 1)..];
                    ProcessMessage(msg);
                }
            }
            catch (Exception ex) when (_running) { OnError?.Invoke($"Price read: {ex.Message}"); break; }
        }
    }

    private void ProcessMessage(string raw)
    {
        var f = ParseFields(raw);
        if (!f.TryGetValue("35", out var mt)) return;

        switch (mt)
        {
            case "A":
                IsLoggedOn = true;
                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine("[PRICE] ✓ Price feed connected");
                Console.ResetColor();
                OnLogon?.Invoke();
                break;
            case "5":
                IsLoggedOn = false;
                var r = f.TryGetValue("58", out var t) ? t : "?";
                Console.WriteLine($"[PRICE] Disconnected: {r}");
                break;
            case "0":
                if (f.TryGetValue("112", out var hid)) Send("0", $"112={hid}{SOH}");
                break;
            case "W": // MarketDataSnapshotFullRefresh
            case "X": // MarketDataIncrementalRefresh
                ParseMarketData(f, raw);
                break;
            case "y": // SecurityList response
                ParseSecurityList(f, raw);
                break;
            case "Y": // MarketDataRequestReject
                var reason = f.TryGetValue("58", out var rr) ? rr : "unknown";
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[PRICE] Subscription rejected: {reason}");
                Console.ResetColor();
                break;
        }
    }

    private void ParseSecurityList(Dictionary<string, string> f, string raw)
    {
        // Walk repeating group: 146=NoRelatedSym, then 55=symbolId 48=securityId 107=SecurityDesc
        var parts = raw.Split(SOH, StringSplitOptions.RemoveEmptyEntries);
        string curId = "", curName = "";
        int count = 0;
        foreach (var p in parts)
        {
            var eq = p.IndexOf('='); if (eq < 0) continue;
            var tag = p[..eq]; var val = p[(eq+1)..];
            // Save completed entry when we see a new tag 55 (start of next symbol)
            if (tag == "55" && curId != "" && curName != "")
            {
                _symbolIds[curId] = curName;
                curId = val; curName = ""; count++;
            }
            else if (tag == "55")  curId   = val;
            else if (tag == "48" && string.IsNullOrEmpty(curId)) curId = val;
            else if (tag == "107") curName = val;
        }
        // Save last entry
        if (curId != "" && curName != "")
            _symbolIds[curId] = curName;

        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine($"[PRICE] Security list received: {_symbolIds.Count} symbols");
        Console.ResetColor();
        OnSecurityListReceived?.Invoke(_symbolIds);
    }

    private void ParseMarketData(Dictionary<string, string> f, string raw)
    {
        var symbol = f.TryGetValue("55", out var s) ? s : "?";
        double bid = 0, ask = 0;

        // Walk repeating group: 268=NoMDEntries, then 269=type 270=price pairs
        var parts  = raw.Split(SOH, StringSplitOptions.RemoveEmptyEntries);
        string curType = "";
        foreach (var p in parts)
        {
            var eq = p.IndexOf('=');
            if (eq < 0) continue;
            var tag = p[..eq]; var val = p[(eq+1)..];
            if (tag == "269") curType = val;
            if (tag == "270")
            {
                if (double.TryParse(val, System.Globalization.NumberStyles.Any,
                    System.Globalization.CultureInfo.InvariantCulture, out double px))
                {
                    if (curType == "0") bid = px;
                    if (curType == "1") ask = px;
                }
            }
        }

        if (bid > 0 || ask > 0)
        {
            var tick = new Tick(symbol, bid, ask, DateTime.UtcNow);
            OnTick?.Invoke(tick);
        }
    }

    private static Dictionary<string, string> ParseFields(string raw)
    {
        var d = new Dictionary<string, string>();
        foreach (var p in raw.Split(SOH, StringSplitOptions.RemoveEmptyEntries))
        { var e = p.IndexOf('='); if (e > 0) d[p[..e]] = p[(e+1)..]; }
        return d;
    }

    public void Dispose() { _running = false; IsLoggedOn = false; _ssl?.Dispose(); _tcp?.Dispose(); }
}

public record Tick(string Symbol, double Bid, double Ask, DateTime Time)
{
    public double Mid => (Bid + Ask) / 2;
}
