# Build, verify, and publish

The distribution route is a third-party Jellyfin catalog. Inclusion in Jellyfin's
documentation or official catalog requires maintainer review. These instructions
prepare two distinct builds; publishing is a separate step after user testing.

| Server | Framework | Plugin version | Catalog targetAbi |
| --- | --- | --- | --- |
| Jellyfin 10.11.11 | .NET 9 | 1.0.3.0 | 10.11.11 |
| Jellyfin 12.0 / 12.1 | .NET 10 | 2.0.0.0 | 12.0 |

Version 2.0.0.0 must remain above the 10.11 plugin version. A combined catalog
advertises both; Jellyfin filters by its server version and selects a compatible
package. Do not replace a 10.11 DLL with the 12 DLL on a 10.11 server.

## Build both packages

Install the .NET 10 SDK and the .NET 9/10 runtimes needed to run the checks.
Use reference assemblies from each official container image. From the repository
root, these commands create temporary stopped containers and copy only references
into ignored directories; they do not run or change a Jellyfin server:

```bash
mkdir -p plugin/artifacts/server-reference/usr/lib/jellyfin plugin/artifacts/server-reference-12
jf10_reference=$(docker create jellyfin/jellyfin:10.11.11)
docker cp "$jf10_reference:/jellyfin/." plugin/artifacts/server-reference/usr/lib/jellyfin/
docker rm "$jf10_reference"
jf12_reference=$(docker create jellyfin/jellyfin:12.0)
docker cp "$jf12_reference:/jellyfin/." plugin/artifacts/server-reference-12/
docker rm "$jf12_reference"
dotnet build plugin/FinampLyrics/FinampLyrics.csproj -c Release -p:JellyfinLine=10.11 -o plugin/artifacts/build-10.11
dotnet run --project plugin/Checks/Checks.csproj -c Release -p:JellyfinLine=10.11
dotnet build plugin/FinampLyrics/FinampLyrics.csproj -c Release -p:JellyfinLine=12 -o plugin/artifacts/build-12
dotnet run --project plugin/Checks/Checks.csproj -c Release -p:JellyfinLine=12
python3 -m unittest -q test_lyrics_fetcher.py
python3 plugin/package_release.py --server-line 10.11
python3 plugin/package_release.py --server-line 12
```

`JellyfinInstallDir` can override the reference directory via `-p:`. Packaging
reads ECMA assembly metadata without executing the supplied DLL and rejects a
name/version/framework mismatch. Rebuild first. The ZIP allowlist contains the
DLL, Python worker, requirements, license, content notice and catalog installation
instructions. No credentials, media, state, logs or server binaries are packaged.
The ZIP is reproducible for unchanged inputs; generated timestamps live only in
the catalog. Artifacts remain ignored by git.

Prepare the combined catalog in an ignored staging directory:

```bash
python3 - <<'PY'
import json
from pathlib import Path
root = Path('plugin/artifacts')
catalog = json.loads((root / 'release-12/manifest.json').read_text())
older = json.loads((root / 'release-10.11/manifest.json').read_text())
assert catalog[0]['guid'] == older[0]['guid']
catalog[0]['versions'] += older[0]['versions']
output = root / 'catalog'
output.mkdir(exist_ok=True)
(output / 'manifest.json').write_text(json.dumps(catalog, indent=2) + '\n')
PY
```

## Integration tests

Build disposable runtimes with Python dependencies:

```bash
docker build --build-arg JELLYFIN_VERSION=10.11.11 -t finamp-lyrics-smoke-runtime:10.11.11 plugin/smoke-runtime
docker build --build-arg JELLYFIN_VERSION=12.0 -t finamp-lyrics-smoke-runtime:12.0 plugin/smoke-runtime
docker build --build-arg JELLYFIN_VERSION=12.1 -t finamp-lyrics-smoke-runtime:12.1 plugin/smoke-runtime
python3 plugin/catalog_smoke.py --previous-archive /path/to/finamp-lyrics-1.0.1.0.zip
python3 plugin/catalog_smoke.py \
  --server-line 12 --release plugin/artifacts/release-12 \
  --server-version 12.0 --image finamp-lyrics-smoke-runtime:12.0 \
  --upgrade-from-image finamp-lyrics-smoke-runtime:10.11.11 \
  --previous-archive /path/to/finamp-lyrics-1.0.1.0.zip
python3 plugin/catalog_smoke.py \
  --server-line 12 --release plugin/artifacts/release-12 \
  --server-version 12.1 --image finamp-lyrics-smoke-runtime:12.1 \
  --upgrade-from-image finamp-lyrics-smoke-runtime:10.11.11 \
  --previous-archive /path/to/finamp-lyrics-1.0.1.0.zip
```

Supply the authentic old ZIP; it is not committed. Maintainers can download it
with `gh release download plugin-v1.0.1.0 --pattern 'finamp-lyrics-1.0.1.0.zip'`.
The runtime image build downloads system dependencies. Fixture tests run on an
internal Docker network, publish no host ports, and use synthetic audio and
temporary credentials. Docker access grants substantial host privileges.

Each test installs 1.0.1.0, saves its bundled worker path and launches its real
worker. The server-migration tests quarantine the old incompatible DLL before
starting 12 with the same disposable data. They then install the new ZIP via
Jellyfin, verify Active status, settings, exact extracted files, preserved old
lyrics and a new plugin-triggered worker upload. The saved old worker path must
resolve to the current bundle. Packaging must reject the old DLL without output.
Containers, network and temporary data are removed. Result JSON is in each
ignored release directory as `catalog-smoke-SERVER.json`.

Validated: 39 C# checks per server line; 72 Python tests; catalog upgrade on
10.11.11; server migration to 12.0 and 12.1 with fixture lyric retrieval. A separate
12.1 migration test also fetched and stored real Genius lyrics on a copied
authorized library recording. See [JF12.md](JF12.md) for live test options and
host upgrade instructions. Finamp display and other installed plugins still
require user testing. The existing public draft 1.0.1.0 assets are unchanged.

## Publish after user testing

Review the files and ZIP contents, then commit with your own message and push
the reviewed source. The commands below show the publication workflow.
Use the reviewed source commit SHA for `--target` in place of `main` if needed.

```bash
gh release create plugin-v1.0.3.0 \
  plugin/artifacts/release-10.11/finamp-lyrics-1.0.3.0.zip \
  plugin/artifacts/release-10.11/finamp-lyrics-1.0.3.0.zip.sha256 \
  --draft --target main --title 'Finamp Lyrics 1.0.3.0 (Jellyfin 10.11.11)' \
  --notes-file plugin/CATALOG_INSTALL.md
gh release create plugin-v2.0.0.0 \
  plugin/artifacts/release-12/finamp-lyrics-2.0.0.0.zip \
  plugin/artifacts/release-12/finamp-lyrics-2.0.0.0.zip.sha256 \
  --draft --target main --title 'Finamp Lyrics 2.0.0.0 (Jellyfin 12.x)' \
  --notes-file plugin/CATALOG_INSTALL.md
```

Verify uploaded ZIP checksums and source versions before publishing:

```bash
gh release edit plugin-v1.0.3.0 --draft=false
gh release edit plugin-v2.0.0.0 --draft=false
cp plugin/artifacts/catalog/manifest.json manifest.json
git add manifest.json
# Commit with your own message, then push the reviewed manifest.
```

Publish both releases before advertising the combined manifest so its source URLs
exist. Retain older compatible versions in future catalogs; merge any existing
published versions before replacing the public manifest. Never change a published
ZIP without changing its version/checksum. Verify the public manifest and install
it on a separate server before requesting a Jellyfin directory listing.

## Request a third-party repository listing

Fork `jellyfin/jellyfin.org` and add the entry to `ThirdPartyRepositories` in
`src/data/pluginRepositories.ts`:

```typescript
{
  id: 'gh:mcollard0/finamp-lyrics',
  name: 'Finamp Lyrics',
  url: 'https://raw.githubusercontent.com/mcollard0/finamp-lyrics/main/manifest.json',
  includes: { 'Finamp Lyrics': 'https://github.com/mcollard0/finamp-lyrics' }
},
```

Use `gh repo fork jellyfin/jellyfin.org --clone`, create a branch, add the entry,
run that project's documented checks, and commit/push with your own message.
Submit with `gh pr create --repo jellyfin/jellyfin.org`, using the reviewed text
in [LISTING_PR.md](LISTING_PR.md). Describe both builds and Python/Genius setup.
This is a directory listing; official catalog inclusion is a separate maintainer
decision. See [Jellyfin plugin documentation](https://jellyfin.org/docs/general/server/plugins/).
