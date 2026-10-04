using System.Reflection;
using System.Security.Claims;
using System.Text.Json;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.FinampLyrics;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Audio;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Session;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Logging;

var assertions = 0;
void Check(bool ok, string message)
{
    assertions++;
    if (!ok) throw new Exception(message);
}
var id = Guid.NewGuid();
var config = new PluginConfiguration();
Check(config.PythonPath == "/usr/bin/python3" && config.ScriptPath == ""
    && config.AdditionalArguments.Length == 0, "default executable, script and extra arguments");
Check(typeof(Plugin).Assembly.GetManifestResourceNames().Contains("Jellyfin.Plugin.FinampLyrics.config.html"), "dashboard settings embedded");
Check(typeof(Plugin).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>().Any(a => a.Key == "Developer" && a.Value == "mcollard0"), "developer metadata");
Check(typeof(Plugin).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>().Any(a => a.Key == "RepositoryUrl" && a.Value == "https://github.com/mcollard0/finamp-lyrics"), "repository metadata");
var route = $"/Items/{id:N}/PlaybackInfo";
Check(TriggerPolicy.PlaybackInfoItem(route, "GET", 200, true) == id, "prefetch GET");
Check(TriggerPolicy.PlaybackInfoItem(route, "POST", 200, true) == id, "prefetch POST");
Check(TriggerPolicy.PlaybackInfoItem(route, "GET", 403, true) is null, "deny unauthorized response");
Check(TriggerPolicy.PlaybackInfoItem(route, "GET", 200, false) is null, "deny unauthenticated request");
Check(TriggerPolicy.PlaybackInfoItem(route, "DELETE", 200, true) is null, "ignore other verbs");
Check(TriggerPolicy.PlaybackInfoItem("/Items/not-a-guid/PlaybackInfo", "GET", 200, true) is null, "reject malformed item");
Check(TriggerPolicy.PlaybackInfoItem(route + "/other", "GET", 200, true) is null, "exact endpoint only");
var capture = new CaptureQueue();
var context = new DefaultHttpContext();
context.Request.Path = route;
context.Request.Method = "GET";
context.User = new ClaimsPrincipal(new ClaimsIdentity([new Claim("sub", "test")], "fixture"));
var downstream = false;
await new PrefetchMiddleware(ctx => { downstream = true; Check(capture.Items.Count == 0, "queue after downstream"); return Task.CompletedTask; }, () => config).InvokeAsync(context, capture);
Check(downstream && capture.Items.Single().Item1 == id, "middleware queues successful prefetch");
config.PrefetchEnabled = false;
await new PrefetchMiddleware(_ => Task.CompletedTask, () => config).InvokeAsync(context, capture);
Check(capture.Items.Count == 1, "disabled prefetch ignored");

var activeDirectory = "/tmp/catalog/plugins/Finamp Lyrics_1.0.2.0";
var currentWorker = activeDirectory + "/worker/lyrics_fetcher.py";
Check(WorkerCommand.ResolveScriptPath("", activeDirectory) == currentWorker, "bundled worker follows active assembly directory");
Check(WorkerCommand.ResolveScriptPath("/tmp/catalog/plugins/Finamp Lyrics_1.0.1.0/worker/lyrics_fetcher.py", activeDirectory) == currentWorker,
    "old catalog worker override follows upgrade after old directory removal");
Check(WorkerCommand.ResolveScriptPath("/srv/custom/lyrics_fetcher.py", activeDirectory) == "/srv/custom/lyrics_fetcher.py", "custom worker override preserved");
Check(WorkerCommand.ResolveScriptPath("/elsewhere/Finamp Lyrics_1.0.1.0/worker/lyrics_fetcher.py", activeDirectory)
    == "/elsewhere/Finamp Lyrics_1.0.1.0/worker/lyrics_fetcher.py", "other installation override preserved");
Check(WorkerCommand.ResolveScriptPath("/tmp/catalog/plugins/Finamp Lyrics_custom/worker/lyrics_fetcher.py", activeDirectory)
    == "/tmp/catalog/plugins/Finamp Lyrics_custom/worker/lyrics_fetcher.py", "non-version custom directory preserved");
try { WorkerCommand.ResolveScriptPath("../worker.py", activeDirectory); Check(false, "relative override rejected"); }
catch (InvalidOperationException) { Check(true, "relative override rejected"); }

var credentials = new Dictionary<string, string> { ["GENIUS_CLIENT_ACCESS"] = "fixture-genius", ["JELLYFIN_API_KEY"] = "fixture-jellyfin" };
config.ScriptPath = "/tmp/worker with spaces.py";
config.LibraryIds = ["library with spaces"];
config.AdditionalArguments = ["--retry-429", "--ranking-minutes", "30"];
var command = WorkerCommand.Create(config, id, true, credentials);
Check(!command.UseShellExecute && command.ArgumentList[0] == config.ScriptPath, "shell-free arguments preserve spaces");
Check(command.ArgumentList.Contains("--enqueue-only"), "enqueue-only promoted priorities");
Check(command.Environment["GENIUS_CLIENT_ACCESS"] == "fixture-genius" && !command.ArgumentList.Contains("fixture-genius"), "credentials in environment only");
Check(command.ArgumentList.Contains("library with spaces"), "library argument preserved");
Check(command.FileName == config.PythonPath && command.ArgumentList.Contains("--retry-429")
    && command.ArgumentList[command.ArgumentList.IndexOf("--ranking-minutes") + 1] == "30", "selected executable and extra arguments used");
Check(command.ArgumentList[command.ArgumentList.IndexOf("--top") + 1] == "0", "enqueue extra arguments retain queue protocol");
config.AdditionalArguments = ["--top=65535"];
try { WorkerCommand.Create(config, id, false, credentials); Check(false, "conflicting extra arguments rejected"); }
catch (InvalidOperationException) { Check(true, "conflicting extra arguments rejected"); }
config.AdditionalArguments = ["literal ; argument $(not-a-command)"];
Check(WorkerCommand.Create(config, id, false, credentials).ArgumentList.Contains(config.AdditionalArguments[0]), "shell text remains a single literal argument");
config.AdditionalArguments = [];
config.DelaySeconds = -0.1;
try { WorkerCommand.Create(config, id, false, credentials); Check(false, "invalid delay rejected"); }
catch (InvalidOperationException) { Check(true, "invalid delay rejected"); }
config = new PluginConfiguration();

EventHandler<PlaybackProgressEventArgs>? playback = null;
var sessions = Proxy.Create<ISessionManager>((method, args) =>
{
    if (method.Name == "add_PlaybackStart") playback += (EventHandler<PlaybackProgressEventArgs>)args![0]!;
    else if (method.Name == "remove_PlaybackStart") playback -= (EventHandler<PlaybackProgressEventArgs>)args![0]!;
    else throw new Exception(method.Name);
    return null;
});
capture.Items.Clear();
var listener = new PlaybackListener(sessions, capture, () => config);
await listener.StartAsync(default);
playback!(null, new PlaybackProgressEventArgs { Item = new Audio { Id = id } });
Check(capture.Items.Count == 1 && capture.Items[0] == (id, "playback"), "playback event dispatch");
playback!(null, new PlaybackProgressEventArgs { Item = new Folder { Id = Guid.NewGuid() } });
Check(capture.Items.Count == 1, "non-audio ignored");
config.PlaybackEnabled = false;
playback!(null, new PlaybackProgressEventArgs { Item = new Audio { Id = id } });
Check(capture.Items.Count == 1, "disabled playback ignored");
await listener.StopAsync(default);
Check(playback is null, "unsubscribe on shutdown");

// Exercise real child processes, music scoping, de-bouncing, and queue handoff.
var temp = Path.Combine(Path.GetTempPath(), "finamp-plugin-check-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(temp);
try
{
    var log = Path.Combine(temp, "calls.jsonl");
    var script = Path.Combine(temp, "fixture.py");
    await File.WriteAllTextAsync(script, "import sys,json,time,os\nfrom pathlib import Path\na=sys.argv[1:]\nid=a[a.index('--priority')+1]\ne='--enqueue-only' in a\nwith Path(__file__).with_name('calls.jsonl').open('a') as f: f.write(json.dumps({'id':id,'enqueue':e,'top':a[a.index('--top')+1],'credentials':bool(os.getenv('GENIUS_CLIENT_ACCESS'))})+'\\n')\nif not e: time.sleep(0.7)\n");
    var secretFile = Path.Combine(temp, "credentials.json");
    await File.WriteAllTextAsync(secretFile, JsonSerializer.Serialize(credentials));
    config = new PluginConfiguration { ScriptPath = script, StateDirectory = temp, CredentialsFile = secretFile,
        AdditionalArguments = ["--retry-429"] };
    var music = new CollectionFolder { Id = Guid.NewGuid(), CollectionType = CollectionType.music };
    var folder = music;
    var second = Guid.NewGuid();
    BaseItem track = new Audio { Id = id };
    var library = Proxy.Create<ILibraryManager>((method, args) => method.Name switch
    {
        "GetItemById" => new Audio { Id = (Guid)args![0]! },
        "GetCollectionFolders" => method.ReturnType == typeof(List<CollectionFolder>)
            ? new List<CollectionFolder> { folder } : method.ReturnType == typeof(List<Folder>)
            ? new List<Folder> { folder } : new[] { folder },
        _ => throw new Exception(method.Name)
    });
    using var logs = LoggerFactory.Create(builder => builder.AddConsole());
    using var service = new WorkerService(library, logs.CreateLogger<WorkerService>(), () => config);
    await service.StartAsync(default);
    service.Queue(id, "prefetch");
    service.Queue(id, "playback");
    var deadline = DateTime.UtcNow.AddSeconds(5);
    while ((!File.Exists(log) || File.ReadAllLines(log).Length < 2) && DateTime.UtcNow < deadline) await Task.Delay(20);
    Check(File.Exists(log) && File.ReadAllLines(log).Length == 2, "single enqueue/worker despite duplicate triggers");
    service.Queue(second, "playback");
    await Task.Delay(1800);
    var calls = File.ReadAllLines(log).Select(line => JsonSerializer.Deserialize<JsonElement>(line)).ToArray();
    Check(calls.Count(c => c.GetProperty("enqueue").GetBoolean()) == 2, "new priority enqueued while worker active");
    Check(calls.Any(c => c.GetProperty("id").GetString() == second.ToString("N") && !c.GetProperty("enqueue").GetBoolean() && c.GetProperty("top").GetString() == "0"), "handoff drains without duplicate background batch");
    Check(calls.All(c => c.GetProperty("credentials").GetBoolean()), "child receives credentials");
    folder = new CollectionFolder { Id = Guid.NewGuid(), CollectionType = CollectionType.books };
    service.Queue(Guid.NewGuid(), "playback");
    await Task.Delay(100);
    Check(File.ReadAllLines(log).Length == calls.Length, "audio outside music ignored");
    folder = music;
    config.LibraryIds = [Guid.NewGuid().ToString("N")];
    service.Queue(Guid.NewGuid(), "prefetch");
    await Task.Delay(100);
    Check(File.ReadAllLines(log).Length == calls.Length, "excluded music library ignored");
    await service.StopAsync(default);
}
finally { Directory.Delete(temp, true); }
Console.WriteLine($"Passed {assertions} plugin checks");

public class Proxy : DispatchProxy
{
    public Func<MethodInfo, object?[]?, object?> Handler { get; set; } = null!;
    protected override object? Invoke(MethodInfo? method, object?[]? args) => Handler(method!, args);
    public static T Create<T>(Func<MethodInfo, object?[]?, object?> handler) where T : class
    {
        var value = Create<T, Proxy>();
        ((Proxy)(object)value).Handler = handler;
        return value;
    }
}
public sealed class CaptureQueue : ITrackQueue
{
    public List<(Guid, string)> Items { get; } = [];
    public void Queue(Guid id, string trigger) => Items.Add((id, trigger));
}
