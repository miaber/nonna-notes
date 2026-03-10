export default function RecipeSteps({ steps, completedSteps }) {
  if (steps.length === 0) return null;

  // Find the current step (first incomplete)
  let currentStep = null;
  for (let i = 0; i < steps.length; i++) {
    if (!completedSteps.has(i + 1)) {
      currentStep = { num: i + 1, text: steps[i] };
      break;
    }
  }

  return (
    <div className="recipe-steps">
      {currentStep && (
        <div className="current-step-callout">
          <span className="current-step-label">Now</span>
          <span className="current-step-text">{currentStep.text}</span>
        </div>
      )}
      <h2 className="steps-heading">Steps</h2>
      <ol className="steps-list">
        {steps.map((step, i) => {
          const stepNum = i + 1;
          const done = completedSteps.has(stepNum);
          const current = !done && !Array.from({ length: i }, (_, k) => k + 1).some((n) => !completedSteps.has(n));

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
