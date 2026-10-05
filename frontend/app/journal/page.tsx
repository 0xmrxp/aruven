"use client";

import { useEffect, useState } from "react";
import { getJSON, short, JournalRow } from "@/lib/api";

export default function JournalPage() {
  const [rows, setRows] = useState<JournalRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      getJSON<JournalRow[]>("/journal?limit=500")
        .then(setRows)
        .catch((e) => setErr(String(e)));
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, []);

  if (err) return <div className="error">backend unreachable: {err}</div>;
  if (!rows) return <div>loading…</div>;

  return (
    <main>
      <section className="hero">
        <h1>Agent journal</h1>
        <p>
          The complete, hash-chained decision log: every observation, proposal,
          review, execution, and halt. Each entry links to the previous one by
          hash; tampering breaks the chain and is detectable by anyone.
        </p>
      </section>
      <div className="card feed">
        {rows.map((e) => (
          <div className="feed-item" key={e.id}>
            <span className={`feed-kind${e.kind === "halt" ? " halt" : ""}`}>{e.kind}</span>
            <div className="feed-body">
              <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, fontFamily: "var(--mono)" }}>
                {JSON.stringify(e.payload, null, 2)}
              </pre>
              <div className="muted">
                {new Date(e.ts * 1000).toISOString()} · prev {short(e.prev_hash)} → {short(e.hash)}
              </div>
            </div>
          </div>
        ))}
        {rows.length === 0 && <div className="feed-item"><div className="feed-body muted">empty journal</div></div>}
      </div>
    </main>
  );
}
