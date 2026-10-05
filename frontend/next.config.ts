import type { NextConfig } from "next";

const config: NextConfig = {
  // The dashboard is fully static + client-side fetches from the backend API
  output: "export",
};

export default config;
