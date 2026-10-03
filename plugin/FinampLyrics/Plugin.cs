using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.FinampLyrics;

public sealed class PluginConfiguration : BasePluginConfiguration
{
    public bool Enabled { get; set; } = true;
    public bool PrefetchEnabled { get; set; } = true;
    public bool PlaybackEnabled { get; set; } = true;
    public string PythonPath { get; set; } = "/usr/bin/python3";
    public string ScriptPath { get; set; } = "/var/lib/jellyfin/finamp-lyrics/lyrics_fetcher.py";
    public string[] AdditionalArguments { get; set; } = [];
    public string StateDirectory { get; set; } = "/var/lib/jellyfin/finamp-lyrics/state";
    public string CredentialsFile { get; set; } = "/var/lib/jellyfin/finamp-lyrics/credentials.json";
    public string ServerUrl { get; set; } = "http://localhost:8096/";
    public int BackgroundCount { get; set; } = 3;
    public int MinimumPlays { get; set; } = 2;
    public double DelaySeconds { get; set; } = 0.5;
    public int WorkerTimeoutMinutes { get; set; } = 15;
    public string[] LibraryIds { get; set; } = [];
}

public sealed class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    public Plugin(IApplicationPaths paths, IXmlSerializer serializer) : base(paths, serializer)
    {
        Instance = this;
    }
    public static Plugin? Instance { get; private set; }
    public override string Name => "Finamp Lyrics";
    public override string Description => "Queue Genius lyric checks on music metadata prefetch and playback. Developed by mcollard0; source: https://github.com/mcollard0/finamp-lyrics.";
    public override Guid Id => Guid.Parse("a7d2b5ac-63af-4a71-8197-e4b4528b56c8");
    public IEnumerable<PluginPageInfo> GetPages() =>
    [new PluginPageInfo { Name = "Finamp Lyrics", EmbeddedResourcePath = "Jellyfin.Plugin.FinampLyrics.config.html" }];
}
