import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "TeamSwarm — Multi-agent orchestration",
  description: "A permissioned, observable control plane for teams of AI agents.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
