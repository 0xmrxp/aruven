"use client";

import { useEffect, useState } from "react";
import { getJSON, DeploymentRow } from "@/lib/api";

export default function Deployments() {
  const [rows, setRows] = useState<DeploymentRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getJSON<DeploymentRow[]>("/deployments")
      .then(setRows)
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) return <div className="error">backend unreachable: {err}</div>;
  if (!rows) return <div>loading…</div>;

  return (
    <main>
      <section className="hero">
        <h1>Deployments</h1>
        <p>
          Every agent-run token launched through the ARUVEN factory, discovered
          from the factory&rsquo;s <span className="nav">Launch</span> events.
          Forks that bypass the factory are not indexed here — that is the point.
        </p>
      </section>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>token</th><th>vault</th><th>creator</th><th>fee share</th><th>block</th><th>tx</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.tx_hash}>
                <td>{r.token.slice(0, 10)}… <span className="badge">verified</span></td>
                <td>{r.vault.slice(0, 10)}…</td>
                <td>{r.creator.slice(0, 10)}…</td>
                <td>{r.fee_share_bps / 100}%</td>
                <td>{r.block}</td>
                <td>{r.tx_hash.slice(0, 10)}…</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={6} className="muted">no deployments yet — be the first</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </main>
  );
}
