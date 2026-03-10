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

export default function App() {
  const { user, signInWithGoogle, logout, getToken } = useAuth();

  // Still loading auth state
  if (user === undefined) {
    return <div className="auth-loading">Loading…</div>;
  }

  // Not signed in and Firebase is configured — show sign-in screen
  if (user === null && getToken === null && !!import.meta.env.VITE_FIREBASE_API_KEY) {
    return (
      <div className="auth-screen">
        <h1 className="logo">Nonna Notes</h1>
        <p className="tagline">cook with Nonna</p>
        <button className="start-btn auth-google-btn" onClick={signInWithGoogle}>
          Sign in with Google
        </button>
      </div>
    );
  }
  const videoRef = useRef(null);
  const recipeInputRef = useRef(null);
  const [recipe, setRecipe] = useState("");
  const [recipeFromLibrary, setRecipeFromLibrary] = useState(null);
  const [libraryIndex, setLibraryIndex] = useState(null); // index in saved_recipes for persistence
  const [persona, setPersona] = useState("nonna");
  const [showLibrary, setShowLibrary] = useState(false);
  const [toast, setToast] = useState(null);
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
    recipeSearchStatus,
    startSession,
    stopSession,
    startTimer,
    dismissTimer,
    currentMusic,
    isSpeaking,
    isReconnecting,
    musicVolume,
    stopMusic,
  } = useGeminiLive();

  const handleStart = (resumeDraft = false, resumeDraftStartedAt = null) => {
    const recipeValue = resumeDraft ? "" : (recipeInputRef.current?.value?.trim() || recipe.trim());
    startSession(videoRef.current, recipeValue, persona, resumeDraft ? null : recipeFromLibrary, resumeDraft, resumeDraftStartedAt, getToken);
  };

  const handleRecipeEdit = async (updated) => {
    updateRecipe(updated);
    if (libraryIndex !== null) {
      const token = getToken ? await getToken() : null;
      fetch(`${BACKEND_URL}/recipes/${libraryIndex}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ recipe: updated }),
      }).catch(() => {});
    }
  };

  const displayRecipe = structuredRecipe ||
    (liveSteps.length > 0 || ingredients.length > 0
      ? {
          name: "Documenting your cook…",
          description: "Steps and ingredients appear as you go.",
          servings: 2,
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

  return (
    <div className="app">
      <header className="header">
        <h1 className="logo logo-easter" onClick={handleLogoClick} title="Nonna Notes">Nonna Notes</h1>
        <span className="tagline">{persona === "gordon" ? "cook with Gordon" : "cook with Nonna"}</span>
        <div className="header-actions">
          {status === "idle" && (
            <button type="button" className="library-btn" onClick={() => setShowLibrary(true)}>
              My Recipes
            </button>
          )}
          {user && (
            <button type="button" className="library-btn library-btn-signout" onClick={logout} title={user.email}>
              Sign out
            </button>
          )}
        </div>
      </header>

      <main className="main">
        {/* ── Ingredients column (left) ── */}
        <div className="main-ingredients">
          <IngredientsPanel ingredients={displayRecipe?.ingredients || ingredients} />
        </div>

        {/* ── Center column ── */}
        <div className="main-left">
          <div className="camera-container">
            <video ref={videoRef} autoPlay muted playsInline className="camera-feed" />
            {isReconnecting && (
              <div className="camera-overlay reconnecting-overlay">
                <div className="reconnecting-indicator">
                  <div className="reconnecting-spinner" />
                  <span>Reconnecting…</span>
                </div>
              </div>
            )}
            {status === "idle" && (
              <div className="camera-overlay">
                <div className="setup-panel">
                  <textarea
                    ref={recipeInputRef}
                    className="recipe-input"
                    placeholder="Paste a recipe URL, YouTube URL, or describe what you want to cook!"
                    defaultValue=""
                    rows={3}
                  />
                  <button className="start-btn" onClick={() => handleStart(false)}>
                    Start Cooking
                  </button>
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
            <div className={`status-dot ${status}`} title={status} />
          </div>

          <TimerPanel timers={timers} onStart={startTimer} onDismiss={dismissTimer} />

          {displayRecipe ? (
            <RecipePanel
              recipe={displayRecipe}
              completedSteps={structuredRecipe ? completedSteps : new Set(liveSteps.map((_, i) => i + 1))}
              editable={!!structuredRecipe}
              onChange={handleRecipeEdit}
              hideIngredients={true}
            />
          ) : (
            <RecipeSteps steps={recipeSteps} completedSteps={completedSteps} />
          )}

          {status === "connected" && (
            <button className="stop-btn" onClick={() => { stopSession(); if (recipeInputRef.current) recipeInputRef.current.value = ""; setRecipe(""); setRecipeFromLibrary(null); setLibraryIndex(null); }}>
              End Session
            </button>
          )}
        </div>

        {/* ── Right column: transcript ── */}
        <div className="main-right">
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
      </main>

      {toast && (
        <div className="toast-easter" onClick={dismissToast} role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && dismissToast()}>
          {toast}
        </div>
      )}

      {currentMusic && (
        <MusicPlayer query={currentMusic.query} videoId={currentMusic.videoId} volume={musicVolume} isSpeaking={isSpeaking} onStop={stopMusic} />
      )}

      {showLibrary && (
        <RecipeLibrary
          getToken={getToken}
          onClose={() => setShowLibrary(false)}
          onCook={(name, recipeObj, source, index) => {
            const libRecipe = recipeObj ? { recipe: recipeObj, source: source ?? "saved" } : null;
            setRecipe(name);
            setRecipeFromLibrary(libRecipe);
            setLibraryIndex(index ?? null);
            setShowLibrary(false);
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
