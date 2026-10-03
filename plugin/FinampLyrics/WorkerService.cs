using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using System.Threading.Channels;
using Jellyfin.Data.Enums;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Entities.Audio;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Entities;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.FinampLyrics;

public static class WorkerCommand
{
    public static ProcessStartInfo Create(PluginConfiguration config, Guid id, bool enqueueOnly,
        IReadOnlyDictionary<string, string> credentials)
    {
        if (!Path.IsPathFullyQualified(config.PythonPath) || !Path.IsPathFullyQualified(config.ScriptPath)
            || !Path.IsPathFullyQualified(config.StateDirectory) || config.BackgroundCount < 0
            || config.MinimumPlays < 1 || config.DelaySeconds < 0 || config.DelaySeconds > 10
            || config.WorkerTimeoutMinutes < 1)
            throw new InvalidOperationException("Invalid Finamp Lyrics worker configuration");
        // The queue protocol requires these arguments to be controlled by the plugin.
        string[] reserved = ["--server-url", "--state-dir", "--priority", "--upload", "--delay", "--min-plays",
            "--top", "--enqueue-only", "--library", "--title", "--artist", "--list", "--inspect", "--verify",
            "--retry", "--disk-fallback", "--help", "-h"];
        foreach (var argument in config.AdditionalArguments ?? [])
        {
            if (string.IsNullOrWhiteSpace(argument) || argument.Contains('\0')
                || reserved.Contains(argument.Split('=')[0], StringComparer.Ordinal))
                throw new InvalidOperationException("Additional arguments conflict with the Finamp Lyrics queue protocol");
        }
        var info = new ProcessStartInfo(config.PythonPath)
        {
            UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true,
            WorkingDirectory = Path.GetDirectoryName(config.ScriptPath)!
        };
        string[] arguments = [config.ScriptPath, "--server-url", config.ServerUrl, "--state-dir", config.StateDirectory,
            "--priority", id.ToString("N"), "--upload", "--delay", config.DelaySeconds.ToString(CultureInfo.InvariantCulture),
            "--min-plays", config.MinimumPlays.ToString(CultureInfo.InvariantCulture), "--top",
            (enqueueOnly ? 0 : config.BackgroundCount).ToString(CultureInfo.InvariantCulture)];
        foreach (var argument in arguments) info.ArgumentList.Add(argument);
        foreach (var argument in config.AdditionalArguments ?? []) info.ArgumentList.Add(argument);
        if (enqueueOnly) info.ArgumentList.Add("--enqueue-only");
        foreach (var library in config.LibraryIds)
        {
            info.ArgumentList.Add("--library");
            info.ArgumentList.Add(library);
        }
        foreach (var name in new[] { "GENIUS_CLIENT_ACCESS", "JELLYFIN_API_KEY" })
        {
            if (!credentials.TryGetValue(name, out var value) || string.IsNullOrWhiteSpace(value))
                throw new InvalidOperationException("Missing required worker credentials");
            info.Environment[name] = value;
        }
        info.Environment["PYTHONUNBUFFERED"] = "1";
        return info;
    }
}

public sealed class WorkerService(ILibraryManager library, ILogger<WorkerService> logger,
    Func<PluginConfiguration?>? configuration = null) : BackgroundService, ITrackQueue
{
    private readonly Channel<Guid> _queue = Channel.CreateBounded<Guid>(new BoundedChannelOptions(256)
    { SingleReader = true, FullMode = BoundedChannelFullMode.Wait });
    private readonly ConcurrentDictionary<Guid, DateTimeOffset> _recent = new();

    public void Queue(Guid itemId, string trigger)
    {
        try
        {
            var config = configuration?.Invoke() ?? Plugin.Instance?.Configuration;
            logger.LogInformation("Finamp Lyrics received {ItemId} from {Trigger} at epoch {Epoch}",
                itemId, trigger, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0);
            if (config is not { Enabled: true })
            {
                logger.LogInformation("Finamp Lyrics skipped {ItemId}: plugin disabled", itemId);
                return;
            }
            var item = library.GetItemById(itemId);
            if (item is not Audio || !library.GetCollectionFolders(item).OfType<CollectionFolder>().Any(
                    folder => folder.CollectionType == CollectionType.music &&
                    (config.LibraryIds.Length == 0 || config.LibraryIds.Any(id => Guid.TryParse(id, out var selected) && selected == folder.Id))))
            {
                logger.LogInformation("Finamp Lyrics skipped {ItemId}: outside selected music libraries", itemId);
                return;
            }
            var now = DateTimeOffset.UtcNow;
            var previous = _recent.GetOrAdd(itemId, DateTimeOffset.MinValue);
            if (now - previous < TimeSpan.FromSeconds(30) || !_recent.TryUpdate(itemId, now, previous))
            {
                logger.LogInformation("Finamp Lyrics skipped {ItemId} from {Trigger}: duplicate within 30 seconds", itemId, trigger);
                return;
            }
            if (!_queue.Writer.TryWrite(itemId))
            {
                _recent.TryRemove(itemId, out _);
                logger.LogWarning("Finamp Lyrics trigger queue is full; skipped {ItemId}", itemId);
                return;
            }
            foreach (var entry in _recent)
                if (now - entry.Value > TimeSpan.FromMinutes(5)) _recent.TryRemove(entry.Key, out _);
            logger.LogInformation("Finamp Lyrics queued {ItemId} from {Trigger}", itemId, trigger);
        }
        catch (Exception ex)
        {
            // Trigger failures must never fail the playback response.
            logger.LogWarning("Finamp Lyrics trigger failed ({Type})", ex.GetType().Name);
        }
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        logger.LogInformation("Finamp Lyrics worker started; music prefetch and playback triggers registered");
        Task<int>? worker = null;
        var ready = _queue.Reader.WaitToReadAsync(stoppingToken).AsTask();
        var recheck = false;
        Guid lastId = default;
        PluginConfiguration? lastConfig = null;
        try
        {
            while (!stoppingToken.IsCancellationRequested)
            {
                if (worker is not null) await Task.WhenAny(ready, worker).ConfigureAwait(false);
                else await ready.ConfigureAwait(false);
                if (worker?.IsCompleted == true)
                {
                    await ObserveWorkerAsync(worker).ConfigureAwait(false);
                    worker = null;
                    if (recheck && lastConfig is not null)
                    {
                        // Drain again to close the enqueue/worker-exit race, without
                        // adding a second background batch for those same triggers.
                        worker = RunAsync(lastConfig, lastId, false, stoppingToken, 0);
                        recheck = false;
                    }
                    continue;
                }
                if (!await ready.ConfigureAwait(false)) break;
                while (_queue.Reader.TryRead(out var id))
                {
                    var config = configuration?.Invoke() ?? Plugin.Instance?.Configuration;
                    if (config is not { Enabled: true }) continue;
                    try
                    {
                        // Persist new priorities even while the active Python worker is fetching.
                        if (await RunAsync(config, id, true, stoppingToken).ConfigureAwait(false) != 0) continue;
                        lastId = id;
                        lastConfig = config;
                        if (worker is null || worker.IsCompleted)
                        {
                            if (worker is not null) await ObserveWorkerAsync(worker).ConfigureAwait(false);
                            worker = RunAsync(config, id, false, stoppingToken);
                            recheck = false;
                        }
                        else
                        {
                            recheck = true;
                        }
                    }
                    catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { throw; }
                    catch (Exception ex)
                    {
                        logger.LogWarning("Finamp Lyrics could not start worker ({Type}); check configuration and credentials file", ex.GetType().Name);
                    }
                }
                ready = _queue.Reader.WaitToReadAsync(stoppingToken).AsTask();
            }
        }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { }
        finally
        {
            if (worker is not null) await ObserveWorkerAsync(worker).ConfigureAwait(false);
        }
    }

    private async Task ObserveWorkerAsync(Task<int> active)
    {
        try
        {
            await active.ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            logger.LogWarning("Finamp Lyrics worker failed ({Type}); persisted jobs remain recoverable", ex.GetType().Name);
        }
    }

    private async Task<int> RunAsync(PluginConfiguration config, Guid id, bool enqueueOnly, CancellationToken token,
        int? backgroundCount = null)
    {
        var credentials = JsonSerializer.Deserialize<Dictionary<string, string>>(
            await File.ReadAllTextAsync(config.CredentialsFile, token).ConfigureAwait(false))
            ?? throw new InvalidOperationException("Invalid worker credentials file");
        using var process = new Process { StartInfo = WorkerCommand.Create(config, id, enqueueOnly, credentials) };
        if (backgroundCount.HasValue)
        {
            var index = process.StartInfo.ArgumentList.IndexOf("--top");
            process.StartInfo.ArgumentList[index + 1] = backgroundCount.Value.ToString(CultureInfo.InvariantCulture);
        }
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(enqueueOnly ? TimeSpan.FromMinutes(2) : TimeSpan.FromMinutes(config.WorkerTimeoutMinutes));
        if (!process.Start()) throw new InvalidOperationException("Cannot start Python worker");
        logger.LogInformation("Finamp Lyrics launched {Mode} for {ItemId} with PID {Pid}", enqueueOnly ? "enqueue" : "worker", id, process.Id);
        var output = ReadOutputAsync(process.StandardOutput, false);
        var errors = ReadOutputAsync(process.StandardError, true);
        try
        {
            await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
            await process.WaitForExitAsync(CancellationToken.None).ConfigureAwait(false);
            logger.LogWarning("Finamp Lyrics stopped worker {ItemId} on shutdown or timeout; persisted jobs remain recoverable", id);
        }
        await Task.WhenAll(output, errors).ConfigureAwait(false);
        logger.LogInformation("Finamp Lyrics {Mode} finished with exit code {ExitCode}", enqueueOnly ? "enqueue" : "worker", process.ExitCode);
        return process.ExitCode;
    }

    private async Task ReadOutputAsync(StreamReader stream, bool error)
    {
        while (await stream.ReadLineAsync().ConfigureAwait(false) is { } line)
        {
            if (error) logger.LogWarning("Finamp Lyrics Python: {Result}", line);
            else logger.LogInformation("Finamp Lyrics Python: {Result}", line);
        }
    }
}
