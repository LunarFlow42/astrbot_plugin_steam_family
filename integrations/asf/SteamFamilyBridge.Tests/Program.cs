using System.Reflection;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using ArchiSteamFarm.Core;
using ArchiSteamFarm.Helpers.Json;
using ArchiSteamFarm.Plugins.Interfaces;
using ArchiSteamFarm.Storage;
using SteamFamilyBridge;

// Dependency-free offline bridge tests; never creates or connects a Steam bot.
Assembly assembly = typeof(BridgePlugin).Assembly;
Type settingsType = assembly.GetType("SteamFamilyBridge.BridgeSettings", throwOnError: true)!;
MethodInfo parse = settingsType.GetMethod("Parse", BindingFlags.Static | BindingFlags.NonPublic)!;
PropertyInfo current = settingsType.GetProperty("Current", BindingFlags.Static | BindingFlags.NonPublic)!;
PropertyInfo enabled = settingsType.GetProperty("Enabled")!;
int assertions = 0;

void Check(bool condition, string message) {
    assertions++;
    if (!condition) {
        throw new InvalidOperationException(message);
    }
}

object Parse(string value) {
    using JsonDocument doc = JsonDocument.Parse(value);
    return parse.Invoke(null, [doc.RootElement])!;
}

// Exercise the actual ASF 7 interfaces, not just methods on the concrete class.
BridgePlugin plugin = new();
IPlugin pluginContract = plugin;
IASF initContract = plugin;
Check(typeof(ASF).Assembly.GetName().Version == new Version(7, 0, 0, 8), "test uses the requested ASF 7.0.0.8 assemblies");
Check(pluginContract.Version == new Version(0, 2, 0), "plugin version matches ASF 7 adapter release");
Check(assembly.GetName().Version == new Version(0, 2, 0, 0), "bridge assembly version matches plugin version");
await pluginContract.OnLoaded(CancellationToken.None);
using CancellationTokenSource cancelled = new();
cancelled.Cancel();
bool loadCancelled = false;
try {
    await pluginContract.OnLoaded(cancelled.Token);
} catch (OperationCanceledException) {
    loadCancelled = true;
}
Check(loadCancelled, "ASF 7 OnLoaded honors cancellation without side effects");

string key = new('k', 48);
object optIn = Parse(JsonSerializer.Serialize(new { Enabled = true, Key = key, Bots = new[] { "main" } }));
Check((bool) enabled.GetValue(optIn)!, "valid explicit configuration must enable bridge");
current.SetValue(null, optIn);
bool initCancelled = false;
try {
    await initContract.OnASFInit(cancellationToken: cancelled.Token);
} catch (OperationCanceledException) {
    initCancelled = true;
}
Check(initCancelled, "ASF 7 OnASFInit honors cancellation");
Check(ReferenceEquals(current.GetValue(null), optIn), "cancelled initialization does not clear previous bridge configuration");
using (JsonDocument config = JsonDocument.Parse(JsonSerializer.Serialize(new { Enabled = true, Key = key, Bots = new[] { "main" } }))) {
    await initContract.OnASFInit(new Dictionary<string, JsonElement> { ["SteamFamilyBridge"] = config.RootElement }, CancellationToken.None);
}
Check((bool) enabled.GetValue(current.GetValue(null))!, "ASF 7 initialization accepts valid explicit opt-in configuration");
await initContract.OnASFInit(cancellationToken: CancellationToken.None);
Check(!(bool) enabled.GetValue(current.GetValue(null))!, "ASF 7 initialization without opt-in disables the bridge");
using (JsonDocument config = JsonDocument.Parse("{}")) {
    await initContract.OnASFInit(new Dictionary<string, JsonElement> { ["SteamFamilyBridge"] = config.RootElement }, CancellationToken.None);
}
Check(!(bool) enabled.GetValue(current.GetValue(null))!, "invalid ASF 7 initialization configuration keeps the bridge disabled");
foreach (string config in new[] {
    "{}", "null", "[]",
    JsonSerializer.Serialize(new { Enabled = false, Key = key, Bots = new[] { "main" } }),
    JsonSerializer.Serialize(new { Enabled = true, Key = "short", Bots = new[] { "main" } }),
    JsonSerializer.Serialize(new { Enabled = true, Key = key, Bots = new[] { "ASF,.." } }),
    JsonSerializer.Serialize(new { Enabled = true, Key = key, Bots = new[] { "main/other" } }),
    JsonSerializer.Serialize(new { Enabled = true, Key = key, Bots = Array.Empty<string>() })
}) {
    Check(!(bool) enabled.GetValue(Parse(config))!, "invalid or non-opt-in settings must stay disabled");
}

current.SetValue(null, Parse("{}"));
AccessTokenController controller = new() {
    ControllerContext = new ControllerContext { HttpContext = new DefaultHttpContext() }
};
ObjectResult disabled = (ObjectResult) controller.AccessTokenGet("main");
Check(disabled.StatusCode == 404, "endpoint is disabled by default");
Check(controller.Response.Headers.CacheControl == "no-store, private", "responses must not be cached");

current.SetValue(null, optIn);
ObjectResult noPassword = (ObjectResult) controller.AccessTokenGet("main");
Check(noPassword.StatusCode == 403, "require ASF IPCPassword even if global config not loaded");

// Use only a mock global configuration, no Bot construction or Steam client.
GlobalConfig global = JsonSerializer.Deserialize<GlobalConfig>("{\"IPCPassword\":\"offline-test-password\"}")!;
typeof(ASF).GetProperty("GlobalConfig")!.SetValue(null, global);
controller.HttpContext.Connection.RemoteIpAddress = System.Net.IPAddress.Loopback;
ObjectResult badKey = (ObjectResult) controller.AccessTokenGet("main");
Check(badKey.StatusCode == 401, "an IPC password alone is not enough to read tokens");
controller.Request.Headers["X-Steam-Family-Key"] = key;
ObjectResult noBot = (ObjectResult) controller.AccessTokenGet("not-allowlisted");
Check(noBot.StatusCode == 404, "never expose bots outside exact allowlist");
controller.HttpContext.Connection.RemoteIpAddress = System.Net.IPAddress.Parse("192.0.2.1");
ObjectResult insecure = (ObjectResult) controller.AccessTokenGet("main");
Check(insecure.StatusCode == 403, "remote HTTP must never export credentials");
controller.Request.Scheme = "https";
ObjectResult secureNoBot = (ObjectResult) controller.AccessTokenGet("not-allowlisted");
Check(secureNoBot.StatusCode == 404, "remote HTTPS with a bridge key still enforces allowlist");

MethodInfo tokenMetadata = typeof(AccessTokenController).GetMethod("TryTokenMetadata", BindingFlags.Static | BindingFlags.NonPublic)!;
string Jwt(object value) => "header." + Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(value))).TrimEnd('=').Replace('+', '-').Replace('/', '_') + ".signature";
bool Accept(string? token) {
    object?[] args = [token, "", 0L];
    return (bool) tokenMetadata.Invoke(null, args)!;
}

Check(Accept(Jwt(new { sub = "76561198555000001", exp = 9999999999L, aud = new[] { "web" } })), "accept short-lived web-access token metadata");
foreach (string? token in new[] {
    null, "bad", "header.bad.signature",
    Jwt(new { sub = "123", exp = 9999999999L, aud = new[] { "web" } }),
    Jwt(new { sub = "76561198555000001", exp = 9999999999L, aud = new[] { "web", "renew" } }),
    Jwt(new { sub = "76561198555000001", exp = 9999999999L, aud = new[] { "derive" } }),
    Jwt(new { sub = "76561198555000001", exp = 9999999999L, aud = "web" }),
    Jwt(new { sub = "76561198555000001", exp = 9999999999L })
}) {
    Check(!Accept(token), "reject malformed or refresh/machine-token metadata");
}

Check(!typeof(AccessTokenController).GetMethods().Any(m => m.Name.Contains("Post", StringComparison.Ordinal)), "bridge exposes no write action");

// Exercise real MVC route discovery and ASF's actual IPC middleware over a local
// test-only HTTP listener. No ASF process or Steam client is started.
WebApplicationBuilder builder = WebApplication.CreateBuilder();
builder.Logging.ClearProviders();
builder.WebHost.ConfigureKestrel(options => options.Listen(System.Net.IPAddress.Loopback, 0));
builder.Services.Configure<ForwardedHeadersOptions>(_ => { });
builder.Services.AddControllers().AddApplicationPart(assembly).AddJsonOptions(options => {
    options.JsonSerializerOptions.PropertyNamingPolicy = JsonUtilities.DefaultJsonSerializerOptions.PropertyNamingPolicy;
    options.JsonSerializerOptions.TypeInfoResolver = JsonUtilities.DefaultJsonSerializerOptions.TypeInfoResolver;
});
await using WebApplication app = builder.Build();
Type middleware = typeof(ASF).Assembly.GetType("ArchiSteamFarm.IPC.Integration.ApiAuthenticationMiddleware", throwOnError: true)!;
app.UseRouting();
app.UseWhen(context => context.Request.Path.StartsWithSegments("/Api"), branch => branch.UseMiddleware(middleware));
app.MapControllers();
await app.StartAsync();
try {
    string address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
    using HttpClient http = new() { BaseAddress = new Uri(address) };
    string endpoint = "/Api/SteamFamily/main/AccessToken";
    using HttpResponseMessage unauthenticated = await http.GetAsync(endpoint);
    Check(unauthenticated.StatusCode == System.Net.HttpStatusCode.Unauthorized, "actual ASF IPC middleware protects bridge route");
    http.DefaultRequestHeaders.Add("Authentication", "offline-test-password");
    using HttpResponseMessage noBridgeKey = await http.GetAsync(endpoint);
    Check(noBridgeKey.StatusCode == System.Net.HttpStatusCode.Unauthorized, "bridge route also requires independent key");
    http.DefaultRequestHeaders.Add("X-Steam-Family-Key", key);
    using HttpResponseMessage missingBot = await http.GetAsync(endpoint);
    Check(missingBot.StatusCode == System.Net.HttpStatusCode.NotFound, "authenticated route only reads an allowlisted running bot");
    Check(missingBot.Headers.CacheControl?.NoStore == true, "HTTP response forbids token caching");
    using JsonDocument response = JsonDocument.Parse(await missingBot.Content.ReadAsStringAsync());
    Check(response.RootElement.GetProperty("Success").GetBoolean() == false, "ASF JSON options preserve protocol property names");
    Check(response.RootElement.GetProperty("ErrorCode").GetString() == "bot_not_allowed", "bridge response serializes under ASF's JSON resolver");
    using HttpResponseMessage writeAttempt = await http.PostAsync(endpoint, new StringContent("{}"));
    Check(writeAttempt.StatusCode == System.Net.HttpStatusCode.MethodNotAllowed, "bridge cannot be used for writes");
} finally {
    await app.StopAsync();
}
Console.WriteLine($"SteamFamilyBridge: {assertions} offline assertions passed; no Steam connection opened.");
