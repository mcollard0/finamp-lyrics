# Release and Jellyfin listing

The supported distribution route is a third-party plugin catalog. Jellyfin
accepts custom repository manifests; inclusion in its documentation or official
catalog requires maintainer review and is not automatic.

Build and check from the repository root against Jellyfin 10.11.11 reference
assemblies (see plugin/README.md), then run:

```bash
python3 plugin/package_release.py
```

The allowlist ZIP contains the plugin DLL, worker, requirements, license, content
notice and setup instructions. Generated artifacts stay ignored. Review the ZIP
and commit source changes with your own message; no command below commits for you.

After pushing the reviewed source commit, create a draft release:

```bash
gh release create plugin-v1.0.1.0 \
  plugin/artifacts/release/finamp-lyrics-1.0.1.0.zip \
  plugin/artifacts/release/finamp-lyrics-1.0.1.0.zip.sha256 \
  --draft --target main --title 'Finamp Lyrics 1.0.1.0 (Jellyfin 10.11.11)' \
  --notes-file plugin/CATALOG_INSTALL.md
```

Verify the release ZIP/version and publish when ready:

```bash
gh release edit plugin-v1.0.1.0 --draft=false
cp plugin/artifacts/release/manifest.json manifest.json
git add manifest.json
# Write your own commit message, then push the manifest.
```

Publish the release before advertising the manifest so its sourceUrl exists.
Never change the release ZIP after publishing its checksum. Future manifests
should retain older versions as new versions are added.

Test catalog installation on a separate Jellyfin 10.11.11 instance before
requesting a directory listing. Do not claim 12.x support without a new build
and integration testing. For a listing, fork jellyfin/jellyfin.org and edit
`src/data/pluginRepositories.ts` in `ThirdPartyRepositories` to add the
repository URL and plugin name. Optionally add a description to
`docs/general/server/plugins/index.mdx`. Submit a pull request explaining dependencies
and tested compatibility. A published, working manifest is needed first.

Useful upstream references:
- https://jellyfin.org/docs/general/server/plugins/
- https://jellyfin.org/posts/plugin-updates/
- https://github.com/jellyfin/jellyfin.org/blob/master/docs/general/server/plugins/index.mdx
- https://github.com/jellyfin/jellyfin-meta-plugins

Official catalog inclusion is a separate maintainer decision. Ask maintainers
about their requirements before treating a third-party listing as official.

Suggested repository entry (after publishing and testing):

```typescript
{
  id: 'gh:mcollard0/finamp-lyrics',
  name: 'Finamp Lyrics',
  url: 'https://raw.githubusercontent.com/mcollard0/finamp-lyrics/main/manifest.json',
  includes: { 'Finamp Lyrics': 'https://github.com/mcollard0/finamp-lyrics' }
},
```

Fork and clone with `gh repo fork jellyfin/jellyfin.org --clone`, create a branch,
add the entry, run the website checks documented by that project, then commit and
push your branch. Use `gh pr create --repo jellyfin/jellyfin.org` to submit it.
Describe Linux/Python requirements, Genius extraction, and the 10.11.11-only build.

## Repeatable catalog verification

Pull `jellyfin/jellyfin:10.11.11` and `python:3.12-alpine`, then run:

```bash
python3 plugin/catalog_smoke.py
```

This creates disposable Docker containers on an internal network, with no
production media or credentials. It serves the same release ZIP through a local
catalog, completes a fresh server wizard with a temporary account, installs via
Jellyfin's package API, compares extracted files byte for byte, restarts, and
checks Active status/version and developer links on the settings page. Test
containers/network and temporary server data are removed afterwards. It requires
Docker access, which grants substantial host privileges; review the script first.
The default test image tag must resolve to Jellyfin 10.11.11; the script checks it.

Verified October 3, 2026: catalog install, ZIP extraction, Active status for
1.0.1.0, and embedded settings links passed on a fresh Jellyfin 10.11.11 Docker
instance. The test did not exercise Genius retrieval, media playback, or 12.x.
Result JSON is in ignored `plugin/artifacts/release/catalog-smoke.json`.
A draft listing PR body is in `plugin/LISTING_PR.md`; submit only after the release
and manifest are public and the public manifest URL has been checked.
