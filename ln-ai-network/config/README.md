# config/

Static configuration templates used by the boot scripts.

| File | Purpose |
|------|---------|
| `cln/lightning.conf.tpl` | Core Lightning node configuration template. Copied and populated per-node during `0.2.control_plane_boot.sh`. |
| `network.defaults.yml` | Default network topology parameters (node count, channel sizes, fee policies). |

These files are **not** modified at runtime. Per-node configs are generated into `runtime/` during boot.
