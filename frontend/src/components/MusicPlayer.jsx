import { useEffect, useRef, useState } from "react";
import "./MusicPlayer.css";

const DUCK_VOLUME = 12;

export default function MusicPlayer({ query, videoId, volume = 75, isSpeaking, onStop, compact = false }) {
  const [minimized, setMinimized] = useState(!compact);
  const playerRef = useRef(null);
  const containerRef = useRef(null);
  const volumeRef = useRef(volume);
  useEffect(() => { volumeRef.current = volume; }, [volume]);

  // Create/destroy the YT player when videoId changes
  useEffect(() => {
    if (!videoId || !containerRef.current) return;

    const createPlayer = () => {
      if (!containerRef.current) return;
      playerRef.current = new window.YT.Player(containerRef.current, {
        height: "158",
        width: "100%",
        videoId,
        playerVars: { autoplay: 1, modestbranding: 1, rel: 0 },
        events: {
          onReady: (e) => {
            e.target.setVolume(volumeRef.current);
            e.target.playVideo();
          },
        },
      });
    };

    if (window.YT?.Player) {
      createPlayer();
    } else {
      if (!document.getElementById("yt-api-script")) {
        const tag = document.createElement("script");
        tag.id = "yt-api-script";
        tag.src = "https://www.youtube.com/iframe_api";
        document.head.appendChild(tag);
      }
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => {
        prev?.();
        createPlayer();
      };
    }

    return () => {
      playerRef.current?.destroy();
      playerRef.current = null;
    };
  }, [videoId]);

  // Duck while Gemini is speaking, restore when done
  useEffect(() => {
    const player = playerRef.current;
    if (!player?.setVolume) return;
    player.setVolume(isSpeaking ? DUCK_VOLUME : volumeRef.current);
  }, [isSpeaking, volume]);

  const ytSearchUrl = `https://www.youtube.com/results?search_query=${encodeURIComponent(query)}`;

  if (compact) {
    return (
      <div className="music-player music-player-compact">
        <div className="music-player-compact-bar">
          <span className="music-label" title={query}>{query}</span>
          <button type="button" className="music-close-btn" onClick={onStop} title="Stop music">✕</button>
        </div>
        {videoId && <div ref={containerRef} className="music-frame-container music-frame-hidden" aria-hidden="true" />}
      </div>
    );
  }

  return (
    <div className={`music-player ${minimized ? "minimized" : ""}`}>
      <div className="music-player-header">
        <span className="music-icon">♪</span>
        <span className="music-label" title={query}>{query}</span>
        <button className="music-min-btn" onClick={() => setMinimized((v) => !v)} title={minimized ? "Expand" : "Minimise"}>
          {minimized ? "▲" : "▼"}
        </button>
        <button className="music-close-btn" onClick={onStop} title="Stop music">✕</button>
      </div>
      <div style={{ display: minimized ? "none" : "block" }}>
        {videoId ? (
          <div ref={containerRef} className="music-frame-container" />
        ) : (
          <div className="music-fallback">
            <p>No YouTube API key set.</p>
            <a href={ytSearchUrl} target="_blank" rel="noreferrer">Open on YouTube ↗</a>
          </div>
        )}
      </div>
    </div>
  );
}
