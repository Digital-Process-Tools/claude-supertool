// Entry point for the claude-channel MCP server (#2486).
//
// `.mcp.json` used to name `channel.ts` directly. That file imports
// `@modelcontextprotocol/sdk` at module load, and a plugin install has no
// `node_modules`: the dependency is gitignored, nothing in an install runs
// `install.sh`, and `package.json` declares no `postinstall`. So the server
// the plugin declares for every user died on `ERR_MODULE_NOT_FOUND` before a
// line of its own code ran. Measured by the v0.59.0 release audit; the only
// reason it was never felt here is that somebody ran `install.sh` on this
// machine by hand, an hour and fifty-two minutes after the cache was written.
//
// **stdout is the JSON-RPC stream.** Every diagnostic below goes to stderr,
// and nothing here touches the standard output stream at all -- it belongs to
// `channel.ts`'s own transport. `npm` is spawned with its stdout redirected onto our stderr for
// the same reason: one line of install progress on the wrong stream corrupts
// the MCP handshake, which fails in the one place a session cannot see.
//
// Three states, not two. The dependency is present; it was absent and we
// installed it; or it is absent and cannot be installed — and the third says
// so in a sentence naming the remedy, rather than letting a module-resolution
// stack reach the operator.
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

/** Does the SDK resolve from this directory? Asked rather than assumed. */
function sdkResolves() {
  try {
    require.resolve("@modelcontextprotocol/sdk/server/index.js", {
      paths: [HERE],
    });
    return true;
  } catch {
    return false;
  }
}

if (!sdkResolves()) {
  console.error(
    "claude-channel: @modelcontextprotocol/sdk is not installed here; " +
      "installing it once into " + HERE,
  );
  // `inherit` on stderr so npm's own diagnosis reaches the operator, and
  // "ignore" on stdout so not one byte of it can reach the protocol stream.
  const npm = spawnSync("npm", ["install", "--omit=dev", "--no-audit", "--no-fund"], {
    cwd: HERE,
    stdio: ["ignore", "ignore", "inherit"],
  });
  const failed =
    npm.error !== undefined || npm.status !== 0 || !sdkResolves();
  if (failed) {
    const why =
      npm.error !== undefined
        ? `npm could not be started (${npm.error.code ?? npm.error.message})`
        : npm.status !== 0
          ? `npm exited ${npm.status}`
          : "npm reported success but the package still does not resolve";
    console.error(
      "claude-channel: could not install @modelcontextprotocol/sdk -- " +
        why +
        ".\n" +
        "The channel cannot start without it. Install it by hand and retry:\n" +
        "  bash " +
        path.join(HERE, "install.sh") +
        "\n" +
        "or, if npm is unavailable here, install node 22.6.0 or later with " +
        "npm on PATH.",
    );
    process.exit(1);
  }
  console.error("claude-channel: dependency installed, starting");
}

await import("./channel.ts");
