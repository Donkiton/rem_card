# Vendored AntV Infographic

- Package: `@antv/infographic`
- Version: `0.2.20`
- License: MIT (see `LICENSE`)
- Upstream: https://github.com/antvis/Infographic
- Registry tarball: https://registry.npmjs.org/@antv/infographic/-/infographic-0.2.20.tgz
- npm integrity: `sha512-XC1YXpQu6lJ3ZODUS7qBtai5cA8rUrzpegiOHjeCLElzJ6fki+N3yeosIKoYCUNcRCCUovgeSyMMIzzf2ndN2w==`
- Registry tarball SHA-1: `3829ece204aa8ea810d7c91db34de78c510a7a56`
- Downloaded tarball SHA-256: `cb82ca6a37be7c21ab3d2e5caa495cc6546441d30c214f261d4c732db465696e`
- Vendored bundle SHA-256: `2890e658d6018b9ab385b4da9775b917a99e113ea333c59f03e538290e302b13`

The 0.2.20 registry package declares `dist/infographic.umd.min.js` as its
jsDelivr/unpkg entry, while the tarball contains the same UMD build as
`dist/infographic.min.js`. RemCard vendors that file under the declared UMD
name `infographic.umd.min.js`.

The bundle is executed only inside the offline infographic helper. HTTP(S),
FTP and WebSocket requests are blocked there, and RemCard does not provide
remote icon or font references.
