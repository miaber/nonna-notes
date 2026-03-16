import { useRef, useState, useCallback, useEffect } from "react";
import { useGeminiLive } from "./hooks/useGeminiLive";
import { useAuth } from "./context/AuthContext";
import TimerPanel from "./components/TimerPanel";
import RecipePanel from "./components/RecipePanel";
import RecipeSteps from "./components/RecipeSteps";
import IngredientsPanel from "./components/IngredientsPanel";
import RecipeLibrary from "./components/RecipeLibrary";
import MusicPlayer from "./components/MusicPlayer";
import "./App.css";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";
const RECIPE_AGENT_URL = import.meta.env.VITE_RECIPE_AGENT_URL || "http://localhost:8001";

export default function App() {
  const { user, isGuest, signInWithGoogle, continueAsGuest, logout, getToken } = useAuth();

  const videoRef = useRef(null);
  const cameraColRef = useRef(null);
  const convoRef = useRef(null);
  const recipeInputRef = useRef(null);
  const [recipe, setRecipe] = useState("");
  const [recipeFromLibrary, setRecipeFromLibrary] = useState(null);
  const [libraryRecipeId, setLibraryRecipeId] = useState(null);
  const [recipeImages, setRecipeImages] = useState([]);     // File[]
  const [recipeImagePreviews, setRecipeImagePreviews] = useState([]); // object URL[]
  const imageInputRef = useRef(null);
  const [persona, setPersona] = useState("nonna");
  const [showLibrary, setShowLibrary] = useState(false);
  const [homeView, setHomeView] = useState(null); // null = books, "follow" = recipe input
  const [toast, setToast] = useState(null);
  const [cameraFlash, setCameraFlash] = useState(false);
  const prevCapturedPhotosRef = useRef({});
  const logoTapCount = useRef(0);
  const logoTapTimeout = useRef(null);

  const handleLogoClick = useCallback(() => {
    logoTapCount.current += 1;
    if (logoTapCount.current >= 5) {
      logoTapCount.current = 0;
      if (logoTapTimeout.current) clearTimeout(logoTapTimeout.current);
      logoTapTimeout.current = null;
      setPersona((p) => {
        const next = p === "nonna" ? "gordon" : "nonna";
        setToast(next === "gordon" ? "🔪 Gordon mode" : "🍝 Nonna mode");
        return next;
      });
    } else {
      if (logoTapTimeout.current) clearTimeout(logoTapTimeout.current);
      logoTapTimeout.current = setTimeout(() => { logoTapCount.current = 0; logoTapTimeout.current = null; }, 1500);
    }
  }, []);

  const dismissToast = useCallback(() => setToast(null), []);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(dismissToast, 2500);
    return () => clearTimeout(t);
  }, [toast, dismissToast]);

  const {
    status,
    transcript,
    timers,
    completedSteps,
    recipeSteps,
    ingredients,
    structuredRecipe,
    updateRecipe,
    liveSteps,
    draftName,
    recipeSearchStatus,
    startSession,
    stopSession,
    saveRecipe,
    recipeSaved,
    recipeSavedId,
    startTimer,
    dismissTimer,
    currentMusic,
    isSpeaking,
    isReconnecting,
    musicVolume,
    capturedPhotos,
    setCapturedPhotos,
    stopMusic,
  } = useGeminiLive();

  useEffect(() => {
    const prev = prevCapturedPhotosRef.current;
    const added = Object.keys(capturedPhotos).some(
      (k) => (capturedPhotos[k]?.length || 0) > (prev[k]?.length || 0)
    );
    prevCapturedPhotosRef.current = capturedPhotos;
    if (added) {
      setCameraFlash(true);
      const t = setTimeout(() => setCameraFlash(false), 600);
      return () => clearTimeout(t);
    }
  }, [capturedPhotos]);

  const handleStart = (resumeDraft = false, resumeDraftStartedAt = null, documentMode = false) => {
    const recipeValue = resumeDraft ? "" : (recipeInputRef.current?.value?.trim() || recipe.trim());
    const imageFiles = resumeDraft ? null : (recipeImages.length > 0 ? recipeImages : null);
    startSession(videoRef.current, recipeValue, persona, resumeDraft ? null : recipeFromLibrary, resumeDraft, resumeDraftStartedAt, getToken, imageFiles, documentMode);
    setRecipeImages([]);
    setRecipeImagePreviews([]);
  };

  const handleImageUpload = async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const converted = await Promise.all(files.map(async (f) => {
      const isHeic = f.type === "image/heic" || f.type === "image/heif" ||
        /\.heic$/i.test(f.name) || /\.heif$/i.test(f.name);
      if (isHeic) {
        try {
          const form = new FormData();
          form.append("image", f);
          const resp = await fetch(`${RECIPE_AGENT_URL}/convert-preview`, { method: "POST", body: form });
          if (resp.ok) return await resp.blob(); // JPEG blob — used for both upload and preview
        } catch {}
      }
      return f;
    }));
    setRecipeImages((prev) => [...prev, ...converted]);
    const dataUrls = await Promise.all(converted.map((blob) =>
      new Promise((resolve) => {
        const r = new FileReader();
        r.onloadend = () => resolve(r.result);
        r.readAsDataURL(blob);
      })
    ));
    setRecipeImagePreviews((prev) => [...prev, ...dataUrls]);
    if (imageInputRef.current) imageInputRef.current.value = "";
  };

  const removeRecipeImage = (idx) => {
    setRecipeImages((prev) => prev.filter((_, i) => i !== idx));
    setRecipeImagePreviews((prev) => prev.filter((_, i) => i !== idx));
  };

  const clearRecipeImages = () => {
    setRecipeImages([]);
    setRecipeImagePreviews([]);
    if (imageInputRef.current) imageInputRef.current.value = "";
  };

  const handleRecipeEdit = async (updated) => {
    updateRecipe(updated);
    if (libraryRecipeId !== null) {
      const token = getToken ? await getToken() : null;
      fetch(`${BACKEND_URL}/recipes/${encodeURIComponent(libraryRecipeId)}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ recipe: updated }),
      }).catch(() => {});
    }
  };

  const isCooking = status !== "idle";

  // When recipe is saved via the button, wire up libraryRecipeId so edits persist
  useEffect(() => {
    if (recipeSavedId) setLibraryRecipeId(recipeSavedId);
  }, [recipeSavedId]);

  useEffect(() => {
    const cam = cameraColRef.current;
    const conv = convoRef.current;
    if (!cam || !conv) return;
    const ro = new ResizeObserver(() => {
      conv.style.maxHeight = `${cam.offsetHeight}px`;
    });
    ro.observe(cam);
    return () => ro.disconnect();
  }, [isCooking]);

  const displayRecipe = structuredRecipe ||
    (liveSteps.length > 0 || ingredients.length > 0 || draftName
      ? {
          name: draftName || "Documenting your cook…",
          description: "Steps and ingredients appear as you go.",
          servings: null,
          total_time_minutes: null,
          ingredients,
          steps: liveSteps.map((s, i) => ({
            id: i + 1,
            instruction: s.instruction,
            timer_seconds: s.timer_seconds ?? null,
          })),
          tips: [],
        }
      : null);

  const currentStep = (() => {
    if (!isCooking) return null;
    if (displayRecipe?.steps?.length) {
      const cs = structuredRecipe ? completedSteps : new Set(liveSteps.map((_, i) => i + 1));
      for (let i = 0; i < displayRecipe.steps.length; i++) {
        const step = displayRecipe.steps[i];
        if (!cs?.has(step.id)) {
          return displayRecipe.steps.slice(0, i).every((s) => cs?.has(s.id)) ? step : null;
        }
      }
      return null;
    }
    for (let i = 0; i < recipeSteps.length; i++) {
      if (!completedSteps.has(i + 1)) return { instruction: recipeSteps[i], id: i + 1 };
    }
    return null;
  })();

  if (user === undefined) {
    return <div className="auth-loading">Loading…</div>;
  }

  const requireAuth = import.meta.env.VITE_REQUIRE_AUTH !== "false";
  if (user === null && !isGuest && getToken === null && !!import.meta.env.VITE_FIREBASE_API_KEY && requireAuth) {
    return (
      <div className="auth-screen">
        <h1 className="logo">Nonna Notes</h1>
        <p className="tagline">cook with Nonna</p>
        <button className="start-btn auth-google-btn" onClick={signInWithGoogle}>
          Sign in with Google
        </button>
        <div className="auth-guest-group">
          <button className="auth-guest-btn" onClick={continueAsGuest}>
            Continue without signing in
          </button>
          <p className="auth-guest-note">Recipes won't be synced across devices</p>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="header">
        <h1 className="logo logo-easter" onClick={handleLogoClick} title="Nonna Notes">Nonna Notes</h1>
        <span className="tagline">{persona === "gordon" ? "cook with Gordon" : "cook with Nonna"}</span>
        <div className="header-actions">
          {isCooking && !libraryRecipeId && (
            <button
              type="button"
              className="header-btn header-btn-save"
              onClick={saveRecipe}
              disabled={recipeSaved}
            >
              {recipeSaved ? "Saved ✓" : "Save Recipe"}
            </button>
          )}
          {isCooking && (
            <button
              type="button"
              className="header-btn header-btn-end"
              onClick={() => { stopSession(); if (recipeInputRef.current) recipeInputRef.current.value = ""; setRecipe(""); setRecipeFromLibrary(null); setLibraryRecipeId(null); setHomeView(null); }}
            >
              End Session
            </button>
          )}
          {user && (
            <button type="button" className="library-btn library-btn-signout" onClick={logout} title={user.email}>
              Sign out
            </button>
          )}
          {!user && isGuest && (
            <button type="button" className="library-btn library-btn-signin" onClick={signInWithGoogle} title="Sign in to save recipes across devices">
              Sign in
            </button>
          )}
        </div>
      </header>

      {/* ---- HOME SCREEN (idle, no cooking) ---- */}
      {status === "idle" && !homeView && (
        <main className="main main-home">
          <div className="home-books">
            <button type="button" className="home-book home-book-follow" onClick={() => setHomeView("follow")}>
              <span className="home-book-spine" />
              <span className="home-book-oval"><span>Follow a Recipe</span></span>
              <span className="home-book-desc">Paste a URL, YouTube video, or snap a photo of a recipe. Or ask Nonna to find one for you!</span>
            </button>
            <button type="button" className="home-book home-book-record" onClick={() => handleStart(false, null, true)}>
              <span className="home-book-spine" />
              <span className="home-book-oval"><span>Record a Recipe</span></span>
              <span className="home-book-desc">Cook with Nonna watching and she'll write the recipe for you</span>
            </button>
            <button type="button" className="home-book home-book-library" onClick={() => setShowLibrary(true)}>
              <span className="home-book-spine" />
              <span className="home-book-oval"><span>My Recipes</span></span>
              <span className="home-book-desc">{isGuest && !user ? "Sign in to save recipes across devices" : "View your saved recipes"}</span>
            </button>
          </div>
        </main>
      )}

      {/* ---- FOLLOW A RECIPE INPUT ---- */}
      {status === "idle" && homeView === "follow" && (
        <main className="main main-home">
          <div className="follow-panel">
            <button type="button" className="follow-back" onClick={() => { setHomeView(null); clearRecipeImages(); }}>&larr; Back</button>
            <h2 className="follow-title">Follow a Recipe</h2>
            {recipeImagePreviews.length > 0 ? (
              <div className="recipe-images-grid">
                {recipeImagePreviews.map((url, i) => (
                  <div key={i} className="recipe-image-thumb">
                    {url ? (
                      <img src={url} alt={`Page ${i + 1}`} />
                    ) : (
                      <div className="recipe-image-placeholder">📄</div>
                    )}
                    <button type="button" className="recipe-image-remove" onClick={() => removeRecipeImage(i)} title="Remove">&times;</button>
                  </div>
                ))}
                <button type="button" className="recipe-image-add" onClick={() => imageInputRef.current?.click()} title="Add another page">+</button>
              </div>
            ) : (
              <textarea
                ref={recipeInputRef}
                className="recipe-input"
                placeholder="Paste a recipe URL, YouTube URL, or describe what you want to cook!"
                defaultValue=""
                rows={3}
              />
            )}
            <div className="setup-actions">
              <input
                ref={imageInputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden-file-input"
                onChange={handleImageUpload}
              />
              {recipeImagePreviews.length === 0 && (
                <button type="button" className="upload-photo-btn" onClick={() => imageInputRef.current?.click()} title="Upload a photo of a recipe">
                  📷 Photo
                </button>
              )}
              {recipeImagePreviews.length > 0 && (
                <button type="button" className="upload-photo-btn" onClick={clearRecipeImages}>Clear</button>
              )}
              <button className="start-btn" onClick={() => { handleStart(false); setHomeView(null); }}>
                Start Cooking
              </button>
            </div>
          </div>
        </main>
      )}

      {/* ---- COOKING / LOADING STATES ---- */}
      <main className={`main ${isCooking ? "main-cooking" : ""}`} style={status === "idle" ? { display: "none" } : undefined}>
        <div className="main-camera" ref={cameraColRef}>
          <div className="camera-container">
            <video ref={videoRef} autoPlay muted playsInline className="camera-feed" />
            {cameraFlash && <div className="camera-flash" />}
            {isReconnecting && (
              <div className="camera-overlay reconnecting-overlay">
                <div className="reconnecting-indicator">
                  <div className="reconnecting-spinner" />
                  <span>Reconnecting…</span>
                </div>
              </div>
            )}
            {(status === "parsing_recipe" || status === "connecting" ||
              (status === "connected" && recipe.trim() && recipeSteps.length === 0 && !structuredRecipe && /^https?:\/\//i.test(recipe.trim())) ||
              recipeSearchStatus === "searching") && (
              <div className="camera-overlay">
                <span className="connecting-label">
                  {recipeSearchStatus === "searching" ? "Finding recipe…" :
                   status === "parsing_recipe" ? "Parsing recipe…" :
                   /^https?:\/\//i.test(recipe.trim()) ? "Loading recipe…" : "Connecting…"}
                </span>
              </div>
            )}
            {recipeSearchStatus && typeof recipeSearchStatus === "object" && recipeSearchStatus.error && (
              <div className="camera-overlay recipe-search-failed">
                <span className="connecting-label">Couldn't load recipe. Nonna will try again or use another.</span>
              </div>
            )}
            {isCooking && timers.length > 0 && (
              <div className="camera-overlay-timers">
                <TimerPanel timers={timers} onStart={startTimer} onDismiss={dismissTimer} />
              </div>
            )}
            {isCooking && currentMusic && (
              <div className="camera-overlay-music">
                <MusicPlayer compact query={currentMusic.query} videoId={currentMusic.videoId} volume={musicVolume} isSpeaking={isSpeaking} onStop={stopMusic} />
              </div>
            )}
            <div className={`status-dot ${status}`} title={status} />
          </div>

          {isCooking && currentStep && (
            <div className="current-step-callout current-step-callout-orange">
              <div className="current-step-content">
                <span className="current-step-label">Step {currentStep.id}</span>
                <span className="current-step-text">{currentStep.instruction}</span>
              </div>
              {(currentStep.image || capturedPhotos[currentStep.id]?.length > 0) && (
                <div className="current-step-images">
                  {currentStep.image && (
                    <img src={currentStep.image} alt="" className="current-step-img" loading="lazy" />
                  )}
                  {(capturedPhotos[currentStep.id] || []).map((src, i) => (
                    <img key={`cap-${i}`} src={src} alt="" className="current-step-img nonna-photo" loading="lazy" />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {isCooking && (
          <div className="main-conversation" ref={convoRef}>
            {transcript.length > 0 && <p className="transcript-label">Conversation</p>}
            <div className="transcript">
              {[...transcript].reverse().map((msg, i) => {
                const origIndex = transcript.length - 1 - i;
                return (
                  <div key={origIndex} className={`msg ${msg.role}`}>
                    {msg.text}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {isCooking && (
          <div className="main-ingredients">
            <IngredientsPanel ingredients={displayRecipe?.ingredients || ingredients} />
          </div>
        )}

        {isCooking && (
          <div className="main-recipe">
            {displayRecipe ? (
              <RecipePanel
                recipe={displayRecipe}
                completedSteps={structuredRecipe ? completedSteps : new Set(liveSteps.map((_, i) => i + 1))}
                editable={!!structuredRecipe}
                onChange={handleRecipeEdit}
                hideIngredients={true}
                capturedPhotos={capturedPhotos}
              />
            ) : (
              <RecipeSteps steps={recipeSteps} completedSteps={completedSteps} />
            )}
          </div>
        )}
      </main>

      {toast && (
        <div className="toast-easter" onClick={dismissToast} role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && dismissToast()}>
          {toast}
        </div>
      )}

      {showLibrary && (
        <RecipeLibrary
          getToken={getToken}
          onClose={() => setShowLibrary(false)}
          onCook={(name, recipeObj, source, recipeId, prevPhotos) => {
            const libRecipe = recipeObj ? { recipe: recipeObj, source: source ?? "saved" } : null;
            setRecipe(name);
            setRecipeFromLibrary(libRecipe);
            setLibraryRecipeId(recipeId ?? null);
            setShowLibrary(false);
            setHomeView(null);
            if (prevPhotos?.length) {
              const grouped = {};
              for (const p of prevPhotos) {
                if (p.step_id != null && p.data) {
                  const src = p.data.startsWith("data:") ? p.data : `data:image/jpeg;base64,${p.data}`;
                  (grouped[p.step_id] ??= []).push(src);
                }
              }
              setCapturedPhotos(grouped);
            } else {
              setCapturedPhotos({});
            }
            startSession(videoRef.current, name, persona, libRecipe, false, null, getToken);
          }}
          onContinueDraft={(startedAt) => {
            setShowLibrary(false);
            handleStart(true, startedAt ?? null);
          }}
        />
      )}
    </div>
  );
}
