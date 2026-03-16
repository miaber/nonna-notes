import { useState, useEffect } from "react";

const API_URL = import.meta.env.VITE_BACKEND_URL || import.meta.env.VITE_API_URL || "http://localhost:8000";

export default function RecipeLibrary({ onClose, onCook, onContinueDraft, getToken }) {
  const [recipes, setRecipes] = useState(null);
  const [detail, setDetail] = useState(null); // { id, entry }

  const authHeaders = async (extra = {}) => {
    const token = getToken ? await getToken() : null;
    return { ...extra, ...(token ? { Authorization: `Bearer ${token}` } : {}) };
  };

  useEffect(() => {
    (async () => {
      try {
        const headers = await authHeaders();
        const r = await fetch(`${API_URL}/recipes`, { headers });
        const d = await r.json();
        setRecipes(d.recipes ?? []);
      } catch {
        setRecipes([]);
      }
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const fetchFullRecipe = async (recipeId) => {
    const headers = await authHeaders();
    const res = await fetch(`${API_URL}/recipes/${encodeURIComponent(recipeId)}`, { headers });
    if (!res.ok) return null;
    const d = await res.json();
    return d.recipe ?? null;
  };

  const handleUpdate = async (recipeId, patch) => {
    try {
      const headers = await authHeaders({ "Content-Type": "application/json" });
      const res = await fetch(`${API_URL}/recipes/${encodeURIComponent(recipeId)}`, {
        method: "PATCH",
        headers,
        body: JSON.stringify(patch),
      });
      const data = await res.json();
      setRecipes(data.recipes ?? []);
      if (detail?.id === recipeId) {
        const fullEntry = await fetchFullRecipe(recipeId);
        if (fullEntry) setDetail({ id: recipeId, entry: fullEntry });
      }
    } catch {}
  };

  const handleDelete = async (recipeId) => {
    if (!confirm("Delete this recipe from My Recipes?")) return;
    try {
      const headers = await authHeaders();
      const res = await fetch(`${API_URL}/recipes/${encodeURIComponent(recipeId)}`, { method: "DELETE", headers });
      const data = await res.json();
      setRecipes(data.recipes ?? []);
      setDetail(null);
    } catch {}
  };

  return (
    <div className="library-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="library-panel" onClick={(e) => e.stopPropagation()}>
        <div className="library-header">
          <h2 className="library-title">My Recipes</h2>
          <button type="button" className="library-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="library-list">
          {recipes === null && <p className="library-empty">Loading…</p>}
          {recipes?.length === 0 && (
            <p className="library-empty">
              No saved recipes yet. Start cooking and say “Save this to my recipes” when you’re done.
            </p>
          )}
          {recipes?.map((entry) => (
            <RecipeCard
              key={entry.id}
              entry={entry}
              onClick={async () => {
                setDetail({ id: entry.id, entry });
                const fullEntry = await fetchFullRecipe(entry.id);
                if (fullEntry != null) setDetail({ id: entry.id, entry: fullEntry });
              }}
            />
          ))}
        </div>
      </div>

      {detail != null && (
        <RecipeDetailModal
          recipeId={detail.id}
          entry={detail.entry}
          onClose={() => setDetail(null)}
          onUpdate={handleUpdate}
          onDelete={handleDelete}
          onCook={(name, recipeObj, source, _id, prevPhotos) => {
            const id = detail.id;
            setDetail(null);
            onClose();
            onCook(name, recipeObj, source, id, prevPhotos);
          }}
          onContinueDraft={onContinueDraft ? (startedAt) => { setDetail(null); onClose(); onContinueDraft(startedAt); } : null}
        />
      )}
    </div>
  );
}

function RecipeCard({ entry, onClick }) {
  const { recipe, saved_at, updated_at, draft } = entry;
  const dateStr = saved_at || updated_at;
  const date = dateStr
    ? new Date(dateStr).toLocaleDateString(undefined, {
        month: "short", day: "numeric", year: "numeric",
      })
    : null;

  return (
    <article
      className={`library-card library-card-preview ${draft ? "library-card-draft" : ""}`}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onClick())}
    >
      <div className="library-card-meta">
        <span className="library-card-name">{recipe.name}</span>
        {draft && <span className="library-badge library-badge-draft">Draft</span>}
        {date && <span className="library-card-date">{draft && !saved_at ? "Updated " : ""}{date}</span>}
        <div className="library-card-badges">
          {recipe.servings != null && <span className="library-badge">{recipe.servings} servings</span>}
          {recipe.total_time_minutes != null && <span className="library-badge">{recipe.total_time_minutes} min</span>}
        </div>
      </div>
      <span className="library-card-arrow" aria-hidden>→</span>
    </article>
  );
}

function RecipeDetailModal({ recipeId, entry, onClose, onUpdate, onDelete, onCook, onContinueDraft }) {
  const { recipe: initialRecipe, saved_at, photos: rawPhotos = [] } = entry;
  const photos = Array.isArray(rawPhotos) ? rawPhotos.filter((p) => p && p.data) : [];
  const sourceImages = Array.isArray(initialRecipe.source_images) ? initialRecipe.source_images : [];
  const sourceUrl = initialRecipe.source_url || null;

  const [editingName, setEditingName] = useState(false);
  const [nameValue, setNameValue] = useState(initialRecipe.name ?? "");
  const [description, setDescription] = useState(initialRecipe.description ?? "");
  const [ingredients, setIngredients] = useState(() =>
    Array.isArray(initialRecipe.ingredients)
      ? initialRecipe.ingredients.map((ing) => ({
          amount: ing.amount ?? "",
          item: ing.item ?? "",
          prep: ing.prep ?? "",
        }))
      : []
  );
  const [steps, setSteps] = useState(() =>
    Array.isArray(initialRecipe.steps)
      ? initialRecipe.steps.map((s, i) => ({
          id: s.id ?? i + 1,
          instruction: s.instruction ?? "",
          timer_seconds: s.timer_seconds ?? null,
          visual_checkpoint: s.visual_checkpoint ?? false,
          image: s.image ?? null,
        }))
      : []
  );
  const [tips, setTips] = useState(() =>
    Array.isArray(initialRecipe.tips) ? [...initialRecipe.tips] : []
  );
  const [servings, setServings] = useState(initialRecipe.servings ?? "");
  const [totalTime, setTotalTime] = useState(initialRecipe.total_time_minutes ?? "");
  const [notes, setNotes] = useState(entry.notes ?? "");
  const [recipeDirty, setRecipeDirty] = useState(false);
  const [notesDirty, setNotesDirty] = useState(false);
  const [isEditing, setIsEditing] = useState(false);

  useEffect(() => {
    setNameValue(initialRecipe.name ?? "");
    setDescription(initialRecipe.description ?? "");
    setIngredients(
      Array.isArray(initialRecipe.ingredients)
        ? initialRecipe.ingredients.map((ing) => ({
            amount: ing.amount ?? "",
            item: ing.item ?? "",
            prep: ing.prep ?? "",
          }))
        : []
    );
    setSteps(
      Array.isArray(initialRecipe.steps)
        ? initialRecipe.steps.map((s, i) => ({
            id: s.id ?? i + 1,
            instruction: s.instruction ?? "",
            timer_seconds: s.timer_seconds ?? null,
            visual_checkpoint: s.visual_checkpoint ?? false,
            image: s.image ?? null,
          }))
        : []
    );
    setTips(Array.isArray(initialRecipe.tips) ? [...initialRecipe.tips] : []);
    setServings(initialRecipe.servings ?? "");
    setTotalTime(initialRecipe.total_time_minutes ?? "");
    setNotes(entry.notes ?? "");
  }, [initialRecipe, entry.notes]);

  const date = saved_at
    ? new Date(saved_at).toLocaleDateString(undefined, {
        month: "short", day: "numeric", year: "numeric",
      })
    : null;

  const saveName = () => {
    setEditingName(false);
    if (nameValue.trim() && nameValue.trim() !== initialRecipe.name) {
      onUpdate(recipeId, { name: nameValue.trim() });
    }
  };

  const saveRecipe = () => {
    const recipePatch = {
      name: nameValue.trim() || initialRecipe.name,
      description: description.trim() || null,
      servings: servings === "" ? null : parseInt(servings, 10) || null,
      total_time_minutes: totalTime === "" ? null : parseInt(totalTime, 10) || null,
      ingredients: ingredients.filter((ing) => ing.item.trim()).map((ing) => ({
        amount: ing.amount.trim(),
        item: ing.item.trim(),
        prep: ing.prep.trim(),
      })),
      steps: steps
        .filter((s) => s.instruction.trim())
        .map((s, i) => ({
          id: i + 1,
          instruction: s.instruction.trim(),
          timer_seconds: s.timer_seconds,
          visual_checkpoint: s.visual_checkpoint,
          image: s.image ?? null,
        })),
      tips: tips.filter((t) => t.trim()),
    };
    onUpdate(recipeId, { recipe: recipePatch });
    setRecipeDirty(false);
    setIsEditing(false);
  };

  const cancelEdit = () => {
    setIsEditing(false);
    setRecipeDirty(false);
    setNameValue(initialRecipe.name ?? "");
    setDescription(initialRecipe.description ?? "");
    setIngredients(
      Array.isArray(initialRecipe.ingredients)
        ? initialRecipe.ingredients.map((ing) => ({
            amount: ing.amount ?? "",
            item: ing.item ?? "",
            prep: ing.prep ?? "",
          }))
        : []
    );
    setSteps(
      Array.isArray(initialRecipe.steps)
        ? initialRecipe.steps.map((s, i) => ({
            id: s.id ?? i + 1,
            instruction: s.instruction ?? "",
            timer_seconds: s.timer_seconds ?? null,
            visual_checkpoint: s.visual_checkpoint ?? false,
            image: s.image ?? null,
          }))
        : []
    );
    setTips(Array.isArray(initialRecipe.tips) ? [...initialRecipe.tips] : []);
    setServings(initialRecipe.servings ?? "");
    setTotalTime(initialRecipe.total_time_minutes ?? "");
  };

  const saveNotes = () => {
    setNotesDirty(false);
    onUpdate(recipeId, { notes });
  };

  const addIngredient = () => setIngredients((prev) => [...prev, { amount: "", item: "", prep: "" }]);
  const removeIngredient = (i) => setIngredients((prev) => prev.filter((_, j) => j !== i));
  const setIngredient = (i, field, value) =>
    setIngredients((prev) => {
      const next = [...prev];
      next[i] = { ...next[i], [field]: value };
      setRecipeDirty(true);
      return next;
    });

  const addStep = () =>
    setSteps((prev) => [...prev, { id: prev.length + 1, instruction: "", timer_seconds: null, visual_checkpoint: false }]);
  const removeStep = (i) => setSteps((prev) => prev.filter((_, j) => j !== i));
  const setStep = (i, field, value) => {
    setSteps((prev) => {
      const next = [...prev];
      next[i] = { ...next[i], [field]: value };
      setRecipeDirty(true);
      return next;
    });
  };

  const addTip = () => setTips((prev) => [...prev, ""]);
  const removeTip = (i) => setTips((prev) => prev.filter((_, j) => j !== i));
  const setTip = (i, value) => {
    setTips((prev) => {
      const next = [...prev];
      next[i] = value;
      setRecipeDirty(true);
      return next;
    });
  };

  const photosByStep = {};
  const generalPhotos = [];
  for (const photo of photos) {
    if (photo.step_id != null) {
      (photosByStep[photo.step_id] ??= []).push(photo);
    } else {
      generalPhotos.push(photo);
    }
  }

  const currentRecipe = {
    ...initialRecipe,
    name: nameValue.trim() || initialRecipe.name,
    description,
    ingredients,
    steps,
    tips,
    notes: notes ?? "",
  };

  return (
    <div className="library-detail-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="library-detail-modal" onClick={(e) => e.stopPropagation()}>
        <div className="library-detail-header">
          <h3 className="library-detail-title">
            {isEditing && editingName ? (
              <input
                className="library-name-input library-name-input-modal"
                value={nameValue}
                autoFocus
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => setNameValue(e.target.value)}
                onBlur={saveName}
                onKeyDown={(e) => e.key === "Enter" && saveName()}
                aria-label="Recipe name"
              />
            ) : isEditing ? (
              <button
                type="button"
                className="library-detail-name-btn"
                onClick={() => setEditingName(true)}
                title="Click to edit name"
              >
                {nameValue || initialRecipe.name} ✎
              </button>
            ) : (
              <span className="library-detail-name-readonly">{nameValue || initialRecipe.name}</span>
            )}
          </h3>
          {date && <span className="library-detail-date">{date}</span>}
          {sourceUrl && (
            <a className="library-detail-source" href={sourceUrl} target="_blank" rel="noreferrer">
              {sourceUrl.includes("youtube.com") || sourceUrl.includes("youtu.be") ? "YouTube" : (() => { try { return new URL(sourceUrl).hostname.replace("www.", ""); } catch { return "source"; } })()} ↗
            </a>
          )}
          <button type="button" className="library-detail-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="library-detail-body">
          {!isEditing ? (
            <>
              {sourceImages.length > 0 && (
                <div className="library-photos-grid library-hero-images">
                  {sourceImages.map((url, i) => (
                    <img key={`src-${i}`} src={url} alt={`${initialRecipe.name}`} className="library-photo" loading="lazy" />
                  ))}
                </div>
              )}
              {description && (
                <div className="library-section">
                  <h4 className="library-section-title">Description</h4>
                  <p className="library-detail-desc">{description}</p>
                </div>
              )}
              {(servings || totalTime) && (
                <div className="library-section">
                  <h4 className="library-section-title">Servings & time</h4>
                  <p className="library-detail-meta-readonly">
                    {[servings && `${servings} servings`, totalTime && `${totalTime} min`].filter(Boolean).join(" · ")}
                  </p>
                </div>
              )}
              {ingredients.length > 0 && (
                <div className="library-section">
                  <h4 className="library-section-title">Ingredients</h4>
                  <ul className="library-ingredients">
                    {ingredients.map((ing, i) => (
                      <li key={i}>
                        <span className="ingredient-amount">{ing.amount}</span>{" "}
                        <span className="ingredient-name">{ing.item}</span>
                        {ing.prep && <span className="ingredient-prep"> ({ing.prep})</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {steps.length > 0 && (
                <div className="library-section">
                  <h4 className="library-section-title">Steps</h4>
                  <ol className="library-steps">
                    {steps.map((step, i) => {
                      const stepPhotos = photosByStep[step.id] || [];
                      return (
                        <li key={i}>
                          <span>{step.instruction}</span>
                          {step.timer_seconds != null && (
                            <span className="step-timer">
                              {" "}({Math.floor(step.timer_seconds / 60)}:{String(step.timer_seconds % 60).padStart(2, "0")})
                            </span>
                          )}
                          {(step.image || stepPhotos.length > 0) && (
                            <div className="step-images">
                              {step.image && (
                                <img src={step.image} alt="" className="step-img" loading="lazy" />
                              )}
                              {stepPhotos.map((photo, pi) => (
                                <img
                                  key={`nonna-${pi}`}
                                  src={`data:image/jpeg;base64,${photo.data}`}
                                  alt={`Step ${step.id}`}
                                  className="step-img nonna-photo"
                                  loading="lazy"
                                />
                              ))}
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ol>
                </div>
              )}
              {tips.length > 0 && (
                <div className="library-section">
                  <h4 className="library-section-title">Tips</h4>
                  <ul className="library-steps">
                    {tips.map((tip, i) => <li key={i}>{tip}</li>)}
                  </ul>
                </div>
              )}
              <div className="library-section">
                <button type="button" className="library-edit-mode-btn" onClick={() => setIsEditing(true)}>
                  Edit recipe
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="library-section">
                <h4 className="library-section-title">Description</h4>
                <textarea
                  className="library-notes library-edit-desc"
                  placeholder="Short description of the recipe"
                  value={description}
                  onChange={(e) => { setDescription(e.target.value); setRecipeDirty(true); }}
                  rows={2}
                />
              </div>
              <div className="library-section library-edit-meta">
                <h4 className="library-section-title">Servings & time</h4>
                <div className="library-meta-fields">
                  <label className="library-meta-label">
                    Servings
                    <input
                      type="number"
                      min={1}
                      className="library-meta-input"
                      value={servings}
                      onChange={(e) => { setServings(e.target.value); setRecipeDirty(true); }}
                      placeholder="—"
                    />
                  </label>
                  <label className="library-meta-label">
                    Total time (min)
                    <input
                      type="number"
                      min={0}
                      className="library-meta-input"
                      value={totalTime}
                      onChange={(e) => { setTotalTime(e.target.value); setRecipeDirty(true); }}
                      placeholder="—"
                    />
                  </label>
                </div>
              </div>
              <div className="library-section">
                <h4 className="library-section-title">Ingredients</h4>
                <div className="library-edit-list">
                  {ingredients.map((ing, i) => (
                    <div key={i} className="library-edit-row library-ingredient-row">
                      <input
                        type="text"
                        className="library-edit-input library-edit-amount"
                        placeholder="Amount"
                        value={ing.amount}
                        onChange={(e) => setIngredient(i, "amount", e.target.value)}
                      />
                      <input
                        type="text"
                        className="library-edit-input library-edit-item"
                        placeholder="Item"
                        value={ing.item}
                        onChange={(e) => setIngredient(i, "item", e.target.value)}
                      />
                      <input
                        type="text"
                        className="library-edit-input library-edit-prep"
                        placeholder="Prep (optional)"
                        value={ing.prep}
                        onChange={(e) => setIngredient(i, "prep", e.target.value)}
                      />
                      <button type="button" className="library-edit-remove" onClick={() => removeIngredient(i)} aria-label="Remove">−</button>
                    </div>
                  ))}
                  <button type="button" className="library-edit-add" onClick={addIngredient}>+ Add ingredient</button>
                </div>
              </div>
              <div className="library-section">
                <h4 className="library-section-title">Steps</h4>
                <div className="library-edit-list">
                  {steps.map((step, i) => (
                    <div key={i} className="library-edit-row library-step-row">
                      <span className="library-step-num">{i + 1}.</span>
                      <textarea
                        className="library-edit-input library-edit-step-text"
                        placeholder="Instruction"
                        value={step.instruction}
                        onChange={(e) => setStep(i, "instruction", e.target.value)}
                        rows={2}
                      />
                      <label className="library-step-timer-label">
                        Timer (sec)
                        <input
                          type="number"
                          min={0}
                          className="library-meta-input library-step-timer-input"
                          value={step.timer_seconds ?? ""}
                          onChange={(e) => setStep(i, "timer_seconds", e.target.value === "" ? null : parseInt(e.target.value, 10))}
                          placeholder="—"
                        />
                      </label>
                      <button type="button" className="library-edit-remove" onClick={() => removeStep(i)} aria-label="Remove step">−</button>
                    </div>
                  ))}
                  <button type="button" className="library-edit-add" onClick={addStep}>+ Add step</button>
                </div>
              </div>
              <div className="library-section">
                <h4 className="library-section-title">Tips</h4>
                <div className="library-edit-list">
                  {tips.map((tip, i) => (
                    <div key={i} className="library-edit-row">
                      <input
                        type="text"
                        className="library-edit-input"
                        placeholder="Tip"
                        value={tip}
                        onChange={(e) => setTip(i, e.target.value)}
                      />
                      <button type="button" className="library-edit-remove" onClick={() => removeTip(i)} aria-label="Remove">−</button>
                    </div>
                  ))}
                  <button type="button" className="library-edit-add" onClick={addTip}>+ Add tip</button>
                </div>
              </div>
              <div className="library-save-row">
                <button type="button" className="library-cancel-btn" onClick={cancelEdit}>
                  Cancel
                </button>
                <button type="button" className="library-save-notes library-save-recipe" onClick={saveRecipe}>
                  Save recipe
                </button>
              </div>
            </>
          )}

          <div className="library-section">
            <h4 className="library-section-title">My Notes</h4>
            <textarea
              className="library-notes"
              placeholder="Add notes about this cook — what worked, what to change next time…"
              value={notes}
              onChange={(e) => { setNotes(e.target.value); setNotesDirty(true); }}
              rows={3}
            />
            {notesDirty && (
              <button type="button" className="library-save-notes" onClick={saveNotes}>
                Save notes
              </button>
            )}
          </div>

          {generalPhotos.length > 0 && (
            <div className="library-photos-section">
              <h4 className="library-section-title">Photos</h4>
              <div className="library-photos-grid">
                {generalPhotos.map((photo, i) => (
                  <figure key={i} className="library-photo-wrap">
                    <img
                      src={`data:image/jpeg;base64,${photo.data}`}
                      alt="Cook"
                      className="library-photo"
                      loading="lazy"
                    />
                  </figure>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="library-detail-actions">
          {entry.draft && onContinueDraft ? (
            <button type="button" className="library-cook-btn" onClick={() => onContinueDraft(entry.started_at ?? entry.id ?? null)}>
              Continue this recipe
            </button>
          ) : (
            <button type="button" className="library-cook-btn" onClick={() => onCook(currentRecipe.name, currentRecipe, "saved", recipeId, photos)}>
              Cook this again
            </button>
          )}
          <div className="library-detail-secondary">
            <button
              type="button"
              className="library-delete-btn"
              onClick={() => onDelete(recipeId)}
              title="Delete recipe"
            >
              Delete recipe
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
