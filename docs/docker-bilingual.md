# Bilingual Docker image

The standard `lscr.io/linuxserver/bazarr` image is maintained in the separate
LinuxServer Docker repository. This fork does not publish into the LinuxServer
namespace. It builds a compatible image from this repository and publishes it
to the fork owner’s GitHub Container Registry namespace.

The image keeps the LinuxServer Alpine base and s6 service layout, builds the
frontend in a dedicated Node stage, installs the Bazarr runtime dependencies,
and publishes one manifest for `linux/amd64` and `linux/arm64`. The workflow is
`.github/workflows/docker-bilingual.yml`.

## Publish

Create a Git tag beginning with `v`, such as `v1.0.0-bilingual.1`, and push it.
The workflow publishes an image with the same tag. It also supports a manual
run for a `development` tag while the fork is being tested.

Use an immutable version tag for NAS deployments:

```text
ghcr.io/OWNER/bazarr-bilingual:v1.0.0-bilingual.1
```

Replace `OWNER` with the GitHub account or organization that owns the fork.
The first pull may require logging in to GHCR if the package is private.

## NAS rollout

Back up the Bazarr `/config` directory before the first run. The first startup
adds the bilingual requirement columns to the subtitle index; the migration is
additive, but the backup is the rollback safety point.

Keep the existing Sonarr/Radarr paths and environment values. A minimal Compose
service is:

```yaml
services:
  bazarr:
    image: ghcr.io/OWNER/bazarr-bilingual:v1.0.0-bilingual.1
    container_name: bazarr
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - /path/to/bazarr/config:/config
      - /path/to/movies:/movies
      - /path/to/tv:/tv
    ports:
      - 6767:6767
    restart: unless-stopped
```

On the NAS, update the image tag and run:

```text
docker compose pull bazarr
docker compose up -d --force-recreate bazarr
```

Do not run the old and new containers against the same `/config` directory at
the same time. The image stores generated bilingual files beside the media
file, using names such as `Movie.zh-en.srt` (with `.forced` or `.hi` when
applicable).

## Rollback

Stop the container, change the Compose image back to the last known-good
immutable tag (or digest), then recreate it:

```text
docker compose down bazarr
docker compose pull bazarr
docker compose up -d bazarr
```

Because the schema migration is intentionally additive and leaves Bazarr's
existing subtitle uniqueness constraints unchanged, an older Bazarr build
should normally be able to ignore the new columns. If it cannot start after a
rollback, stop it and restore the `/config` backup made before the migration.

Keep the previous image tag available until the new image has completed a
real subtitle search and a library refresh on the NAS.
