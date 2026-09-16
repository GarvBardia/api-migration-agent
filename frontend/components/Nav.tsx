"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Runs" },
  { href: "/review", label: "Review queue" },
  { href: "/repos", label: "Saved repos" },
];

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/" || pathname.startsWith("/runs");
  return pathname.startsWith(href);
}

export default function Nav() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-10 border-b border-border bg-surface/80 backdrop-blur supports-[backdrop-filter]:bg-surface/60">
      <nav className="mx-auto flex max-w-5xl items-center gap-1 px-6 py-3.5">
        <Link
          href="/"
          className="mr-4 flex items-center gap-2 text-[15px] font-semibold tracking-tight text-foreground"
        >
          <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent text-[11px] font-bold text-accent-foreground">
            M
          </span>
          Migration Agent
        </Link>
        {LINKS.map((link) => {
          const active = isActive(pathname ?? "", link.href);
          return (
            <Link
              key={link.href}
              href={link.href}
              aria-current={active ? "page" : undefined}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                active
                  ? "bg-surface-muted text-foreground"
                  : "text-foreground-muted hover:text-foreground"
              }`}
            >
              {link.label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}
