using System.Net.Security;
using System.Net.Sockets;
using System.Text;

namespace CTraderFIX;

/// <summary>
/// Raw FIX 4.4 client for cTrader — zero external dependencies.
/// </summary>
public class FixClient : IDisposable
{
    private const char SOH = '\u0001';

    private readonly string _host;
    private readonly int    _port;
    private TcpClient?  _tcp;
    private SslStream?  _ssl;
    private Thread?     _readThread;
    private bool        _running;

    private readonly string _senderCompId;
    private readonly string _targetCompId;
    private readonly string _senderSubId;
    private readonly string _password;
    private readonly string _account;

    private int  _seqNum  = 1;
    private bool _loggedOn = false;
    private readonly object _sendLock = new();
    private int _orderId = 1;

    public bool IsLoggedOn => _loggedOn;

    public event Action<string, string>? OnExecutionReport;
    public event Action<string>?         OnError;

    public FixClient(string host, int port,
                     string senderCompId, string targetCompId,
                     string senderSubId, string password, string account)
    {
        _host         = host;
        _port         = port;
        _senderCompId = senderCompId;
        _targetCompId = targetCompId;
        _senderSubId  = senderSubId;
        _password     = password;
        _account      = account;
    }

    // ── Connect ──────────────────────────────────────────────────────────────

    public async Task<bool> ConnectAsync(int timeoutSeconds = 15)
    {
        try
        {
            _tcp = new TcpClient();
            await _tcp.ConnectAsync(_host, _port);

            _ssl = new SslStream(_tcp.GetStream(), false,
                (_, _, _, _) => true); // accept demo self-signed cert
            await _ssl.AuthenticateAsClientAsync(_host);

            _running = true;
            _readThread = new Thread(ReadLoop) { IsBackground = true };
            _readThread.Start();

            SendLogon();

            var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
            while (!_loggedOn && DateTime.UtcNow < deadline)
                await Task.Delay(200);

            return _loggedOn;
        }
        catch (Exception ex)
        {
            OnError?.Invoke($"Connect failed: {ex.Message}");
            return false;
        }
    }

    // ── FIX Message Builder ──────────────────────────────────────────────────
    // FIX 4.4 frame: 8=FIX.4.4|9=<bodylen>|<body>|10=<chk>|
    // BodyLength = byte count from tag 35 up to (but not including) tag 10

    private string BuildMessage(string msgType, string body)
    {
        var now = DateTime.UtcNow.ToString("yyyyMMdd-HH:mm:ss.fff");

        // Compose the body (everything between 9= and 10=)
        var sb = new StringBuilder();
        sb.Append($"35={msgType}{SOH}");
        sb.Append($"49={_senderCompId}{SOH}");
        if (!string.IsNullOrEmpty(_senderSubId))
            sb.Append($"50={_senderSubId}{SOH}");
        sb.Append($"56={_targetCompId}{SOH}");
        sb.Append($"57=TRADE{SOH}");
        sb.Append($"34={_seqNum++}{SOH}");
        sb.Append($"52={now}{SOH}");
        sb.Append(body); // caller ensures this ends with SOH

        var bodyStr    = sb.ToString();
        var bodyLength = Encoding.ASCII.GetByteCount(bodyStr);

        // Assemble full message without checksum
        var preCheck = $"8=FIX.4.4{SOH}9={bodyLength}{SOH}{bodyStr}";

        // Checksum over every byte in preCheck
        var checksum = Encoding.ASCII.GetBytes(preCheck).Aggregate(0, (acc, b) => acc + b) % 256;

        var full = $"{preCheck}10={checksum:D3}{SOH}";
        Console.WriteLine($"[OUT] {msgType} → {full.Replace(SOH, '|')}");
        return full;
    }

    private void Send(string msgType, string body)
    {
        var msg   = BuildMessage(msgType, body);
        var bytes = Encoding.ASCII.GetBytes(msg);
        lock (_sendLock)
        {
            _ssl?.Write(bytes, 0, bytes.Length);
            _ssl?.Flush();
        }
    }

    // ── Session Messages ─────────────────────────────────────────────────────

    private void SendLogon()
    {
        // cTrader FIX Logon fields:
        //   98 = EncryptMethod (0 = none)
        //   108 = HeartBtInt
        //   553 = Username  (account number)
        //   554 = Password
        var body = $"98=0{SOH}108=30{SOH}553={_account}{SOH}554={_password}{SOH}";
        Send("A", body);
    }

    private void SendHeartbeat(string? testReqId = null)
    {
        var body = testReqId != null ? $"112={testReqId}{SOH}" : "";
        Send("0", body);
    }

    // ── Orders ───────────────────────────────────────────────────────────────

    /// <summary>Market order. side: '1'=Buy, '2'=Sell</summary>
    public void SendMarketOrder(string symbol, char side, double qty)
    {
        var clOrdId = $"MKT{_orderId++}{DateTime.UtcNow:HHmmss}";
        var now     = DateTime.UtcNow.ToString("yyyyMMdd-HH:mm:ss.fff");
        // OrdType 1=Market, TimeInForce 3=IOC
        // cTrader FIX: no tag 1 (Account) - identified by session
        // no tag 59 (TimeInForce) for market orders
        var body = $"11={clOrdId}{SOH}55={symbol}{SOH}54={side}{SOH}" +
                   $"60={now}{SOH}40=1{SOH}38={qty:F2}{SOH}";
        Send("D", body);
        Console.WriteLine($"[ORDER] MARKET {(side == '1' ? "BUY" : "SELL")} {qty} {symbol} | ClOrdID: {clOrdId}");
    }

    /// <summary>Limit order GTC. side: '1'=Buy, '2'=Sell</summary>
    public void SendLimitOrder(string symbol, char side, double qty, double price)
    {
        var clOrdId = $"LMT{_orderId++}{DateTime.UtcNow:HHmmss}";
        var now     = DateTime.UtcNow.ToString("yyyyMMdd-HH:mm:ss.fff");
        // OrdType 2=Limit, TimeInForce 1=GTC
        // cTrader FIX: no tag 1 (Account), TimeInForce 1=GTC is OK for limits
        var body = $"11={clOrdId}{SOH}55={symbol}{SOH}54={side}{SOH}" +
                   $"60={now}{SOH}40=2{SOH}44={price}{SOH}38={qty:F2}{SOH}59=1{SOH}";
        Send("D", body);
        Console.WriteLine($"[ORDER] LIMIT {(side == '1' ? "BUY" : "SELL")} {qty} {symbol} @ {price} | ClOrdID: {clOrdId}");
    }

    // ── Read Loop ────────────────────────────────────────────────────────────

    private void ReadLoop()
    {
        var buffer   = new byte[65536];
        var leftover = "";
        var lastHb   = DateTime.UtcNow;

        while (_running)
        {
            try
            {
                if ((DateTime.UtcNow - lastHb).TotalSeconds > 25)
                {
                    SendHeartbeat();
                    lastHb = DateTime.UtcNow;
                }

                if (_ssl == null || !_ssl.CanRead) break;
                _ssl.ReadTimeout = 1000;

                int n;
                try { n = _ssl.Read(buffer, 0, buffer.Length); }
                catch (IOException) { continue; }

                if (n == 0) break;

                leftover += Encoding.ASCII.GetString(buffer, 0, n);

                // Extract complete FIX messages (each ends with 10=ddd\x01)
                while (true)
                {
                    var end = leftover.IndexOf($"10=", StringComparison.Ordinal);
                    if (end < 0) break;
                    var eom = leftover.IndexOf(SOH, end);
                    if (eom < 0) break;

                    var msg = leftover[..(eom + 1)];
                    leftover = leftover[(eom + 1)..];
                    ProcessMessage(msg);
                }
            }
            catch (Exception ex) when (_running)
            {
                OnError?.Invoke($"Read error: {ex.Message}");
                break;
            }
        }
    }

    // ── Message Parser ───────────────────────────────────────────────────────

    private void ProcessMessage(string raw)
    {
        var fields = ParseFields(raw);
        if (!fields.TryGetValue("35", out var msgType)) return;

        var preview = raw.Replace(SOH, '|');
        Console.WriteLine($"[IN]  {msgType} ← {preview[..Math.Min(160, preview.Length)]}");

        switch (msgType)
        {
            case "A": // Logon
                _loggedOn = true;
                Console.ForegroundColor = ConsoleColor.Green;
                Console.WriteLine("[SESSION] ✓ Logged ON — ready to trade!");
                Console.ResetColor();
                break;

            case "0": // Heartbeat
                if (fields.TryGetValue("112", out var hbId))
                    SendHeartbeat(hbId);
                break;

            case "1": // TestRequest — reply with Heartbeat
                if (fields.TryGetValue("112", out var trId))
                    SendHeartbeat(trId);
                break;

            case "5": // Logout
                _loggedOn = false;
                var reason = fields.TryGetValue("58", out var txt) ? txt : "unknown";
                Console.ForegroundColor = ConsoleColor.Yellow;
                Console.WriteLine($"[SESSION] Logged OUT — {reason}");
                Console.ResetColor();
                break;

            case "8": // ExecutionReport
                HandleExecutionReport(fields);
                break;

            case "j": // BusinessMessageReject
                var bText = fields.TryGetValue("58", out var bt) ? bt : "unknown";
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine($"[REJECT] {bText}");
                Console.ResetColor();
                OnExecutionReport?.Invoke("REJECTED", bText);
                break;
        }
    }

    private void HandleExecutionReport(Dictionary<string, string> f)
    {
        var orderId   = f.TryGetValue("37",  out var oi) ? oi : "?";
        var clOrdId   = f.TryGetValue("11",  out var ci) ? ci : "?";
        var ordStatus = f.TryGetValue("39",  out var os) ? os : "?";
        var symbol    = f.TryGetValue("55",  out var sy) ? sy : "?";
        var side      = f.TryGetValue("54",  out var sd) ? (sd == "1" ? "BUY" : "SELL") : "?";
        var cumQty    = f.TryGetValue("14",  out var cq) ? cq : "0";
        var avgPx     = f.TryGetValue("6",   out var ap) ? ap : "0";
        var rejectTxt = f.TryGetValue("58",  out var rt) ? rt : "";

        var status = ordStatus switch
        {
            "0" => "NEW",
            "1" => "PARTIAL FILL",
            "2" => "FILLED ✓",
            "4" => "CANCELLED",
            "8" => "REJECTED ✗",
            _   => $"STATUS({ordStatus})"
        };

        Console.ForegroundColor = ordStatus == "2" ? ConsoleColor.Green
                                : ordStatus == "8" ? ConsoleColor.Red
                                : ConsoleColor.Cyan;
        Console.WriteLine($"[EXEC] {symbol} {side} | {status} | OrderID: {orderId} | Qty: {cumQty} | AvgPx: {avgPx}");
        if (!string.IsNullOrEmpty(rejectTxt))
            Console.WriteLine($"       Reason: {rejectTxt}");
        Console.ResetColor();

        OnExecutionReport?.Invoke(status, $"{symbol} {side} qty={cumQty} px={avgPx} {rejectTxt}".Trim());
    }

    private static Dictionary<string, string> ParseFields(string raw)
    {
        var dict = new Dictionary<string, string>();
        foreach (var pair in raw.Split(SOH, StringSplitOptions.RemoveEmptyEntries))
        {
            var eq = pair.IndexOf('=');
            if (eq > 0) dict[pair[..eq]] = pair[(eq + 1)..];
        }
        return dict;
    }

    public void Disconnect()
    {
        if (_running)
        {
            _running = false;
            try { Send("5", ""); } catch { /* ignore */ }
            Thread.Sleep(300);
        }
        _loggedOn = false;
        _ssl?.Dispose();
        _tcp?.Dispose();
    }

    public void Dispose() => Disconnect();
}
