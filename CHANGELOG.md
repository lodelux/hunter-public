# Changelog

## Unreleased

### Changed

- Hunter now runs exclusively as a React web application backed by FastAPI.
- Local development uses one `npm run dev` launcher with a same-origin API proxy.
- Resume and plugin selection upload browser files to the Hunter host.
- Gmail OAuth supports both localhost and the private hosted web callback.

### Removed

- Retired native application packaging, build scripts, dependencies, and assets.
- The obsolete marketing site, translated upstream READMEs, and generated screenshots.
- The stale upstream GitHub feedback screen and its browser-exposed token configuration.
