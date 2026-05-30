"use client";

import { useState } from "react";
import CrawlButton from "./CrawlButton";

export default function MatchPicker() {
  const [url, setUrl] = useState("");
  const valid = /\/jogo\/[^/]+\/\d+/.test(url);
  return (
    <div className="row">
      <input
        type="url"
        placeholder="https://www.zerozero.pt/jogo/.../12083086"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        style={{ flex: 1, minWidth: 260 }}
      />
      {valid ? (
        <CrawlButton kind="match" target={url.trim()} key={url.trim()} label="Crawl match" />
      ) : (
        <button className="btn" disabled>
          Crawl match
        </button>
      )}
    </div>
  );
}
