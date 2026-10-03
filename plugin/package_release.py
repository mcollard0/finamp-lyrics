#!/usr/bin/env python3
"""Package an explicit public file allowlist; never install, commit, or publish."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
GUID = "a7d2b5ac-63af-4a71-8197-e4b4528b56c8"
REPO = "https://github.com/mcollard0/finamp-lyrics"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, default=ROOT / "plugin/artifacts/Jellyfin.Plugin.FinampLyrics.dll")
    parser.add_argument("--output", type=Path, default=ROOT / "plugin/artifacts/release")
    args = parser.parse_args()
    version = ET.parse(ROOT / "plugin/FinampLyrics/FinampLyrics.csproj").findtext("PropertyGroup/Version")
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
        "description": "Genius lyrics for Jellyfin music. Requires Linux, Python 3.10+, requests, beautifulsoup4, and configured worker credentials. Targets Jellyfin 10.11.11; not compatible with 12.x. Source: " + REPO,
        "versions": [{"version": version, "targetAbi": "10.11.11",
            "sourceUrl": REPO + "/releases/download/" + tag + "/" + filename,
            "checksum": checksum, "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "changelog": "Developer and repository metadata; bundled Python worker and catalog installation instructions."}]}]
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.output / (filename + ".sha256")).write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + filename + "\n")
    print(json.dumps({"archive": str(archive), "manifest": str(args.output / "manifest.json"), "tag": tag}))


if __name__ == "__main__":
    main()
