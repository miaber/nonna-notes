import { useState, useEffect, useRef } from "react";

function playTimerAlarm() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const playBeep = (startTime, freq = 880, duration = 0.15) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = freq;
      osc.type = "sine";
      gain.gain.setValueAtTime(0.25, startTime);
      gain.gain.exponentialRampToValueAtTime(0.01, startTime + duration);
      osc.start(startTime);
      osc.stop(startTime + duration);
    };
    playBeep(0);
    playBeep(0.25);
    playBeep(0.5);
  } catch (_) {}
}

function Timer({ timer, onStart, onDismiss }) {
  const { startedAt, duration, label, id, stopwatch } = timer;
  const pending = !stopwatch && !startedAt;
  const hasPlayedAlarm = useRef(false);

  const [remaining, setRemaining] = useState(() => {
    if (stopwatch && startedAt) return null;
    if (!startedAt) return duration;
    const elapsed = Math.floor((Date.now() - startedAt) / 1000);
    return Math.max(0, duration - elapsed);
  });
  const [elapsed, setElapsed] = useState(() => {
    if (!stopwatch || !startedAt) return 0;
    return Math.floor((Date.now() - startedAt) / 1000);
  });
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (stopwatch && startedAt) {
      const tick = setInterval(() => {
        setElapsed(Math.floor((Date.now() - startedAt) / 1000));
      }, 1000);
      return () => clearInterval(tick);
    }
    if (!startedAt || done) return;
    if (remaining <= 0) {
      if (!hasPlayedAlarm.current) {
        hasPlayedAlarm.current = true;
        playTimerAlarm();
      }
      setDone(true);
      return;
    }
    const tick = setInterval(() => {
      setRemaining((r) => {
        if (r <= 1) {
          if (!hasPlayedAlarm.current) {
            hasPlayedAlarm.current = true;
            playTimerAlarm();
          }
          setDone(true);
          return 0;
        }
        return r - 1;
      });
    }, 1000);
    return () => clearInterval(tick);
  }, [startedAt, stopwatch, done]); // eslint-disable-line react-hooks/exhaustive-deps

  if (stopwatch && startedAt) {
    const mins = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const secs = String(elapsed % 60).padStart(2, "0");
    return (
      <div className="timer-card timer-card-stopwatch pending">
        <div className="timer-content">
          <span className="timer-label">{label}</span>
          <span className="timer-time">{mins}:{secs}</span>
        </div>
      </div>
    );
  }

  const progress = done ? 100 : pending ? 0 : ((duration - remaining) / duration) * 100;
  const mins = String(Math.floor(remaining / 60)).padStart(2, "0");
  const secs = String(remaining % 60).padStart(2, "0");

  return (
    <div
      className={`timer-card ${done ? "done" : ""} ${pending ? "pending" : ""}`}
      onClick={pending ? () => onStart(id) : undefined}
      style={pending ? { cursor: "pointer" } : undefined}
    >
      <div className="timer-progress" style={{ width: `${progress}%` }} />
      <div className="timer-content">
        <span className="timer-label">{label}</span>
        <span className="timer-time">
          {done ? "Done" : pending ? `${mins}:${secs} — tap or say "start"` : `${mins}:${secs}`}
        </span>
      </div>
      {done && (
        <button className="timer-dismiss" onClick={(e) => { e.stopPropagation(); onDismiss(id); }}>
          ✕
        </button>
      )}
    </div>
  );
}

export default function TimerPanel({ timers, onStart, onDismiss }) {
  if (timers.length === 0) return null;
  return (
    <div className="timer-panel">
      {timers.map((t) => (
        <Timer key={t.id} timer={t} onStart={onStart} onDismiss={onDismiss} />
      ))}
    </div>
  );
}
