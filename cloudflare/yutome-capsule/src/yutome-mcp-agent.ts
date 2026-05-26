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
import {
  buildOfflineResponseMetadata,
  deriveBridgeRelayObjectName,
  resolveMcpBridgeIdentity,
  validateTenantIdsNotInToolArguments,
  type BridgeInstallIdentity,
} from "./tenant-routing";
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
const TEST_VIDEO_ICON =
  "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMjgiIGhlaWdodD0iMTI4IiB2aWV3Qm94PSIwIDAgMTI4IDEyOCI+PHJlY3Qgd2lkdGg9IjEyOCIgaGVpZ2h0PSIxMjgiIHJ4PSIyNCIgZmlsbD0iIzExMTExMSIvPjx0ZXh0IHg9IjY0IiB5PSI4MyIgZm9udC1zaXplPSI2NCIgdGV4dC1hbmNob3I9Im1pZGRsZSI+8J+OpTwvdGV4dD48L3N2Zz4=";

type DispatchResult = { result?: unknown; error?: { code?: number; message?: string; data?: unknown } };
type DispatchError = NonNullable<DispatchResult["error"]>;

export class YutomeMcpAgent extends McpAgent<Env, unknown, YutomeAuthProps> {
  server = new Server(
    {
      name: CONTRACT.server_name,
      title: "🎥 Yutome",
      version: "0.2.0",
      icons: [{ src: TEST_VIDEO_ICON, mimeType: "image/svg+xml", sizes: ["128x128"] }],
    },
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
      const tenantValidation = validateTenantIdsNotInToolArguments(args ?? {});
      if (!tenantValidation.ok) {
        throw new McpError(
          ErrorCode.InvalidParams,
          tenantValidation.message ?? "Tool arguments include hosted tenant identity fields.",
          { violations: tenantValidation.violations },
        );
      }
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
        const errorData = this.errorDataWithOfflineMetadata(err);
        const isOffline =
          err.code === -32002 ||
          errorData.desktop_offline === true;
        const fallback = isOffline
          ? "Yutome Desktop bridge is offline."
          : "Yutome resource unavailable.";
        throw new McpError(code, err.message ?? fallback, errorData);
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
          ...this.errorDataWithOfflineMetadata(err),
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
    const id = this.env.RELAY.idFromName(this.relayObjectName());
    return this.env.RELAY.get(id) as unknown as DurableObjectStub<YutomeRelay>;
  }

  private relayObjectName(): string {
    return deriveBridgeRelayObjectName(this.bridgeIdentity());
  }

  private bridgeIdentity(): BridgeInstallIdentity {
    const props = (this.props ?? {}) as Partial<YutomeAuthProps>;
    return resolveMcpBridgeIdentity(props, this.env);
  }

  private errorDataWithOfflineMetadata(err: DispatchError): Record<string, unknown> {
    const data = recordFromUnknown(err.data);
    if (err.code !== -32002 && data.desktop_offline !== true) {
      return data;
    }
    return {
      ...data,
      ...buildOfflineResponseMetadata(this.bridgeIdentity(), {
        attempted_served_from: "bridge",
        durable_object_name: this.relayObjectName(),
        reason: "bridge_offline",
      }),
    };
  }
}

function recordFromUnknown(value: unknown): Record<string, unknown> {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}
