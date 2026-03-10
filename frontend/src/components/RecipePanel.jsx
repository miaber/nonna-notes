import { useState, useEffect } from "react";

export default function RecipePanel({ recipe, completedSteps, editable = false, onChange, hideIngredients = false }) {
  const [editing, setEditing] = useState(false);
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
  const { name, description, servings, total_time_minutes, ingredients, steps, tips } = r;

  return (
    <div className="recipe-panel">
      <div className="recipe-header">
        {editing ? (
          <input
            className="recipe-edit-name"
            value={name}
            onChange={(e) => setField("name", e.target.value)}
            placeholder="Recipe name"
          />
        ) : (
          <h2 className="recipe-name">{name}</h2>
        )}
        {description && !editing && <p className="recipe-description">{description}</p>}
        <div className="recipe-meta">
          {editing ? (
            <>
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
            </>
          ) : (
            <>
              {servings && <span className="recipe-meta-item">{servings} servings</span>}
              {servings && total_time_minutes && <span className="recipe-meta-sep">·</span>}
              {total_time_minutes && <span className="recipe-meta-item">{total_time_minutes} min</span>}
            </>
          )}
          {editable && (
            editing ? (
              <button className="recipe-edit-done-btn" onClick={handleDone}>Save</button>
            ) : (
              <button className="recipe-edit-btn" onClick={() => setEditing(true)} title="Edit recipe">✎</button>
            )
          )}
        </div>
      </div>

      {!hideIngredients && (ingredients?.length > 0 || editing) && (
        <div className="recipe-ingredients">
          <h3 className="steps-heading">Ingredients</h3>
          <ul className="ingredients-list">
            {(ingredients || []).map((ing, i) =>
              editing ? (
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
              ) : (
                <li key={i} className="ingredient-item">
                  <span className="ingredient-amount">{ing.amount}</span>
                  <span className="ingredient-main">
                    <span className="ingredient-name">{ing.item}</span>
                    {ing.prep && <span className="ingredient-prep"> {ing.prep}</span>}
                  </span>
                </li>
              )
            )}
          </ul>
          {editing && (
            <button className="library-edit-add" onClick={addIngredient}>+ Add ingredient</button>
          )}
        </div>
      )}

      {(steps?.length > 0 || editing) && (() => {
        const currentStep = !editing
          ? steps?.find((step, i) => !completedSteps?.has(step.id) && steps.slice(0, i).every((s) => completedSteps?.has(s.id)))
          : null;
        return (
        <div className="recipe-steps">
          {currentStep && (
            <div className="current-step-callout">
              <span className="current-step-label">Now</span>
              <span className="current-step-text">{currentStep.instruction}</span>
            </div>
          )}
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
                </li>
              );
            })}
          </ol>
          {editing && (
            <button className="library-edit-add" onClick={addStep}>+ Add step</button>
          )}
        </div>
        );
      })()}

      {tips?.length > 0 && !editing && (
        <div className="recipe-tips">
          <h3 className="steps-heading">Tips</h3>
          <ul className="steps-list">
            {tips.map((tip, i) => (
              <li key={i} className="step"><span className="step-text">{tip}</span></li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
