import { useEffect, useRef, useState } from "react";
import "./MusicPlayer.css";

const DUCK_VOLUME = 12;

export default function MusicPlayer({ query, videoId, volume = 75, isSpeaking, onStop, compact = false }) {
  const [minimized, setMinimized] = useState(!compact);
  const [isPaused, setIsPaused] = useState(false);
  const playerRef = useRef(null);
  const containerRef = useRef(null);
  // Keep a ref so the ducking effect always sees the latest baseline volume
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

  const togglePlayPause = () => {
    const player = playerRef.current;
    if (!player?.getPlayerState) return;
    try {
      const state = player.getPlayerState?.();
      if (state === 1) {
        player.pauseVideo?.();
        setIsPaused(true);
      } else {
        player.playVideo?.();
        setIsPaused(false);
      }
    } catch (_) {}
  };

  useEffect(() => {
    if (!playerRef.current?.getPlayerState) return;
    const interval = setInterval(() => {
      try {
        const state = playerRef.current?.getPlayerState?.();
        setIsPaused(state !== 1 && state !== 3);
      } catch (_) {}
    }, 500);
    return () => clearInterval(interval);
  }, [videoId]);

  if (compact) {
    return (
      <div className="music-player music-player-compact">
        <div className="music-player-compact-bar">
          <button type="button" className="music-play-pause-btn" onClick={togglePlayPause} title={isPaused ? "Play" : "Pause"} aria-label={isPaused ? "Play" : "Pause"}>
            {isPaused ? "▶" : "⏸"}
          </button>
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
      {/* Always mount the container so the player isn't destroyed on minimize */}
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
