"use client";

import { useEffect, useState } from "react";
import { getJSON, short, fmtRaw, VaultResponse, TradeRow, JournalRow, VerifyResponse } from "@/lib/api";

export default function Dashboard() {
  const [vault, setVault] = useState<VaultResponse | null>(null);
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [journal, setJournal] = useState<JournalRow[]>([]);
  const [verify, setVerify] = useState<VerifyResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [v, t, j, ver] = await Promise.all([
          getJSON<VaultResponse>("/vault"),
          getJSON<TradeRow[]>("/trades?limit=20"),
          getJSON<JournalRow[]>("/journal?limit=30"),
          getJSON<VerifyResponse>("/verify"),
        ]);
        setVault(v); setTrades(t); setJournal(j); setVerify(ver);
      } catch (e) {
        setErr(String(e));
      }
    };
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, []);

  if (err) return <div className="error">backend unreachable: {err}</div>;
  if (!vault) return <div>loading…</div>;

  return (
    <main>
      <section className="hero">
        <h1>
          The agent-run vault.
          <br />
          <span className="nav">Verifiable, on-chain, live.</span>
        </h1>
        <p>
          ARUVEN pairs an open-source trading agent with a non-custodial treasury on
          Robinhood Chain. Fees route in by hook. Trades execute within
          contract-enforced risk limits. The journal below is the agent&rsquo;s
          actual decision log.
        </p>
      </section>

      <section className="grid cols-4">
        <div className="card">
          <h3>Vault NAV</h3>
          <div className="big">${vault.nav_usd.toLocaleString()}</div>
          <div className="sub">live holdings × configured prices</div>
        </div>
        <div className="card">
          <h3>Epoch</h3>
          <div className="big">{vault.epoch}</div>
          <div className="sub">journal head: {short(vault.journal_head)}</div>
        </div>
        <div className="card">
          <h3>Trades indexed</h3>
          <div className="big">{verify?.event_counts.trades ?? "—"}</div>
          <div className="sub">from raw chain logs</div>
        </div>
        <div className="card">
          <h3>Burns indexed</h3>
          <div className="big">{verify?.event_counts.burns ?? "—"}</div>
          <div className="sub">total: {fmtRaw(verify?.burn_total_raw ?? 0)} $ARUVEN</div>
        </div>
      </section>

      <section className="grid cols-2" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>Vault holdings</h3>
          <table>
            <tbody>
              {Object.entries(vault.holdings_raw).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td style={{ textAlign: "right" }}>{fmtRaw(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card">
          <h3>Fee inflow (lifetime, raw)</h3>
          <table>
            <tbody>
              {Object.entries(vault.fee_inflow_raw).map(([k, v]) => (
                <tr key={k}>
                  <td>{short(k)}</td>
                  <td style={{ textAlign: "right" }}>{fmtRaw(v)}</td>
                </tr>
              ))}
              {Object.keys(vault.fee_inflow_raw).length === 0 && (
                <tr><td className="muted">no fees indexed yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="grid cols-2" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>Agent journal — live decisions</h3>
          <div className="feed">
            {journal.map((e) => (
              <div className="feed-item" key={e.id}>
                <span className={`feed-kind${e.kind === "halt" ? " halt" : ""}`}>{e.kind}</span>
                <div className="feed-body">
                  {e.kind === "proposal"
                    ? `${e.payload.token_in} → ${e.payload.token_out}: ${e.payload.reason ?? ""}`
                    : e.kind === "execution"
                      ? `tx ${short(String(e.payload.tx_hash))}`
                      : e.kind === "halt"
                        ? `${e.payload.stage}: ${e.payload.reason}`
                        : JSON.stringify(e.payload).slice(0, 120)}
                  <div className="muted">{new Date(e.ts * 1000).toISOString()} · {short(e.hash)}</div>
                </div>
              </div>
            ))}
            {journal.length === 0 && <div className="feed-item"><div className="feed-body muted">no journal entries yet</div></div>}
          </div>
        </div>
        <div className="card">
          <h3>Recent vault trades</h3>
          <table>
            <thead>
              <tr><th>block</th><th>in</th><th>out</th><th>tx</th></tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <tr key={t.tx_hash}>
                  <td>{t.block}</td>
                  <td>{short(t.token_in)}</td>
                  <td>{short(t.token_out)}</td>
                  <td>{short(t.tx_hash)}</td>
                </tr>
              ))}
              {trades.length === 0 && (
                <tr><td colSpan={4} className="muted">the agent has not traded yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
