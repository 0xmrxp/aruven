import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ARUVEN — agent-run vault, verifiable on-chain",
  description:
    "ARUVEN: an autonomous trading agent managing a transparent on-chain treasury on Robinhood Chain. Every number is a read call.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="container">
          <header className="site">
            <div className="logo">ARUVEN</div>
            <nav>
              <a href="/">Dashboard</a>
              <a href="/deployments">Deployments</a>
              <a href="/journal">Journal</a>
            </nav>
          </header>
          {children}
          <footer className="site">
            Every figure on this page is served by the ARUVEN indexer and can be
            re-derived from raw chain logs via <span className="nav">/verify</span>.
            The agent can lose money — that risk is journaled, on-chain, and visible.
          </footer>
        </div>
      </body>
    </html>
  );
}
