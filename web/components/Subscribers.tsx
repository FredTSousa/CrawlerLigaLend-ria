"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

type Competition = { id: string; name: string | null };
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

  async function backfill(s: Subscriber) {
    if (
      !confirm(
        `Re-send every match in "${compName(s.competition_id)}" to all` +
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
                        disabled={busy}
                        onClick={() => backfill(s)}
                        title="Re-send every match in this league"
                      >
                        backfill
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
