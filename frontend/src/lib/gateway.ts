import { GatewayInfo, Instance } from "./api";

/** The model id a client must send for this instance.
 *
 * Aliases replace the registry name in `--served-model-name`, so when an
 * instance has aliases the registry name is NOT routable — mirror the backend's
 * `_served_names` exactly or we'd hand out a name vLLM 404s.
 */
export function gatewayModelName(i: Instance): string {
  const aliases = (i.served_model_names ?? "").split(/\s+/).filter(Boolean);
  return aliases[0] ?? i.model_name;
}

/** Where clients point: the portal's own origin. Never a node IP — the gateway
 * is served by the portal, and behind an ingress the browser's origin is the
 * only thing that is correct for the caller too. */
export function gatewayBaseUrl(): string {
  return `${window.location.origin}/v1`;
}

/** A ready-to-paste OpenAI-client config. */
export function clientSnippet(model: string, info?: GatewayInfo | null): string {
  const lines = [`Base URL: ${gatewayBaseUrl()}`, `Model: ${model}`];
  if (info?.auth_required) {
    lines.push(
      info.token_configured
        ? "Auth: Authorization: Bearer <your gateway token> (Settings → API gateway)"
        : "Auth: portal auth is ON but no gateway token is set yet — set one in Settings → API gateway",
    );
  }
  return lines.join("\n");
}
