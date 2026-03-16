import { useRef, useState, useCallback, useEffect } from "react";

const _WS_BASE = import.meta.env.VITE_WS_URL || "ws://localhost:8000/ws";
const _DEV_TOKEN = import.meta.env.VITE_ACCESS_TOKEN || "";

async function _buildWsUrl(getToken) {
  if (getToken) {
    const tok = await getToken();
    return `${_WS_BASE}?token=${encodeURIComponent(tok)}`;
  }
  return _DEV_TOKEN ? `${_WS_BASE}?token=${encodeURIComponent(_DEV_TOKEN)}` : _WS_BASE;
}
const RECIPE_AGENT_URL = import.meta.env.VITE_RECIPE_AGENT_URL || "http://localhost:8001";
const VIDEO_FPS = 0.5; // 1 frame every 2 seconds — reduces Live API 1008/1011 disconnects

export function useGeminiLive() {
  const [status, setStatus] = useState("idle");
  const [transcript, setTranscript] = useState([]);
  const [timers, setTimers] = useState([]);         // { id, label, duration, startedAt }
  const [completedSteps, setCompletedSteps] = useState(new Set());
  const [recipeSteps, setRecipeSteps] = useState([]); // string[]
  const [ingredients, setIngredients] = useState([]); // { amount, name }[]
  const [structuredRecipe, setStructuredRecipe] = useState(null); // full RecipeSchema object
  const [liveSteps, setLiveSteps] = useState([]);     // document mode observed steps
  const [draftName, setDraftName] = useState(null);   // document mode: recipe name set by set_draft_name
  const [recipeSearchStatus, setRecipeSearchStatus] = useState(null); // null | "searching" | { error: string }
  const [currentMusic, setCurrentMusic] = useState(null); // null | { query: string, videoId: string|null }
  const [isSpeaking, setIsSpeaking] = useState(false);   // true while Gemini audio is playing
  const [isReconnecting, setIsReconnecting] = useState(false);
  const [musicVolume, setMusicVolume] = useState(35);     // 0-100, user/AI-controlled baseline
  const [capturedPhotos, setCapturedPhotos] = useState({}); // { [stepNumber]: ["data:image/jpeg;base64,...", ...] }
  const [recipeSaved, setRecipeSaved] = useState(false);
  const [recipeSavedId, setRecipeSavedId] = useState(null);
  const speakingRef = useRef(false);
  // Set to true when turn_complete arrives but audio is still playing.
  // The last buffer's "ended" callback reads this to clear isSpeaking.
  const turnCompleteRef = useRef(false);

  // Pre-load the AudioWorklet module on mount so the browser caches it.
  // When startSession runs, addModule resolves instantly from cache.
  useEffect(() => {
    const ctx = new AudioContext();
    ctx.audioWorklet.addModule("/audio-processor.js")
      .catch(() => {})
      .finally(() => ctx.close());
  }, []);

  // Auto-clear recipe search error after 5s so overlay doesn't block
  useEffect(() => {
    if (recipeSearchStatus && typeof recipeSearchStatus === "object" && recipeSearchStatus.error) {
      const t = setTimeout(() => setRecipeSearchStatus(null), 5000);
      return () => clearTimeout(t);
    }
  }, [recipeSearchStatus]);

  const wsRef = useRef(null);
  const isStartingRef = useRef(false); // guard against concurrent startSession calls

  // Close WS on unmount so the backend session doesn't outlive the component (HMR, page nav, etc.)
  useEffect(() => {
    return () => {
      wsRef.current?.close();
      streamRef.current?.getTracks().forEach(t => t.stop());
      playbackContextRef.current?.close().catch(() => {});
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const stopwatchStartedAtRef = useRef(null);
  useEffect(() => {
    const stopwatch = timers.find((t) => t.stopwatch && t.startedAt);
    stopwatchStartedAtRef.current = stopwatch?.startedAt ?? null;
  }, [timers]);

  const audioContextRef = useRef(null);      // mic input context
  const playbackContextRef = useRef(null);   // speaker output context (persistent)
  const nextPlayTimeRef = useRef(0);         // scheduled end of last queued chunk
  const activeSourcesRef = useRef([]);       // all live BufferSourceNodes (so we can stop them instantly)
  const streamRef = useRef(null);
  const videoIntervalRef = useRef(null);
  const workletNodeRef = useRef(null);

  const startSession = useCallback(async (videoElement, recipe = "", persona = "nonna", recipeFromLibrary = null, resumeDraft = false, resumeDraftStartedAt = null, getToken = null, recipeImageFiles = null, documentMode = false) => {
    if (isStartingRef.current) return; // prevent double-start from rapid clicks / StrictMode
    isStartingRef.current = true;
    // Tear down any previous session first to avoid 409 ALREADY_EXISTS
    clearInterval(videoIntervalRef.current);
    if (wsRef.current && wsRef.current.readyState <= WebSocket.OPEN) {
      wsRef.current.close();
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    workletNodeRef.current?.disconnect();
    workletNodeRef.current = null;
    activeSourcesRef.current.forEach(s => { try { s.stop(0); } catch {} });
    activeSourcesRef.current = [];
    audioContextRef.current?.close().catch(() => {});
    playbackContextRef.current?.close().catch(() => {});
    playbackContextRef.current = null;
    nextPlayTimeRef.current = 0;

    setTranscript([]);
    setTimers([]);
    setCompletedSteps(new Set());
    setRecipeSteps([]);
    setIngredients([]);
    setStructuredRecipe(null);
    setLiveSteps([]);
    setDraftName(null);
    setCapturedPhotos({});
    setCurrentMusic(null);
    setRecipeSaved(false);
    setRecipeSavedId(null);

    const looksLikeUrl = /^(https?:\/\/|[\w-]+\.(com|org|net|io|co|me|app|dev)\b)/i.test(recipe.trim());
    if (looksLikeUrl && !/^https?:\/\//i.test(recipe.trim())) {
      recipe = "https://" + recipe.trim();
    }
    const inputIsUrl = /^https?:\/\//i.test(recipe.trim());
    let recipeData = null;
    if (recipeFromLibrary?.recipe) {
      recipeData = { recipe: recipeFromLibrary.recipe, source: recipeFromLibrary.source ?? "saved" };
    } else if (recipeImageFiles?.length) {
      setStatus("parsing_recipe");
      try {
        const form = new FormData();
        for (const file of recipeImageFiles) form.append("images", file);
        form.append("persona", persona);
        const resp = await fetch(`${RECIPE_AGENT_URL}/parse-image`, {
          method: "POST",
          body: form,
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.recipe) recipeData = data;
        }
      } catch {}
    } else if (recipe.trim() && inputIsUrl) {
      setStatus("parsing_recipe");
      try {
        const resp = await fetch(`${RECIPE_AGENT_URL}/parse`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input: recipe.trim(), persona }),
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.recipe) recipeData = data;
        }
      } catch {}
    }

    setStatus("connecting");

    const wsUrl = await _buildWsUrl(getToken);
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    const wsOpenPromise = new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });

    const stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "environment" },
      // Don't force sampleRate here — let the browser pick native rate and resample
      // into the 16 kHz AudioContext. autoGainControl normalises quieter mics
      // (e.g. MacBook built-in) to the same level as headset mics.
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });

    streamRef.current = stream;

    const audioContext = new AudioContext({ sampleRate: 16000 });
    audioContextRef.current = audioContext;
    const actualSampleRate = audioContext.sampleRate;

    const micSource = audioContext.createMediaStreamSource(stream);

    await Promise.all([
      audioContext.audioWorklet.addModule("/audio-processor.js"),
      wsOpenPromise,
    ]);

    const workletNode = new AudioWorkletNode(audioContext, "mic-processor", {
      processorOptions: { chunkSize: 512 },
    });
    micSource.connect(workletNode);
    workletNodeRef.current = workletNode;

    {
      ws.send(JSON.stringify({
        type: "config",
        recipe: recipeData ? "" : (inputIsUrl ? recipe.trim() : ""),
        recipe_json: recipeData?.recipe ?? null,
        recipe_source: recipeData?.source ?? null,
        recipe_hint: recipeData ? "" : recipe.trim(),
        persona,
        resume_draft: resumeDraft,
        resume_draft_started_at: resumeDraftStartedAt ?? null,
        document_mode: documentMode,
      }));

      workletNode.port.onmessage = (event) => {
        if (ws.readyState !== WebSocket.OPEN) return;
        const float32 = event.data;
        const toSend = actualSampleRate === 16000 ? float32 : resampleTo16k(float32, actualSampleRate);
        const int16 = float32ToInt16(toSend);
        ws.send(JSON.stringify({ type: "audio", data: arrayBufferToBase64(int16.buffer) }));
      };

      const videoEl = document.createElement("video");
      videoEl.srcObject = stream;
      videoEl.muted = true;
      videoEl.playsInline = true;
      await videoEl.play();
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      videoIntervalRef.current = setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN) return;
        if (videoEl.readyState < videoEl.HAVE_CURRENT_DATA) return;
        try {
          canvas.width = Math.round(videoEl.videoWidth / 2);
          canvas.height = Math.round(videoEl.videoHeight / 2);
          ctx.drawImage(videoEl, 0, 0, canvas.width, canvas.height);
          canvas.toBlob((blob) => {
            if (!blob) return;
            blobToBase64(blob).then((encoded) => {
              if (ws.readyState === WebSocket.OPEN)
                ws.send(JSON.stringify({ type: "video", data: encoded }));
            });
          }, "image/jpeg", 0.7);
        } catch {
          // frame grab failed, skip
        }
      }, 1000 / VIDEO_FPS);
    }

    let videoShown = false;

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === "interrupted") {
        // Model was interrupted — immediately stop every scheduled source node.
        // Calling source.stop(0) is synchronous and instant; closing/recreating
        // the AudioContext is async and slow, causing audio to bleed through.
        activeSourcesRef.current.forEach(s => { try { s.stop(0); } catch {} });
        activeSourcesRef.current = [];
        nextPlayTimeRef.current = 0;
        turnCompleteRef.current = false;
        speakingRef.current = false; setIsSpeaking(false);
        return;
      }

      if (data.type === "reconnecting") {
        // Server is retrying after a transient Live API drop.
        // Preserve all UI state. Show reconnecting indicator on video.
        setIsReconnecting(true);
        speakingRef.current = false; setIsSpeaking(false);
        activeSourcesRef.current.forEach(s => { try { s.stop(0); } catch {} });
        activeSourcesRef.current = [];
        playbackContextRef.current?.close().catch(() => {});
        playbackContextRef.current = null;
        nextPlayTimeRef.current = 0;
        return;
      }

      if (data.type === "reset") {
        setTranscript([]);
        setRecipeSteps([]);
        setIngredients([]);
        setCompletedSteps(new Set());
        setTimers([]);
        setStructuredRecipe(null);
        setLiveSteps([]);
        setDraftName(null);
        setCapturedPhotos({});
        setRecipeSearchStatus(null);
        setCurrentMusic(null);
        speakingRef.current = false; setIsSpeaking(false);
        activeSourcesRef.current.forEach(s => { try { s.stop(0); } catch {} });
        activeSourcesRef.current = [];
        playbackContextRef.current?.close().catch(() => {});
        playbackContextRef.current = null;
        nextPlayTimeRef.current = 0;
        return;
      }

      if (data.type === "recipe_search_start") {
        setRecipeSearchStatus("searching");
      } else if (data.type === "recipe_search_failed") {
        setRecipeSearchStatus(data.error ? { error: data.error } : { error: "Could not load recipe" });
      } else if (data.type === "recipe") {
        setRecipeSearchStatus(null);
        const normalized = { ...data.recipe };
        if (Array.isArray(normalized.steps)) {
          normalized.steps = normalized.steps.map((s, i) => ({ ...s, id: i + 1 }));
        }
        setStructuredRecipe(normalized);
        setRecipeSteps((normalized.steps || []).map((s) => s.instruction));
        setIngredients(normalized.ingredients || []);
      } else if (data.type === "live_step") {
        const startedAt = stopwatchStartedAtRef.current;
        if (startedAt != null) {
          const sec = Math.floor((Date.now() - startedAt) / 1000);
          ws.send(JSON.stringify({ type: "stopwatch_elapsed", seconds: sec }));
        }
        setLiveSteps((prev) => {
          const pos = data.position;
          if (pos != null && pos >= 1 && pos <= prev.length) {
            const next = [...prev];
            next.splice(pos - 1, 0, data.step);
            return next;
          }
          return [...prev, data.step];
        });
        setTimers((prev) => prev.filter((t) => !t.stopwatch));
      } else if (data.type === "delete_live_ingredient") {
        // index is 1-based
        setIngredients((prev) => prev.filter((_, i) => i !== data.index - 1));
      } else if (data.type === "delete_live_step") {
        // step_number is 1-based
        setLiveSteps((prev) => prev.filter((_, i) => i !== data.step_number - 1));
      } else if (data.type === "edit_live_step") {
        setLiveSteps((prev) => prev.map((s, i) => i === data.step_number - 1 ? data.step : s));
      } else if (data.type === "live_ingredient") {
        const ing = data.ingredient || {};
        if ((ing.item || "").trim())
          setIngredients((prev) => [
            ...prev,
            { amount: (ing.amount || "").trim(), item: (ing.item || "").trim(), prep: (ing.prep || "").trim() },
          ]);
      } else if (data.type === "draft_loaded") {
        setLiveSteps(data.steps || []);
        setIngredients((data.ingredients || []).map((i) => ({ amount: i.amount ?? "", item: i.item ?? "", prep: i.prep ?? "" })));
        setDraftName(data.name ?? null);
      } else if (data.type === "draft_name") {
        setDraftName(data.name ?? null);
      } else if (data.type === "play_music") {
        setCurrentMusic({ query: data.query, videoId: data.videoId || null });
      } else if (data.type === "stop_music") {
        setCurrentMusic(null);
      } else if (data.type === "set_music_volume") {
        setMusicVolume(data.volume);
      } else if (data.type === "edit_ingredient") {
        const idx = typeof data.index === "number" ? data.index : parseInt(data.index, 10);
        if (Number.isNaN(idx) || idx < 0) return;
        const ing = data.ingredient || {};
        const nextIng = {
          amount: (ing.amount ?? "").toString().trim(),
          item: (ing.item ?? "").toString().trim(),
          prep: (ing.prep ?? "").toString().trim(),
        };
        setStructuredRecipe((prev) => {
          if (!prev || !Array.isArray(prev.ingredients)) return prev;
          const ingredients = prev.ingredients.map((item, i) =>
            i === idx ? { ...item, ...nextIng } : item
          );
          return { ...prev, ingredients };
        });
        setIngredients((prev) => {
          if (idx >= prev.length) return prev;
          const next = [...prev];
          next[idx] = { ...next[idx], ...nextIng };
          return next;
        });
      } else if (data.type === "edit_step") {
        setStructuredRecipe((prev) => {
          if (!prev) return prev;
          const steps = (prev.steps || []).map((s) =>
            s.id === data.step_number ? { ...s, ...data.step } : s
          );
          return { ...prev, steps };
        });
      } else if (data.type === "photo_captured") {
        if (data.data && data.step_number != null) {
          const src = data.data.startsWith("data:") ? data.data : `data:image/jpeg;base64,${data.data}`;
          setCapturedPhotos((prev) => ({
            ...prev,
            [data.step_number]: [...(prev[data.step_number] || []), src],
          }));
        }
      } else if (data.type === "recipe_saved") {
        setRecipeSaved(true);
        if (data.id) setRecipeSavedId(data.id);
      } else if (data.type === "audio") {
        if (!videoShown) {
          videoShown = true;
          setStatus("connected");
          if (videoElement) videoElement.srcObject = stream;
        }
        setIsReconnecting(false);
        if (!speakingRef.current) { speakingRef.current = true; setIsSpeaking(true); }
        enqueueAudio(data.data);
      } else if (data.type === "transcript") {
        setTranscript((prev) => {
          const last = prev[prev.length - 1];
          let next;
          if (last && last.role === "assistant" && !last.complete) {
            next = [...prev.slice(0, -1), { ...last, text: last.text + data.text }];
          } else {
            next = [...prev, { role: "assistant", text: data.text, complete: false }];
          }
          return next.length > 200 ? next.slice(-200) : next;
        });
      } else if (data.type === "turn_complete") {
        // Don't clear isSpeaking immediately — buffered audio may still be playing.
        // If the queue is already empty (e.g. no audio this turn), clear now.
        // Otherwise set the flag; the last buffer's "ended" callback will clear it.
        if (activeSourcesRef.current.length === 0) {
          speakingRef.current = false; setIsSpeaking(false);
        } else {
          turnCompleteRef.current = true;
        }
        setTranscript((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === "assistant" && !last.complete) {
            return [...prev.slice(0, -1), { ...last, complete: true }];
          }
          return prev;
        });
      } else if (data.type === "retract_transcript") {
        setTranscript((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.role === "assistant" && !last.complete) {
            return prev.slice(0, -1);
          }
          return prev;
        });
      } else if (data.type === "tool_call") {
        handleToolCall(data);
      }
    };

    ws.onerror = () => setStatus("error");
    ws.onclose = () => { setStatus("idle"); isStartingRef.current = false; };
    ws.onopen = () => { isStartingRef.current = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleToolCall = useCallback((data) => {
    const { name, args } = data;

    if (name === "set_timer") {
      setTimers((prev) => [
        ...prev,
        {
          id: data.call_id,
          label: args.label || "Timer",
          duration: args.duration_seconds,
          startedAt: Date.now(),
        },
      ]);
    } else if (name === "cancel_timer") {
      setTimers((prev) => prev.filter((t) => t.label.toLowerCase() !== (args.label || "").toLowerCase()));
    } else if (name === "edit_timer") {
      setTimers((prev) => prev.map((t) =>
        t.label.toLowerCase() === (args.label || "").toLowerCase()
          ? { ...t, duration: args.new_duration_seconds, startedAt: Date.now() }
          : t
      ));
    } else if (name === "start_stopwatch") {
      setTimers((prev) => [
        ...prev,
        {
          id: data.call_id,
          label: args.label || "Step",
          stopwatch: true,
          startedAt: Date.now(),
        },
      ]);
    } else if (name === "start_timer") {
      const labelLower = (args.label || "").toLowerCase();
      setTimers((prev) => {
        const idx = prev.findIndex(
          (t) => !t.startedAt && t.label.toLowerCase().includes(labelLower)
        );
        const fallbackIdx = idx === -1
          ? [...prev].reverse().findIndex((t) => !t.startedAt)
          : -1;
        const targetIdx = idx !== -1 ? idx : (prev.length - 1 - fallbackIdx);
        if (targetIdx === -1 || (idx === -1 && fallbackIdx === -1)) return prev;
        return prev.map((t, i) =>
          i === targetIdx ? { ...t, startedAt: Date.now() } : t
        );
      });
    } else if (name === "complete_step") {
      setCompletedSteps((prev) => {
        const next = new Set(prev);
        for (let i = 1; i <= args.step_number; i++) next.add(i);
        return next;
      });
    } else if (name === "jump_to_step") {
      setCompletedSteps(() => {
        const next = new Set();
        for (let i = 1; i < args.step_number; i++) next.add(i);
        return next;
      });
    } else if (name === "set_recipe_steps") {
      setRecipeSteps(args.steps || []);
    } else if (name === "set_ingredients") {
      const raw = args.ingredients || [];
      setIngredients(raw.map((i) => (typeof i === "object" && i != null && "item" in i
        ? { amount: i.amount ?? "", item: i.item ?? "", prep: i.prep ?? "" }
        : { amount: "", item: String(i), prep: "" })));
    }
  }, []);

  const startTimer = useCallback((id) => {
    setTimers((prev) =>
      prev.map((t) => t.id === id ? { ...t, startedAt: Date.now() } : t)
    );
  }, []);

  const dismissTimer = useCallback((id) => {
    setTimers((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const stopMusic = useCallback(() => setCurrentMusic(null), []);

  const saveRecipe = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "save_recipe" }));
      setRecipeSaved(true);
    }
  }, []);

  const stopPlayback = useCallback(() => {
    activeSourcesRef.current.forEach(s => { try { s.stop(0); } catch {} });
    activeSourcesRef.current = [];
    nextPlayTimeRef.current = 0;
    if (playbackContextRef.current) {
      playbackContextRef.current.close().catch(() => {});
      playbackContextRef.current = null;
    }
  }, []);

  const stopSession = useCallback(() => {
    clearInterval(videoIntervalRef.current);
    wsRef.current?.close();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    workletNodeRef.current?.disconnect();
    workletNodeRef.current = null;
    audioContextRef.current?.close().catch(() => {});
    playbackContextRef.current?.close().catch(() => {});
    playbackContextRef.current = null;
    nextPlayTimeRef.current = 0;
    stopPlayback();
    setStatus("idle");
    setTranscript([]);
    setTimers([]);
    setCompletedSteps(new Set());
    setRecipeSteps([]);
    setIngredients([]);
    setStructuredRecipe(null);
    setLiveSteps([]);
    setRecipeSearchStatus(null);
    setCurrentMusic(null);
    speakingRef.current = false; setIsSpeaking(false);
  }, [stopPlayback]);

  // --- Audio playback ---
  // Single persistent AudioContext with scheduled start times for gapless playback.

  const enqueueAudio = useCallback((base64Data) => {
    if (!playbackContextRef.current || playbackContextRef.current.state === "closed") {
      playbackContextRef.current = new AudioContext({ sampleRate: 24000 });
      nextPlayTimeRef.current = 0;
    }
    const ctx = playbackContextRef.current;
    if (ctx.state === "suspended") ctx.resume();

    const int16 = base64ToInt16(base64Data);
    const float32 = int16ToFloat32(int16);
    const buffer = ctx.createBuffer(1, float32.length, 24000);
    buffer.getChannelData(0).set(float32);

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);

    const startAt = Math.max(ctx.currentTime, nextPlayTimeRef.current);
    nextPlayTimeRef.current = startAt + buffer.duration;

    activeSourcesRef.current.push(source);
    source.addEventListener("ended", () => {
      const i = activeSourcesRef.current.indexOf(source);
      if (i >= 0) activeSourcesRef.current.splice(i, 1);
      // If turn_complete already arrived and this was the last chunk, stop speaking now.
      if (turnCompleteRef.current && activeSourcesRef.current.length === 0) {
        turnCompleteRef.current = false;
        speakingRef.current = false; setIsSpeaking(false);
      }
    });

    source.start(startAt);
  }, []);

  return {
    status,
    transcript,
    timers,
    completedSteps,
    recipeSteps,
    ingredients,
    structuredRecipe,
    updateRecipe: setStructuredRecipe,
    liveSteps,
    draftName,
    recipeSearchStatus,
    currentMusic,
    isSpeaking,
    isReconnecting,
    musicVolume,
    capturedPhotos,
    setCapturedPhotos,
    startSession,
    stopSession,
    startTimer,
    dismissTimer,
    stopMusic,
    saveRecipe,
    recipeSaved,
    recipeSavedId,
  };
}

// --- Helpers ---

/** Resample mono float32 to 16 kHz (API expects 16 kHz). Handles new mics that give 48 kHz etc. */
function resampleTo16k(float32, sourceSampleRate) {
  if (sourceSampleRate === 16000) return float32;
  const targetLength = Math.round((float32.length * 16000) / sourceSampleRate);
  if (targetLength <= 0) return float32;
  const out = new Float32Array(targetLength);
  for (let i = 0; i < targetLength; i++) {
    const srcIdx = (i * (float32.length - 1)) / (targetLength - 1 || 1);
    const lo = Math.floor(srcIdx);
    const hi = Math.min(lo + 1, float32.length - 1);
    const t = srcIdx - lo;
    out[i] = float32[lo] * (1 - t) + float32[hi] * t;
  }
  return out;
}

function float32ToInt16(float32) {
  const int16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    int16[i] = Math.max(-32768, Math.min(32767, float32[i] * 32768));
  }
  return int16;
}

function int16ToFloat32(int16) {
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
  return float32;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToInt16(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

async function blobToBase64(blob) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result.split(",")[1]);
    reader.readAsDataURL(blob);
  });
}
