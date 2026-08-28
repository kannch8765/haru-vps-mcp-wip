# Secure tunnel deployment

ChatGPT connects to remote MCP servers; OpenAI's current documentation says that when an MCP server is on a private network, on-premises, or a developer machine, **Secure MCP Tunnel** can connect it to supported OpenAI products without exposing that MCP server to the public Internet.

OpenAI product availability, permissions, tunnel creation UI, client downloads, and command-line details can change. Treat the current official documentation as the source of truth:

- [Developer mode and MCP apps in ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)

This document intentionally covers the **server-side operational boundary after a tunnel has been created**. It does not freeze the human-side OpenAI UI flow.

## Target topology

```text
ChatGPT / supported OpenAI client
          |
          v
   Secure MCP Tunnel
          |
          v
 customer-hosted tunnel client
          |
          v
 http://127.0.0.1:8765/mcp
          |
          v
     Haru MCP gateway
          |
          v
 loopback workspace backends
```

The important property is what does **not** change: Haru MCP remains bound to loopback. The tunnel provides private/authenticated remote reachability; it is not a reason to change `HARU_MCP_HOST` to `0.0.0.0` or to create another anonymous reverse proxy.

Host/Origin checks and DNS-rebinding protection in Haru MCP are request/transport hardening. They are **not client authentication** and do not replace the tunnel's access boundary.

## What the server-side operator needs

Once the human-side tunnel exists, the server-side operator needs the values and artifacts provided by the **current** OpenAI tunnel workflow, typically including:

- the current supported tunnel client and installation instructions;
- the tunnel identity/registration selected for this private MCP server;
- the current credential or configuration material required by the client;
- the local MCP target URL, which for the default Haru deployment is `http://127.0.0.1:8765/mcp`;
- any current health, readiness, or diagnostic commands/endpoints supported by that tunnel client.

Do not infer missing values from old screenshots or copy a historical client version from another deployment. Re-read the official instructions when provisioning or upgrading a tunnel.

## Keep tunnel credentials out of Git

Run the tunnel process as an unprivileged service. For the smallest single-host deployment, it can use the same dedicated `haru` account as the gateway/workspace, but that choice deliberately shares a trust boundary: workspace shell commands running as the same UID may be able to inspect same-UID process state and any tunnel material readable by that UID. If the tunnel uses reusable credentials or you want a stronger credential boundary, use a separate `haru-tunnel` identity as defense in depth.

Store tunnel credentials and product-specific configuration outside the repository, for example under `/etc/haru-tunnel/`. Prefer root ownership and restrictive permissions. If systemd reads a secret `EnvironmentFile=`, `root:root` mode `0600` can keep the file unreadable by the runtime account; if the tunnel client itself must open a config file after privilege drop, grant only the minimum group/read permission required by that client.

Never copy tunnel credentials into:

- Git files or commits;
- issue/PR text;
- shell transcripts or screenshots;
- CI logs;
- example environment files;
- acceptance evidence.

## Validate in the foreground first

Before creating a persistent service:

1. Confirm Haru itself is healthy on `127.0.0.1`.
2. Confirm the gateway still refuses a non-loopback bind configuration.
3. Start the current tunnel client using the command/configuration produced by the current OpenAI workflow.
4. If the client exposes local health/readiness endpoints or a diagnostic command, keep those probes on loopback and require them to pass.
5. From the real OpenAI client, verify MCP discovery and make one harmless call such as Haru `health` or a read/list operation in the disposable workspace.

A local tunnel process being alive is not enough. The acceptance test should prove the real client can discover and invoke the intended MCP server through the private path.

## Promote the proven command to systemd

Once the foreground canary works, supervise the **same proven current command** with systemd rather than inventing different product flags for the daemonized form.

A generic hardening shape is shown below. This example uses the optional split `haru-tunnel` identity; a minimal single-user deployment may substitute its dedicated `haru` account after accepting the same-UID trust-boundary tradeoff above:

```ini
[Unit]
Description=Secure MCP tunnel for Haru MCP
After=network-online.target haru-mcp.service
Wants=network-online.target

[Service]
Type=simple
User=haru-tunnel
Group=haru-tunnel
EnvironmentFile=/etc/haru-tunnel/tunnel.env
# Use the exact current OpenAI-supported foreground command here.
ExecStart=/opt/haru-tunnel/tunnel-client <current-run-arguments>
Restart=on-failure
RestartSec=2s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

`<current-run-arguments>` is deliberately a placeholder: copy the current supported invocation from the OpenAI setup you just validated. Do not commit the resulting secret-bearing environment/configuration file.

If the current client bundles helpers or needs writable runtime/state directories, add only those paths required by the current release and keep them separate from the Haru workspace.

## Persistent-deployment acceptance

After enabling the unit:

1. Confirm the tunnel service is active and enabled.
2. Confirm Haru and the workspace backend are independently healthy.
3. Repeat tunnel health/readiness checks where the current client supports them.
4. Repeat real-client discovery plus one harmless MCP call.
5. End the administrative SSH/session that started or configured the service.
6. From a fresh client interaction, repeat the harmless MCP call.

That last check proves the tunnel is owned by the service manager rather than accidentally depending on an interactive shell.

## Fail closed

A broken tunnel should make Haru unreachable from the remote client. That is a safe degraded state.

Do **not** recover tunnel availability by:

- binding Haru MCP to a public interface;
- exposing the workspace proxy publicly;
- adding a temporary anonymous Caddy/nginx reverse proxy;
- weakening Host/Origin checks and calling them authentication;
- committing tunnel secrets so another process can read them more easily.

If a fail-closed public reverse-proxy example such as `deploy/Caddyfile.example` is present, leave it denied unless you have a separately reviewed authenticated ingress design.

## Troubleshooting order

Keep the layers separate while diagnosing:

1. **Gateway:** is Haru MCP running and reachable locally on its loopback URL?
2. **Workspace backend:** can Haru delegate to the filesystem/shell/file-ingress endpoints?
3. **Tunnel client:** is the current client active, and do its supported local diagnostics pass?
4. **Remote acceptance:** can the OpenAI client discover tools and make a harmless call?

A failure at one layer is not evidence that another layer should be exposed publicly.

## Upgrades and credential rotation

When OpenAI changes tunnel-client releases or setup requirements, re-read the current official documentation, validate the new client in the foreground, then update the supervised service. Keep the previous known-good binary/configuration available for rollback without re-enabling public ingress.

Rotate tunnel credentials according to current OpenAI guidance and your own secret-management policy. A rollback should restore the last known-good **private tunnel**, not a historical public route.

## Human-side setup handoff

A future owner-facing guide may document how a human creates/selects a Secure MCP Tunnel in the current OpenAI product UI. That guide should be maintained separately because the UI, entitlements, roles, and labels are product behavior rather than a stable Haru runtime contract.

This server-side guide begins once that human workflow has produced the current tunnel registration and client configuration needed by the operator.
