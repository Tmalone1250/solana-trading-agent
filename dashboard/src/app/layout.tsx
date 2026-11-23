import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Chibi Trading Agent | Live Telemetry Dashboard",
  description: "Autonomous 3-State Focus Machine Solana trading agent dashboard",
  icons: {
    icon: "/chibi_agent.png",
    apple: "/chibi_agent.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen antialiased selection:bg-sky-500/30">
        {children}
      </body>
    </html>
  );
}
