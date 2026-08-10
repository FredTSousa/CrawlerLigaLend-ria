"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

type Item = { href: string; label: string };
type Group = { label: string; items: Item[] };
type Entry = Item | Group;

const NAV: Entry[] = [
  { href: "/", label: "Dashboard" },
  { href: "/competitions", label: "Competitions" },
  {
    label: "Runs",
    items: [
      { href: "/runs", label: "Crawl runs" },
      { href: "/reporter-runs", label: "Reporter runs" },
    ],
  },
  {
    label: "Data",
    items: [
      { href: "/linking-issues", label: "Linking issues" },
      { href: "/mvp-missing", label: "MVP missing" },
      { href: "/team-aliases", label: "Team aliases" },
      { href: "/subscribers", label: "Subscribers" },
    ],
  },
];

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(href + "/");
}

export default function Nav() {
  const pathname = usePathname();
  const [open, setOpen] = useState<string | null>(null);
  const navRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (navRef.current && !navRef.current.contains(e.target as Node)) {
        setOpen(null);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(null);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Close any open menu after navigating.
  useEffect(() => {
    setOpen(null);
  }, [pathname]);

  return (
    <nav ref={navRef}>
      {NAV.map((entry) => {
        if ("href" in entry) {
          return (
            <Link
              key={entry.href}
              href={entry.href}
              className={isActive(pathname, entry.href) ? "active" : undefined}
            >
              {entry.label}
            </Link>
          );
        }

        const groupActive = entry.items.some((it) => isActive(pathname, it.href));
        const isOpen = open === entry.label;
        return (
          <div key={entry.label} className="navgroup">
            <button
              type="button"
              className={`navtrigger${groupActive ? " active" : ""}`}
              aria-expanded={isOpen}
              aria-haspopup="true"
              onClick={() => setOpen(isOpen ? null : entry.label)}
            >
              {entry.label}
              <span className="caret" aria-hidden>▾</span>
            </button>
            {isOpen && (
              <div className="navmenu" role="menu">
                {entry.items.map((it) => (
                  <Link
                    key={it.href}
                    href={it.href}
                    role="menuitem"
                    className={isActive(pathname, it.href) ? "active" : undefined}
                  >
                    {it.label}
                  </Link>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </nav>
  );
}
