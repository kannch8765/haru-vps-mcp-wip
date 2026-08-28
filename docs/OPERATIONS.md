# Operator runbook

This runbook covers reusable Haru VPS MCP operations without assuming a particular host, private account, domain, or workspace layout.

For the separate workspace backend, see [`WORKSPACE-BACKENDS.md`](WORKSPACE-BACKENDS.md). For private remote access, see [`SECURE-TUNNEL.md`](SECURE-TUNNEL.md).

## Install under a dedicated service account

Treat Haru MCP as a privileged gateway. Install it into an owner-controlled prefix/virtual environment and run it as a dedicated unprivileged service account rather than root.

A typical layout is:

```text
/opt/haru-mcp/                    application + virtual environment
/etc/haru-mcp/haru-mcp.env        non-public service configuration
/var/lib/haru-mcp/                 optional runtime state/home
```

The service account should not own the application binaries or `/etc` configuration. Grant only the runtime access the gateway actually needs.

`deploy/haru-mcp.service.example` is a starting point, not a universal unit. Review its hardening against your distribution and deployment layout before installation.

## Configuration boundary

The gateway itself stays on loopback:

```text
HARU_MCP_HOST=127.0.0.1
HARU_MCP_PORT=8765
HARU_MCP_PATH=/mcp
```

The delegated backend URLs must also be explicit loopback HTTP endpoints. See `deploy/haru-mcp.env.example` for the default reference values.

`HARU_MCP_PUBLIC_HOST` and `HARU_MCP_PUBLIC_ORIGIN` only extend the transport allowlist when an authenticated/private ingress forwards requests to Haru. They do not authenticate a client.

Keep secrets out of the repository. Prefer a root-owned environment/secret file with the narrowest permissions compatible with the service manager and runtime.

## Start, restart, status, and logs

With a systemd unit named `haru-mcp.service`:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now haru-mcp.service
sudo systemctl status haru-mcp.service
sudo journalctl -u haru-mcp.service --since today
```

After a configuration or binary change:

```bash
sudo systemctl restart haru-mcp.service
sudo systemctl status haru-mcp.service
```

Do not restart unrelated services as part of a routine Haru change.

## Health is layered

Keep these signals distinct:

1. **Gateway health:** Haru process/protocol is alive.
2. **Workspace backend health:** filesystem/shell/file-transfer proxy children are alive and scoped correctly.
3. **Tunnel health/readiness:** the private remote path is connected where supported by the current tunnel client.
4. **End-to-end acceptance:** a real MCP client can discover tools and make one harmless call.

A green gateway health check does not prove the workspace backends work. A green local tunnel process does not prove a remote client can invoke Haru.

When delegated backend calls fail, Haru should report a backend failure rather than silently changing routes or opening a public fallback.

## Repository read-before-write discipline

For repository operations, exact remote identity is part of the safety boundary:

1. Read the remote branch/head you intend to modify.
2. Verify the commit/tree/parent assumptions needed by your change.
3. Make the narrow mutation against that observed state.
4. Use normal pushes by default.
5. If a history rewrite is genuinely required, use an exact force-with-lease tied to the remote SHA you observed; never use blind force as a convenience.
6. Re-read the branch and `main` after mutation.

A generic forced-update shape is:

```bash
expected_remote='<sha-you-just-read>'
target='<new-target-sha>'
branch='task/example'

git push origin \
  "$target:refs/heads/$branch" \
  "--force-with-lease=refs/heads/$branch:$expected_remote"
```

If the remote moved unexpectedly, stop and inspect rather than overwriting someone else's work.

## Upgrade principle

Prefer replace-and-verify over in-place mutation:

1. Record the currently deployed source/package versions.
2. Build/install the candidate in a separate staging/versioned location.
3. Run repository tests and `./deploy/verify.sh`.
4. Validate configuration and service syntax before switching.
5. Switch the service to the candidate.
6. Run layered health and end-to-end acceptance checks.
7. Keep the previous known-good install until the candidate is proven.

Dependency pins used by the optional workspace stack are documented in [`../THIRD-PARTY.md`](../THIRD-PARTY.md) and should be upgraded independently and deliberately.

## Rollback principle

Rollback should restore the last known-good private/loopback topology, not reopen an anonymous network path.

Typical rollback order:

1. Stop or switch away from the failed candidate service/binary.
2. Restore the previous gateway environment/install.
3. Restore the previous workspace-backend install/config if that layer changed.
4. Restart only the affected services.
5. Re-run gateway, backend, tunnel, and real-client acceptance in order.

If the secure tunnel is broken, the safe degraded state is **remote access unavailable while Haru remains private**.

## Shell and browser probe hygiene

Workspace shell access can spawn subprocess trees. Browser automation is especially capable of leaving helpers/renderers behind after the initiating command exits.

After disposable probes that spawn descendants:

- inspect the process tree/process group;
- terminate descendants that belong to the probe;
- verify no unexpected listener remains;
- check service task/memory pressure before assuming a runtime limit needs to be raised.

Do not solve leaked-process pressure by reflexively increasing service limits. First establish whether the workload is legitimate or simply leftover descendants.

## Secret discipline

Never print or persist credentials merely to prove that authentication exists. Prefer status commands and redacted metadata.

Keep these out of Git and review artifacts:

- tunnel credentials and session material;
- OAuth/PAT/API tokens;
- private environment-file contents;
- SSH private keys or agents;
- unrelated workspace data;
- production host/domain inventory.

Examples in this repository use loopback addresses, `example.com`, and generic filesystem paths intentionally.

## Recovery matrix

| Symptom | Inspect first | Safe response |
| --- | --- | --- |
| Haru service down | systemd status/logs, config syntax, installed package | restore/restart the last known-good gateway on loopback |
| Haru healthy, workspace tools fail | workspace proxy/service, named-server config, child processes, workspace permissions | repair/restart the private backend; do not expose it publicly |
| Local Haru works, remote client fails | tunnel service and current tunnel diagnostics | repair/restart the private tunnel; keep public ingress denied |
| Resource/thread exhaustion | leftover descendants, task count, memory, recent probes | clean up leaked descendants and identify the workload before changing limits |
| Unexpected remote Git branch head | re-read remote refs and commits | stop mutation; reconcile provenance before pushing |
