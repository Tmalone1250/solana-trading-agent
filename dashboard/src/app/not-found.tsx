import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
      <h1 className="font-mono text-2xl font-bold text-slate-100">404</h1>
      <p className="text-sm text-slate-400">Page not found.</p>
      <Link
        href="/"
        className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 font-mono text-xs text-sky-300 hover:border-sky-500"
      >
        Return to Dashboard
      </Link>
    </main>
  );
}
