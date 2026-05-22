/**
 * /pair — pairing-code consent flow used by the OAuthProvider's defaultHandler.
 *
 * When Claude/ChatGPT redirects the browser to /authorize, the OAuth provider
 * routes the unauthenticated request here. We render an HTML form asking for
 * the pairing code that `yutome connect` printed. On a valid code we call
 * `OAuthHelpers.completeAuthorization()`, which finalizes the grant and
 * redirects the browser back to the MCP client with the auth code.
 */
import type { Env, YutomeAuthProps } from "./env";
import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";

interface PairingContext {
  request: Request;
  env: Env;
  oauthHelpers: OAuthHelpers;
}

export async function handlePairingRequest(ctx: PairingContext): Promise<Response> {
  const { request, env, oauthHelpers } = ctx;
  const url = new URL(request.url);

  if (request.method === "GET") {
    return renderForm(url, "");
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  const form = await request.formData();
  const supplied = String(form.get("pairing_code") || "").trim().toUpperCase();
  const expected = String(env.YUTOME_PAIRING_CODE || "").trim().toUpperCase();
  if (!expected) {
    return errorResponse("Yutome pairing is not configured. Run `yutome connect --deploy` again.");
  }
  if (!supplied || supplied !== expected) {
    return renderForm(url, "That pairing code was not accepted. Check `yutome status` or rerun `yutome connect`.");
  }

  // Reconstruct the original AuthRequest from the hidden form fields the
  // GET handler stamped in. We trust them because the OAuthProvider verifies
  // the redirect_uri/client_id against the registered client metadata.
  const authReqPayload = String(form.get("__auth_request") || "");
  if (!authReqPayload) {
    return errorResponse("Missing authorization request context. Restart from your assistant.");
  }
  let authRequest: Awaited<ReturnType<OAuthHelpers["parseAuthRequest"]>>;
  try {
    authRequest = JSON.parse(authReqPayload);
  } catch {
    return errorResponse("Invalid authorization request payload.");
  }

  const props: YutomeAuthProps = {
    capsule: "owner",
    paired_at: new Date().toISOString(),
  };

  const { redirectTo } = await oauthHelpers.completeAuthorization({
    request: authRequest,
    userId: "yutome-owner",
    metadata: { paired_at: props.paired_at },
    scope: authRequest.scope,
    props,
  });

  return Response.redirect(redirectTo, 302);
}

/**
 * Renders the pairing form. Called from both the `/authorize` GET path (where
 * we serialize the AuthRequest into a hidden field) and from POST retries on
 * a bad pairing code.
 */
export function renderForm(url: URL, error: string, authRequestJson?: string): Response {
  const hidden = authRequestJson
    ? `<input type="hidden" name="__auth_request" value="${escapeHtml(authRequestJson)}" />`
    : "";
  const body = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Pair Yutome Remote MCP</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.5; margin: 2.5rem auto; max-width: 38rem; padding: 0 1rem; color: #111; }
    h1 { font-size: 1.4rem; margin-bottom: 0.5rem; }
    label { display: grid; gap: 0.4rem; margin: 1.25rem 0; font-weight: 500; }
    input { padding: 0.7rem; border: 1px solid #999; border-radius: 8px; font: inherit; }
    button { padding: 0.8rem 1.1rem; border: 0; border-radius: 8px; background: #111; color: white; font-weight: 600; font-size: 1rem; }
    code { background: #f3f3f3; padding: 0.1rem 0.35rem; border-radius: 4px; }
    .error { color: #9f1239; font-weight: 500; }
    .hint { color: #555; font-size: 0.95rem; }
  </style>
</head>
<body>
  <h1>Pair Yutome with this assistant</h1>
  <p class="hint">Claude or ChatGPT wants permission to search this Yutome library while your computer is online.</p>
  <p class="hint">Enter the pairing code that <code>yutome connect</code> printed. No Yutome account is needed.</p>
  ${error ? `<p class="error">${escapeHtml(error)}</p>` : ""}
  <form method="post" action="${escapeHtml(url.pathname + url.search)}">
    ${hidden}
    <label>Pairing code
      <input name="pairing_code" autocomplete="one-time-code" autofocus required />
    </label>
    <button type="submit">Approve</button>
  </form>
</body>
</html>
`;
  return new Response(body, {
    status: error ? 401 : 200,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
  });
}

function errorResponse(message: string): Response {
  return new Response(message, {
    status: 400,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      case "'": return "&#39;";
      default: return char;
    }
  });
}
