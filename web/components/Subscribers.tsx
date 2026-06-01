"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

type Competition = { id: string; name: string | null; slug: string | null };
type Subscriber = {
  id: number;
  label: string | null;
  competition_id: string | null;
  callback_url: string;
  active: boolean;
  created_at: string;
};
type Outbox = {
  id: number;
  match_id: string;
  competition_id: string | null;
  status: string;
  attempts: number;
  last_error: string | null;
  updated_at: string;
};

const STATUSES = ["pending", "sending", "delivered", "failed"] as const;

export default function Subscribers({
  subscribers,
  competitions,
  outbox,
}: {
  subscribers: Subscriber[];
  competitions: Competition[];
  outbox: Outbox[];
}) {
  const router = useRouter();
  const supabase = useMemo(() => createClient(), []);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // Live outbox view (poll, like RunsPanel) layered over the server snapshot.
  const [rows, setRows] = useState<Outbox[]>(outbox);
  useEffect(() => {
    let active = true;
    const load = async () => {
      const { data } = await supabase
        .from("delivery_outbox")
        .select(
          "id,match_id,competition_id,status,attempts,last_error,updated_at",
        )
        .order("id", { ascending: false })
        .limit(200);
      if (active && data) setRows(data as Outbox[]);
    };
    const t = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [supabase]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const r of rows) c[r.status] = (c[r.status] ?? 0) + 1;
    return c;
  }, [rows]);

  const compName = (id: string | null) =>
    id == null ? "All leagues" : competitions.find((c) => c.id === id)?.name ?? id;
  const compSlug = (id: string | null) =>
    id == null ? null : competitions.find((c) => c.id === id)?.slug ?? null;

  // Trigger a crawl via the server route (it dispatches the GitHub workflow).
  async function postCrawl(payload: {
    kind: string;
    target: string;
    force?: boolean;
  }): Promise<number | null> {
    const res = await fetch("/api/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.error || `crawl dispatch failed (${res.status})`);
    return json.run_id ?? null;
  }

  // ---- new subscriber form ----
  const [form, setForm] = useState({
    label: "",
    competition_id: "",
    callback_url: "",
    secret: "",
  });

  function genSecret() {
    const a = new Uint8Array(32);
    crypto.getRandomValues(a);
    const hex = [...a].map((b) => b.toString(16).padStart(2, "0")).join("");
    setForm((f) => ({ ...f, secret: hex }));
  }

  // ---- crawl a (new) league ----
  const [crawl, setCrawl] = useState({ competition: "", force: false });

  async function crawlNewLeague() {
    const target = crawl.competition.trim();
    if (!target) {
      setErr("Enter a zerozero competition slug or URL to crawl.");
      return;
    }
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const runId = await postCrawl({
        kind: "backfill",
        target,
        force: crawl.force,
      });
      setNote(
        `Backfill crawl dispatched (run #${runId}). When it finishes, the ` +
          `league appears in the dropdown above and you can add a subscriber.`,
      );
      setCrawl({ competition: "", force: false });
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function addSubscriber() {
    setErr(null);
    setNote(null);
    if (!form.callback_url.trim() || !form.secret.trim()) {
      setErr("Callback URL and secret are required.");
      return;
    }
    setBusy(true);
    const { error } = await supabase.from("subscribers").insert({
      label: form.label.trim() || null,
      competition_id: form.competition_id || null,
      callback_url: form.callback_url.trim(),
      secret: form.secret.trim(),
    });
    setBusy(false);
    if (error) return setErr(error.message);
    setForm({ label: "", competition_id: "", callback_url: "", secret: "" });
    router.refresh();
  }

  async function toggleActive(s: Subscriber) {
    setBusy(true);
    setErr(null);
    const { error } = await supabase
      .from("subscribers")
      .update({ active: !s.active, updated_at: new Date().toISOString() })
      .eq("id", s.id);
    setBusy(false);
    if (error) return setErr(error.message);
    router.refresh();
  }

  async function removeSubscriber(s: Subscriber) {
    if (!confirm(`Delete subscriber "${s.label ?? s.callback_url}"?`)) return;
    setBusy(true);
    setErr(null);
    const { error } = await supabase.from("subscribers").delete().eq("id", s.id);
    setBusy(false);
    if (error) return setErr(error.message);
    router.refresh();
  }

  // Re-deliver matches ALREADY stored for this league (no crawl) — re-enqueues
  // one outbox event per match so a freshly-added subscriber gets the backlog.
  async function resend(s: Subscriber) {
    if (s.competition_id == null) {
      setErr("Re-send needs a specific league (not the all-leagues wildcard).");
      return;
    }
    if (
      !confirm(
        `Re-send every stored match in "${compName(s.competition_id)}" to all` +
          ` subscribers of that league?`,
      )
    )
      return;
    setBusy(true);
    setErr(null);
    setNote(null);
    const { data, error } = await supabase.rpc("replay_competition", {
      p_competition_id: s.competition_id,
    });
    setBusy(false);
    if (error) return setErr(error.message);
    setNote(`Enqueued ${data ?? 0} match event(s) for re-delivery.`);
  }

  // Crawl zerozero for every not-yet-complete round of this subscriber's league.
  async function crawlSubscriberLeague(s: Subscriber) {
    const slug = compSlug(s.competition_id);
    if (!slug) {
      setErr(
        "This league has no zerozero slug on record — crawl it from " +
          "“Crawl a league” below using its slug or URL.",
      );
      return;
    }
    if (
      !confirm(
        `Crawl all fixtures for "${compName(s.competition_id)}" from zerozero?` +
          ` Rounds already final are skipped.`,
      )
    )
      return;
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const runId = await postCrawl({ kind: "backfill", target: slug });
      setNote(`Backfill crawl dispatched (run #${runId}). Track it on Crawl runs.`);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function retry(id: number) {
    setBusy(true);
    setErr(null);
    const { error } = await supabase.rpc("requeue_outbox", { p_id: id });
    setBusy(false);
    if (error) return setErr(error.message);
  }

  const failed = rows.filter((r) => r.status === "failed");

  return (
    <>
      {err && <p style={{ color: "var(--red)", marginTop: 0 }}>{err}</p>}
      {note && <p style={{ color: "var(--green)", marginTop: 0 }}>{note}</p>}

      {/* ---- how to subscribe (on-page docs) ---- */}
      <div className="panel">
        <details>
          <summary style={{ cursor: "pointer", fontWeight: 600 }}>
            How to subscribe a site to a league
          </summary>
          <div style={{ marginTop: 12 }}>
            <p className="muted" style={{ marginTop: 0 }}>
              A subscriber is any URL that receives a signed{" "}
              <code>POST</code> every time a match in its league changes (score,
              status, player stats, reporter ratings). Each event is the full
              current snapshot of the match. Recommended order:
            </p>
            <ol style={{ lineHeight: 1.7, paddingLeft: 20 }}>
              <li>
                <strong>Make sure the league is crawled.</strong> If it&apos;s
                already in the league dropdown, it&apos;s in the database. For a
                brand-new league, use <strong>Crawl a league</strong> below with
                its zerozero slug (e.g. <code>liga-portuguesa</code>) or full{" "}
                <code>/competicao/…</code> URL. The crawl fetches every
                not-yet-final round and creates the league; track it on{" "}
                <a href="/runs">Crawl runs</a>. Once done, refresh this page and
                the league shows in the dropdown.
              </li>
              <li>
                <strong>Add the subscriber</strong> (form below): pick the
                league, paste the subscriber&apos;s callback URL, and generate a
                shared HMAC secret. Give that same secret to the subscriber site
                (its <code>CRAWLER_WEBHOOK_SECRET</code>) so it can verify the{" "}
                <code>X-Signature</code> header.
              </li>
              <li>
                <strong>Seed the backlog.</strong> New events flow
                automatically from here on. To push the matches that already
                existed <em>before</em> you added the subscriber, hit{" "}
                <strong>re-send</strong> on the subscriber row — it re-delivers
                every stored match in that league (ingest is idempotent, so
                re-sends are safe). If you crawled in step 1, do this after
                subscribing so those fixtures land too.
              </li>
            </ol>
            <p className="muted" style={{ marginBottom: 0 }}>
              <strong>crawl</strong> (re)fetches fixtures from zerozero;{" "}
              <strong>re-send</strong> re-delivers what&apos;s already stored —
              no crawl. A subscriber with <em>All leagues</em> receives every
              league and can&apos;t be crawled/re-sent per-league. Full design
              and the subscriber-side setup live in{" "}
              <code>docs/SUBSCRIPTIONS.md</code>.
            </p>
          </div>
        </details>
      </div>

      {/* ---- subscribers list ---- */}
      <div className="panel">
        <h2>Active subscribers</h2>
        {subscribers.length === 0 ? (
          <p className="muted">No subscribers yet — add one below.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Label</th>
                <th>League</th>
                <th>Callback</th>
                <th>State</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {subscribers.map((s) => (
                <tr key={s.id}>
                  <td>{s.label ?? <span className="muted">—</span>}</td>
                  <td>{compName(s.competition_id)}</td>
                  <td
                    className="muted"
                    style={{
                      maxWidth: 280,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={s.callback_url}
                  >
                    {s.callback_url}
                  </td>
                  <td>
                    <span className={`pill ${s.active ? "success" : "queued"}`}>
                      {s.active ? "active" : "paused"}
                    </span>
                  </td>
                  <td>
                    <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                      <button
                        className="btn secondary"
                        disabled={busy}
                        onClick={() => toggleActive(s)}
                      >
                        {s.active ? "pause" : "resume"}
                      </button>
                      <button
                        className="btn secondary"
                        disabled={busy || s.competition_id == null}
                        onClick={() => crawlSubscriberLeague(s)}
                        title="Crawl zerozero for every fixture of this league"
                      >
                        crawl
                      </button>
                      <button
                        className="btn secondary"
                        disabled={busy || s.competition_id == null}
                        onClick={() => resend(s)}
                        title="Re-deliver matches already stored for this league"
                      >
                        re-send
                      </button>
                      <button
                        className="btn secondary"
                        disabled={busy}
                        onClick={() => removeSubscriber(s)}
                      >
                        ✕
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ---- add subscriber ---- */}
      <div className="panel">
        <h2>Add subscriber</h2>
        <div style={{ display: "grid", gap: 10, maxWidth: 560 }}>
          <input
            placeholder="Label (e.g. fantasy-site prod)"
            value={form.label}
            disabled={busy}
            onChange={(e) => setForm((f) => ({ ...f, label: e.target.value }))}
          />
          <select
            value={form.competition_id}
            disabled={busy}
            onChange={(e) =>
              setForm((f) => ({ ...f, competition_id: e.target.value }))
            }
          >
            <option value="">All leagues</option>
            {competitions.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name ?? c.id}
              </option>
            ))}
          </select>
          <input
            placeholder="Callback URL (https://…/ingest)"
            value={form.callback_url}
            disabled={busy}
            onChange={(e) =>
              setForm((f) => ({ ...f, callback_url: e.target.value }))
            }
          />
          <div className="row" style={{ gap: 6 }}>
            <input
              placeholder="Shared HMAC secret"
              value={form.secret}
              disabled={busy}
              style={{ flex: 1, fontFamily: "monospace", fontSize: 13 }}
              onChange={(e) =>
                setForm((f) => ({ ...f, secret: e.target.value }))
              }
            />
            <button className="btn secondary" disabled={busy} onClick={genSecret}>
              generate
            </button>
          </div>
          <p className="muted" style={{ fontSize: 12, margin: 0 }}>
            Give the same secret to the subscriber site (its{" "}
            <code>CRAWLER_WEBHOOK_SECRET</code>) so it can verify the{" "}
            <code>X-Signature</code> header.
          </p>
          <div>
            <button className="btn" disabled={busy} onClick={addSubscriber}>
              Add subscriber
            </button>
          </div>
        </div>
      </div>

      {/* ---- crawl a (new) league ---- */}
      <div className="panel">
        <h2>Crawl a league</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Fetch every fixture of a zerozero competition (all rounds). Rounds
          already complete are skipped unless you force a full re-crawl. Use
          this to add a league that isn&apos;t in the dropdown yet, or to
          refresh a league&apos;s schedule.
        </p>
        <div style={{ display: "grid", gap: 10, maxWidth: 560 }}>
          <input
            placeholder="zerozero slug or URL (e.g. liga-portuguesa)"
            value={crawl.competition}
            disabled={busy}
            onChange={(e) =>
              setCrawl((c) => ({ ...c, competition: e.target.value }))
            }
          />
          <label className="row" style={{ gap: 8, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={crawl.force}
              disabled={busy}
              onChange={(e) =>
                setCrawl((c) => ({ ...c, force: e.target.checked }))
              }
            />
            <span className="muted" style={{ fontSize: 13 }}>
              Force: re-crawl every round, even ones already final
            </span>
          </label>
          <div>
            <button className="btn" disabled={busy} onClick={crawlNewLeague}>
              Crawl fixtures
            </button>
          </div>
        </div>
      </div>

      {/* ---- delivery health ---- */}
      <div className="panel">
        <h2>Delivery outbox</h2>
        <div className="row" style={{ gap: 8, marginBottom: 12 }}>
          {STATUSES.map((s) => (
            <span
              key={s}
              className={`pill ${
                s === "delivered"
                  ? "success"
                  : s === "failed"
                    ? "error"
                    : s === "sending"
                      ? "running"
                      : "queued"
              }`}
            >
              {s}: {counts[s] ?? 0}
            </span>
          ))}
        </div>

        {failed.length === 0 ? (
          <p className="muted">No failed deliveries.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Match</th>
                <th>League</th>
                <th className="num">Tries</th>
                <th>Last error</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {failed.map((r) => (
                <tr key={r.id}>
                  <td>{r.match_id}</td>
                  <td>{compName(r.competition_id)}</td>
                  <td className="num">{r.attempts}</td>
                  <td
                    className="tag-red"
                    style={{
                      maxWidth: 360,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={r.last_error ?? ""}
                  >
                    {r.last_error}
                  </td>
                  <td>
                    <button
                      className="btn secondary"
                      disabled={busy}
                      onClick={() => retry(r.id)}
                    >
                      retry
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
