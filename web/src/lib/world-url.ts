/** DNS-hosted article links only; mirror quantmind.world.urls at the UI boundary.
 * Does not resolve DNS or authorize fetching arbitrary URLs server-side.
 */
export function safeWorldUrl(value: string): string | null {
  if (!value || value.length > 2048 || value.includes("\\")
    || Array.from(value).some((char) => char.charCodeAt(0) <= 32 || char.charCodeAt(0) === 127)) return null;
  // Inspect the raw authority before WHATWG normalizes encoded/numeric hosts.
  const authority = /^https?:\/\/([^/?#]*)/i.exec(value)?.[1];
  if (!authority || /[^\u0021-\u007e]|[@%]/u.test(authority)) return null;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, "");
    const labels = host.split(".");
    if (host.length > 253 || labels.length < 2 || /(?:^|\.)(?:localhost|local)$/.test(host)) return null;
    if (!/^[a-z][a-z0-9-]*$/.test(labels.at(-1)!)) return null;
    if (labels.some((label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return null;
    return value;
  } catch {
    return null;
  }
}
