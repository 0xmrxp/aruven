export const API_BASE =
  process.env.NEXT_PUBLIC_ARUVEN_API ?? "http://127.0.0.1:8000";

export interface VaultResponse {
  vault_address: string;
  holdings_raw: Record<string, number>;
  nav_usd: number;
  price_sources: Record<string, number>;
  fee_inflow_raw: Record<string, number>;
  epoch: number;
  journal_head: string | null;
  note?: string;
}

export interface TradeRow {
  block: number;
  tx_hash: string;
  token_in: string;
  token_out: string;
  amount_in: string;
  amount_out: string;
  reason_hash: string;
}

export interface JournalRow {
  id: string;
  ts: number;
  kind: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface DeploymentRow {
  block: number;
  tx_hash: string;
  token: string;
  vault: string;
  creator: string;
  fee_share_bps: number;
  meta_hash: string;
}

export interface VerifyResponse {
  event_counts: { trades: number; fees: number; burns: number; launches: number };
  burn_total_raw: number;
  head_block: number;
  method: string;
}

export async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json() as Promise<T>;
}

export function short(a: string | null | undefined): string {
  if (!a) return "—";
  return `${a.slice(0, 8)}…${a.slice(-6)}`;
}

export function fmtRaw(raw: number | string, decimals = 18, digits = 2): string {
  const v = typeof raw === "string" ? Number(raw) : raw;
  if (!Number.isFinite(v)) return "—";
  return (v / 10 ** decimals).toLocaleString(undefined, {
    maximumFractionDigits: digits,
  });
}
