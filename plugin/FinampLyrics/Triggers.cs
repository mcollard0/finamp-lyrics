using MediaBrowser.Controller;
using MediaBrowser.Controller.Entities.Audio;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Controller.Session;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

namespace Jellyfin.Plugin.FinampLyrics;

public interface ITrackQueue
{
    void Queue(Guid itemId, string trigger);
}

public static class TriggerPolicy
{
    public static Guid? PlaybackInfoItem(string path, string method, int status, bool authenticated)
    {
        if (!authenticated || status < 200 || status >= 300 || (method != "GET" && method != "POST"))
            return null;
        var parts = path.Trim('/').Split('/');
        return parts.Length == 3 && parts[0].Equals("Items", StringComparison.OrdinalIgnoreCase)
            && parts[2].Equals("PlaybackInfo", StringComparison.OrdinalIgnoreCase)
            && Guid.TryParse(parts[1], out var id) ? id : null;
    }
}

public sealed class PrefetchMiddleware(RequestDelegate next, Func<PluginConfiguration?>? configuration = null)
{
    public async Task InvokeAsync(HttpContext context, ITrackQueue queue)
    {
        await next(context).ConfigureAwait(false);
        var config = configuration?.Invoke() ?? Plugin.Instance?.Configuration;
        if (config is not { Enabled: true, PrefetchEnabled: true }) return;
        var id = TriggerPolicy.PlaybackInfoItem(context.Request.Path.Value ?? "",
            context.Request.Method, context.Response.StatusCode, context.User.Identity?.IsAuthenticated == true);
        if (id.HasValue) queue.Queue(id.Value, "prefetch");
    }
}

public sealed class PrefetchStartupFilter : IStartupFilter
{
    public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next) => app =>
    {
        app.UseMiddleware<PrefetchMiddleware>();
        next(app);
    };
}

public sealed class PlaybackListener(ISessionManager sessions, ITrackQueue queue, Func<PluginConfiguration?>? configuration = null) : IHostedService
{
    public Task StartAsync(CancellationToken token)
    {
        sessions.PlaybackStart += OnPlayback;
        return Task.CompletedTask;
    }
    public Task StopAsync(CancellationToken token)
    {
        sessions.PlaybackStart -= OnPlayback;
        return Task.CompletedTask;
    }
    private void OnPlayback(object? sender, PlaybackProgressEventArgs args)
    {
        if ((configuration?.Invoke() ?? Plugin.Instance?.Configuration) is { Enabled: true, PlaybackEnabled: true } && args.Item is Audio audio)
            queue.Queue(audio.Id, "playback");
    }
}

public sealed class ServiceRegistrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection services, IServerApplicationHost host)
    {
        services.AddSingleton<WorkerService>();
        services.AddSingleton<ITrackQueue>(provider => provider.GetRequiredService<WorkerService>());
        services.AddHostedService(provider => provider.GetRequiredService<WorkerService>());
        services.AddHostedService<PlaybackListener>();
        services.AddSingleton<IStartupFilter, PrefetchStartupFilter>();
    }
}
