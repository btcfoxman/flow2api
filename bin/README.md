# Runtime binaries

Place the Linux executable `gwt-video` in this directory for Docker deployments:

```bash
chmod +x bin/gwt-video
```

The compose files mount this directory read-only into the container at `/app/bin`.
The binary itself is ignored by Git.
