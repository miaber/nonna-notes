export default function RecipeSteps({ steps, completedSteps }) {
  if (steps.length === 0) return null;

  return (
    <div className="recipe-steps">
      <h2 className="steps-heading">Steps</h2>
      <ol className="steps-list">
        {steps.map((step, i) => {
          const stepNum = i + 1;
          const done = completedSteps.has(stepNum);
          // Current step = first incomplete step
          const current =
            !done && !Array.from({ length: i }, (_, k) => k + 1).some((n) => !completedSteps.has(n));

          return (
            <li
              key={i}
              className={`step ${done ? "step-done" : ""} ${current ? "step-current" : ""}`}
            >
              <span className="step-num">{done ? "✓" : stepNum}</span>
              <span className="step-text">{step}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
