import Link from "next/link";
import RunsPanel from "@/components/RunsPanel";

export const dynamic = "force-dynamic";

export default function RunsPage() {
  return (
    <>
      <div className="panel">
        <h2>Crawl runs</h2>
        <RunsPanel limit={50} />
      </div>
      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}
