// mem0-hook-client.cs — v0.20 A.6: compiled thin UserPromptSubmit pipe client.
//
// WHY: with the A.5 resident daemon serving the whole per-prompt pipeline, the
// remaining warm-path cost was the CLIENT itself: powershell.exe spawn ~242ms
// + extract+lib parse ~130ms + lib SHA256 JIT ~18ms (~390ms) before the ~70ms
// pipe transaction even starts (warm p50 843ms, target <=600ms). This exe
// replaces powershell.exe as the REGISTERED UserPromptSubmit command and does
// only what the A.5 PS fast path did: read stdin, hash the deployed lib,
// probe-then-connect the daemon pipe, one bundle_raw transaction, write the
// returned [MEMORY CONTEXT] block to stdout.
//
// HARD CONSTRAINT (unchanged from A.5): the daemon is an ACCELERATOR, never a
// dependency. ANY failure here — missing lib, absent pipe, connect timeout,
// response timeout, garbage response, lib-hash mismatch, an exception anywhere
// — falls back to the EXISTING PowerShell inline path: this exe spawns
//   powershell.exe -NoProfile -ExecutionPolicy Bypass -File user-prompt-extract.ps1 -SkipDaemon
// with the verbatim stdin BYTES relayed, the child's stdout relayed to our
// stdout, and the child's exit code relayed (-SkipDaemon stops the PS script
// from re-probing the daemon and double-paying the probe/connect this exe
// already paid). On the no-pipe failure the exe also triggers the detached
// daemon respawn (the PS script can no longer do it: its fast path — where the
// respawn lives — is skipped under -SkipDaemon).
//
// Exit-code contract (Claude Code UserPromptSubmit): exit 0 => stdout becomes
// context; exit 2 => BLOCKS and erases the user's prompt; other non-zero =>
// non-blocking error. The PS hook always exits 0; this exe mirrors that — all
// internal failures exit 0 with whatever output the fallback produced, and a
// child exit code of 2 is mapped to 0 so a broken fallback can never block the
// prompt.
//
// Phase 0.B (decision capture) stays in PowerShell: after a successful daemon
// transaction the exe mirrors the PS client's cheap pre-gates
// (Test-DecisionLikePrompt + transcript existence) and, only when a decision
// is plausible (rare: "yes" / "1 and 2" / "go ahead"...), spawns the PS script
// with MEM0_HOOK_DAEMON_SERVED=1 — the script then skips the daemon txn AND
// sections 1-4 (all already done daemon-side) and runs ONLY 0.B.
//
// Build/deploy: scripts\windows\build-hook-client.ps1 (framework csc — no new
// toolchain) compiles the DEPLOYED .cs, smoke-gates the candidate, and only
// then installs C:\Users\<user>\.claude\scripts\mem0-hook-client.exe. R9
// (Test-MemoryStack) hashes the .cs and checks exe-vs-.cs freshness.
// No payload logging: the single log line carries session id + daemon diag
// counters only (same privacy contract as the PS client and the daemon).
//
// Env knobs (tests only): MEM0_HOOK_PIPE overrides the pipe name so Pester can
// drive the fail-open matrix against scripted fake daemons.

using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

static class HookClient
{
    const int ConnectTimeoutMs  = 100;   // same budget as the PS client
    const int ResponseTimeoutMs = 2500;  // same budget as the PS client

    static string ScriptDir;             // exe's own dir = deployed scripts dir

    static int Main()
    {
        // Never block the prompt: a crash anywhere still exits 0 (empty output).
        try { return Run(); } catch { return 0; }
    }

    static int Run()
    {
        ScriptDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');

        byte[] stdin = ReadAllStdin();
        if (stdin == null || stdin.Length == 0) return 0;  // PS inline also no-ops on empty stdin

        string pipeName = Environment.GetEnvironmentVariable("MEM0_HOOK_PIPE");
        if (string.IsNullOrEmpty(pipeName)) pipeName = "mem0-hook-daemon";

        var sw = Stopwatch.StartNew();

        // Staleness handshake input (v0.21 Phase B M3/M6): the COMBINED digest
        // SHA256( Sha256Hex(user-prompt-lib.ps1) + Sha256Hex(mem0-hook-daemon.ps1) )
        // over the CURRENTLY deployed files, computed identically to the daemon
        // startup and the PS client (lib Get-HandshakeHash). The daemon stamps
        // the digest IT loaded on every response; a mismatch means a lib OR a
        // daemon-only deploy happened after the daemon started -> it must never
        // serve stale logic. Either file unhashable -> inline (the PS client
        // makes the same call: a null handshake hash => no daemon).
        string libHash    = Sha256Hex(Path.Combine(ScriptDir, "user-prompt-lib.ps1"));
        string daemonHash = Sha256Hex(Path.Combine(ScriptDir, "mem0-hook-daemon.ps1"));
        if (string.IsNullOrEmpty(libHash) || string.IsNullOrEmpty(daemonHash)) return RunFallback(stdin);
        string expectedHash = Sha256HexOfString(libHash + daemonHash);
        if (string.IsNullOrEmpty(expectedHash)) return RunFallback(stdin);

        // Probe BEFORE Connect (A.5 finding: Connect() on an ABSENT pipe spins
        // its full timeout ~110-155ms; \\.\pipe\ enumeration costs ~1-4ms).
        // Absent -> detached respawn for the NEXT prompt + inline fallback now.
        // Present-but-connect-fail = busy daemon -> inline, NO spawn (the
        // daemon's single-instance mutex would no-op a duplicate anyway).
        if (!PipePresent(pipeName))
        {
            SpawnDaemonDetached();
            return RunFallback(stdin);
        }

        string line = PipeTransaction(pipeName, stdin, expectedHash);
        if (line == null) return RunFallback(stdin);

        byte[] contextBytes; string prompt, tpath, sid, diag; bool needs0b;
        int verdict = ParseRawResponse(line, expectedHash, out contextBytes, out prompt, out tpath, out sid, out diag, out needs0b);
        if (verdict == 2)  // hash mismatch: stale daemon -> shutdown signal, fresh daemon next prompt
        {
            SendShutdown(pipeName);
            return RunFallback(stdin);
        }
        if (verdict != 0) return RunFallback(stdin);

        // SUCCESS: emit the daemon-rendered block (raw bytes + CRLF — the PS
        // client's [Console]::Out.WriteLine equivalent).
        if (contextBytes != null && contextBytes.Length > 0) WriteStdout(contextBytes, true);
        Log("0.A+0.D served by daemon (exe): session=" + (sid ?? "?") + " " + (diag ?? "") + " exe_ms=" + sw.ElapsedMilliseconds);

        // Phase 0.B pre-gates: the decision verdict (needs_0b) is computed
        // DAEMON-side under the combined handshake (v0.21 Phase B M4) — the C#
        // duplicate of Test-DecisionLikePrompt is gone, so the gate can never
        // drift from the lib. The client keeps only the transcript-exists check
        // (a local filesystem fact the daemon cannot vouch for). A MISSING
        // needs_0b field defaults to false (ParseRawResponse), so the relay does
        // NOT spawn — fail-CLOSED. This is safe: the combined handshake digest
        // hashes mem0-hook-daemon.ps1 itself, so any daemon stamping the matching
        // digest IS the current daemon, which ALWAYS emits needs_0b
        // (mem0-hook-daemon.ps1: hashtable default + always-set on bundle_raw).
        // An OLD daemon stamps a different digest -> verdict 2 (hash_mismatch)
        // above -> RunFallback runs the full inline path, which re-derives 0.B
        // itself. Either way no decision capture is lost.
        if (prompt != null && tpath != null && SafeFileExists(tpath) && needs0b)
            return RunPowerShellRelay(stdin, false /* 0.B-only mode, not -SkipDaemon */);

        return 0;
    }

    // ---------------------------------------------------------------- stdin

    static byte[] ReadAllStdin()
    {
        // RAW bytes (not Console.In): byte-faithful relay to the daemon
        // (stdin_b64) and to the PS fallback's stdin — no console-codepage
        // round-trip can mangle the payload.
        try
        {
            using (Stream s = Console.OpenStandardInput())
            using (MemoryStream ms = new MemoryStream())
            {
                byte[] buf = new byte[65536];
                int n;
                while ((n = s.Read(buf, 0, buf.Length)) > 0) ms.Write(buf, 0, n);
                return ms.ToArray();
            }
        }
        catch { return null; }
    }

    // ------------------------------------------------------------ pipe txn

    static bool PipePresent(string pipeName)
    {
        // Enumeration-only probe (never opens the pipe — a CreateFile would
        // consume the single server instance). Enumeration can throw on exotic
        // pipe names in .NET Framework -> return true ("maybe") and let
        // Connect() decide, exactly like lib Test-DaemonPipePresent.
        try
        {
            string target = @"\\.\pipe\" + pipeName;
            foreach (string p in Directory.EnumerateFiles(@"\\.\pipe\"))
                if (string.Equals(p, target, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }
        catch { return true; }
    }

    static string PipeTransaction(string pipeName, byte[] stdin, string expectedHash)
    {
        // {"op":"bundle_raw","expected_lib_hash":"<digest>","stdin_b64":"<verbatim
        // stdin>"} -> one response line. Null on ANY failure (caller falls back
        // inline). expected_lib_hash (v0.21 Phase B L1) lets the daemon refuse
        // stale service BEFORE any side effect (no rate-limit token, no HTTP);
        // the client-side hash check in ParseRawResponse stays as defense-in-depth.
        NamedPipeClientStream client = null;
        try
        {
            client = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
            try { client.Connect(ConnectTimeoutMs); } catch { return null; }
            string req = "{\"op\":\"bundle_raw\",\"expected_lib_hash\":\"" + expectedHash + "\",\"stdin_b64\":\"" + Convert.ToBase64String(stdin) + "\"}";
            byte[] bytes = Encoding.UTF8.GetBytes(req + "\n");
            client.Write(bytes, 0, bytes.Length);
            client.Flush();
            return ReadLineWithDeadline(client, ResponseTimeoutMs);
        }
        catch { return null; }
        finally { if (client != null) { try { client.Dispose(); } catch { } } }
    }

    static string ReadLineWithDeadline(PipeStream stream, int timeoutMs)
    {
        // One newline-terminated UTF-8 line under a hard total deadline (pipe
        // streams have no ReadTimeout in .NET Framework; APM read + WaitOne,
        // same as lib Read-PipeLineWithDeadline).
        try
        {
            var sw = Stopwatch.StartNew();
            byte[] buf = new byte[65536];
            using (MemoryStream acc = new MemoryStream())
            {
                while (true)
                {
                    long remaining = timeoutMs - sw.ElapsedMilliseconds;
                    if (remaining <= 0) return null;
                    IAsyncResult iar = stream.BeginRead(buf, 0, buf.Length, null, null);
                    if (!iar.AsyncWaitHandle.WaitOne((int)remaining)) return null;
                    int n = stream.EndRead(iar);
                    if (n <= 0) break;
                    acc.Write(buf, 0, n);
                    if (Array.IndexOf(buf, (byte)10, 0, n) >= 0) break;
                }
                if (acc.Length == 0) return null;
                string text = Encoding.UTF8.GetString(acc.ToArray());
                int idx = text.IndexOf('\n');
                if (idx >= 0) text = text.Substring(0, idx);
                return text.TrimEnd('\r');
            }
        }
        catch { return null; }
    }

    static void SendShutdown(string pipeName)
    {
        // Best-effort {op:'shutdown'} on lib-hash mismatch so the stale daemon
        // exits and the next prompt respawns a fresh one. Never throws.
        NamedPipeClientStream c = null;
        try
        {
            c = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
            c.Connect(200);
            byte[] b = Encoding.UTF8.GetBytes("{\"op\":\"shutdown\"}\n");
            c.Write(b, 0, b.Length);
            c.Flush();
            ReadLineWithDeadline(c, 500);
        }
        catch { }
        finally { if (c != null) { try { c.Dispose(); } catch { } } }
    }

    // ------------------------------------------------------- response parse

    static int ParseRawResponse(string line, string expectedHash,
        out byte[] contextBytes, out string prompt, out string tpath, out string sid, out string diag, out bool needs0b)
    {
        // Verdicts: 0=ok, 1=invalid, 2=hash_mismatch. Field-for-field mirror
        // of lib ConvertFrom-DaemonRawResponse: anchored regexes over base64
        // fields (immune to serializer key order/escaping), any decode failure
        // = invalid (-> inline fallback). needs_0b (v0.21 Phase B M4) is the
        // daemon-computed 0.B verdict; a MISSING field defaults to false here,
        // so the caller's gate is fail-CLOSED on it. The hash handshake
        // guarantees a matching-digest response always carries the field (the
        // current daemon always emits it); an old daemon omits it but also
        // stamps a non-matching digest -> verdict-2/hash_mismatch -> inline
        // fallback (which re-derives 0.B). No decision capture is lost.
        contextBytes = null; prompt = null; tpath = null; sid = null; diag = null; needs0b = false;
        try
        {
            if (string.IsNullOrEmpty(line)) return 1;
            if (!Regex.IsMatch(line, "\"ok\"\\s*:\\s*true")) return 1;
            Match m = Regex.Match(line, "\"lib_hash\"\\s*:\\s*\"([0-9a-f]{64})\"");
            if (!m.Success) return 1;
            if (!string.IsNullOrEmpty(expectedHash) && m.Groups[1].Value != expectedHash) return 2;
            if (!Regex.IsMatch(line, "\"served\"\\s*:\\s*true")) return 1;
            contextBytes = B64FieldBytes(line, "context_b64");
            prompt = B64FieldString(line, "prompt_b64");
            tpath  = B64FieldString(line, "tpath_b64");
            sid    = B64FieldString(line, "sid_b64");
            diag   = B64FieldString(line, "diag_b64");
            needs0b = Regex.IsMatch(line, "\"needs_0b\"\\s*:\\s*true");
            return 0;
        }
        catch { contextBytes = null; return 1; }
    }

    static byte[] B64FieldBytes(string line, string name)
    {
        Match m = Regex.Match(line, "\"" + name + "\"\\s*:\\s*\"([A-Za-z0-9+/=]*)\"");
        if (!m.Success || m.Groups[1].Value.Length == 0) return null;
        return Convert.FromBase64String(m.Groups[1].Value);   // throws -> caller treats response invalid
    }

    static string B64FieldString(string line, string name)
    {
        byte[] b = B64FieldBytes(line, name);
        return b == null ? null : Encoding.UTF8.GetString(b);
    }

    // ------------------------------------------------------------ 0.B gate
    // (v0.21 Phase B M4: the decision predicate now lives ONLY in the lib —
    // the daemon computes needs_0b under the handshake; the C# duplicate that
    // could silently drift from the lib vocabulary was deleted.)

    static bool SafeFileExists(string path)
    {
        try { return File.Exists(path); } catch { return false; }
    }

    // --------------------------------------------------- PowerShell spawns

    static int RunFallback(byte[] stdin)
    {
        return RunPowerShellRelay(stdin, true);
    }

    static int RunPowerShellRelay(byte[] stdin, bool skipDaemon)
    {
        // skipDaemon=true  -> FULL inline path (-SkipDaemon: never re-probe the
        //                     daemon; this exe already paid/handled that).
        // skipDaemon=false -> 0.B-ONLY path (MEM0_HOOK_DAEMON_SERVED=1: the
        //                     daemon already served sections 1-4 and the block
        //                     is already on our stdout).
        // Stdin bytes relayed verbatim; child stdout relayed verbatim to our
        // stdout (Claude Code reads OUR stdout — the child must never inherit
        // it unmanaged); child exit code relayed with 2 mapped to 0 (exit 2
        // would BLOCK the user's prompt; the PS hook never legitimately
        // exits 2). Missing script -> exit 0 with no output (never block).
        try
        {
            string script = Path.Combine(ScriptDir, "user-prompt-extract.ps1");
            if (!File.Exists(script))
            {
                Log("ERROR: fallback script missing at " + script + " - exiting 0 (prompt never blocked)");
                return 0;
            }
            var psi = new ProcessStartInfo();
            psi.FileName = PowerShellPath();
            psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"" + (skipDaemon ? " -SkipDaemon" : "");
            psi.UseShellExecute = false;
            psi.RedirectStandardInput = true;
            psi.RedirectStandardOutput = true;
            // stderr intentionally NOT redirected: the child shares this exe's
            // stderr, so hook-error surfacing in Claude Code is unchanged.
            if (!skipDaemon) psi.EnvironmentVariables["MEM0_HOOK_DAEMON_SERVED"] = "1";
            using (Process p = Process.Start(psi))
            {
                Stream si = p.StandardInput.BaseStream;
                si.Write(stdin, 0, stdin.Length);
                si.Close();
                using (Stream o = Console.OpenStandardOutput())
                {
                    p.StandardOutput.BaseStream.CopyTo(o);
                    o.Flush();
                }
                p.WaitForExit();
                int code = p.ExitCode;
                return code == 2 ? 0 : code;
            }
        }
        catch { return 0; }
    }

    static void SpawnDaemonDetached()
    {
        // Detached hidden respawn so the NEXT prompt is fast. CRITICAL:
        // UseShellExecute=true — with false the resident child INHERITS this
        // hook's stdout handle and Claude Code waits for EOF until the daemon
        // exits (measured hang, A.5). ShellExecute = no inherited std handles.
        try
        {
            string daemon = Path.Combine(ScriptDir, "mem0-hook-daemon.ps1");
            if (!File.Exists(daemon)) return;
            var psi = new ProcessStartInfo();
            psi.FileName = PowerShellPath();
            psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + daemon + "\"";
            psi.UseShellExecute = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            Process p = Process.Start(psi);
            if (p != null) p.Dispose();
        }
        catch { }
    }

    static string PowerShellPath()
    {
        string root = Environment.GetEnvironmentVariable("SystemRoot");
        if (string.IsNullOrEmpty(root)) root = @"C:\Windows";
        return root + @"\System32\WindowsPowerShell\v1.0\powershell.exe";
    }

    // -------------------------------------------------------------- output

    static void WriteStdout(byte[] data, bool newline)
    {
        // Raw bytes (the daemon's block is UTF-8) + CRLF, matching the PS
        // client's [Console]::Out.WriteLine framing.
        Stream o = Console.OpenStandardOutput();
        o.Write(data, 0, data.Length);
        if (newline) o.Write(new byte[] { 13, 10 }, 0, 2);
        o.Flush();
    }

    // --------------------------------------------------------------- misc

    static string Sha256Hex(string path)
    {
        try
        {
            using (SHA256 sha = SHA256.Create())
            using (FileStream fs = File.OpenRead(path))
            {
                byte[] h = sha.ComputeHash(fs);
                var sb = new StringBuilder(64);
                foreach (byte b in h) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }
        catch { return null; }
    }

    static string Sha256HexOfString(string text)
    {
        // v0.21 Phase B (M3/M6): SHA256 of a UTF-8 string -> lowercase hex, or
        // null on failure. Used to fold Sha256Hex(lib) + Sha256Hex(daemon) into
        // ONE combined handshake digest (mirror of lib Get-StringSha256Hex).
        try
        {
            using (SHA256 sha = SHA256.Create())
            {
                byte[] h = sha.ComputeHash(Encoding.UTF8.GetBytes(text));
                var sb = new StringBuilder(64);
                foreach (byte b in h) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }
        catch { return null; }
    }

    static void Log(string msg)
    {
        // Same file as the PS client; one line, no payload contents.
        try
        {
            string dir = Environment.GetEnvironmentVariable("USERPROFILE") + @"\.claude\logs";
            if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
            File.AppendAllText(dir + @"\user-prompt-extract.log",
                "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] " + msg + Environment.NewLine);
        }
        catch { }
    }
}
