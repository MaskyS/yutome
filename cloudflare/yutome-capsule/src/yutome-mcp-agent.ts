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
 * Hosted mode sends tool/resource calls to the hosted Python API. Connector
 * mode keeps the YutomeRelay Durable Object path to the laptop bridge.
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
  HostedAccountGrantError,
  resolveHostedMcpAuthContextFromStoredGrant,
} from "./account-grants.ts";
import {
  buildOfflineResponseMetadata,
  deriveBridgeRelayObjectName,
  HostedMcpApiClient,
  HostedMcpApiError,
  HostedMcpAuthError,
  isHostedWorkerMode,
  resolveMcpBridgeIdentity,
  validateTenantIdsNotInToolArguments,
  type BridgeInstallIdentity,
  type HostedMcpAuthContext,
  TenantRoutingError,
} from "./tenant-routing.ts";
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
const MAX_TOOL_RESULT_TEXT_CHARS = 50_000;

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
      const dispatch = await this.dispatchTool(name, (args ?? {}) as Record<string, unknown>);
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
      const dispatch = await this.dispatchResource(uri);
      if (dispatch.error) {
        const err = dispatch.error;
        const code = this.toMcpErrorCode(err);
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

  private async dispatchTool(name: string, args: Record<string, unknown>): Promise<DispatchResult> {
    if (!this.hostedMode()) {
      return (await this.relay().dispatch("tool", "tools/call", {
        name,
        arguments: args,
      })) as DispatchResult;
    }

    const auth = await this.hostedAuthContext();
    const dispatch = await this.hostedDispatch(() =>
      this.hostedApi().callTool(auth, name, args),
    );
    if (dispatch.error) {
      return dispatch;
    }
    return {
      result: {
        content: [{ type: "text", text: toolResultText(name, dispatch.result) }],
        structuredContent: dispatch.result,
      },
    };
  }

  private async dispatchResource(uri: string): Promise<DispatchResult> {
    if (!this.hostedMode()) {
      return (await this.relay().dispatch("resource", "resources/read", {
        uri,
      })) as DispatchResult;
    }

    const auth = await this.hostedAuthContext();
    const dispatch = await this.hostedDispatch(() =>
      this.hostedApi().readResource(auth, uri),
    );
    if (dispatch.error) {
      return dispatch;
    }
    return { result: hostedResourceResult(uri, dispatch.result) };
  }

  private async hostedDispatch(operation: () => Promise<unknown>): Promise<DispatchResult> {
    try {
      return { result: await operation() };
    } catch (err) {
      return { error: dispatchErrorFromHostedError(err) };
    }
  }

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

  private hostedMode(): boolean {
    return isHostedWorkerMode(this.env.YUTOME_WORKER_MODE);
  }

  private hostedApi(): HostedMcpApiClient {
    return new HostedMcpApiClient(this.env);
  }

  private async hostedAuthContext(): Promise<HostedMcpAuthContext> {
    try {
      const props = (this.props ?? {}) as Partial<YutomeAuthProps>;
      return await resolveHostedMcpAuthContextFromStoredGrant(this.env, props, {
        requiredScope: CONTRACT.auth_scope,
        sessionId: this.currentMcpSessionId(),
      });
    } catch (err) {
      if (err instanceof HostedMcpAuthError) {
        throw new McpError(
          err.status === 403 ? ErrorCode.InvalidRequest : ErrorCode.InternalError,
          err.message,
          { code: err.code },
        );
      }
      if (err instanceof TenantRoutingError) {
        throw new McpError(ErrorCode.InvalidRequest, err.message, { code: err.code });
      }
      if (err instanceof HostedAccountGrantError) {
        throw new McpError(
          err.status >= 400 && err.status < 500 ? ErrorCode.InvalidRequest : ErrorCode.InternalError,
          err.message,
          { code: err.code },
        );
      }
      throw err;
    }
  }

  private currentMcpSessionId(): string | undefined {
    try {
      return this.getSessionId();
    } catch {
      return undefined;
    }
  }

  private toMcpErrorCode(err: DispatchError): ErrorCode {
    switch (err.code) {
      case ErrorCode.InvalidParams:
      case ErrorCode.MethodNotFound:
      case ErrorCode.InvalidRequest:
        return err.code;
      case -32002:
        return ErrorCode.InvalidParams;
      default:
        return ErrorCode.InternalError;
    }
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

function dispatchErrorFromHostedError(err: unknown): DispatchError {
  if (err instanceof HostedMcpApiError) {
    return {
      code: mcpErrorCodeForHostedStatus(err.status, err.code),
      message: err.message,
      data: withoutUndefined({
        hosted_api_error: err.code,
        hosted_api_status: err.status,
        details: err.data,
      }),
    };
  }
  if (err instanceof Error) {
    return {
      code: ErrorCode.InternalError,
      message: "Hosted Yutome API request failed.",
      data: { error: err.message },
    };
  }
  return {
    code: ErrorCode.InternalError,
    message: "Hosted Yutome API request failed.",
    data: { error: String(err) },
  };
}

function mcpErrorCodeForHostedStatus(status: number, hostedCode: string): ErrorCode {
  if (status === 400 || hostedCode === "invalid_arguments" || hostedCode === "workspace_argument_not_allowed") {
    return ErrorCode.InvalidParams;
  }
  if (status === 404 || hostedCode === "unsupported_tool" || hostedCode === "unsupported_resource") {
    return ErrorCode.MethodNotFound;
  }
  if (status === 401 || status === 403 || hostedCode === "insufficient_scope") {
    return ErrorCode.InvalidRequest;
  }
  return ErrorCode.InternalError;
}

function hostedResourceResult(uri: string, payload: unknown): { contents: Array<{ uri: string; mimeType: string; text: string }> } {
  const record = recordFromUnknown(payload);
  const contents = record.contents;
  if (Array.isArray(contents)) {
    return { contents: contents as Array<{ uri: string; mimeType: string; text: string }> };
  }
  const tpl = CONTRACT.resource_templates.find((t) => uri.startsWith(`yutome://${t.host}/`));
  return {
    contents: [
      {
        uri,
        mimeType: tpl?.mimeType ?? "application/json",
        text: JSON.stringify(payload ?? {}),
      },
    ],
  };
}

function toolResultText(tool: string, payload: unknown): string {
  let raw = JSON.stringify(payload ?? {}, null, 2);
  if (raw.length > MAX_TOOL_RESULT_TEXT_CHARS) {
    raw = `${raw.slice(0, MAX_TOOL_RESULT_TEXT_CHARS)}\n... truncated ...`;
  }
  return `Yutome ${tool} result:\n${raw}`;
}

function withoutUndefined<T extends Record<string, unknown>>(value: T): T {
  return Object.fromEntries(
    Object.entries(value).filter(([, entryValue]) => entryValue !== undefined),
  ) as T;
}
