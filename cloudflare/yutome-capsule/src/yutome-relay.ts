/**
 * YutomeRelay — Durable Object that brokers between McpAgent (running in the
 * Worker) and the laptop bridge process (running on the user's machine).
 *
 * Protocol on the WebSocket (both directions, line-delimited JSON):
 *   server → client (job):    { type: "job", job_id, kind, method, params }
 *   client → server (result): { type: "result", job_id, result?, error? }
 *   client → server (bye):    { type: "bye" }
 *   server → client (ping):   { type: "ping" }
 *
 * The DO uses WebSocket Hibernation: it stays in memory only while a request
 * is in flight; once idle it sleeps without disconnecting the bridge.
 */
import type { Env } from "./env";
import { DurableObject } from "cloudflare:workers";

type PendingResolver = (payload: { result?: unknown; error?: unknown }) => void;

interface JobFrame {
  type: "job";
  job_id: string;
  kind: string;
  method: string;
  params: Record<string, unknown>;
}

interface ResultFrame {
  type: "result";
  job_id: string;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

const DISPATCH_TIMEOUT_MS = 30_000;

export class YutomeRelay extends DurableObject<Env> {
  // In-memory pending dispatches. Hibernation evicts these on sleep — that's
  // fine because every dispatch awaits the result inline; we never have a
  // pending entry that outlives its caller.
  private pending = new Map<string, PendingResolver>();
  private lastSeenAt: number | null = null;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
  }

  // ---------- HTTP entry points ----------

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/relay/connect") {
      return this.acceptBridge(request);
    }
    if (url.pathname === "/relay/status") {
      return Response.json({
        bridge_online: this.bridgeOnline(),
        last_seen_at: this.lastSeenAt ? new Date(this.lastSeenAt).toISOString() : null,
      });
    }
    return new Response("Not Found", { status: 404 });
  }

  private async acceptBridge(request: Request): Promise<Response> {
    const expected = this.env.YUTOME_RELAY_TOKEN;
    if (!expected) {
      return Response.json({ error: "YUTOME_RELAY_TOKEN not configured" }, { status: 500 });
    }
    const header = request.headers.get("authorization") || "";
    const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
    if (token !== expected) {
      return new Response("Unauthorized", { status: 401 });
    }

    const upgrade = request.headers.get("upgrade");
    if (!upgrade || upgrade.toLowerCase() !== "websocket") {
      return new Response("Expected WebSocket upgrade", { status: 426 });
    }

    // Close any existing bridge connection before accepting the new one.
    for (const ws of this.ctx.getWebSockets()) {
      try {
        ws.close(1001, "replaced by new bridge");
      } catch {
        /* already closed */
      }
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair) as [WebSocket, WebSocket];
    this.ctx.acceptWebSocket(server);
    this.lastSeenAt = Date.now();

    return new Response(null, {
      status: 101,
      webSocket: client,
    });
  }

  // ---------- WebSocket Hibernation event handlers ----------

  webSocketMessage(_ws: WebSocket, message: ArrayBuffer | string): void {
    const text = typeof message === "string" ? message : new TextDecoder().decode(message);
    let frame: ResultFrame | { type: "bye" } | { type: "pong" };
    try {
      frame = JSON.parse(text);
    } catch {
      return;
    }
    this.lastSeenAt = Date.now();
    if (frame.type === "result") {
      const resolver = this.pending.get(frame.job_id);
      if (resolver) {
        this.pending.delete(frame.job_id);
        resolver({ result: frame.result, error: frame.error });
      }
    }
    // pong / bye fall through; bye does not require explicit handling because
    // the bridge will follow with ws.close().
  }

  webSocketClose(_ws: WebSocket, _code: number, _reason: string, _wasClean: boolean): void {
    // Reject every in-flight dispatch so callers get a prompt offline error
    // instead of waiting for the per-call timeout.
    for (const [jobId, resolver] of this.pending.entries()) {
      resolver({
        error: {
          code: -32002,
          message: "Yutome Desktop bridge disconnected.",
          data: { desktop_offline: true, job_id: jobId },
        },
      });
    }
    this.pending.clear();
  }

  webSocketError(_ws: WebSocket, _error: unknown): void {
    this.webSocketClose(_ws, 1011, "error", false);
  }

  // ---------- RPC surface for McpAgent (cross-DO call via stub) ----------

  /** Sends a job to the connected bridge and awaits its result.
   *
   * Returns a discriminated union: `{ result }` on success, `{ error }` on
   * failure (including offline). McpAgent translates `error` into a
   * JSON-RPC error response to the MCP client.
   */
  async dispatch(
    kind: string,
    method: string,
    params: Record<string, unknown>,
  ): Promise<{ result?: unknown; error?: unknown }> {
    if (!this.bridgeOnline()) {
      return {
        error: {
          code: -32002,
          message: "Yutome Desktop bridge is offline.",
          data: {
            desktop_offline: true,
            last_seen_at: this.lastSeenAt ? new Date(this.lastSeenAt).toISOString() : null,
          },
        },
      };
    }

    const jobId = crypto.randomUUID();
    const frame: JobFrame = { type: "job", job_id: jobId, kind, method, params };

    const result = await new Promise<{ result?: unknown; error?: unknown }>((resolve) => {
      const resolver: PendingResolver = (payload) => resolve(payload);
      this.pending.set(jobId, resolver);

      // Send the job. If sending fails (socket closed mid-flight), reject
      // immediately and clean up.
      try {
        for (const ws of this.ctx.getWebSockets()) {
          ws.send(JSON.stringify(frame));
          break; // only one bridge connection at a time
        }
      } catch (err) {
        this.pending.delete(jobId);
        resolve({
          error: {
            code: -32603,
            message: `Bridge send failed: ${(err as Error).message}`,
          },
        });
        return;
      }

      // Per-dispatch timeout so a stuck bridge doesn't hold the request forever.
      setTimeout(() => {
        if (this.pending.has(jobId)) {
          this.pending.delete(jobId);
          resolve({
            error: {
              code: -32002,
              message: "Yutome Desktop did not answer this call before the timeout.",
              data: { desktop_offline: true, job_id: jobId },
            },
          });
        }
      }, DISPATCH_TIMEOUT_MS);
    });

    return result;
  }

  /** Returns true while at least one accepted WebSocket is open. */
  private bridgeOnline(): boolean {
    return this.ctx.getWebSockets().length > 0;
  }
}
