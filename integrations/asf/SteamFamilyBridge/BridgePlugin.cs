using System.Composition;
using System.Text.Json;
using ArchiSteamFarm.Core;
using ArchiSteamFarm.Plugins.Interfaces;

namespace SteamFamilyBridge;

// Targets the cancellation-aware ASF 7.0.0.8 plugin interfaces (.NET 11).
[Export(typeof(IPlugin))]
public sealed class BridgePlugin : IASF {
    public string Name => "SteamFamilyBridge";
    public Version Version => new(0, 2, 0);

    public Task OnLoaded(CancellationToken cancellationToken = default) {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.CompletedTask;
    }

    public Task OnASFInit(
        IReadOnlyDictionary<string, JsonElement>? additionalConfigProperties = null,
        CancellationToken cancellationToken = default
    ) {
        cancellationToken.ThrowIfCancellationRequested();
        BridgeSettings settings = BridgeSettings.Disabled;
        if (additionalConfigProperties?.TryGetValue("SteamFamilyBridge", out JsonElement config) == true) {
            settings = BridgeSettings.Parse(config);
        }
        cancellationToken.ThrowIfCancellationRequested();
        BridgeSettings.Current = settings;
        ASF.ArchiLogger.LogGenericInfo(settings.Enabled
            ? "SteamFamilyBridge enabled: read-only Access Token export for allowlisted bots."
            : "SteamFamilyBridge disabled (missing/invalid opt-in configuration).");
        return Task.CompletedTask;
    }
}

internal sealed record BridgeSettings(bool Enabled, string Key, HashSet<string> Bots) {
    internal static readonly BridgeSettings Disabled = new(false, "", new(StringComparer.Ordinal));
    private static BridgeSettings _current = Disabled;

    internal static BridgeSettings Current {
        get => Volatile.Read(ref _current);
        set => Volatile.Write(ref _current, value);
    }

    internal static BridgeSettings Parse(JsonElement config) {
        if (config.ValueKind != JsonValueKind.Object
            || !config.TryGetProperty("Enabled", out JsonElement enabled)
            || enabled.ValueKind != JsonValueKind.True
            || !config.TryGetProperty("Key", out JsonElement key)
            || key.ValueKind != JsonValueKind.String
            || !config.TryGetProperty("Bots", out JsonElement bots)
            || bots.ValueKind != JsonValueKind.Array) {
            return Disabled;
        }
        string secret = key.GetString() ?? "";
        if (secret.Length < 32 || secret.Length > 256 || secret.Any(c => c < 33 || c > 126)) {
            return Disabled;
        }
        HashSet<string> allowedBots = new(StringComparer.Ordinal);
        foreach (JsonElement item in bots.EnumerateArray()) {
            string? name = item.ValueKind == JsonValueKind.String ? item.GetString() : null;
            if (string.IsNullOrEmpty(name) || name.Length > 128 || name.Contains("..", StringComparison.Ordinal)
                || name == "." || name.Any(c => !char.IsLetterOrDigit(c) && c is not '_' and not '-' and not '.')) {
                return Disabled;
            }
            allowedBots.Add(name);
        }
        return allowedBots.Count > 0 ? new(true, secret, allowedBots) : Disabled;
    }
}
