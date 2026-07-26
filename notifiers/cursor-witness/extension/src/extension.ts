// Cursor Witness — listens on a Unix socket for supertool op events and opens
// the touched file in the editor. Lets the human watch the agent work in real time.
//
// Wire format (NDJSON, one JSON per line):
//   {"op": "edit", "file": "/abs/path", "line": 42|null, "ts": 1234, "cwd": "..."}
//
// Socket path: /tmp/supertool-witness-<sha1(workspace-root)[:12]>.sock
// Override via cursorWitness.socketPath setting.

import * as vscode from "vscode";
import * as net from "net";
import * as fs from "fs";
import * as path from "path";
import * as crypto from "crypto";

interface WitnessEvent {
  op: string;
  file: string;
  line: number | null;
  line_end?: number | null;
  before_file?: string;
  ts: number;
  cwd: string;
}

// Decoration for read-op range highlight (auto-fades).
const readDecoration = vscode.window.createTextEditorDecorationType({
  backgroundColor: "rgba(120, 180, 255, 0.18)",
  isWholeLine: true,
  overviewRulerColor: "rgba(120, 180, 255, 0.6)",
  overviewRulerLane: vscode.OverviewRulerLane.Right,
});
let highlightTimer: NodeJS.Timeout | null = null;

const READ_OPS = new Set(["read", "grep", "glob", "ls", "around", "around_line", "between", "map"]);
const MUTATING_OPS = new Set(["edit", "replace", "replace_lines", "paste", "append", "vim"]);

let server: net.Server | null = null;
let statusBar: vscode.StatusBarItem | null = null;
let socketPath = "";
let idleTimer: NodeJS.Timeout | null = null;

function deriveSocketPath(): string {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    return "";
  }
  const cwd = folders[0].uri.fsPath;
  const hash = crypto.createHash("sha1").update(cwd).digest("hex").substring(0, 12);
  return `/tmp/supertool-witness-${hash}.sock`;
}

function setIdle(): void {
  if (statusBar) {
    statusBar.text = "$(eye) Max: idle";
    statusBar.tooltip = `Listening on ${socketPath}`;
  }
}

function opIcon(op: string): string {
  if (MUTATING_OPS.has(op)) return "$(edit)";
  if (READ_OPS.has(op)) return "$(eye)";
  return "$(circle-outline)";
}

function setActive(event: WitnessEvent): void {
  if (!statusBar) return;
  const name = path.basename(event.file);
  const icon = opIcon(event.op);
  statusBar.text = `${icon} Max: ${event.op} ${name}`;
  statusBar.tooltip = `${event.op} on ${event.file}${event.line ? ":" + event.line : ""}`;
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(setIdle, 3000);
}

async function openFile(event: WitnessEvent): Promise<void> {
  const config = vscode.workspace.getConfiguration("cursorWitness");
  const openOnRead = config.get<boolean>("openOnRead", true);

  if (READ_OPS.has(event.op) && !openOnRead) return;
  if (!MUTATING_OPS.has(event.op) && !READ_OPS.has(event.op)) return;
  if (!event.file || !fs.existsSync(event.file)) return;

  try {
    const fileUri = vscode.Uri.file(event.file);

    // Mutating op with a before-snapshot → show diff view (Max's edit visible).
    if (MUTATING_OPS.has(event.op) && event.before_file && fs.existsSync(event.before_file)) {
      const beforeUri = vscode.Uri.file(event.before_file);
      const label = `${event.op} ${path.basename(event.file)} (Max)`;
      await vscode.commands.executeCommand("vscode.diff", beforeUri, fileUri, label, { preview: false });
      // Scroll the diff to the edited line (issue #236). vscode.diff focuses
      // the right-hand (after) editor by default, so activeTextEditor is the
      // after-file — check fsPath to confirm before revealing.
      if (event.line && event.line > 0) {
        const editor = vscode.window.activeTextEditor;
        if (editor && editor.document.uri.fsPath === event.file) {
          const startLine = Math.max(0, event.line - 1);
          const endLine = event.line_end && event.line_end >= event.line
            ? Math.min(event.line_end - 1, editor.document.lineCount - 1)
            : startLine;
          const lastLine = Math.min(endLine, editor.document.lineCount - 1);
          const range = new vscode.Range(
            new vscode.Position(startLine, 0),
            new vscode.Position(lastLine, editor.document.lineAt(lastLine).text.length),
          );
          editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
        }
      }
      // Best-effort cleanup of the temp before-file once VSCode has read it.
      // Delay so the diff editor finishes loading.
      setTimeout(() => {
        try { fs.unlinkSync(event.before_file as string); } catch { /* ignore */ }
      }, 60_000);
      return;
    }

    // Read op or no snapshot → focus + (if line range known) highlight + reveal.
    const opts: vscode.TextDocumentShowOptions = { preserveFocus: false, preview: false };
    const editor = await vscode.window.showTextDocument(fileUri, opts);

    if (event.line && event.line > 0) {
      const startLine = Math.max(0, event.line - 1);
      const endLine = event.line_end && event.line_end >= event.line
        ? event.line_end - 1
        : startLine;
      const lastLine = Math.min(endLine, editor.document.lineCount - 1);
      const range = new vscode.Range(
        new vscode.Position(startLine, 0),
        new vscode.Position(lastLine, editor.document.lineAt(lastLine).text.length),
      );
      editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
      editor.setDecorations(readDecoration, [range]);
      if (highlightTimer) clearTimeout(highlightTimer);
      highlightTimer = setTimeout(() => {
        try { editor.setDecorations(readDecoration, []); } catch { /* editor closed */ }
      }, 4000);
    }
  } catch (err) {
    console.error("cursor-witness: open failed", err);
  }
}

function handleConnection(client: net.Socket): void {
  let buf = "";
  client.on("data", (chunk) => {
    buf += chunk.toString("utf-8");
    const nl = buf.indexOf("\n");
    if (nl >= 0) {
      const line = buf.substring(0, nl);
      buf = buf.substring(nl + 1);
      try {
        const event = JSON.parse(line) as WitnessEvent;
        setActive(event);
        openFile(event);
      } catch (err) {
        console.error("cursor-witness: parse failed", err, "line:", line);
      }
    }
  });
  client.on("error", () => client.destroy());
  client.on("end", () => client.destroy());
}

function fileLog(msg: string): void {
  try {
    fs.appendFileSync("/tmp/cursor-witness.log",
      `${new Date().toISOString()} pid=${process.pid} ${msg}\n`);
  } catch { /* ignore */ }
}

function startServer(): void {
  const folders = (vscode.workspace.workspaceFolders || []).map(f => f.uri.fsPath).join(",") || "(none)";
  fileLog(`startServer folders=${folders}`);
  socketPath = vscode.workspace.getConfiguration("cursorWitness").get<string>("socketPath") || deriveSocketPath();
  if (!socketPath) {
    fileLog("no workspace folder, skipping");
    return;
  }
  fileLog(`binding socket: ${socketPath}`);

  // Cursor spawns multiple extension hosts (main, agent-worker, shadow-workspace).
  // Each will activate this extension. Only one should bind the shared socket;
  // others must back off and stay dormant — otherwise they race on unlink at
  // deactivate time and the live socket vanishes.
  if (fs.existsSync(socketPath)) {
    // Try a quick connect: if someone's serving, back off; if dead socket, take it.
    const probe = net.createConnection(socketPath);
    probe.setTimeout(200);
    probe.on("connect", () => {
      probe.destroy();
      fileLog(`socket already in use by another extension host — backing off`);
    });
    probe.on("error", () => {
      // Stale socket file with no server — safe to take over
      try { fs.unlinkSync(socketPath); } catch { /* ignore */ }
      bindServer();
    });
    probe.on("timeout", () => {
      probe.destroy();
      fileLog(`socket probe timed out — backing off`);
    });
    return;
  }
  bindServer();
}

function bindServer(): void {
  server = net.createServer(handleConnection);
  server.on("error", (err: NodeJS.ErrnoException) => {
    // TOCTOU: another extension host probed + unlinked + bound between our
    // probe and our bind. Treat EADDRINUSE as "they won, we back off."
    if (err.code === "EADDRINUSE") {
      fileLog(`bind lost race (EADDRINUSE) — backing off`);
      try { server?.close(); } catch { /* ignore */ }
      server = null;
      return;
    }
    fileLog(`server error: ${err.message}`);
  });
  server.listen(socketPath, () => {
    fileLog(`LISTENING on ${socketPath}`);
    setIdle();
  });
}

export function activate(context: vscode.ExtensionContext): void {
  fileLog("=== activate ===");
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.text = "$(eye) Max: starting";
  statusBar.show();
  context.subscriptions.push(statusBar);

  startServer();

  // Restart on config change
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("cursorWitness.socketPath")) {
        deactivate();
        startServer();
      }
    })
  );
}

export function deactivate(): void {
  if (idleTimer) {
    clearTimeout(idleTimer);
    idleTimer = null;
  }
  // Only the host that bound `server` cleans up the socket file. Dormant hosts
  // (who saw the socket already in use at activate time) leave it alone.
  if (server) {
    server.close();
    server = null;
    try {
      if (socketPath && fs.existsSync(socketPath)) fs.unlinkSync(socketPath);
    } catch {
      // ignore
    }
  }
}
