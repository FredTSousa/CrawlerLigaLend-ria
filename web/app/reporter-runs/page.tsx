import Link from "next/link";
import RunsPanel from "@/components/RunsPanel";

export const dynamic = "force-dynamic";

export default function ReporterRunsPage() {
  return (
    <>
      <div className="panel">
        <h2>Reporter crawl runs — A Bola &amp; Goal</h2>
        <RunsPanel limit={50} kind="reporter" />
      </div>
      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}
