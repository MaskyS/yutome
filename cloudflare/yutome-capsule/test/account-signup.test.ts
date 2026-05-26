import assert from "node:assert/strict";
import test from "node:test";
import { ACCOUNT_SESSION_COOKIE_NAME } from "../src/account-grants.ts";
import { handleAccountSignupRequest } from "../src/account-signup.ts";
import { handleAuthorizeRequest } from "../src/pairing.ts";
import type { Env } from "../src/env.ts";

test("hosted signup GET renders the account bootstrap form with safe return_to", async () => {
  const response = await handleAccountSignupRequest(
    new Request("https://mcp.yutome.com/account/signup?return_to=%2Fauthorize%3Fclient_id%3Dabc"),
    hostedEnv(),
  );

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  const body = await response.text();
  assert.match(body, /Create your Yutome account/);
  assert.match(body, /name="email"/);
  assert.match(body, /name="name"/);
  assert.match(body, /name="workspace_name"/);
  assert.match(body, /name="return_to" value="\/authorize\?client_id=abc"/);
});

test("hosted signup POST bootstraps account session, sets cookie, and redirects", async () => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const response = await handleAccountSignupRequest(
    signupPostRequest({
      email: "alice@example.com",
      name: "Alice",
      workspace_name: "Alice Research",
      return_to: "/authorize?client_id=client_1&state=ok",
    }),
    hostedEnv(),
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init: init ?? {} });
      return Response.json({
        ok: true,
        principal: { user_id: "user_alice", workspace_id: "ws_alice" },
        session: {
          token: "session-token",
          expires_at: new Date(Date.now() + 3_600_000).toISOString(),
          audience: "yutome:hosted-oauth",
          cookie_name: ACCOUNT_SESSION_COOKIE_NAME,
        },
      });
    }) as typeof fetch,
  );

  assert.equal(response.status, 302);
  assert.equal(response.headers.get("location"), "/authorize?client_id=client_1&state=ok");
  const setCookie = response.headers.get("set-cookie") || "";
  assert.match(setCookie, new RegExp(`^${ACCOUNT_SESSION_COOKIE_NAME}=session-token;`));
  assert.match(setCookie, /HttpOnly/);
  assert.match(setCookie, /Secure/);
  assert.match(setCookie, /SameSite=Lax/);
  assert.match(setCookie, /Path=\//);
  assert.match(setCookie, /Max-Age=3[0-9]{3}/);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://hosted.example/account/bootstrap");
  assert.equal(new Headers(calls[0].init.headers).get("authorization"), "Bearer edge-secret");
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), {
    email: "alice@example.com",
    name: "Alice",
    workspace_name: "Alice Research",
  });
});

test("hosted authorize redirects missing account sessions to signup", async () => {
  const response = await handleAuthorizeRequest({
    request: new Request("https://mcp.yutome.com/authorize?client_id=client_1&state=abc"),
    env: hostedEnv({ YUTOME_ACCOUNT_SESSION_HMAC_SECRET: "account-session-secret" }),
    oauthHelpers: {
      parseAuthRequest: async () => ({
        clientId: "client_1",
        redirectUri: "https://assistant.example/callback",
        scope: ["yutome.search.read"],
        resource: "https://mcp.yutome.com/mcp",
      }),
      lookupClient: async () => null,
    } as never,
  });

  assert.equal(response.status, 302);
  assert.equal(
    response.headers.get("location"),
    "/account/signup?return_to=%2Fauthorize%3Fclient_id%3Dclient_1%26state%3Dabc",
  );
});

test("signup defaults unsafe return_to before redirecting", async () => {
  const response = await handleAccountSignupRequest(
    signupPostRequest({
      email: "alice@example.com",
      name: "Alice",
      workspace_name: "Alice Research",
      return_to: "https://attacker.example/callback",
    }),
    hostedEnv(),
    accountBootstrapFetch(),
  );

  assert.equal(response.status, 302);
  assert.equal(response.headers.get("location"), "/authorize");
});

test("hosted signup surfaces hosted API errors cleanly", async () => {
  const response = await handleAccountSignupRequest(
    signupPostRequest({
      email: "alice@example.com",
      name: "Alice",
      workspace_name: "Alice Research",
      return_to: "/authorize",
    }),
    hostedEnv(),
    (async () =>
      Response.json(
        { ok: false, error: "workspace_name_unavailable", message: "Workspace name is unavailable." },
        { status: 409 },
      )) as typeof fetch,
  );

  assert.equal(response.status, 409);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(await response.json(), {
    ok: false,
    error: "workspace_name_unavailable",
    message: "Workspace name is unavailable.",
  });
});

test("connector-only deployments do not expose account signup", async () => {
  const response = await handleAccountSignupRequest(
    new Request("https://mcp.yutome.com/account/signup"),
    hostedEnv({ YUTOME_WORKER_MODE: "connector_only" }),
  );

  assert.equal(response.status, 404);
});

test("hosted signup scopes the cookie to YUTOME_COOKIE_DOMAIN when set", async () => {
  const response = await handleAccountSignupRequest(
    signupPostRequest({
      email: "carol@example.com",
      name: "Carol",
      workspace_name: "Carol Research",
      return_to: "/authorize?client_id=c&state=s",
    }),
    hostedEnv({ YUTOME_COOKIE_DOMAIN: "yutome.com" }),
    (async () =>
      Response.json({
        ok: true,
        principal: { user_id: "user_carol", workspace_id: "ws_carol" },
        session: {
          token: "session-token",
          expires_at: new Date(Date.now() + 3_600_000).toISOString(),
          audience: "yutome:hosted-oauth",
          cookie_name: ACCOUNT_SESSION_COOKIE_NAME,
        },
      })) as typeof fetch,
  );

  assert.equal(response.status, 302);
  const setCookie = response.headers.get("set-cookie") || "";
  assert.match(setCookie, /Domain=yutome\.com/);
  assert.match(setCookie, /HttpOnly/);
  assert.match(setCookie, /Secure/);
  assert.match(setCookie, /SameSite=Lax/);
});

function hostedEnv(overrides: Partial<Env> = {}): Env {
  return {
    OAUTH_KV: {} as KVNamespace,
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    YUTOME_HOSTED_API_URL: "https://hosted.example",
    YUTOME_HOSTED_API_TOKEN: "edge-secret",
    RELAY: {} as DurableObjectNamespace,
    MCP_OBJECT: {} as DurableObjectNamespace,
    ...overrides,
  } as Env;
}

function signupPostRequest(values: Record<string, string>): Request {
  return new Request("https://mcp.yutome.com/account/signup", {
    method: "POST",
    body: new URLSearchParams(values),
  });
}

function accountBootstrapFetch(): typeof fetch {
  return (async () =>
    Response.json({
      ok: true,
      principal: { user_id: "user_alice", workspace_id: "ws_alice" },
      session: {
        token: "session-token",
        expires_at: new Date(Date.now() + 3_600_000).toISOString(),
        audience: "yutome:hosted-oauth",
        cookie_name: ACCOUNT_SESSION_COOKIE_NAME,
      },
    })) as typeof fetch;
}
