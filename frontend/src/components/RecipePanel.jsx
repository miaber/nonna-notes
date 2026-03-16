import { useState, useEffect } from "react";

export default function RecipePanel({ recipe, completedSteps, editable = false, onChange, hideIngredients = false, capturedPhotos = {} }) {
  const [editing, setEditing] = useState(false);
  const [showAbout, setShowAbout] = useState(false);
  const [draft, setDraft] = useState(recipe);

  // Sync draft when recipe prop changes (e.g. AI adds a step), but only when not editing
  useEffect(() => {
    if (!editing) setDraft(recipe);
  }, [recipe, editing]);

  if (!recipe) return null;

  const handleDone = () => {
    setEditing(false);
    onChange?.(draft);
  };

  const setField = (field, value) =>
    setDraft((d) => ({ ...d, [field]: value }));

  const setIngredient = (i, field, value) =>
    setDraft((d) => {
      const ings = d.ingredients.map((ing, idx) => idx === i ? { ...ing, [field]: value } : ing);
      return { ...d, ingredients: ings };
    });

  const setStep = (i, field, value) =>
    setDraft((d) => {
      const steps = d.steps.map((s, idx) => idx === i ? { ...s, [field]: value } : s);
      return { ...d, steps };
    });

  const addIngredient = () =>
    setDraft((d) => ({ ...d, ingredients: [...(d.ingredients || []), { amount: "", item: "", prep: "" }] }));

  const removeIngredient = (i) =>
    setDraft((d) => ({ ...d, ingredients: d.ingredients.filter((_, idx) => idx !== i) }));

  const addStep = () =>
    setDraft((d) => {
      const steps = d.steps || [];
      return { ...d, steps: [...steps, { id: steps.length + 1, instruction: "", timer_seconds: null, visual_checkpoint: false }] };
    });

  const removeStep = (i) =>
    setDraft((d) => {
      const steps = d.steps.filter((_, idx) => idx !== i).map((s, idx) => ({ ...s, id: idx + 1 }));
      return { ...d, steps };
    });

  const r = editing ? draft : recipe;
  const { name, description, servings, total_time_minutes, ingredients, steps, tips, source_url, source_images, notes } = r;
  const placeholderDesc = "Steps and ingredients appear as you go.";
  const hasRealDescription = description && description.trim() && description.trim() !== placeholderDesc;
  const hasNotes = notes != null && String(notes).trim().length > 0;
  const hasAboutInfo = !editing && (hasRealDescription || hasNotes || (servings != null && servings > 0) || (total_time_minutes != null && total_time_minutes > 0) || (tips?.length > 0) || source_url || (source_images?.length > 0));

  return (
    <div className="recipe-panel">
      <div className="recipe-header-row">
        {editing ? (
          <input
            className="recipe-edit-name"
            value={name}
            onChange={(e) => setField("name", e.target.value)}
            placeholder="Recipe name"
          />
        ) : (
          <div className="recipe-name-group">
            <h2 className="recipe-name">{name}</h2>
            {source_url && (
              <a className="recipe-source-link" href={source_url} target="_blank" rel="noreferrer">
                {(() => { try { return new URL(source_url).hostname.replace("www.", ""); } catch { return "source"; } })()}
              </a>
            )}
          </div>
        )}
        {hasAboutInfo && (
          <button type="button" className="about-recipe-btn" onClick={() => setShowAbout(true)}>
            About this recipe
          </button>
        )}
        {editable && (
          editing ? (
            <button className="recipe-edit-done-btn" onClick={handleDone}>Save</button>
          ) : (
            <button className="recipe-edit-btn" onClick={() => setEditing(true)} title="Edit recipe">✎</button>
          )
        )}
      </div>

      {editing && (
        <>
          <div className="recipe-edit-section">
            <label className="recipe-edit-meta-label">
              Description
              <textarea
                className="recipe-input"
                value={description || ""}
                onChange={(e) => setField("description", e.target.value)}
                rows={2}
                placeholder="Short description"
              />
            </label>
            <div className="recipe-meta">
              <label className="recipe-edit-meta-label">
                Serves
                <input
                  className="recipe-edit-meta-input"
                  type="number" min="1"
                  value={servings || ""}
                  onChange={(e) => setField("servings", parseInt(e.target.value) || null)}
                />
              </label>
              <label className="recipe-edit-meta-label">
                Min
                <input
                  className="recipe-edit-meta-input"
                  type="number" min="1"
                  value={total_time_minutes || ""}
                  onChange={(e) => setField("total_time_minutes", parseInt(e.target.value) || null)}
                />
              </label>
            </div>
          </div>

          {!hideIngredients && (
            <div className="recipe-ingredients">
              <h3 className="steps-heading">Ingredients</h3>
              <ul className="ingredients-list">
                {(ingredients || []).map((ing, i) => (
                  <li key={i} className="ingredient-item recipe-edit-row">
                    <input
                      className="library-edit-input library-ingredient-row library-edit-amount"
                      value={ing.amount} placeholder="qty"
                      onChange={(e) => setIngredient(i, "amount", e.target.value)}
                    />
                    <input
                      className="library-edit-input library-edit-item"
                      value={ing.item} placeholder="ingredient"
                      onChange={(e) => setIngredient(i, "item", e.target.value)}
                      style={{ flex: 1 }}
                    />
                    <input
                      className="library-edit-input library-edit-prep"
                      value={ing.prep} placeholder="prep"
                      onChange={(e) => setIngredient(i, "prep", e.target.value)}
                    />
                    <button className="library-edit-remove" onClick={() => removeIngredient(i)}>−</button>
                  </li>
                ))}
              </ul>
              <button className="library-edit-add" onClick={addIngredient}>+ Add ingredient</button>
            </div>
          )}
        </>
      )}

      {(steps?.length > 0 || editing) && (
        <div className="recipe-steps">
          <h3 className="steps-heading">Steps</h3>
          <ol className="steps-list">
            {(steps || []).map((step, i) => {
              const done = completedSteps?.has(step.id);
              const current = !editing && !done && !steps.slice(0, i).some((s) => !completedSteps?.has(s.id));
              return editing ? (
                <li key={step.id} className="step recipe-edit-row" style={{ alignItems: "flex-start" }}>
                  <span className="step-num">{step.id}</span>
                  <textarea
                    className="library-edit-input library-edit-step-text"
                    value={step.instruction}
                    rows={2}
                    onChange={(e) => setStep(i, "instruction", e.target.value)}
                  />
                  <button className="library-edit-remove" onClick={() => removeStep(i)}>−</button>
                </li>
              ) : (
                <li key={step.id} className={`step ${done ? "step-done" : ""} ${current ? "step-current" : ""}`}>
                  <span className="step-num">{done ? "✓" : step.id}</span>
                  <div className="step-body">
                    <span className="step-text">
                      {step.instruction}
                      {step.timer_seconds != null && (
                        <span className="step-badges">
                          <span className="step-timer">
                            {Math.floor(step.timer_seconds / 60)}:{String(step.timer_seconds % 60).padStart(2, "0")}
                          </span>
                        </span>
                      )}
                    </span>
                    {(step.image || capturedPhotos[step.id]?.length > 0) && (
                      <div className="step-images">
                        {step.image && (
                          <img src={step.image} alt="" className="step-img" loading="lazy" />
                        )}
                        {(capturedPhotos[step.id] || []).map((src, pi) => (
                          <img key={`cap-${pi}`} src={src} alt="" className="step-img nonna-photo" loading="lazy" />
                        ))}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
          {editing && (
            <button className="library-edit-add" onClick={addStep}>+ Add step</button>
          )}
        </div>
      )}

      {editing && tips?.length > 0 && (
        <div className="recipe-tips">
          <h3 className="steps-heading">Tips</h3>
          <ul className="steps-list">
            {tips.map((tip, i) => (
              <li key={i} className="step"><span className="step-text">{tip}</span></li>
            ))}
          </ul>
        </div>
      )}

      {showAbout && (
        <div className="about-popup-overlay" onClick={(e) => e.target === e.currentTarget && setShowAbout(false)}>
          <div className="about-popup">
            <div className="about-popup-header">
              <h3>{name}</h3>
              <button type="button" className="about-popup-close" onClick={() => setShowAbout(false)} aria-label="Close">✕</button>
            </div>
            {source_url && (
              <a className="about-popup-source" href={source_url} target="_blank" rel="noreferrer">
                {source_url.includes("youtube.com") || source_url.includes("youtu.be") ? "View on YouTube ↗" : "View original recipe ↗"}
              </a>
            )}
            {source_images?.length > 0 && (
              <div className="about-popup-images">
                {source_images.map((url, i) => (
                  <img key={i} src={url} alt={`${name} photo ${i + 1}`} className="about-popup-img" loading="lazy" />
                ))}
              </div>
            )}
            {description && <p className="about-popup-desc">{description}</p>}
            {(servings || total_time_minutes) && (
              <p className="about-popup-meta">
                {[servings && `${servings} servings`, total_time_minutes && `${total_time_minutes} min`].filter(Boolean).join(" · ")}
              </p>
            )}
            {tips?.length > 0 && (
              <div className="about-popup-tips">
                <h4>Tips</h4>
                <ul>
                  {tips.map((tip, i) => <li key={i}>{tip}</li>)}
                </ul>
              </div>
            )}
            {hasNotes && (
              <div className="about-popup-notes">
                <h4>Notes</h4>
                <p className="about-popup-desc">{notes}</p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
