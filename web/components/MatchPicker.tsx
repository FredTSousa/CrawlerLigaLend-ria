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
        <>
          <CrawlButton kind="match" target={url.trim()} key={"c" + url.trim()} label="Crawl once" />
          <CrawlButton kind="watch" target={url.trim()} key={"w" + url.trim()} label="Watch live" />
        </>
      ) : (
        <button className="btn" disabled>
          Crawl match
        </button>
      )}
    </div>
  );
}
