/**
 * Stable, time-sortable identifiers — identical in shape to the Python SDK:
 * a type prefix plus 20 Crockford base32 characters (48 bits of ms time + 52 random bits).
 */

const ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz";

export const PREFIXES = { run: "run", span: "sp", event: "ev", artifact: "art", link: "ln" } as const;
export type IdKind = keyof typeof PREFIXES;

function randomBits52(): bigint {
  const bytes = new Uint8Array(7);
  const c = (globalThis as { crypto?: { getRandomValues?: (a: Uint8Array) => Uint8Array } }).crypto;
  if (c?.getRandomValues) c.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  return n >> 4n; // 56 → 52 bits
}

export function newId(kind: IdKind, now: number = Date.now()): string {
  const time = BigInt(Math.floor(now)) & ((1n << 48n) - 1n);
  let n = (time << 52n) | randomBits52();
  const chars: string[] = [];
  for (let i = 0; i < 20; i++) {
    chars.push(ALPHABET[Number(n & 31n)]);
    n >>= 5n;
  }
  return `${PREFIXES[kind]}_${chars.reverse().join("")}`;
}
