/**
 * YutomeMcpAgent — Cloudflare Agents SDK `McpAgent` subclass.
 *
 * Tool and resource definitions come from `src/contract.json`, emitted by
 * `uv run yutome contract emit` from the Python contract registry. Because
 * the MCP TypeScript SDK's `McpServer.registerTool` only accepts Zod
 * schemas while our SSOT emits JSON Schema, this agent registers handlers
 * against the lower-level `Server` directly via `setRequestHandler`. The
 * parity test catches any drift between the Python registry and this JSON.
 *
 * Every tool/resource call is forwarded to the YutomeRelay Durable Object,
 * which proxies to the laptop bridge.
 */
import { McpAgent } from "agents/mcp";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  CallToolRequestSchema,
  ListResourceTemplatesRequestSchema,
  ListResourcesRequestSchema,
  ListToolsRequestSchema,
  ReadResourceRequestSchema,
  ErrorCode,
  McpError,
} from "@modelcontextprotocol/sdk/types.js";
import type { Env, YutomeAuthProps } from "./env";
import type { YutomeRelay } from "./yutome-relay";
import contractData from "./contract.json" with { type: "json" };

interface ToolEntry {
  name: string;
  title: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations: { title: string; readOnlyHint: boolean; openWorldHint: boolean };
}

interface ResourceTemplateEntry {
  uriTemplate: string;
  name: string;
  description: string;
  mimeType: string;
  host: string;
}

interface ContractPayload {
  auth_scope: string;
  server_name: string;
  instructions: string;
  tools: ToolEntry[];
  resource_templates: ResourceTemplateEntry[];
}

const CONTRACT = contractData as ContractPayload;

type DispatchResult = { result?: unknown; error?: { code?: number; message?: string; data?: unknown } };

export class YutomeMcpAgent extends McpAgent<Env, unknown, YutomeAuthProps> {
  server = new Server(
    { name: CONTRACT.server_name, version: "0.2.0" },
    {
      instructions: CONTRACT.instructions,
      capabilities: {
        tools: {},
        resources: { listChanged: false, subscribe: false },
      },
    },
  );

  async init(): Promise<void> {
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: CONTRACT.tools.map((tool) => ({
        name: tool.name,
        title: tool.title,
        description: tool.description,
        inputSchema: tool.inputSchema,
        annotations: tool.annotations,
      })),
    }));

    this.server.setRequestHandler(CallToolRequestSchema, async (request) => {
      const { name, arguments: args } = request.params;
      const dispatch = (await this.relay().dispatch("tool", "tools/call", {
        name,
        arguments: args ?? {},
      })) as DispatchResult;
      return this.unwrapToolResult(dispatch);
    });

    this.server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => ({
      resourceTemplates: CONTRACT.resource_templates.map((tpl) => ({
        uriTemplate: tpl.uriTemplate,
        name: tpl.name,
        description: tpl.description,
        mimeType: tpl.mimeType,
      })),
    }));

    this.server.setRequestHandler(ListResourcesRequestSchema, async () => {
      // Templates-only for chunks (millions). Channels/videos enumeration
      // would route through the bridge; deferred until needed.
      return { resources: [] };
    });

    this.server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
      const uri = request.params.uri;
      const dispatch = (await this.relay().dispatch("resource", "resources/read", {
        uri,
      })) as DispatchResult;
      if (dispatch.error) {
        const err = dispatch.error;
        const code = err.code === -32002 ? ErrorCode.InvalidParams : ErrorCode.InternalError;
        const isOffline =
          err.code === -32002 ||
          (typeof err.data === "object" &&
            err.data !== null &&
            (err.data as { desktop_offline?: unknown }).desktop_offline === true);
        const fallback = isOffline
          ? "Yutome Desktop bridge is offline."
          : "Yutome resource unavailable.";
        throw new McpError(code, err.message ?? fallback, err.data);
      }
      const wrapped = (dispatch.result ?? {}) as {
        contents?: Array<{ uri: string; mimeType: string; text: string }>;
      };
      const tpl = CONTRACT.resource_templates.find((t) => uri.startsWith(`yutome://${t.host}/`));
      return {
        contents:
          wrapped.contents ??
          [{ uri, mimeType: tpl?.mimeType ?? "application/json", text: "{}" }],
      };
    });
  }

  // ---------- Helpers ----------

  private unwrapToolResult(dispatch: DispatchResult): {
    content: Array<{ type: "text"; text: string }>;
    structuredContent?: unknown;
    isError?: boolean;
  } {
    if (dispatch.error) {
      const err = dispatch.error;
      return {
        isError: true,
        content: [{ type: "text", text: err.message ?? "Yutome Desktop is offline." }],
        structuredContent: {
          ok: false,
          error: err.message ?? "unknown",
          ...((err.data ?? {}) as Record<string, unknown>),
        },
      };
    }
    const wrapped = (dispatch.result ?? {}) as {
      content?: Array<{ type: "text"; text: string }>;
      structuredContent?: unknown;
      isError?: boolean;
    };
    return {
      content: wrapped.content ?? [{ type: "text", text: "" }],
      structuredContent: wrapped.structuredContent,
      isError: wrapped.isError,
    };
  }

  private relay(): DurableObjectStub<YutomeRelay> {
    const id = this.env.RELAY.idFromName("default");
    return this.env.RELAY.get(id) as unknown as DurableObjectStub<YutomeRelay>;
  }
}
