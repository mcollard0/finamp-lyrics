#!/usr/bin/env python3
"""Package an explicit public file allowlist; never install, commit, or publish."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import subprocess
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
GUID = "a7d2b5ac-63af-4a71-8197-e4b4528b56c8"
REPO = "https://github.com/mcollard0/finamp-lyrics"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-line", choices=["10.11", "12"], default="10.11")
    parser.add_argument("--dll", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.dll = args.dll or ROOT / "plugin/artifacts" / ("build-" + args.server_line) / "Jellyfin.Plugin.FinampLyrics.dll"
    args.output = args.output or ROOT / "plugin/artifacts" / ("release-" + args.server_line)
    version = subprocess.check_output(["dotnet", "msbuild", str(ROOT / "plugin/FinampLyrics/FinampLyrics.csproj"),
        "-getProperty:Version", "-p:JellyfinLine=" + args.server_line], text=True).strip()
    target_abi = "12.0" if args.server_line == "12" else "10.11.11"
    tag = "plugin-v" + version
    filename = "finamp-lyrics-" + version + ".zip"
    files = {
        "Jellyfin.Plugin.FinampLyrics.dll": args.dll,
        "worker/lyrics_fetcher.py": ROOT / "lyrics_fetcher.py",
        "worker/requirements.txt": ROOT / "requirements.txt",
        "INSTALL.md": ROOT / "plugin/CATALOG_INSTALL.md",
        "LICENSE": ROOT / "LICENSE",
        "THIRD_PARTY.md": ROOT / "THIRD_PARTY.md",
    }
    for path in files.values():
        if not path.is_file():
            parser.error("Missing release input: " + str(path))
    # Read ECMA assembly metadata without loading or executing the supplied DLL.
    result = subprocess.run(["dotnet", "run", "--project", str(ROOT / "plugin/AssemblyInfo/AssemblyInfo.csproj"),
        "--configuration", "Release", "--", str(args.dll.resolve())],
        text=True, capture_output=True, check=True)
    assembly = json.loads(result.stdout.strip().splitlines()[-1])
    if assembly["name"] != "Jellyfin.Plugin.FinampLyrics" or assembly["version"] != version or assembly["framework"] != (".NETCoreApp,Version=v10.0" if args.server_line == "12" else ".NETCoreApp,Version=v9.0"):
        parser.error(f"DLL identity/version mismatch: expected Jellyfin.Plugin.FinampLyrics {version}; got {assembly['name']} {assembly['version']} {assembly['framework']}")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / filename
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, path.read_bytes())
    checksum = hashlib.md5(archive.read_bytes()).hexdigest()  # Jellyfin catalog format
    manifest = [{"guid": GUID, "name": "Finamp Lyrics", "owner": "mcollard0",
        "category": "Metadata", "overview": "Fetch music lyrics on prefetch and playback",
        "description": "Genius lyrics for Jellyfin music. Requires Linux, Python 3.10+, requests, beautifulsoup4, and configured worker credentials. Separate builds for Jellyfin 10.11.11 and 12.x. Source: " + REPO,
        "versions": [{"version": version, "targetAbi": target_abi,
            "sourceUrl": REPO + "/releases/download/" + tag + "/" + filename,
            "checksum": checksum, "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "changelog": "Separate Jellyfin 10.11/.NET 9 and 12.x/.NET 10 builds; modern Jellyfin authentication and upgrade-safe bundled workers. Migration and lyric-upload tests on 12.0 and 12.1."}]}]
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.output / (filename + ".sha256")).write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + filename + "\n")
    print(json.dumps({"archive": str(archive), "manifest": str(args.output / "manifest.json"), "tag": tag}))


if __name__ == "__main__":
    main()
