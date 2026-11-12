using System.Globalization;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using ArchiSteamFarm.Core;
using ArchiSteamFarm.IPC.Controllers.Api;
using ArchiSteamFarm.Steam;
using Microsoft.AspNetCore.Mvc;

namespace SteamFamilyBridge;

// /Api prefix is essential: ASF's standard IPC authentication middleware covers it.
[Route("Api/SteamFamily")]
public sealed class AccessTokenController : ArchiController {
    [HttpGet("{botName}/AccessToken")]
    public IActionResult AccessTokenGet(string botName) {
        HttpContext.Response.Headers.CacheControl = "no-store, private";
        HttpContext.Response.Headers.Pragma = "no-cache";
        HttpContext.Response.Headers.Expires = "0";

        BridgeSettings settings = BridgeSettings.Current;
        if (!settings.Enabled) {
            return Failure(404, "bridge_disabled");
        }
        // Require ASF's IPCPassword even on loopback, plus a separate bridge key.
        if (string.IsNullOrEmpty(ASF.GlobalConfig?.IPCPassword)) {
            return Failure(403, "ipc_password_required");
        }
        IPAddress? remote = HttpContext.Connection.RemoteIpAddress;
        if (remote?.IsIPv4MappedToIPv6 == true) {
            remote = remote.MapToIPv4();
        }
        if (!HttpContext.Request.IsHttps && (remote == null || !IPAddress.IsLoopback(remote))) {
            return Failure(403, "secure_transport_required");
        }
        string key = HttpContext.Request.Headers["X-Steam-Family-Key"].ToString();
        if (key.Length > 256 || !CryptographicOperations.FixedTimeEquals(
            SHA256.HashData(Encoding.UTF8.GetBytes(key)),
            SHA256.HashData(Encoding.UTF8.GetBytes(settings.Key)))) {
            return Failure(401, "bridge_auth_failed");
        }
        // Exact dictionary lookup only. Never use Bot.GetBots with ranges/wildcards.
        IReadOnlyDictionary<string, Bot>? bots = Bot.BotsReadOnly;
        if (!settings.Bots.Contains(botName) || bots == null
            || !bots.TryGetValue(botName, out Bot? bot)
            || !string.Equals(bot.BotName, botName, StringComparison.Ordinal)) {
            return Failure(404, "bot_not_allowed");
        }
        if (bot.IsAccountLocked) {
            return Failure(409, "account_locked");
        }
        if (!bot.IsConnectedAndLoggedOn) {
            return Failure(503, "bot_offline");
        }
        // Only READ the current token. Do not call RefreshWebSession, SteamClient
        // authentication, Actions.Start, Commands.Response or any renewal method.
        string? token = bot.AccessToken;
        if (!TryTokenMetadata(token, out string steamID, out long expiresAt)
            || steamID != bot.SteamID.ToString(CultureInfo.InvariantCulture)
            || expiresAt <= DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 60) {
            return Failure(503, "token_unavailable");
        }
        // Recheck after reading because ASF lifecycle callbacks run concurrently.
        if (bot.IsAccountLocked) {
            return Failure(409, "account_locked");
        }
        if (!bot.IsConnectedAndLoggedOn) {
            return Failure(503, "bot_offline");
        }
        if (steamID != bot.SteamID.ToString(CultureInfo.InvariantCulture) || token != bot.AccessToken) {
            return Failure(503, "token_unavailable");
        }
        return Ok(new {
            Success = true,
            Result = new {
                ProtocolVersion = 1,
                BotName = bot.BotName,
                SteamID = steamID,  // string: uint64 cannot safely pass through JS numbers
                IsConnectedAndLoggedOn = true,
                IsAccountLocked = false,
                AccountFlags = (uint) bot.AccountFlags,
                AccessToken = token,
                ExpiresAt = expiresAt
            }
        });
    }

    private ObjectResult Failure(int status, string code) => StatusCode(status, new {
        Success = false,
        ErrorCode = code
    });

    private static bool TryTokenMetadata(string? token, out string steamID, out long expiresAt) {
        steamID = "";
        expiresAt = 0;
        if (string.IsNullOrEmpty(token)) {
            return false;
        }
        try {
            string[] parts = token.Split('.');
            if (parts.Length != 3) {
                return false;
            }
            string segment = parts[1].Replace('-', '+').Replace('_', '/');
            segment = segment.PadRight(segment.Length + (4 - segment.Length % 4) % 4, '=');
            using JsonDocument json = JsonDocument.Parse(Convert.FromBase64String(segment));
            JsonElement payload = json.RootElement;
            if (!payload.TryGetProperty("sub", out JsonElement subject)
                || subject.ValueKind != JsonValueKind.String
                || !payload.TryGetProperty("exp", out JsonElement exp)
                || !exp.TryGetInt64(out expiresAt)) {
                return false;
            }
            steamID = subject.GetString() ?? "";
            if (!payload.TryGetProperty("aud", out JsonElement audiences) || audiences.ValueKind != JsonValueKind.Array) {
                return false;
            }
            bool hasWebAudience = false;
            foreach (JsonElement item in audiences.EnumerateArray()) {
                if (item.ValueKind != JsonValueKind.String || item.GetString() is "renew" or "derive" or "machine") {
                    return false;
                }
                hasWebAudience |= item.GetString() == "web";
            }
            return hasWebAudience && steamID.Length == 17 && steamID.All(char.IsAsciiDigit);
        } catch (Exception e) when (e is FormatException or JsonException or InvalidOperationException) {
            // Never log token contents or exception text from a payload parser.
            return false;
        }
    }
}
