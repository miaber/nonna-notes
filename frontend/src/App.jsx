import { useRef, useState } from "react";
import { useGeminiLive } from "./hooks/useGeminiLive";
import TimerPanel from "./components/TimerPanel";
import RecipePanel from "./components/RecipePanel";
import RecipeSteps from "./components/RecipeSteps";
import IngredientsPanel from "./components/IngredientsPanel";
import RecipeLibrary from "./components/RecipeLibrary";
import MusicPlayer from "./components/MusicPlayer";
import "./App.css";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";

export default function App() {
  const videoRef = useRef(null);
  const [recipe, setRecipe] = useState("");
  const [recipeFromLibrary, setRecipeFromLibrary] = useState(null);
  const [libraryIndex, setLibraryIndex] = useState(null); // index in saved_recipes for persistence
  const [persona, setPersona] = useState("gordon");
  const [showLibrary, setShowLibrary] = useState(false);
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
    musicVolume,
    stopMusic,
  } = useGeminiLive();

  const handleStart = (resumeDraft = false, resumeDraftStartedAt = null) =>
    startSession(videoRef.current, resumeDraft ? "" : recipe, persona, resumeDraft ? null : recipeFromLibrary, resumeDraft, resumeDraftStartedAt);

  const handleRecipeEdit = (updated) => {
    updateRecipe(updated);
    if (libraryIndex !== null) {
      fetch(`${BACKEND_URL}/recipes/${libraryIndex}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
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
        <h1 className="logo">{persona === "nonna" ? "nonna" : "mise"}</h1>
        <span className="tagline">your sous chef</span>
        {status === "idle" && (
          <button className="library-btn" onClick={() => setShowLibrary(true)}>
            My Recipes
          </button>
        )}
      </header>

      <main className="main">
        {/* ── Left column ── */}
        <div className="main-left">
          <div className="camera-container">
            <video ref={videoRef} autoPlay muted playsInline className="camera-feed" />
            {status === "idle" && (
              <div className="camera-overlay">
                <div className="setup-panel">
                  <div className="persona-toggle">
                    <button
                      className={`persona-btn ${persona === "gordon" ? "active" : ""}`}
                      onClick={() => setPersona("gordon")}
                    >
                      🔪 Gordon
                    </button>
                    <button
                      className={`persona-btn ${persona === "nonna" ? "active" : ""}`}
                      onClick={() => setPersona("nonna")}
                    >
                      🍝 Nonna
                    </button>
                  </div>
                  <textarea
                    className="recipe-input"
                    placeholder="Paste a recipe URL or text (optional)"
                    value={recipe}
                    onChange={(e) => { setRecipe(e.target.value); setRecipeFromLibrary(null); }}
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
                <span className="connecting-label">Couldn't load recipe. Mise will try again or use another.</span>
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
            />
          ) : (
            <>
              <IngredientsPanel ingredients={ingredients} />
              <RecipeSteps steps={recipeSteps} completedSteps={completedSteps} />
            </>
          )}

          {status === "connected" && (
            <button className="stop-btn" onClick={() => { stopSession(); setRecipe(""); setRecipeFromLibrary(null); setLibraryIndex(null); }}>
              End Session
            </button>
          )}
        </div>

        {/* ── Right column: transcript ── */}
        <div className="main-right">
          {transcript.length > 0 && <p className="transcript-label">Conversation</p>}
          <div className="transcript">
            {transcript.map((msg, i) => (
              <div key={i} className={`msg ${msg.role}`}>
                {msg.text}
              </div>
            ))}
          </div>
        </div>
      </main>

      {currentMusic && (
        <MusicPlayer query={currentMusic.query} videoId={currentMusic.videoId} volume={musicVolume} isSpeaking={isSpeaking} onStop={stopMusic} />
      )}

      {showLibrary && (
        <RecipeLibrary
          onClose={() => setShowLibrary(false)}
          onCook={(name, recipeObj, source, index) => {
            const libRecipe = recipeObj ? { recipe: recipeObj, source: source ?? "saved" } : null;
            setRecipe(name);
            setRecipeFromLibrary(libRecipe);
            setLibraryIndex(index ?? null);
            setShowLibrary(false);
            startSession(videoRef.current, name, persona, libRecipe, false, null);
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
